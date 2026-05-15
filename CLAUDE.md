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
- sentinel/ — macro defense (FRED Bear Score + 5-ETF defensive basket; satellite-style per-cycle graduation); built 2026-05-15. 2018-2025 backtest: 1 activation cycle (COVID Apr 2020), single TLT trade −3.37% — macro signal lags fast crashes. SH/PSQ/GLD missing from `platform.prices_daily`; basket renormalizes to available tickers until backfilled.
- tpcore/allocator/ — weekly inverse-vol capital rebalance across engines (deployed 2026-05-13, daemon Mon 13:00 UTC)
- tpcore/forensics/ — daily AAR scanner that emits triggers + auto-generates Sprint Dossiers (deployed 2026-05-14, runs as final step of data-operations)
- tpcore/indicators/ — shared technical indicators (ADX, Bollinger Bands, CHOP) used by every engine's setup_detection plug (2026-05-14).
- tpcore/order_management/ — `BaseOrderManager` base class for per-trade engines (sigma/reversion/vector) — centralizes `__init__`, `_persist_tier1_to_open_orders`, `_fetch_recent_orders` (2026-05-14).
- tpcore/exceptions.py — `SizingError` shared by sigma + reversion (2026-05-14).
- tpcore/models/graduation.py — `PerTradeGraduationStats` shared by all per-trade engines; Reversion subclasses to add `profit_factor` (2026-05-14).
- tpcore/templates/engine_template/ — copy-paste-start scaffold for new engines (see `docs/superpowers/checklists/engine_readiness.md`).
- ops/engine_service.py — daemon polling `platform.application_log` for `DATA_OPERATIONS_COMPLETE`; fires `scripts/run_all_engines.sh` (2026-05-14).
- Shared AAR read-side: `tpcore.aar.AARReader` (used by both allocator + forensics); shared exit-reason classifier: `tpcore.aar.classify_exit_reason`.
- Future engines: s2/, catalyst/

**Engine credibility status as of 2026-05-13 (post data-cleanup):** All four engines produce positive OOS edge candidates (scores 0.78–1.26), all four still fail the DSR ≥ 0.95 / credibility ≥ 60 gate. Data foundation is clean; signal strength is the binding constraint.

## Conventions
- All timestamps UTC. Market hours via `tpcore.calendar` (which wraps `exchange_calendars` XNYS).
- No yfinance. No Discord. No manual execution.
- All orders via Alpaca API. Paper-then-live. Default Alpaca data feed is **SIP** (not IEX — IEX silently misses tickers that trade off-IEX).
- Engines built in order so far: Sigma → Reversion → Vector → Momentum → Sentinel.
- Backtest with self-built survivorship-free database before any live trading. (Note: `prices_daily` is currently only partially survivorship-clean — known caveat in `momentum/backtest.py` docstring.)
- Every engine has 5 Plugs: setup_detection, lifecycle_analysis, execution_risk, aar_logging, capital_gate.
- Every engine's `setup_detection` plug populates a `tpcore.backtest.filter_diagnostics.FilterDiagnostics` instance so SIGNAL events carry per-gate pass/block counters.
- **Data-layer acceptance gate (2026-05-13, expanded 2026-05-14):** validation suite (11 checks: delistings, constituent, splits, row_integrity, fundamentals_integrity, corporate_actions_integrity, catalyst_freshness, sec_filings_freshness, liquidity_tiers_freshness, ticker_classifications_coverage, macro_indicators_freshness) must return `passed=True` with `confidence=1.000`. Cross-table audit (`scripts/run_audit_all_tables.sh`) must return 0 violations across every dependent table.
- **External-API discipline (2026-05-14):** every data adapter on the platform is 5/5 compliant with `docs/superpowers/pipelines/data_adapter_pipeline.md` (ingest / test / validate / dashboard / schedule). New adapters start from `tpcore/templates/adapter_template.py` and pass `docs/superpowers/checklists/adapter_readiness.md` before merging. HTTP retries go through `tpcore.outage.with_retry` — no local `tenacity`, no `asyncio.sleep` loops. Ingest uses the CSV-first sub-protocol (download → validate-at-CSV → load → compress) for any non-trivial pull.
- **Operator workflow (autonomous posture, 2026-05-14):** daily data-operations via `scripts/run_data_operations.sh` (single button: 14-stage update → audit → validation → compress → emit `DATA_OPERATIONS_COMPLETE` → **forensics scan**; macOS notification fires on any failure). The `engine-service` daemon (added 2026-05-14) picks up the event and fires the engine sweep — data-ops latency no longer bleeds into the trade-submit window. Full historical refresh via `scripts/run_full_backfill.sh`. Daemons installed via `scripts/install_all_daemons.sh` (trade_monitor + engine_service + data_operations + allocator — 4 total). SEC EDGAR historical backfill is a single self-verifying command: `python scripts/ops.py --stage sec_filings --backfill`. Dashboard `--check` carries 19 probes including `missed_data_operations`, `supabase_backup`, `disk_space`, `trade_monitor_heartbeat`, and `macro_indicators_freshness` (FRED, last data source — 2026-05-14).

## Session Rules
- Read docs/STYLE_GUIDE.md before writing any code.
- **When building a new engine, read `docs/superpowers/checklists/engine_readiness.md` BEFORE writing code.** The 10 sections are non-optional. Section 10 in particular enumerates the six compliance verifications (BaseEnginePlug on every plug, FilterDiagnostics on signals, credibility write, trading-day gate, classify_exit_reason, stale-order cancel) that the Sentinel 2026-05-15 audit surfaced. Start from `tpcore/templates/engine_template/` — the scaffold satisfies the gaps by construction.
- **Engine-build compliance shortlist** (the recurring gaps; full rationale in STYLE_GUIDE.md "Engine plug compliance"):
  - Every engine plug subclasses `BaseEnginePlug` and implements `validate_dependencies` + `healthcheck`.
  - Every engine backtest calls `write_credibility_score` so the capital gate has a rubric row to read.
  - Every scheduler checks `tpcore.calendar.is_trading_day()` and returns early on non-trading days.
  - Every AAR plug uses `tpcore.aar.classify_exit_reason` — never hardcode `ExitReason` literals.
- **Never access private attributes (`._store`, `._pool`, etc.) on tpcore classes.** Use the public accessors (`RiskGovernor.state_for(...)`, `AARWriter.pool`, etc.). If a public accessor doesn't exist for what you need, extend the tpcore class with one — don't add `# noqa: SLF001`. See `docs/STYLE_GUIDE.md` "Private-attribute access on tpcore classes" for the canonical examples.
- Read docs/glossary.md if present before coding.
- Never modify tpcore without checking all engines that consume it.
- Every trade path goes through tpcore.risk.RiskGovernor.check_trade().
- Sigma/Reversion/Vector use Alpaca bracket orders (take-profit + stop-loss submitted together). Momentum uses day-market orders only — no per-name stops between monthly rebalances; risk is managed by diversification + rotation. Sentinel uses day-market batch orders for the defensive ETF basket — no per-name stops, lifecycle-driven exits.
- All code type-hinted. Pydantic v2 for data models. structlog for logging.
