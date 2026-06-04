---
name: feedback-other-session-db-views-only
description: "Concurrent engines/other session has DB-write scope limited to VIEWS only — never tables, migrations, indexes, constraints, or row-level data writes outside its own engine schema. This (db/system-ops) session owns the table substrate."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

When two sessions are running and the other one is on engines (or any
non-database-substrate track), the other session's DB-mutation scope
is strictly limited to:

  - Creating or modifying **views** (`CREATE VIEW`, `CREATE OR REPLACE
    VIEW`, `DROP VIEW`).
  - Row-level writes to tables it itself owns (engine-specific
    persistence the engine code already manages — e.g. the engine's
    own aar_events rows for that engine, its own risk_state row).

The other session must NOT:

  - Modify table schema (`ALTER TABLE`, `CREATE TABLE`, `DROP TABLE`).
  - Add/drop indexes, constraints, triggers, or FKs.
  - Create or run new Alembic migrations.
  - Bulk-write across tables it doesn't own.

**Why:** Operator 2026-05-24 — "the other session may modify the views
that you left behind... that is the limit to what it should do to the
database". This session (database / system-ops) owns the table
substrate; the other (engines) owns runtime behavior. Views are the
read-side surface engines may shape for their own consumption.

**How to apply:** If a divergence appears — the engines session adds
a migration, or touches `tpcore/quality/validation/checks/*`, or runs
`alembic upgrade` — that's a scope violation; surface it. This
session's structural changes (the v2.2 RI epic, ticker-reuse arch,
ingestion_jobs/tax_lots drops) are the SoT for the table layer.

Related: [[feedback_multi_session_github_flow_resumed]] (workflow),
[[feedback_never_touch_shared_main_checkout]] (worktree hygiene),
[[feedback_ops_table_changes_require_system_rewiring]] (the broader
rule that ops-table changes carry wide blast radius).
