# short-term-trading-engine (STE)

Multi-engine quantitative trading platform built around three layers:

- **`tpcore/`** — shared core: calendar, broker/data interfaces, risk governor, AAR, quality logging,
  parity harness, backtest harness, fundamentals/valuation toolkits, tax tracking, outage policy.
- **`platform/`** — Postgres schema and Alembic migrations for cross-engine state (AAR events,
  quality logs, parity drift, risk state, allocations, tax lots).
- **engines** — five PAPER engines: `reversion/`, `vector/`, `momentum/`, `sentinel/`, `canary/` (heartbeat); plus `lab/` (LAB on-demand search). `sigma/` — Range Scalping, daily timeframe. ARCHIVED 2026-05-16 after two honest gate attempts failed DSR; see `archive/sigma/EULOGY.md` for the post-mortem.

## Ground rules

- Python 3.11+. All timestamps are **UTC**. Market hours via `exchange_calendars` (`XNYS`).
- Free data stack only: **Alpaca** (IEX free tier) for prices, **SEC EDGAR** for fundamentals,
  **FMP** (free → paid) for historical fundamentals, **ApeWisdom** for social, **FRED** for macro,
  **FINRA/NASDAQ** for short interest, **IBorrowDesk** for borrow rates.
- **No `yfinance`. No Tradier in production. No Discord. No manual execution.** All orders go
  through the Alpaca broker interface.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then fill values

# initial DB migration (after configuring DATABASE_URL)
alembic -c platform/migrations/alembic.ini upgrade head

# verify no forbidden imports leak into engine code
python -m tpcore.scripts.check_imports sigma
```

## Layout

See [`tpcore/README.md`](tpcore/README.md) and [`platform/README.md`](platform/README.md).
