---
name: complete-concern-map-first
description: "Operator 2026-05-23: 'you dont think about the entire picture and then we get half through and now i'm asking questions where there are no answers'. Before drafting any infrastructure spec/plan, FIRST enumerate every concern, THEN design phases that cover them. Stop scoping tight + discovering load-bearing surrounding concerns mid-execution."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule (operator 2026-05-23):** Before drafting any infrastructure spec/plan that touches production data or schema, FIRST enumerate every concern, THEN design phases that cover them.

**Why (failure-derived 2026-05-23):** v2 referential-integrity spec/plan scoped tight to "add FKs as NOT VALID". Phase 0/1/2/3 shipped before these load-bearing concerns were even captured:

- Orphan cleanup protocol (delete vs backfill per table — for prices_daily's 335K orphans the answer is BACKFILL ticker_classifications, not DELETE bars; v2 plan was silent)
- `parent_resolver` for ongoing producer FK-violation handling (now Task #24)
- Archive/snapshot substrate (db_snapshots/ — Task #22, surfaced mid-Phase-3)
- Backup regimen (pg_dump daily to S3 — surfaced mid-Phase-3)
- Test coverage for FK-violation paths in handlers
- DATABASE_AND_DATAFLOW + memory propagation

Each gap was discovered AFTER a phase shipped, not before. Operator's hot-seat-tax to point out what should have been there from the start.

## How to apply (every spec/plan task)

Before writing ANY spec, draft a one-page **concern map** answering:

1. **Schema changes** — DDL, what new constraints, what changes existing
2. **Producer changes** — every handler/script/stage that writes to affected tables
3. **Consumer changes** — every engine/check/dashboard that reads from affected tables
4. **Migration safety** — NOT VALID patterns, statement_timeout, lock budgets, transaction shape
5. **Data quality concerns** — orphan handling, duplicate handling, NULL tolerance
6. **Rollback / snapshot substrate** — how do we recover if this is wrong; do we have a pre-state baseline
7. **Backup / disaster recovery** — does this change the backup story; tenant-loss recovery path
8. **Test coverage** — what tests pin the new invariants; what existing tests need updating
9. **Ongoing operations** — daily/weekly stages that hit affected tables; how do they cope post-change
10. **Documentation** — DATABASE_AND_DATAFLOW, runbooks, memory entries that need updating
11. **Cross-table change ordering** — if multiple tables change, what order minimizes blast radius
12. **Operator manual actions** — Supabase dashboard tweaks, env-var changes, etc.

Each concern gets a sentence: "covered in Phase N" or "deferred to Task #X" or "skipped because Y".

If a concern doesn't have a clear coverage answer, the spec isn't ready to ship.

## Anti-pattern (today's example)

I wrote the v2 spec without doing the concern map. Phases 0-2 shipped. Then operator asked:
- "what about archiving" → Task #22 created mid-flight
- "what about updating the database" → parent_resolver pattern emerged
- "what about a backup regimen" → pg_dump design emerged
- "should have been part of the plan"

Each was a load-bearing concern that should have been in the v2 plan from day one. Now they're afterthoughts being retrofit.

## Related

- [[feedback_ask_expert_then_execute]] — expert FIRST, but also enumerate the questions FIRST
- [[feedback_use_official_docs]] — official docs should inform the concern map
- [[git-workflow-commit-push-ci]] — major-deliverable PRs require a complete concern map for the deliverable
