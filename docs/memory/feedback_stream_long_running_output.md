---
name: stream-long-running-output
description: "For long-running scripts/audits the operator runs repeatedly, stream progress as it goes — don't batch all output + persistence to the end."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

When building or revising a script the operator runs repeatedly and that
does many sequential steps (multi-table audits, sweeps, backfills),
**stream output + persist incrementally as each step completes** — do
not collect everything in memory and print/write once at the end.

**Why:** The operator interrupted the first `audit_pipeline.py` run with
"make it so it updates as it goes." A batch-at-end design means: (1) a
long run shows nothing until done, so you can't tell if it's hung vs.
working, and (2) a mid-run crash saves nothing. They also caught me
spinning on a 20M-row dump with no progress output ("you got stuck").

**How to apply:**
- Print each result the moment its step completes (flush stdout).
- Persist incrementally — per-item or per-phase — so a crash keeps
  completed work. `ON CONFLICT DO NOTHING` makes per-item writes safe.
- A `list` subclass whose `.append` fires a hook is a zero-churn way to
  retrofit streaming onto code that already builds a results list (used
  in `scripts/audit_pipeline.py::_FindingSink`).
- Related: the audit's check set must track current reality — when the
  platform gains a guardrail or closes a gap, add/retire the matching
  check in the same change (codified in CLAUDE.md's canonical-audit
  rule). See [[research-builder-persona]] (CI IS SHIP GATE / PROOF OF
  DONE) for the analogous "verify, don't assume" posture.
