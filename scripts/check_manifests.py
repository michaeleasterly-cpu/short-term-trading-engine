#!/usr/bin/env python3
"""Manifest / reference integrity linter for the STE extension surface.

Modeled after Anthropic's ``financial-services/scripts/check.py``
manifest cross-reference linter. Pure stdlib; no external services;
no DB access. Runs locally on commit (via .pre-commit-config.yaml's
``check-manifests`` hook) and is callable directly via
``python scripts/check_manifests.py``.

Scope (conservative — only checks that are unambiguous and have an
on-disk target):

  1. ``.claude/settings.json`` hook command paths resolve to existing
     executable scripts under ``$CLAUDE_PROJECT_DIR/.claude/hooks/``.

  2. Every directory in ``.claude/skills/`` contains a ``SKILL.md``
     (symlinks resolved).

  3. Every ``.claude/agents/*.md`` and ``.claude/rules/*.md`` file
     is a markdown file with YAML frontmatter (delimited by ``---``).

  4. The heavy-lane rule's frontmatter ``paths:`` list matches the
     ``paths:`` filter in
     ``.github/workflows/claude-review-heavy-lane.yml`` for the
     heavy-lane subset (extension-layer paths in the workflow are
     extras, allowed).

  5. ``.github/workflows/*.yml`` ``run: python …`` script invocations
     point at files that exist on disk when the invocation is a
     literal path (heuristic — only fires when the run line is
     unambiguous).

  6. The skills/rules/agents/hooks present on disk match the
     vocabulary pinned in the existing presence-sentinel tests
     (``tests/test_claude_skills_present.py``,
     ``tests/test_claude_rules_present.py``,
     ``tests/test_claude_agents_present.py``,
     ``tests/test_claude_hooks_present.py``) — if the sentinels know
     about a name, the file must exist.

Exit codes:
  0  — all checks passed.
  1  — one or more reference defects. Each line of output names the
       file + the missing target.

Non-goals (deliberately omitted to keep this conservative):
  * No engine-roster (``_PROFILE``) cross-check — that's the ECR
    skill's job; replicating it here risks false-positives on the
    sentinel-fenced regions of the smoke loop.
  * No spec/plan link-checking inside doc bodies — would flag every
    historical doc with a moved-spec link.
  * No secret-scan — gitleaks is the SoT.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _err(target: Path, msg: str) -> str:
    rel = target.resolve().relative_to(REPO_ROOT) if target.exists() else target
    return f"FAIL {rel}: {msg}"


def check_hook_paths_in_settings() -> list[str]:
    """Every hook command in .claude/settings.json must resolve to an
    executable script under .claude/hooks/."""
    failures: list[str] = []
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.exists():
        # Settings file absent is itself a finding.
        return [_err(settings_path, "missing .claude/settings.json")]
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [_err(settings_path, f"invalid JSON: {exc}")]

    discovered: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            cmd = node.get("command")
            if isinstance(cmd, str) and ".claude/hooks/" in cmd:
                discovered.append(cmd)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)

    for cmd in discovered:
        # Strip the placeholder so we can resolve against REPO_ROOT.
        local = cmd.replace("$CLAUDE_PROJECT_DIR", str(REPO_ROOT))
        target = Path(local)
        if not target.exists():
            failures.append(
                _err(settings_path, f"hook script not found on disk: {cmd}")
            )
            continue
        if not target.is_file():
            failures.append(
                _err(settings_path, f"hook path is not a regular file: {cmd}")
            )
            continue
        # Best-effort exec check (skip on filesystems that don't carry
        # the bit through, e.g. some sshfs mounts).
        try:
            mode = target.stat().st_mode
            if not (mode & 0o111):
                failures.append(
                    _err(
                        settings_path,
                        f"hook script not executable (chmod +x needed): {cmd}",
                    )
                )
        except OSError:
            pass
    return failures


def check_skill_directories_have_skill_md() -> list[str]:
    """Every dir under .claude/skills/ must contain a SKILL.md
    (symlinks resolved)."""
    failures: list[str] = []
    skills_dir = REPO_ROOT / ".claude" / "skills"
    if not skills_dir.is_dir():
        return [_err(skills_dir, "missing .claude/skills/ directory")]
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        # Resolve symlinks — a skill may be a symlink to another
        # location (e.g. supabase-postgres-best-practices → .agents/).
        if not skill_md.exists():
            failures.append(
                _err(skill_md, f"skill directory missing SKILL.md: {entry.name}")
            )
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(_err(skill_md, f"unreadable: {exc}"))
            continue
        if not text.strip():
            failures.append(_err(skill_md, "SKILL.md is empty"))
            continue
        if not text.startswith("---"):
            failures.append(
                _err(skill_md, "SKILL.md missing YAML frontmatter (---)")
            )
    return failures


_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def check_markdown_has_frontmatter(
    paths_dir: Path, label: str,
) -> list[str]:
    """Every .md file in ``paths_dir`` carries YAML frontmatter +
    a non-empty body."""
    failures: list[str] = []
    if not paths_dir.is_dir():
        return [_err(paths_dir, f"missing {label} directory")]
    for path in sorted(paths_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(_err(path, f"unreadable: {exc}"))
            continue
        if not _FRONTMATTER_RE.match(text):
            failures.append(
                _err(path, f"{label} missing YAML frontmatter (---/---)")
            )
            continue
        # Body after the second ``---\n`` must be non-empty.
        match = _FRONTMATTER_RE.match(text)
        assert match is not None  # for type checker
        body = text[match.end():]
        if not body.strip():
            failures.append(_err(path, f"{label} body is empty after frontmatter"))
    return failures


def check_heavy_lane_paths_sync() -> list[str]:
    """The heavy-lane paths must appear identically in BOTH:
      (a) .claude/rules/heavy-lane.md frontmatter ``paths:`` list
      (b) .github/workflows/claude-review-heavy-lane.yml ``paths:`` filter

    The workflow may carry EXTRA extension-layer paths (.claude/**,
    .github/workflows/**) — those are allowed and not required to
    appear in the rule. Drift in the canonical heavy-lane subset is
    a finding."""
    failures: list[str] = []
    rule = REPO_ROOT / ".claude" / "rules" / "heavy-lane.md"
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "claude-review-heavy-lane.yml"
    )
    if not rule.exists():
        failures.append(_err(rule, "missing .claude/rules/heavy-lane.md"))
    if not workflow.exists():
        # Workflow absence is OK — adoption is optional. Skip the
        # cross-check (callers haven't installed the action yet).
        return failures
    if failures:
        return failures

    rule_text = rule.read_text(encoding="utf-8")
    rule_paths = _extract_yaml_paths_list(rule_text)
    workflow_text = workflow.read_text(encoding="utf-8")
    workflow_paths = _extract_yaml_paths_list(workflow_text)

    # Canonical heavy-lane subset that MUST appear in BOTH (matches
    # docs/DEV_PIPELINE_STANDARD.md §0 enumeration).
    canonical_subset = {
        "tpcore/risk/**",
        "tpcore/selfheal/**",
        "tpcore/auditheal/**",
        "tpcore/quality/validation/**",
        "ops/engine_service.py",
        "ops/engine_sdlc.py",
        "ops/engine_sdlc/**",
        "platform/migrations/**",
        "tpcore/engine_profile.py",
        "tpcore/providers.py",
    }
    rule_set = set(rule_paths)
    workflow_set = set(workflow_paths)
    missing_from_rule = canonical_subset - rule_set
    missing_from_workflow = canonical_subset - workflow_set
    for p in sorted(missing_from_rule):
        failures.append(
            _err(rule, f"canonical heavy-lane path missing from rule: {p}")
        )
    for p in sorted(missing_from_workflow):
        failures.append(
            _err(
                workflow,
                f"canonical heavy-lane path missing from workflow filter: {p}",
            )
        )
    return failures


def _extract_yaml_paths_list(text: str) -> list[str]:
    """Pull entries from a YAML ``paths:`` list — stdlib-only parser.

    Looks for a ``paths:`` line followed by ``  - "value"`` entries
    until a non-list-indented line. Catches both the frontmatter form
    used in the rule and the on:pull_request form used in the workflow.
    Returns a flat list (the workflow has TWO ``paths:`` blocks if
    there are multiple triggers — that's fine for set-membership).
    """
    paths: list[str] = []
    lines = text.splitlines()
    inside = False
    for raw in lines:
        line = raw.rstrip()
        if not inside:
            if line.strip() in ("paths:", "paths:"):
                inside = True
            continue
        # Inside a paths: block.
        stripped = line.lstrip()
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            # Strip wrapping quotes.
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            # Strip trailing comment.
            if "#" in value:
                value = value.split("#", 1)[0].rstrip()
            if value:
                paths.append(value)
        elif stripped and not stripped.startswith("#"):
            # Out of the paths block.
            inside = False
    return paths


def check_vocabulary_pinned_files_present() -> list[str]:
    """Every name pinned in the presence-sentinel tests has a file on
    disk. Pulls the constants from the test files at parse time so
    this never drifts from what the sentinels expect."""
    failures: list[str] = []
    presence_map = {
        "tests/test_claude_skills_present.py":
            (REPO_ROOT / ".claude" / "skills", "directory", "_SKILLS"),
        "tests/test_claude_rules_present.py":
            (REPO_ROOT / ".claude" / "rules", "file", "_RULES", ".md"),
        "tests/test_claude_agents_present.py":
            (REPO_ROOT / ".claude" / "agents", "file", "_AGENTS", ".md"),
        "tests/test_claude_hooks_present.py":
            (REPO_ROOT / ".claude" / "hooks", "file", "_HOOKS", ".sh"),
    }
    for test_rel, spec in presence_map.items():
        target_dir = spec[0]
        kind = spec[1]
        var_name = spec[2]
        suffix = spec[3] if len(spec) > 3 else ""
        test_path = REPO_ROOT / test_rel
        if not test_path.exists():
            continue
        names = _extract_string_tuple(test_path, var_name)
        if names is None:
            continue
        for name in names:
            if kind == "directory":
                if not (target_dir / name).is_dir():
                    failures.append(
                        _err(
                            target_dir / name,
                            f"pinned by {test_rel} but missing on disk",
                        )
                    )
            else:  # file
                fname = name if name.endswith(suffix) else f"{name}{suffix}"
                if not (target_dir / fname).is_file():
                    failures.append(
                        _err(
                            target_dir / fname,
                            f"pinned by {test_rel} but missing on disk",
                        )
                    )
    return failures


def _extract_string_tuple(path: Path, var_name: str) -> list[str] | None:
    """Extract the values of a top-level string-tuple assignment
    ``<var_name> = (...)`` from a Python source file. stdlib-only
    minimal parser — uses ``ast`` so we don't import the test module
    (which would run pytest discovery side-effects)."""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != var_name:
            continue
        if not isinstance(node.value, ast.Tuple):
            return None
        out: list[str] = []
        for elt in node.value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
        return out
    return None


def check_workflow_script_invocations() -> list[str]:
    """Heuristic check on ``.github/workflows/*.yml`` — for any
    ``run: python <PATH>`` or ``run: <PATH>`` where ``<PATH>`` is a
    repo-relative literal path (no shell interpolation, no env
    expansion), the path must exist on disk.

    Deliberately conservative — only flags lines that are
    unambiguous. We do NOT try to parse arbitrary shell."""
    failures: list[str] = []
    wf_dir = REPO_ROOT / ".github" / "workflows"
    if not wf_dir.is_dir():
        return failures
    literal_re = re.compile(
        r"^\s*run:\s+(?:python\s+|\.\/|bash\s+|sh\s+)?([A-Za-z0-9_./-]+\.(?:py|sh))\b"
    )
    for wf in sorted(wf_dir.glob("*.yml")):
        try:
            text = wf.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "$" in line or "{{" in line:
                # Shell or expression substitution — skip; we can't
                # statically resolve.
                continue
            match = literal_re.match(line)
            if not match:
                continue
            candidate = match.group(1)
            # Skip well-known stdlib invocations like ``python -m``.
            if candidate.startswith("-") or "://" in candidate:
                continue
            target = REPO_ROOT / candidate
            if not target.exists():
                failures.append(
                    _err(
                        wf,
                        f"line {lineno}: workflow invokes nonexistent path "
                        f"{candidate!r}",
                    )
                )
    return failures


def main() -> int:
    all_failures: list[str] = []
    all_failures.extend(check_hook_paths_in_settings())
    all_failures.extend(check_skill_directories_have_skill_md())
    all_failures.extend(
        check_markdown_has_frontmatter(
            REPO_ROOT / ".claude" / "rules", "rule",
        )
    )
    all_failures.extend(
        check_markdown_has_frontmatter(
            REPO_ROOT / ".claude" / "agents", "agent",
        )
    )
    all_failures.extend(check_heavy_lane_paths_sync())
    all_failures.extend(check_vocabulary_pinned_files_present())
    all_failures.extend(check_workflow_script_invocations())

    if all_failures:
        for line in all_failures:
            print(line, file=sys.stderr)
        print(
            f"\ncheck_manifests: {len(all_failures)} defect(s) found",
            file=sys.stderr,
        )
        return 1
    print("check_manifests: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
