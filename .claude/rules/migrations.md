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
