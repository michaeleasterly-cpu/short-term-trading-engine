# CLAUDE.md — Short-Term Trading Engine

## Project Identity
Multi-engine automated trading platform. US equities, daily timeframe, fully automated execution via Alpaca API. Personal-use only.

## Architecture
- tpcore/ — shared library (risk, AAR, quality, parity, backtest, fundamentals, valuation, tax)
- platform/ — Postgres schema, Alembic migrations
- sigma/ — range scalping (daily Bollinger Bands); built, credibility 55/100 (BLOCKED)
- reversion/ — mean reversion + earnings-quality gate; built, credibility 45/100 (BLOCKED)
- vector/ — catalyst-driven swing (P/B + D/E + catalyst + technical); built, credibility 45/100 (BLOCKED); data-blocked on T1+T2 universe
- momentum/ — cross-sectional 12-1 monthly rebalance; built and paper-trading; credibility 40/100 (gated structurally — see momentum spec)
- Future engines: s2/, catalyst/, sentinel/

## Conventions
- All timestamps UTC. Market hours via `tpcore.calendar` (which wraps `exchange_calendars` XNYS).
- No yfinance. No Discord. No manual execution.
- All orders via Alpaca API. Paper-then-live.
- Engines built in order so far: Sigma → Reversion → Vector → Momentum.
- Backtest with self-built survivorship-free database before any live trading. (Note: `prices_daily` is currently only partially survivorship-clean — known caveat in `momentum/backtest.py` docstring.)
- Every engine has 5 Plugs: setup_detection, lifecycle_analysis, execution_risk, aar_logging, capital_gate.
- Every engine's `setup_detection` plug populates a `tpcore.backtest.filter_diagnostics.FilterDiagnostics` instance so SIGNAL events carry per-gate pass/block counters.

## Session Rules
- Read docs/STYLE_GUIDE.md before writing any code.
- Read docs/glossary.md if present before coding.
- Never modify tpcore without checking all engines that consume it.
- Every trade path goes through tpcore.risk.RiskGovernor.check_trade().
- Sigma/Reversion/Vector use Alpaca bracket orders (take-profit + stop-loss submitted together). Momentum uses day-market orders only — no per-name stops between monthly rebalances; risk is managed by diversification + rotation.
- All code type-hinted. Pydantic v2 for data models. structlog for logging.
