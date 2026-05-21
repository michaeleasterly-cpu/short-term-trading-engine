---
name: memory-cleanup-command
description: "Canonical operator trigger phrases for the memory-maintenance procedure (procedure itself lives in docs/MEMORY_MAINTENANCE.md)"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e4b282f8-c3bf-497d-9609-6eed7b7ec5cf
---

**Trigger:** when the operator says *"clean up your memories"* /
*"cleanup your memories"* (or close paraphrase), run the canonical
procedure in `docs/MEMORY_MAINTENANCE.md` (the in-repo Source of
Truth, merged PR #144). The procedure carries the full structural
checks, the step-3a repo-shadow deletion criterion, the consolidation
rule, and the MEMORY.md mechanical invariant.

**Why this memory exists at all:** the procedure doc has no way to
declare its own trigger phrase — that mapping (operator phrase →
canonical procedure) is the non-obvious operator-context bit and is
durable across sessions, which is exactly what memory is for. The
procedure itself lives in the repo and wins on any conflict.

**How to apply:** on the trigger phrase, execute
`docs/MEMORY_MAINTENANCE.md` step-by-step; do NOT improvise; report
the before/after counts table the procedure mandates.
Bound by [[no-shortcuts-100-pct]] — verify every classification
against current code/docs, no hand-waving.
