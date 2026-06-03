---
name: migrations
paths:
  - "platform/migrations/**"
description: "Path-scoped rule: Alembic discipline; IPv4 pooler vs IPv6 dual-URL warning; prices_daily completeness/freshness; idempotency."
---

# Platform migrations (Alembic)

Canonical SoT: `platform/migrations/` (Alembic versions). Heavy-lane rule applies (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Discipline:

- **Alembic is the schema substrate.** Every schema change is a versioned migration; rollback discipline preserved. Never edit live tables outside a migration.
- **Idempotent migrations** where possible: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`. The replay-from-zero invariant must hold.
- **Supabase has TWO DATABASE_URLs**: local IPv4 pooler vs Railway IPv6 (`feedback_supabase_dual_db_urls` / `project_supabase_dual_db_urls`). `.env`/local uses the IPv4 pooler URL (`$DATABASE_URL_IPV4`); never copy one into the other. Asyncpg pooler-safety: `statement_cache_size=0` is required for Railway.
- **`prices_daily` is the critical table**: `prices_daily_completeness` is the ungameable zero-tolerance invariant (every genuinely-liquid currently-trading common stock has a bar for every NYSE session in the 30-session window within its active range — ANY missing `(ticker, session)` fails). `prices_daily_freshness` is hard-gated by `CRITICAL_TICKERS` in `tpcore/quality/validation/checks/prices_daily_freshness.py`.
- **`data_quality_log`** is the durable detector substrate. Persistence is per-check, per-phase, crash-safe (the audit_data_pipeline writes each check as it completes).
- **Pre-Railway archive-substrate migration** ([[project_railway_archive_substrate_migration]]): shrinkage/CSV-archive is local-FS-hardwired → dies on Railway ephemeral FS; decided 2026-05-18: detection→durable Postgres (D2), recovery→attached object-storage bucket (R3); built AT migration; preserve the TP_DATA_DIR seam + the empty-archive WARN-not-silent-OK.

Concurrency hazard: a long `daily_bars` backfill from a separate process contends on the Supabase pooler (`connection was closed`). The data-ops mkdir-atomic self-exclusion lock prevents the scheduled-cycle overlap; ad-hoc concurrent `ops.py --stage daily_bars` is NOT guarded.

Every migration goes through the full §1 pipeline (heavy lane).

## No new platform table without schema rationale (2026-06-04, controls-audit §13 #11)

A migration that adds a new `platform.*` table must carry an **operator-approved schema rationale** in its docstring + the PR body that names:

1. **The readers** — every code path that will query the new table (with `file:line` references where they exist; "future readers" is not an answer).
2. **The writers** — the canonical writer of the table. Single-writer is the default; multi-writer requires explicit justification.
3. **The existing-table alternative** — what existing `platform.*` table was considered as the home for this data, and why was it rejected? "I didn't check" is a `DISCOVERY_REQUIRED` answer (see `.claude/rules/discovery-first.md` CIC gate question #9 + #10).

The 2026-06-02 identity-substrate audit named the **sidecar / evidence / quarantine** class as the specific failure mode: tables created to track "data we're not sure about" that duplicated logic the existing 15 `BEFORE INSERT` triggers + `ticker_history` SCD-2 substrate already handled. **No new sidecar / evidence / quarantine table without consolidation review** — the consolidation question (does the existing identity substrate already cover this?) must be answered explicitly, not implicitly assumed.

Required rationale-section template (paste into the migration docstring + the PR body):

```text
## Schema rationale (controls-audit §13 #11)

Readers (named code paths that will query the new table):
  - <file:line>

Writers (canonical writer; single-writer unless justified):
  - <file:line>

Existing-table alternative considered:
  - <table name>: <why this new table is needed despite the existing one>

Why not extend the existing identity / lifecycle substrate?
  - <one-line evidence — cite ticker_history / ticker_classifications /
    classification_id triggers / SCD-2 mechanics where relevant>
```

For `database_schema_change` classifications, the CIC gate's `OPERATOR_DECISION_REQUIRED` verdict auto-fires (see `.claude/rules/discovery-first.md` §9). This rationale is the input the operator needs to authorize.
