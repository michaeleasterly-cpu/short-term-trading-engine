#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit) — block direct edits to the engine /
# data-feed roster SoT files; force the ECR / DFCR planner path.
# Override: CLAUDE_ECR_RUN=1 (engine roster) or CLAUDE_DFCR_RUN=1 (data feed).
# Override: CLAUDE_ASSET_CLASS_REFINEMENT=1 — operator-approved 2026-05-30
#   for the OpenFIGI-driven 4→10 asset_class taxonomy build (covers
#   engine_profile.py's ``allowed_asset_classes`` field addition + per-
#   engine defaults). See docs/superpowers/specs/2026-05-30-asset-class-
#   refinement.md. Operator directive: "drive the whole thing end-to-end
#   in one push... you have to update the hooks with the new setup."
# Authoritative external: https://code.claude.com/docs/en/hooks-guide
# Project SoT: .claude/rules/engine-roster.md, .claude/rules/data-feed-roster.md.
set -euo pipefail

input="$(cat)"
fp="$(echo "$input" | jq -r '.tool_input.file_path // empty')"

# Normalize: match both absolute and relative paths to the protected files.
case "$fp" in
  */tpcore/engine_profile.py|tpcore/engine_profile.py)
    if [ "${CLAUDE_ECR_RUN:-}" != "1" ] && [ "${CLAUDE_ASSET_CLASS_REFINEMENT:-}" != "1" ]; then
      echo "BLOCK: direct edits to tpcore/engine_profile.py are forbidden — the engine roster SoT (the 22-site-drift lesson, PR #170)." >&2
      echo "Use the ECR:" >&2
      echo "  1. Fill docs/superpowers/checklists/engine_change_request.md into a file (e.g. ecr_<engine>.txt)." >&2
      echo "  2. python -m ops.engine_sdlc --ecr <file>" >&2
      echo "  3. Operator approves \`APPROVE? (y/n)\` on the planner-validated diff." >&2
      echo "See /ecr skill + .claude/rules/engine-roster.md." >&2
      echo "If this IS an ECR planner-driven edit: set CLAUDE_ECR_RUN=1 in the env." >&2
      exit 2
    fi
    ;;
  */tpcore/providers.py|tpcore/providers.py)
    if [ "${CLAUDE_DFCR_RUN:-}" != "1" ]; then
      echo "BLOCK: direct edits to tpcore/providers.py are forbidden — the data-feed ProviderBinding roster SoT." >&2
      echo "Use the DFCR:" >&2
      echo "  1. Fill docs/superpowers/checklists/data_feed_change_request.md." >&2
      echo "  2. Submit via the data-lane planner (see the checklist for the canonical submit command)." >&2
      echo "  3. Operator approves \`APPROVE? (y/n)\` for ADD (ONBOARD) / REMOVE (RETIRE)." >&2
      echo "  CUTOVER / EVALUATE / self-heal are automated, no approval." >&2
      echo "See /dfcr skill + .claude/rules/data-feed-roster.md." >&2
      echo "If this IS a DFCR planner-driven edit: set CLAUDE_DFCR_RUN=1 in the env." >&2
      exit 2
    fi
    ;;
esac
exit 0
