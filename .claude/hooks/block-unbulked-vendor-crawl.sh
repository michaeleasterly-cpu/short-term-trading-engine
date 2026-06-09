#!/usr/bin/env bash
# PreToolUse(Bash) ENFORCEMENT — bulk-before-crawl, un-bypassable.
#
# Anthropic guidance (code.claude.com/docs/en/memory + features-overview):
# CLAUDE.md / memory are CONTEXT, not enforcement — the model can drift past
# them. A rule that must hold every time belongs in a PreToolUse hook. This
# repo's bulk-before-crawl rule (download to CSV first, then ETL; never a
# per-entity vendor API crawl) was violated three times despite living in
# CLAUDE.md + memory. This hook makes the easy-wrong path un-takeable.
#
# It DENIES a shell-level per-entity vendor API crawl: a loop (for/while/xargs,
# or a python -c "...for...in...") that hits a known vendor API host, with NO
# bulk/rate-limit guard. A single call, a bulk/batch call, or a rate-guarded
# loop is allowed. Stage-internal crawls (e.g. ops.py --stage X) are not
# visible at the shell layer — those are fixed in the stage code + the vendor
# rate-limit profile, not here.
#
# Kill switch (documented, for a deliberate one-off): STE_ALLOW_VENDOR_CRAWL=1.
#
# Exit 0 + JSON permissionDecision=deny → Claude Code blocks the call.
set -uo pipefail

INPUT="$(cat)"
CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# Deliberate, operator-sanctioned override.
[ "${STE_ALLOW_VENDOR_CRAWL:-}" = "1" ] && exit 0

shopt -s nocasematch

# A loop / per-entity iteration construct.
loop_re='(for[[:space:]]+[a-z_]+[[:space:]]+in|while[[:space:]]+read|\|[[:space:]]*xargs|\.map\(|for[[:space:]]+[a-z_]+[[:space:]]+in[[:space:]].*:)'
# A known vendor data API (the per-entity crawl surface).
vendor_re='(financialmodelingprep\.com|/historical-price-eod/|/api/v3/historical|data\.alpaca\.markets|/v2/stocks/.*/bars|iborrowdesk\.com|/stable/historical-price)'
# A guard that makes a fetch acceptable: bulk/batch, rate limiting, or a CSV ETL.
guard_re='(sleep|rate.?limit|ratelimit|backoff|Limiter|--from|batch|/batch-eod|bulk|\.csv|make_limiter|asyncio\.sleep|semaphore)'

if [[ "$CMD" =~ $loop_re ]] && [[ "$CMD" =~ $vendor_re ]] && ! [[ "$CMD" =~ $guard_re ]]; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "BLOCKED: per-entity vendor API crawl. The data pipeline is download-to-CSV-first then ETL (bulk file before API crawl) — e.g. FMP batch/bulk EOD (one call per DATE returns all symbols) or the survivorship-free snapshot, NOT a per-ticker loop. If a per-entity fetch is genuinely required, it MUST be rate-limited (sleep/Limiter/backoff) under the vendor profile cap so it cannot lock us out. See CLAUDE.md universal invariants + docs/DATABASE_AND_DATAFLOW.md. Deliberate one-off override: prefix STE_ALLOW_VENDOR_CRAWL=1."
    }
  }'
  exit 0
fi

exit 0
