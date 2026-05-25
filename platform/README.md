# platform

Cross-engine Postgres schema and Alembic migrations.

> **Note:** this directory is intentionally **not** a Python package
> (no `__init__.py`). The name `platform` collides with the stdlib
> `platform` module, which pandas/`exchange_calendars` import transitively.
> Migration scripts are discovered by Alembic via file path; they are
> never imported as `from platform.migrations.*`.

## Schema

All tables live in the `platform` schema; all timestamps are `TIMESTAMPTZ`.

| Table | Purpose |
| --- | --- |
| `platform.aar_events` | After-Action Reports per closed trade (`(engine, trade_id)` unique). |
| `platform.data_quality_log` | Per-source latency / staleness / confidence. |
| `platform.parity_drift_log` | Live/paper parity-harness drift records. |
| `platform.risk_state` | Per-engine PnL counters, position count, kill switch flag, reset clocks. |
| `platform.allocations` | Stub for the future Allocator service. |
| `platform.forensics_triggers` | Stub for the future Forensics service. |
| `platform.prices_daily` | Daily OHLCV (active + delisted), populated by `tpcore.data.ingest_alpaca_bars`. **Latest-only** — no point-in-time revision history (see PIT note below). |
| `platform.ingest_manifest` | One row per ingest batch: source, provider, pulled_at, source_locator, expected/actual row counts, status, checksum. Enables source-vs-DB reconciliation. |
| `platform.ingest_quarantine` | Failed-ingest records retained for inspection: source, payload JSONB, error, retry status. |

## Point-in-time (PIT) boundaries

`platform.prices_daily` is **latest-only**: each `(ticker, date)` pair stores the most-recent value seen from the provider. If FMP or Alpaca revises a historical bar after ingest, the new value overwrites the old; **no revision history is kept**. This is a deliberate choice (2026-05-25 decision after the database acceptance audit):

- Adding revision history to a 21M+ row table means a bitemporal column pair + their indexes ≈ doubling storage.
- Engines today query the "latest known" semantic; no consumer needs "as-of-date X" PIT queries.
- Backtests against this table assume the latest snapshot; if a backtest's correctness depends on point-in-time bar revisions, the backtest MUST be re-flagged as non-PIT-safe OR a separate PIT-safe table is required.

**Trust boundary:** `prices_daily` is trusted for live trading, latest-snapshot analysis, and backtests that explicitly accept latest-revision values. It is **not trusted** for "what did we believe on date X" queries.

Tables that DO carry PIT/bitemporal history:
- `platform.macro_data` — `realtime_start` / `realtime_end` pair; preserves FRED/AAII/CNN F&G revisions.
- `platform.corporate_events` — bitemporal PK `(event_id, realtime_start)`.
- `platform.issuer_history`, `platform.issuer_securities`, `platform.ticker_history` — SCD-2 `valid_from` / `valid_to`.

## Running migrations

```bash
export DATABASE_URL=postgresql+asyncpg://ste:ste@localhost:5432/ste
alembic -c platform/migrations/alembic.ini upgrade head
```

`env.py` reads `DATABASE_URL` from the environment and configures the
SQLAlchemy async engine. The Alembic version table itself lives in the
`platform` schema (`version_table_schema=platform`).

Migration filenames follow `YYYYMMDD_HHMM_<slug>.py`.
