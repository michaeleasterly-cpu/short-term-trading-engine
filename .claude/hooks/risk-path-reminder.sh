#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit) — informational reminder when a file
# under tpcore/risk/ is touched. Reminder ONLY (exit 0); never blocks
# (a PostToolUse block would roll back the edit; that's not the intent —
# the heavy-lane pipeline is operator + reviewer discipline, not a hook
# guarantee).
# Authoritative external: https://code.claude.com/docs/en/hooks-guide
# Project SoT: .claude/rules/risk-path.md + .claude/rules/heavy-lane.md.
set -euo pipefail

input="$(cat)"
fp="$(echo "$input" | jq -r '.tool_input.file_path // empty')"

case "$fp" in
  */tpcore/risk/*|tpcore/risk/*)
    echo "REMINDER: you just edited tpcore/risk/ (live-money trade gate)."
    echo "The .claude/rules/risk-path.md + heavy-lane.md rules apply: full §1 pipeline (brainstorm → expert-harden → spec → plan → split-review → CI conclusion + order-flip) is mandatory. No fast/default lane on this path."
    echo "Skills to invoke: /engine-readiness (if scoped to an engine), and reach for the spec-reviewer + code-quality-reviewer subagent profiles for the split review."
    ;;
esac
exit 0
