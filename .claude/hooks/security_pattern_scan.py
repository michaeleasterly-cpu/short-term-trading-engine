#!/usr/bin/env python3
"""PostToolUse(Edit|Write|MultiEdit) — Layer 1 security pattern scan.

Vendored from anthropics/claude-code plugins/security-guidance Layer 1
("Pattern-based rules") per docs/audits/2026-06-03-vendor-vs-handrolled.md
§2 + operator decision §9 #1 ("Layer 1 only — defer Layers 2 + 3
until cost is measured").

What this hook does:
  - Reads the PostToolUse JSON from stdin.
  - Extracts (tool_name, file_path, content) from tool_input.
  - Matches against:
      * security_patterns_vendored.SECURITY_PATTERNS (Anthropic, ~25 rules)
      * security_patterns_ste.SECURITY_PATTERNS_STE (STE-specific, 5 rules)
  - If any pattern matches, emits a `hookSpecificOutput` with
    `additionalContext` carrying the reminder text.
  - Exit 0 (advisory only — never blocks the edit).

What this hook does NOT do:
  - Never calls an LLM. Layers 2 + 3 of Anthropic's plugin (Stop-hook
    LLM diff review, agentic commit/push review) are not vendored.
  - Never blocks the edit. PostToolUse cannot block retroactively
    (per https://code.claude.com/docs/en/hooks); a reminder is the
    canonical PostToolUse advisory.
  - Never writes to memory, never calls `gh`, never modifies files.

Kill switch:
  - STE_SECURITY_PATTERN_SCAN_DISABLE=1 disables the hook entirely
    (advisory exit 0 with no output).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent

# Import the pattern data (both files live next to this script).
sys.path.insert(0, str(_HOOKS_DIR))
try:
    from security_patterns_vendored import SECURITY_PATTERNS  # noqa: E402
    from security_patterns_ste import SECURITY_PATTERNS_STE  # noqa: E402
except ImportError as exc:
    # Pattern data missing — emit a system message but don't block.
    print(json.dumps({
        "systemMessage": (
            f"security_pattern_scan: pattern data missing ({exc}); "
            "advisory scan disabled this turn"
        )
    }))
    sys.exit(0)


def _kill_switch_engaged() -> bool:
    return os.environ.get("STE_SECURITY_PATTERN_SCAN_DISABLE") == "1"


def _extract_content(tool_name: str, tool_input: dict) -> str:
    """Mirror anthropics/claude-code/.../security_reminder_hook.py
    extract_content_from_input — kept tight so a single change in
    upstream is easy to fold in."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", []) or []
        return "\n".join(e.get("new_string", "") or "" for e in edits)
    if tool_name == "NotebookEdit":
        return tool_input.get("new_source", "") or ""
    return ""


def _check_patterns(file_path: str, content: str) -> list[tuple[str, str]]:
    """Same matcher shape as Anthropic's check_patterns. Iterates both
    pattern lists; returns (rule_name, reminder_text) for each match."""
    normalized_path = file_path.lstrip("/")
    matches: list[tuple[str, str]] = []
    for pattern in list(SECURITY_PATTERNS) + list(SECURITY_PATTERNS_STE):
        if "path_filter" in pattern:
            try:
                if not pattern["path_filter"](normalized_path):
                    continue
            except Exception:
                continue
        matched = False
        if "path_check" in pattern:
            try:
                if pattern["path_check"](normalized_path):
                    matched = True
            except Exception:
                pass
        if not matched and "substrings" in pattern and content:
            for substring in pattern["substrings"]:
                if substring in content:
                    matched = True
                    break
        if not matched and "regex" in pattern and content:
            try:
                if re.search(pattern["regex"], content):
                    matched = True
            except Exception:
                pass
        if matched:
            matches.append((pattern["ruleName"], pattern["reminder"]))
    return matches


def main() -> int:
    if _kill_switch_engaged():
        return 0
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed input — silently exit 0 (advisory hook, never blocks).
        return 0

    tool_name = input_data.get("tool_name", "")
    if tool_name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return 0

    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""
    content = _extract_content(tool_name, tool_input)

    if not content:
        return 0

    matches = _check_patterns(file_path, content)
    if not matches:
        return 0

    # Emit additionalContext per Anthropic's hook output schema. One
    # combined block; Claude can resurface specific warnings as needed.
    rule_names = ", ".join(name for name, _ in matches)
    reminder_block = "\n\n".join(reminder for _, reminder in matches)
    additional_context = (
        f"Security pattern scan (advisory, Layer 1) flagged "
        f"{len(matches)} pattern(s) — {rule_names}:\n\n{reminder_block}\n\n"
        "Acknowledge or address; this is advisory only (never blocks)."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
