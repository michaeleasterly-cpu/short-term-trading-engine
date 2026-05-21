---
name: no-shortcuts-100-pct
description: "Never cut corners; every operation must produce a 100%-verified outcome. No chained pipes that mask failures, no truncated output, no skipping phases, no asking permission for obvious next steps."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

Never cut corners. Every operation must produce a 100%-verified outcome.

**Why:** Multiple incidents on 2026-05-13 wasted the user's tokens and eroded trust:
1. Chained `backfill && load && verify` with pipes (`| tail -5`) that masked exit codes — `tail` always succeeds so `&&` evaluated against the wrong status.
2. Asked "should I run it?" after the user had already approved the work upfront ("yes build the backfill script, i dont know why you are asking").
3. Built dashboard panels on a 7-day rolling validation aggregate that lied (surfaced stale failures as current state, AAPL split ratio showed red even though prices_daily was clean).
4. Initial `row_integrity` predicate missed `high<close`/`low>close`/`high<open`/`low>open` cases; full audit only happened after the user pushed for a comprehensive sweep.
5. Bulk-batched the corrupt 94,979-row Tradier cleanup into one DELETE without first verifying source provenance (`source=tradier` distribution) — the user had to ask.

**How to apply:** For any non-trivial operation:
- **Decompose into explicit phases** with exit-code checks between each. No `&&` chains spanning long-running steps.
- **Preserve full output** when reporting — no `tail -5` / `head -10` truncation. Save logs to disk and read them whole.
- **Verify against the source of truth**, not derived/aggregated views (latest-run, not 7-day rollup; live SQL, not cached numbers).
- **Idempotent + resumable** — every script that touches the DB should leave a clean state on retry, and large operations should checkpoint to disk (CSV-first for big pulls).
- **Audit-log every mutation** — DELETEs and UPDATEs go through `platform.application_log` with the row payload.
- **When the user has stated a goal once, execute it without asking again.** Asking permission after authorization is itself a shortcut.

The standard: would a careful programmer running this manually, expecting 100% physical truth and no surprises, be satisfied with the outcome?

Cross-references: [[research-builder-persona]] (the structured codification of this discipline), [[cut-process-overhead-ship]] (verify ≠ ceremony — verify the outcome, don't pile review passes).
