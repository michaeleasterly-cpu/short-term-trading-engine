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
| `platform.execution_quality_log` | Per-fill slippage / partial / paper-vs-live. |
| `platform.data_quality_log` | Per-source latency / staleness / confidence. |
| `platform.parity_drift_log` | Live/paper parity-harness drift records. |
| `platform.risk_state` | Per-engine PnL counters, position count, kill switch flag, reset clocks. |
| `platform.allocations` | Stub for the future Commander allocator. |
| `platform.coroner_triggers` | Stub for the future Forensics service. |
| `platform.tax_lots` | FIFO-tracked tax lots across all engines. |
| `platform.prices_daily` | Daily OHLCV (active + delisted), populated by `tpcore.data.ingest_alpaca_bars`. |

## Running migrations

```bash
export DATABASE_URL=postgresql+asyncpg://ste:ste@localhost:5432/ste
alembic -c platform/migrations/alembic.ini upgrade head
```

`env.py` reads `DATABASE_URL` from the environment and configures the
SQLAlchemy async engine. The Alembic version table itself lives in the
`platform` schema (`version_table_schema=platform`).

Migration filenames follow `YYYYMMDD_HHMM_<slug>.py`.
