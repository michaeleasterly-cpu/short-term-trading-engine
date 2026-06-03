"""C0.3 (2026-06-01) — Claude execution-surface contract sentinels.

Pins the load-bearing behavior of ``.claude/settings.json``, the
hooks under ``.claude/hooks/``, the agents under ``.claude/agents/``,
and the skills under ``.claude/skills/``. The goal is defense-in-depth
against silent weakening of hook enforcement, agent boundaries, or
skill scope.

The heavy-lane Claude review workflow was removed 2026-06-03 (operator
directive: "turn it off entirely. The subagent profiles + your manual
gate already cover the discipline. The workflow becomes dead weight
you pay for.") — review is now: static checks (Layer 1, gitleaks +
manifest linter + sentinels) → operator gate (Layer 2). The
spec-reviewer + code-quality-reviewer subagent profiles cover what the
paid action did, without the API spend.

Coverage is intentionally conservative — substring presence + smart
negation-aware scans, not semantic NLP. Comments and YAML
frontmatter are stripped before forbidden-command scans on shell
scripts; markdown forbidden-command scans treat a negation (``do
not``, ``never``, ``prohibit``, ``must not``, ``block``, ``forbid``)
within 80 chars before the match as proof the instruction is
explicitly forbidding the action rather than authorizing it.

Stdlib-only (pathlib, json, re).
"""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CLAUDE = _REPO / ".claude"
_SETTINGS = _CLAUDE / "settings.json"
_HOOKS_DIR = _CLAUDE / "hooks"
_AGENTS_DIR = _CLAUDE / "agents"
_SKILLS_DIR = _CLAUDE / "skills"

# ─────────────────────────────────────────────────────────────────────
# Forbidden-command catalogue. Patterns are regexes intended to match
# command-invocation forms specifically, not arbitrary mentions of the
# word. Each carries a short label for failure messages.
#
# Scope (operator clarification 2026-06-01): the contract protects the
# Claude *review/orchestration* surface from silent weakening. It does
# NOT pre-emptively ban deployment paths (``docker`` /
# ``railway up``) — the project deploys to Railway and may legitimately
# automate deploys from operator-controlled paths in the future.
# ─────────────────────────────────────────────────────────────────────
_HOOK_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    (r"\bgh\s+api\s+.*--method\s+(PATCH|POST|PUT|DELETE)\b", "gh api mutation"),
    (r"\bgit\s+push\s+[^\n]*(--force|--force-with-lease|\s-f(\s|$))", "git push --force"),
    (r"curl\s+[^\n]*ANTHROPIC_API_KEY", "Anthropic API call from hook"),
    (r"/memory_stores/[^\s]+/memories", "memstore API endpoint"),
)

_AGENT_SKILL_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    (r"\bauto[- ]?merge\b", "auto-merge"),
    (r"\bauto[- ]?fix\b", "auto-fix"),
    (r"\bauto[- ]?rebase\b", "auto-rebase"),
    (r"\bgit\s+push\s+[^\n]*(--force|-f\s)", "git push --force"),
    (r"/memory_stores/[^\s]+/memories", "memstore API mutation"),
)

_NEGATION_WINDOW = 80
_NEGATION_TERMS = (
    "do not",
    "don't",
    "never",
    "must not",
    "must NOT",
    "MUST NOT",
    "prohibit",
    "prohibited",
    "forbid",
    "forbidden",
    "block",
    "blocks",
    "blocked",
    "refuse",
    "refuses",
    "reject",
    "rejects",
    "without",
    "no ",
    "NEVER",
)


def _strip_shell_comments(text: str) -> str:
    """Return ``text`` with full-line shell comments removed.

    A line whose first non-whitespace character is ``#`` is a comment;
    we drop it entirely. Inline-trailing comments (``foo  # bar``) are
    preserved because they often follow a real command on the same
    line — the forbidden-command regex would still catch the command
    portion.
    """
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(raw)
    return "\n".join(out)


def _has_negation_nearby(text: str, idx: int) -> bool:
    window_start = max(0, idx - _NEGATION_WINDOW)
    window = text[window_start:idx]
    lowered = window.lower()
    for term in _NEGATION_TERMS:
        if term.lower() in lowered:
            return True
    return False


def _find_forbidden(
    text: str,
    patterns: tuple[tuple[str, str], ...],
    *,
    allow_negation: bool,
) -> list[tuple[str, str]]:
    """Return ``(label, matched_text)`` for every forbidden pattern
    that fires in ``text``. When ``allow_negation`` is True, a
    negation term within 80 chars before the match suppresses the
    finding (the instruction is explicitly forbidding the action,
    not authorizing it)."""
    findings: list[tuple[str, str]] = []
    for pattern, label in patterns:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            if allow_negation and _has_negation_nearby(text, m.start()):
                continue
            findings.append((label, m.group(0)))
    return findings


def _markdown_body(text: str) -> str:
    """Strip leading YAML frontmatter from a markdown file so
    forbidden-pattern scans run against the instruction body, not the
    metadata fields."""
    if not text.startswith("---"):
        return text
    # Skip past the closing ``---`` of the frontmatter.
    closing = re.search(r"\n---\s*\n", text)
    if closing is None:
        return text
    return text[closing.end():]


def _settings_data() -> dict:
    assert _SETTINGS.is_file(), f"missing {_SETTINGS.relative_to(_REPO)}"
    return json.loads(_SETTINGS.read_text(encoding="utf-8"))


def _hook_command_paths(data: dict) -> list[str]:
    """Walk the settings hooks block and return every ``command``
    string. The settings file uses the ``$CLAUDE_PROJECT_DIR``
    placeholder; we strip that for path resolution but return the
    raw command for diagnostic messages."""
    out: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            cmd = node.get("command")
            if isinstance(cmd, str):
                out.append(cmd)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    return out


def _resolve_hook_path(cmd: str) -> Path:
    return Path(cmd.replace("$CLAUDE_PROJECT_DIR", str(_REPO)))


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

def test_settings_json_parseable() -> None:
    """``.claude/settings.json`` must parse as JSON. Anything that
    breaks the JSON would silently disable every hook downstream."""
    data = _settings_data()
    assert isinstance(data, dict), (
        f"{_SETTINGS.relative_to(_REPO)} must be a JSON object"
    )
    assert "hooks" in data, (
        f"{_SETTINGS.relative_to(_REPO)} must declare a 'hooks' block"
    )


def test_settings_referenced_hooks_exist_and_executable() -> None:
    """Every hook command referenced in settings.json must resolve to
    an existing executable file under ``.claude/hooks/``. A dangling
    reference would silently no-op the hook at runtime."""
    data = _settings_data()
    failures: list[str] = []
    for cmd in _hook_command_paths(data):
        if ".claude/hooks/" not in cmd:
            # Settings supports non-script hook commands in principle;
            # we only validate the project-local-script form. A future
            # non-script hook would need a separate sentinel.
            continue
        target = _resolve_hook_path(cmd)
        if not target.exists():
            failures.append(f"{cmd}: hook script not found on disk")
            continue
        if not target.is_file():
            failures.append(f"{cmd}: hook path is not a regular file")
            continue
        mode = target.stat().st_mode
        if not (mode & stat.S_IXUSR):
            failures.append(f"{cmd}: hook script not executable (chmod +x)")
    assert not failures, (
        "Settings hooks block references defective scripts:\n  "
        + "\n  ".join(failures)
    )


def test_hooks_have_shebang_and_are_executable() -> None:
    """Every ``*.sh`` in ``.claude/hooks/`` (whether referenced from
    settings or not) must start with a shebang and carry the
    executable bit. An unsigned/unexecutable script falls back to
    shell interpreter guesswork at runtime."""
    assert _HOOKS_DIR.is_dir(), f"missing hooks dir: {_HOOKS_DIR.relative_to(_REPO)}"
    failures: list[str] = []
    hooks = sorted(_HOOKS_DIR.glob("*.sh"))
    assert hooks, "no .sh files in .claude/hooks/"
    for hook in hooks:
        rel = hook.relative_to(_REPO)
        try:
            first = hook.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeDecodeError, IndexError) as exc:
            failures.append(f"{rel}: unreadable / empty ({exc})")
            continue
        if not first.startswith("#!"):
            failures.append(f"{rel}: missing shebang on line 1 (got {first!r})")
        mode = hook.stat().st_mode
        if not (mode & stat.S_IXUSR):
            failures.append(f"{rel}: not executable (chmod +x)")
    assert not failures, (
        "Hook script defects:\n  " + "\n  ".join(failures)
    )


def test_hooks_do_not_invoke_forbidden_commands() -> None:
    """Hooks must not invoke ``gh pr merge``, ``git push --force``,
    Anthropic API calls, or memstore mutations. Comments are stripped
    before scanning so documentation of what a hook *blocks* doesn't
    false-positive. Deployment commands (``docker`` / ``railway``)
    are NOT in scope — this contract protects the review/orchestration
    surface, not the deploy path."""
    failures: list[str] = []
    for hook in sorted(_HOOKS_DIR.glob("*.sh")):
        rel = hook.relative_to(_REPO)
        text = hook.read_text(encoding="utf-8")
        scan_text = _strip_shell_comments(text)
        findings = _find_forbidden(
            scan_text,
            _HOOK_FORBIDDEN_PATTERNS,
            allow_negation=False,
        )
        for label, matched in findings:
            failures.append(f"{rel}: forbidden {label!r}: {matched!r}")
    assert not failures, (
        "Hooks invoke forbidden commands:\n  " + "\n  ".join(failures)
    )


def test_agents_have_frontmatter_and_body() -> None:
    """Every ``.claude/agents/*.md`` must carry YAML frontmatter and
    a non-empty body. Without frontmatter, Claude Code can't dispatch
    the agent by name."""
    assert _AGENTS_DIR.is_dir(), f"missing agents dir: {_AGENTS_DIR.relative_to(_REPO)}"
    failures: list[str] = []
    agents = sorted(_AGENTS_DIR.glob("*.md"))
    assert agents, "no agent files in .claude/agents/"
    for agent in agents:
        rel = agent.relative_to(_REPO)
        text = agent.read_text(encoding="utf-8")
        if not text.startswith("---"):
            failures.append(f"{rel}: missing YAML frontmatter (---/---)")
            continue
        body = _markdown_body(text)
        if not body.strip():
            failures.append(f"{rel}: body is empty after frontmatter")
    assert not failures, (
        "Agent file defects:\n  " + "\n  ".join(failures)
    )


def test_agents_do_not_permit_auto_merge_or_heavy_lane_autofix() -> None:
    """Agent bodies must not instruct Claude to auto-merge, auto-fix,
    auto-rebase, force-push, or write to Anthropic memstores.
    Negation-aware so "do not auto-merge" survives the scan."""
    failures: list[str] = []
    for agent in sorted(_AGENTS_DIR.glob("*.md")):
        rel = agent.relative_to(_REPO)
        body = _markdown_body(agent.read_text(encoding="utf-8"))
        findings = _find_forbidden(
            body,
            _AGENT_SKILL_FORBIDDEN_PATTERNS,
            allow_negation=True,
        )
        for label, matched in findings:
            failures.append(f"{rel}: authorizes {label!r}: {matched!r}")
    assert not failures, (
        "Agent files authorize forbidden actions (no negation "
        "nearby):\n  " + "\n  ".join(failures)
    )


def test_skills_have_frontmatter_and_body() -> None:
    """Every ``.claude/skills/<name>/SKILL.md`` must carry YAML
    frontmatter and a non-empty body. Missing SKILL.md means the
    skill silently doesn't load."""
    assert _SKILLS_DIR.is_dir(), f"missing skills dir: {_SKILLS_DIR.relative_to(_REPO)}"
    failures: list[str] = []
    skill_dirs = sorted([d for d in _SKILLS_DIR.iterdir() if d.is_dir()])
    assert skill_dirs, "no skill directories under .claude/skills/"
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        rel = skill_md.relative_to(_REPO)
        if not skill_md.exists():
            failures.append(f"{rel}: SKILL.md missing")
            continue
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            failures.append(f"{rel}: missing YAML frontmatter (---/---)")
            continue
        body = _markdown_body(text)
        if not body.strip():
            failures.append(f"{rel}: body is empty after frontmatter")
    assert not failures, (
        "Skill file defects:\n  " + "\n  ".join(failures)
    )


def test_skills_do_not_permit_auto_merge_or_unapproved_memory_writes() -> None:
    """Skill bodies must not authorize auto-merge, auto-fix on heavy
    lane, force-push, or unsanctioned memstore writes.
    Negation-aware."""
    failures: list[str] = []
    skill_mds = sorted(_SKILLS_DIR.glob("*/SKILL.md"))
    for skill_md in skill_mds:
        rel = skill_md.relative_to(_REPO)
        body = _markdown_body(skill_md.read_text(encoding="utf-8"))
        findings = _find_forbidden(
            body,
            _AGENT_SKILL_FORBIDDEN_PATTERNS,
            allow_negation=True,
        )
        for label, matched in findings:
            failures.append(f"{rel}: authorizes {label!r}: {matched!r}")
    assert not failures, (
        "Skill files authorize forbidden actions (no negation "
        "nearby):\n  " + "\n  ".join(failures)
    )


def test_paid_claude_review_workflow_absent() -> None:
    """The heavy-lane paid Claude review workflow was removed
    2026-06-03. This sentinel asserts it stays gone — re-adding it
    requires a deliberate operator-approved PR (see
    ``docs/audits/2026-06-03-claude-code-workflow-controls.md`` §12).
    """
    workflow = (
        _REPO / ".github" / "workflows" / "claude-review-heavy-lane.yml"
    )
    assert not workflow.exists(), (
        f"{workflow.relative_to(_REPO)} reappeared. The paid heavy-lane "
        "Claude review was retired 2026-06-03 (operator: 'Turn it off "
        "entirely. The subagent profiles + your manual gate already "
        "cover the discipline.'). If you intend to bring it back, do so "
        "in a deliberate PR that also removes this sentinel."
    )
