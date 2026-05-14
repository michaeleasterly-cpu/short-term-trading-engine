# CLAUDE.md — Short-Term Trading Engine

## Project Identity
Multi-engine automated trading platform. US equities, daily timeframe, fully automated execution via Alpaca API. Personal-use only.

## Architecture
- tpcore/ — shared library (risk, AAR, quality, parity, backtest, fundamentals, valuation, tax, universe)
- platform/ — Postgres schema, Alembic migrations
- dashboard.py + dashboard_components/ — Streamlit operator console (`scripts/run_dashboard.sh`)
- sigma/ — range scalping (daily Bollinger Bands); built, last search top OOS +1.150 (FAILED DSR gate)
- reversion/ — mean reversion + earnings-quality gate; built, last search top OOS +1.174 (FAILED DSR gate)
- vector/ — catalyst-driven swing (P/B + D/E + catalyst + technical); built, last search top OOS +1.257 (FAILED DSR gate; was data-blocked, now unblocked after 2026-05-13 catalyst_events backfill — 1,350 rows / 137 tickers, audited 2026-05-14; recurring weekly refresh active via `ops.py catalyst_refresh` stage)
- momentum/ — cross-sectional 12-1 monthly rebalance; built and paper-trading; last search top OOS +0.784 (FAILED DSR gate; gated structurally per momentum spec)
- tpcore/allocator/ — weekly inverse-vol capital rebalance across engines (deployed 2026-05-13, daemon Mon 13:00 UTC)
- tpcore/forensics/ — daily AAR scanner that emits triggers + auto-generates Sprint Dossiers (deployed 2026-05-14, runs as final step of post-close)
- Shared AAR read-side: `tpcore.aar.AARReader` (used by both allocator + forensics); shared exit-reason classifier: `tpcore.aar.classify_exit_reason`.
- Future engines: s2/, catalyst/, sentinel/

**Engine credibility status as of 2026-05-13 (post data-cleanup):** All four engines produce positive OOS edge candidates (scores 0.78–1.26), all four still fail the DSR ≥ 0.95 / credibility ≥ 60 gate. Data foundation is clean; signal strength is the binding constraint.

## Conventions
- All timestamps UTC. Market hours via `tpcore.calendar` (which wraps `exchange_calendars` XNYS).
- No yfinance. No Discord. No manual execution.
- All orders via Alpaca API. Paper-then-live. Default Alpaca data feed is **SIP** (not IEX — IEX silently misses tickers that trade off-IEX).
- Engines built in order so far: Sigma → Reversion → Vector → Momentum.
- Backtest with self-built survivorship-free database before any live trading. (Note: `prices_daily` is currently only partially survivorship-clean — known caveat in `momentum/backtest.py` docstring.)
- Every engine has 5 Plugs: setup_detection, lifecycle_analysis, execution_risk, aar_logging, capital_gate.
- Every engine's `setup_detection` plug populates a `tpcore.backtest.filter_diagnostics.FilterDiagnostics` instance so SIGNAL events carry per-gate pass/block counters.
- **Data-layer acceptance gate (2026-05-13, expanded 2026-05-14):** validation suite (8 checks: delistings, constituent, splits, row_integrity, fundamentals_integrity, corporate_actions_integrity, catalyst_freshness, sec_filings_freshness) must return `passed=True` with `confidence=1.000`. Cross-table audit (`scripts/run_audit_all_tables.sh`) must return 0 violations across every dependent table.
- **External-API discipline (2026-05-14):** every new data adapter starts from `tpcore/templates/adapter_template.py`, satisfies the 5-stage pipeline contract in `docs/superpowers/pipelines/data_adapter_pipeline.md` (ingest / test / validate / dashboard / schedule), and passes `docs/superpowers/checklists/adapter_readiness.md` before merging. HTTP retries go through `tpcore.outage.with_retry` — no local `tenacity`, no `asyncio.sleep` loops. Ingest uses the CSV-first sub-protocol (download → validate-at-CSV → load → compress) for any non-trivial pull.
- **Operator workflow:** daily post-close via `scripts/run_post_close.sh` (single button: 10-stage update → audit → validation → compress → engine sweep → **forensics scan**). Full historical refresh via `scripts/run_full_backfill.sh`. Daemons installed via `scripts/install_all_daemons.sh` (trade_monitor + post_close + allocator).

## Session Rules
- Read docs/STYLE_GUIDE.md before writing any code.
- Read docs/glossary.md if present before coding.
- Never modify tpcore without checking all engines that consume it.
- Every trade path goes through tpcore.risk.RiskGovernor.check_trade().
- Sigma/Reversion/Vector use Alpaca bracket orders (take-profit + stop-loss submitted together). Momentum uses day-market orders only — no per-name stops between monthly rebalances; risk is managed by diversification + rotation.
- All code type-hinted. Pydantic v2 for data models. structlog for logging.
