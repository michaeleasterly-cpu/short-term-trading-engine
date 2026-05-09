# CLAUDE.md — Short-Term Trading Engine

## Project Identity
Multi-engine automated trading platform. US equities, daily timeframe, fully automated execution via Alpaca API. Personal-use only.

## Architecture
- tpcore/ — shared library (risk, AAR, quality, parity, backtest, fundamentals, valuation, tax)
- platform/ — Postgres schema, Alembic migrations
- sigma/ — first engine (range scalping, daily Bollinger Bands)
- Future engines: reversion/, vector/, s2/, catalyst/, sentinel/

## Conventions
- All timestamps UTC. Market hours via exchange_calendars (NYSE).
- No yfinance. No Discord. No manual execution.
- All orders via Alpaca API. Paper-then-live.
- One engine built at a time. Sigma first.
- Backtest with self-built survivorship-free database before any live trading.
- Every engine has 5 Plugs: setup_detection, lifecycle_analysis, execution_risk, aar_logging, capital_gate.

## Session Rules
- Read docs/STYLE_GUIDE.md before writing any code.
- Read docs/glossary.md if present before coding.
- Never modify tpcore without checking all engines that consume it.
- Every trade path goes through tpcore.risk.RiskGovernor.check_trade().
- Every order uses Alpaca bracket orders (take-profit + stop-loss submitted together).
- All code type-hinted. Pydantic v2 for data models. structlog for logging.
