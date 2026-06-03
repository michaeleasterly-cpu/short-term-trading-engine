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

  4. The canonical path registry at ``.claude/path_registry.yaml``
     is well-formed (schema_version == 1; ``heavy_lane`` and
     ``claude_system`` groups present with non-empty descriptions and
     non-empty per-entry ``path`` + ``why``; no duplicate paths within
     a group; groups disjoint) AND every consumer artifact is in sync
     with it:

       * ``.claude/rules/heavy-lane.md`` frontmatter ``paths:`` equals
         ``heavy_lane`` exactly.
       * ``docs/DEV_PIPELINE_STANDARD.md``,
         ``.github/pull_request_template.md``, and
         ``.claude/hooks/session-start.sh`` each contain every
         ``heavy_lane`` path string verbatim.

     The paid heavy-lane Claude review workflow that previously
     consumed ``heavy_lane ∪ claude_system`` as its ``paths:`` filter
     was retired 2026-06-03 — the registry's ``claude_system`` group
     is now consumed only by the path-scoped rules and docs, not by a
     paid action.

  5. C0.3 (2026-06-01) Claude execution-surface contract:

       * Every ``.claude/hooks/*.sh`` has a shebang and is executable.
       * No hook invokes ``gh pr merge``, ``git push --force``,
         ``curl`` against the Anthropic API, or memstore mutation
         endpoints (comments stripped before scan). Deployment
         commands are intentionally out of scope.
       * Every ``.claude/agents/*.md`` carries YAML frontmatter and a
         non-empty body.
       * No agent or skill body authorizes auto-merge, auto-fix,
         auto-rebase, force-push, or memstore mutation (negation-
         aware — ``do not auto-merge`` survives the scan).

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


# ─────────────────────────────────────────────────────────────────────
# C0.3 (2026-06-01) — Claude execution-surface contract.
# ─────────────────────────────────────────────────────────────────────

# Scope: the contract protects the Claude review / orchestration
# surface from silent weakening. Deployment commands (``docker`` /
# ``railway up``) are intentionally NOT in scope — the project deploys
# to Railway and may automate deploys from operator-controlled paths
# in the future. The workflow ``--allowedTools`` block below still
# bars deploy commands because the review action is review-only by
# design (deployment is operator-side, never run by the reviewer).
_HOOK_FORBIDDEN_REGEXES: tuple[tuple[str, str], ...] = (
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    (r"\bgh\s+api\s+.*--method\s+(PATCH|POST|PUT|DELETE)\b", "gh api mutation"),
    (r"\bgit\s+push\s+[^\n]*(--force|--force-with-lease|\s-f(\s|$))", "git push --force"),
    (r"curl\s+[^\n]*ANTHROPIC_API_KEY", "Anthropic API call from hook"),
    (r"/memory_stores/[^\s]+/memories", "memstore API endpoint"),
)

_AGENT_SKILL_FORBIDDEN_REGEXES: tuple[tuple[str, str], ...] = (
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    (r"\bauto[- ]?merge\b", "auto-merge"),
    (r"\bauto[- ]?fix\b", "auto-fix"),
    (r"\bauto[- ]?rebase\b", "auto-rebase"),
    (r"\bgit\s+push\s+[^\n]*(--force|-f\s)", "git push --force"),
    (r"/memory_stores/[^\s]+/memories", "memstore API mutation"),
)

_NEGATION_WINDOW = 80
_NEGATION_TERMS = (
    "do not", "don't", "never", "must not", "must NOT", "MUST NOT",
    "prohibit", "prohibited", "forbid", "forbidden",
    "block", "blocks", "blocked", "refuse", "refuses",
    "reject", "rejects", "without", "no ", "NEVER",
)


def _strip_shell_comments_for_scan(text: str) -> str:
    out: list[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        out.append(raw)
    return "\n".join(out)


def _has_negation_nearby(text: str, idx: int) -> bool:
    window = text[max(0, idx - _NEGATION_WINDOW):idx].lower()
    return any(term.lower() in window for term in _NEGATION_TERMS)


def _scan_forbidden(
    text: str,
    patterns: tuple[tuple[str, str], ...],
    *,
    allow_negation: bool,
) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for pattern, label in patterns:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            if allow_negation and _has_negation_nearby(text, m.start()):
                continue
            findings.append((label, m.group(0)))
    return findings


def _markdown_body_after_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    closing = re.search(r"\n---\s*\n", text)
    if closing is None:
        return text
    return text[closing.end():]


def check_hooks_have_shebang_and_are_executable() -> list[str]:
    """Every ``.claude/hooks/*.sh`` must carry a shebang on line 1 and
    have the executable bit set. An unsigned / unexecutable hook is
    silently interpreted by whatever shell the runtime picks."""
    failures: list[str] = []
    hooks_dir = REPO_ROOT / ".claude" / "hooks"
    if not hooks_dir.is_dir():
        return failures
    for hook in sorted(hooks_dir.glob("*.sh")):
        try:
            first_line = hook.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeDecodeError, IndexError) as exc:
            failures.append(_err(hook, f"unreadable or empty: {exc}"))
            continue
        if not first_line.startswith("#!"):
            failures.append(
                _err(hook, "missing shebang on line 1 (C0.3 invariant)")
            )
        try:
            mode = hook.stat().st_mode
            if not (mode & 0o111):
                failures.append(
                    _err(hook, "not executable (chmod +x needed)")
                )
        except OSError:
            pass
    return failures


def check_hooks_do_not_invoke_forbidden_commands() -> list[str]:
    """Hooks must not invoke ``gh pr merge``, ``git push --force``,
    ``curl`` against the Anthropic API, or memstore mutation
    endpoints. Comments are stripped before scanning so a hook
    documenting what it *blocks* does not false-positive. Deployment
    commands (``docker`` / ``railway``) are NOT in scope — this
    contract protects the review/orchestration surface, not the
    deploy path."""
    failures: list[str] = []
    hooks_dir = REPO_ROOT / ".claude" / "hooks"
    if not hooks_dir.is_dir():
        return failures
    for hook in sorted(hooks_dir.glob("*.sh")):
        text = hook.read_text(encoding="utf-8")
        scan_text = _strip_shell_comments_for_scan(text)
        findings = _scan_forbidden(
            scan_text, _HOOK_FORBIDDEN_REGEXES, allow_negation=False,
        )
        for label, matched in findings:
            failures.append(_err(hook, f"forbidden {label}: {matched!r}"))
    return failures


def check_agents_have_frontmatter_and_body() -> list[str]:
    """Mirror of the agent-frontmatter rule with an explicit
    non-empty-body check (C0.3 strengthening over the generic
    ``check_markdown_has_frontmatter``)."""
    failures: list[str] = []
    agents_dir = REPO_ROOT / ".claude" / "agents"
    if not agents_dir.is_dir():
        return failures
    for agent in sorted(agents_dir.glob("*.md")):
        text = agent.read_text(encoding="utf-8")
        if not text.startswith("---"):
            failures.append(
                _err(agent, "missing YAML frontmatter (---/---)")
            )
            continue
        body = _markdown_body_after_frontmatter(text)
        if not body.strip():
            failures.append(_err(agent, "body is empty after frontmatter"))
    return failures


def check_agents_skills_no_forbidden_authorizations() -> list[str]:
    """Agent and skill instruction bodies must not authorize
    auto-merge, auto-fix, auto-rebase, ``gh pr merge``, force-push,
    or memstore mutations. Negation-aware scan (``do not auto-merge``
    survives). Deployment commands are intentionally out of scope —
    see ``_HOOK_FORBIDDEN_REGEXES`` for the rationale."""
    failures: list[str] = []
    for base, glob in (
        (REPO_ROOT / ".claude" / "agents", "*.md"),
        (REPO_ROOT / ".claude" / "skills", "*/SKILL.md"),
    ):
        if not base.is_dir():
            continue
        for target in sorted(base.glob(glob)):
            body = _markdown_body_after_frontmatter(
                target.read_text(encoding="utf-8")
            )
            findings = _scan_forbidden(
                body, _AGENT_SKILL_FORBIDDEN_REGEXES, allow_negation=True,
            )
            for label, matched in findings:
                failures.append(
                    _err(
                        target,
                        f"authorizes {label} (no negation nearby): "
                        f"{matched!r}",
                    )
                )
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


_REGISTRY_PATH = REPO_ROOT / ".claude" / "path_registry.yaml"


def _parse_path_registry(text: str) -> dict[str, object] | None:
    """Stdlib-only minimal extractor for ``.claude/path_registry.yaml``.

    Returns a dict shaped::

        {
          "schema_version": <int>,
          "groups": {
            "<group>": {
              "description": "<string>",  # may be empty
              "paths": [{"path": "<glob>", "why": "<string>"}, ...],
            },
            ...
          },
        }

    The parser intentionally supports only the registry's exact shape
    (no anchors, no flow style, no nested mappings beyond what's
    documented in the registry file's header comment). Any deviation
    is a defect either in the registry or in the parser and should
    fail loudly — never silently lose entries.
    """
    out: dict[str, object] = {"schema_version": None, "groups": {}}
    groups: dict[str, dict[str, object]] = {}

    lines = text.splitlines()
    in_groups = False
    in_description = False
    current_group: str | None = None
    current_item: dict[str, str] | None = None
    description_buf: list[str] = []
    description_indent: int | None = None
    in_paths_list = False

    for raw in lines:
        if raw.lstrip().startswith("#"):
            continue
        if not raw.strip():
            # Blank line. Inside a multiline description, preserve it
            # as an empty body line. Outside, it's neutral — skip.
            if in_description and description_indent is not None:
                description_buf.append("")
            continue

        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        # Top-level keys (column 0).
        if indent == 0:
            in_description = False
            description_buf = []
            description_indent = None
            in_paths_list = False
            current_group = None
            current_item = None
            if stripped.startswith("schema_version:"):
                value = stripped.split(":", 1)[1].strip()
                try:
                    out["schema_version"] = int(value)
                except ValueError:
                    out["schema_version"] = value  # surface defect via schema check
                in_groups = False
                continue
            if stripped == "groups:":
                in_groups = True
                continue
            # Any other top-level key → leaves the groups block.
            in_groups = False
            continue

        if not in_groups:
            continue

        # Group name lines (indent 2) terminate any open description /
        # path item belonging to the previous group.
        if indent == 2 and stripped.endswith(":"):
            current_group = stripped[:-1].strip()
            groups[current_group] = {"description": "", "paths": []}
            in_description = False
            description_buf = []
            description_indent = None
            in_paths_list = False
            current_item = None
            continue

        if current_group is None:
            continue
        group = groups[current_group]

        # Inside a multiline description block — append until we
        # dedent back to (or above) the group's child-key indent (4).
        if in_description:
            if description_indent is None:
                # First body line sets the description's base indent.
                # If this line is already at the child-key indent (4),
                # it's actually the next sibling — close the block.
                if indent > 4:
                    description_indent = indent
                    description_buf.append(raw[description_indent:])
                    continue
                # Fall through to dedent close.
            elif indent >= description_indent:
                description_buf.append(raw[description_indent:])
                continue
            # Dedent — close the description, fall through to handle
            # this line as a normal child key.
            group["description"] = "\n".join(description_buf).rstrip() + "\n"
            in_description = False
            description_buf = []
            description_indent = None

        # Group child keys live at indent 4. Recognise ``description:
        # |`` and ``paths:``.
        if indent == 4:
            if stripped.startswith("description:"):
                rest = stripped.split(":", 1)[1].strip()
                if rest == "|":
                    in_description = True
                    description_buf = []
                    description_indent = None  # set on first body line
                else:
                    # Single-line description.
                    group["description"] = rest
                in_paths_list = False
                current_item = None
                continue
            if stripped == "paths:":
                in_paths_list = True
                current_item = None
                continue
            # Unknown sibling key inside a group → ignore (surface via
            # schema check if it matters).
            in_paths_list = False
            current_item = None
            continue

        if not in_paths_list:
            continue

        # ``paths:`` list items.
        if stripped.startswith("- path:"):
            value = stripped[len("- path:"):].strip()
            value = _strip_quotes(value)
            current_item = {"path": value, "why": ""}
            assert isinstance(group["paths"], list)
            group["paths"].append(current_item)
            continue
        if current_item is not None and stripped.startswith("why:"):
            value = stripped[len("why:"):].strip()
            current_item["why"] = _strip_quotes(value)
            continue
        # Continuation lines / unknown — leave the item alone so the
        # schema check can surface anything weird.

    # Close any trailing description block.
    if in_description and description_buf:
        if current_group is not None:
            groups[current_group]["description"] = (
                "\n".join(description_buf).rstrip() + "\n"
            )

    out["groups"] = groups
    return out


def _strip_quotes(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def load_registry() -> tuple[dict[str, object] | None, list[str]]:
    failures: list[str] = []
    if not _REGISTRY_PATH.exists():
        failures.append(
            _err(_REGISTRY_PATH, "missing .claude/path_registry.yaml")
        )
        return None, failures
    try:
        text = _REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(_err(_REGISTRY_PATH, f"unreadable: {exc}"))
        return None, failures
    data = _parse_path_registry(text)
    return data, failures


def registry_paths(data: dict[str, object], group: str) -> list[str]:
    groups = data.get("groups", {})
    if not isinstance(groups, dict):
        return []
    g = groups.get(group)
    if not isinstance(g, dict):
        return []
    paths = g.get("paths")
    if not isinstance(paths, list):
        return []
    out: list[str] = []
    for item in paths:
        if isinstance(item, dict):
            value = item.get("path")
            if isinstance(value, str) and value:
                out.append(value)
    return out


def check_path_registry_schema() -> list[str]:
    """The registry file is well-formed.

      * schema_version == 1
      * heavy_lane + claude_system groups present, both non-empty
      * every path entry has non-empty ``path`` and ``why``
      * no duplicate paths within a group
      * groups are disjoint (no path in both heavy_lane and
        claude_system)
    """
    failures: list[str] = []
    data, load_failures = load_registry()
    failures.extend(load_failures)
    if data is None:
        return failures

    schema_version = data.get("schema_version")
    if schema_version != 1:
        failures.append(
            _err(
                _REGISTRY_PATH,
                f"schema_version must be 1, got {schema_version!r}",
            )
        )

    groups = data.get("groups")
    if not isinstance(groups, dict):
        failures.append(_err(_REGISTRY_PATH, "missing top-level 'groups:' block"))
        return failures

    for required_group in ("heavy_lane", "claude_system"):
        if required_group not in groups:
            failures.append(
                _err(_REGISTRY_PATH, f"missing required group: {required_group}")
            )
            continue
        group_data = groups[required_group]
        if not isinstance(group_data, dict):
            failures.append(
                _err(
                    _REGISTRY_PATH,
                    f"group {required_group!r} is not a mapping",
                )
            )
            continue
        if not group_data.get("description"):
            failures.append(
                _err(
                    _REGISTRY_PATH,
                    f"group {required_group!r} missing non-empty description",
                )
            )
        paths_list = group_data.get("paths")
        if not isinstance(paths_list, list) or not paths_list:
            failures.append(
                _err(
                    _REGISTRY_PATH,
                    f"group {required_group!r} has no 'paths:' entries",
                )
            )
            continue
        seen: set[str] = set()
        for item in paths_list:
            if not isinstance(item, dict):
                failures.append(
                    _err(
                        _REGISTRY_PATH,
                        f"{required_group}: list entry is not a mapping: {item!r}",
                    )
                )
                continue
            path_value = item.get("path")
            why_value = item.get("why")
            if not isinstance(path_value, str) or not path_value.strip():
                failures.append(
                    _err(
                        _REGISTRY_PATH,
                        f"{required_group}: entry missing non-empty 'path': {item!r}",
                    )
                )
                continue
            if not isinstance(why_value, str) or not why_value.strip():
                failures.append(
                    _err(
                        _REGISTRY_PATH,
                        f"{required_group}: entry {path_value!r} missing non-empty 'why'",
                    )
                )
            if path_value in seen:
                failures.append(
                    _err(
                        _REGISTRY_PATH,
                        f"{required_group}: duplicate path entry: {path_value}",
                    )
                )
            seen.add(path_value)

    if isinstance(groups.get("heavy_lane"), dict) and isinstance(
        groups.get("claude_system"), dict
    ):
        heavy = set(registry_paths(data, "heavy_lane"))
        claude = set(registry_paths(data, "claude_system"))
        overlap = heavy & claude
        for path in sorted(overlap):
            failures.append(
                _err(
                    _REGISTRY_PATH,
                    f"path {path!r} appears in BOTH heavy_lane and "
                    f"claude_system — groups must be disjoint",
                )
            )
    return failures


def check_heavy_lane_rule_frontmatter_equals_registry() -> list[str]:
    """``.claude/rules/heavy-lane.md`` frontmatter ``paths:`` MUST
    equal exactly ``heavy_lane`` from the registry."""
    failures: list[str] = []
    rule = REPO_ROOT / ".claude" / "rules" / "heavy-lane.md"
    if not rule.exists():
        failures.append(_err(rule, "missing .claude/rules/heavy-lane.md"))
        return failures
    data, load_failures = load_registry()
    failures.extend(load_failures)
    if data is None:
        return failures
    expected = set(registry_paths(data, "heavy_lane"))
    rule_text = rule.read_text(encoding="utf-8")
    rule_paths = set(_extract_yaml_paths_list(rule_text))
    missing = expected - rule_paths
    extras = rule_paths - expected
    for p in sorted(missing):
        failures.append(
            _err(rule, f"registry heavy_lane path missing from rule: {p}")
        )
    for p in sorted(extras):
        failures.append(
            _err(rule, f"rule lists path not in registry heavy_lane: {p}")
        )
    return failures


def _check_heavy_lane_string_presence(
    target: Path, label: str,
) -> list[str]:
    """Every heavy_lane path in the registry MUST appear verbatim in
    ``target``. Cheap string-search drift sentinel."""
    failures: list[str] = []
    if not target.exists():
        failures.append(_err(target, f"missing {label}"))
        return failures
    data, load_failures = load_registry()
    failures.extend(load_failures)
    if data is None:
        return failures
    text = target.read_text(encoding="utf-8")
    for path in registry_paths(data, "heavy_lane"):
        if path not in text:
            failures.append(
                _err(
                    target,
                    f"{label} missing registry heavy_lane path: {path}",
                )
            )
    return failures


def check_doc_pipeline_standard_lists_heavy_lane() -> list[str]:
    return _check_heavy_lane_string_presence(
        REPO_ROOT / "docs" / "DEV_PIPELINE_STANDARD.md",
        "docs/DEV_PIPELINE_STANDARD.md",
    )


def check_pr_template_lists_heavy_lane() -> list[str]:
    return _check_heavy_lane_string_presence(
        REPO_ROOT / ".github" / "pull_request_template.md",
        ".github/pull_request_template.md",
    )


def check_session_start_hook_lists_heavy_lane() -> list[str]:
    return _check_heavy_lane_string_presence(
        REPO_ROOT / ".claude" / "hooks" / "session-start.sh",
        ".claude/hooks/session-start.sh",
    )


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
    all_failures.extend(check_path_registry_schema())
    all_failures.extend(check_heavy_lane_rule_frontmatter_equals_registry())
    all_failures.extend(check_doc_pipeline_standard_lists_heavy_lane())
    all_failures.extend(check_pr_template_lists_heavy_lane())
    all_failures.extend(check_session_start_hook_lists_heavy_lane())
    all_failures.extend(check_hooks_have_shebang_and_are_executable())
    all_failures.extend(check_hooks_do_not_invoke_forbidden_commands())
    all_failures.extend(check_agents_have_frontmatter_and_body())
    all_failures.extend(check_agents_skills_no_forbidden_authorizations())
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
