#!/usr/bin/env bash
# UserPromptSubmit — advisory pre-prompt reminder for the System-Wide
# Verification gate. Per docs/audits/2026-06-03-claude-code-workflow-controls.md
# §13 #3: "Add it (single advisory line on fix/patch/repair/backfill verbs
# against SWV-scoped diff) or skip it (rule + skill only)?"
#
# When this hook fires:
#   1. The user's prompt mentions a fix / patch / repair / backfill /
#      cleanup verb.
#   2. AND the current working diff (vs HEAD) touches any path the
#      `discovery-first` rule covers (validators / ingestion / auditheal
#      / selfheal / migrations / scripts/ops.py).
#
# What this hook does:
#   Prepends a single advisory line to Claude's context for that turn:
#       "SWV gate applies — invoke `/system-wide-verification` before any fix."
#
# What this hook does NOT do:
#   - Never blocks. Exit 0 always. Discovery-first must remain the agent's
#     discipline (and the path-scoped rule's enforcement), not a fragile
#     hook. The hook is a nudge, not a gate.
#   - Never reads file contents. Only the prompt text + `git diff --name-only`.
#   - Never modifies state.
#
# Kill switch: STE_SWV_ADVISORY_DISABLE=1
#
# Authoritative external:
#   - https://code.claude.com/docs/en/hooks (UserPromptSubmit stdout
#     goes to Claude as context — exit 0 is sufficient, no JSON wrap
#     needed)
#   - https://code.claude.com/docs/en/memory (path-scoped rules)
#
# Sentinel: tests/test_claude_hooks_present.py + the SWV/CIC integration
# in tests/test_claude_surface_contract.py.
set -e

# Kill switch.
if [ "${STE_SWV_ADVISORY_DISABLE:-}" = "1" ]; then
  exit 0
fi

# Read the JSON input.
input="$(cat)"

# Extract the prompt text. Use jq if available; fall back to grep heuristic.
if command -v jq >/dev/null 2>&1; then
  prompt="$(echo "$input" | jq -r '.prompt // empty' 2>/dev/null)"
else
  # No jq — degrade to no-op. Hook is advisory only, never blocks.
  exit 0
fi

# Empty prompt — nothing to scan.
if [ -z "$prompt" ]; then
  exit 0
fi

# Verb check (case-insensitive). The five verbs are the audit's §13 #3
# canonical list.
prompt_lower="$(echo "$prompt" | tr '[:upper:]' '[:lower:]')"
verb_hit=0
for verb in fix patch repair backfill cleanup; do
  if echo "$prompt_lower" | grep -qE "(^|[^a-z])${verb}([^a-z]|$)"; then
    verb_hit=1
    break
  fi
done

if [ "$verb_hit" -eq 0 ]; then
  exit 0
fi

# Path check — does the working diff touch any SWV-scoped path?
# The path list mirrors .claude/rules/discovery-first.md frontmatter
# (the audit's §13 #1 recommended scope). Listed verbatim here because
# this hook runs before any rule files are loaded into context.
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
# Capture BOTH modified-tracked files (git diff HEAD) AND untracked-new
# files (ls-files --others) — a new validator file is a planned change
# the SWV gate should see, not just edits to existing files.
modified="$(git diff --name-only HEAD 2>/dev/null)"
untracked="$(git ls-files --others --exclude-standard 2>/dev/null)"
changed_files="$(printf "%s\n%s\n" "$modified" "$untracked" | grep -v '^$' | sort -u)"
if [ -z "$changed_files" ]; then
  exit 0
fi

path_hit=0
while IFS= read -r f; do
  case "$f" in
    tpcore/quality/validation/*|\
    tpcore/ingestion/*|\
    tpcore/auditheal/*|\
    tpcore/selfheal/*|\
    platform/migrations/*|\
    scripts/ops.py)
      path_hit=1
      break
      ;;
  esac
done <<EOF
$changed_files
EOF

if [ "$path_hit" -eq 0 ]; then
  exit 0
fi

# Both conditions met — emit the advisory. UserPromptSubmit stdout is
# visible to Claude as context for the turn (per
# https://code.claude.com/docs/en/hooks "Exit Code 0: Success" table).
cat <<'EOF'
SWV gate applies — your prompt mentions a fix/patch/repair/backfill/cleanup verb AND the working diff touches a discovery-first-scoped path. Before proposing the fix, invoke `/system-wide-verification` (writers → readers → source authority → existing controls → tests → workflows → config → adjacent callers → blast radius → rollback) AND `/change-impact-classification` (change type → boundary → 12 mandatory questions → type-design pass if applicable). Reference: .claude/rules/discovery-first.md + docs/audits/2026-06-03-claude-code-workflow-controls.md §8 + §9. Kill switch: STE_SWV_ADVISORY_DISABLE=1.
EOF

exit 0
