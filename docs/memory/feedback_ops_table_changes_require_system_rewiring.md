---
name: feedback-ops-table-changes-require-system-rewiring
description: "Operations-table refactors (application_log, ingestion_jobs, data_quality_log, daemon_heartbeats, allocations, risk_state, etc.) require FULL system rewiring — audit scripts, healers, validators, daemons, disposition ladders, docstrings — not just schema migrations."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

When changing a `platform.*` "system operations" table (application_log,
ingestion_jobs, data_quality_log, daemon_heartbeats, allocations,
risk_state, parity_drift_log, forensics_triggers, etc.), the schema
migration is the SMALLEST part. The cross-cutting consumers must all be
rewired at the same time:

  - audit scripts (`scripts/audit_all_tables.py`,
    `scripts/audit_data_pipeline.py`, `tpcore/audit/cross_table.py`)
  - validation checks (`tpcore/quality/validation/checks/*`)
  - self-heal HealSpec registry (`tpcore/selfheal/registry.py`)
  - disposition ladder (`tpcore/ladder/disposition.py`)
  - dispatcher / daemon classes (`tpcore/ingestion/engine.py` etc.)
  - daemon wrappers (`ops/*.py`)
  - operator-facing docs / docstrings
  - any monkeypatch-based tests that mock the consumer surface

**Why:** Operator 2026-05-24 — "for any operations table changes you'll
have to rewire the system for it". The ingestion_jobs drop touched
10+ files across 4 layers. Treating a system-table refactor as "just a
migration" leaves drift behind that the audit-data-pipeline / weekly
digest / disposition ladder still surfaces.

**How to apply:** Before writing the migration, grep the table name
across `scripts/`, `tpcore/`, `ops/`, `tests/`, and `.claude/rules/`.
Build the full consumer list FIRST. The migration is the LAST file
touched, not the first. The PR description must enumerate every
consumer rewired so a reviewer can verify no orphan reference remains.

Related: [[project_three_service_architecture]] (data/engine/aar +
platform overlays), [[feedback_event_driven_not_scheduled]] (new
components are sibling daemons on the application_log bus, not new
scheduler rows).
