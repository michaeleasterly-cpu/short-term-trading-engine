# Session Log

## 2026-05-12 — Phase 1 complete + paper-trading smoke test
- **A1**: Alpaca `all_active` sweep wired (handler `_handle_daily_bars_all_active` + local driver `scripts/run_daily_bars_all_active.py`). Universe expanded from ~50 to 7,694 tickers in `platform.prices_daily`.
- **A2**: Tradier wide-export ingested via `scripts/ingest_tradier_csv.py` with Inf/overflow guards (50k bad-data rows dropped — 0.23% of source). 20.6M rows total.
- **A3**: `scripts/simulate_universe.py` rewritten to batched SQL (32 min → 57 s). Results: Sigma 187, Reversion 4, Vector 0. Vector zero is a calibration issue, not data: 65% of coarse survivors fail on `P/B < 1.5` (current market expensive — AAPL P/B 38.85).
- **Corporate-actions** handler now supports `config.universe = "all_active"`; full universe ingested (109,344 events, 250 splits across 217 tickers, 2 splits actually applied to bars — Tradier was already adjusted for the other 248).
- **Fundamentals backfill**: FMP Starter pulled 178,518 quarters across 5,981 tickers via `scripts/backfill_fundamentals.py --all-active`. `compute_fundamental_ratios.py` rewritten as a single set-based SQL UPDATE (the previous per-row loop dropped its pooler connection mid-run) + tightened input filter (`total_assets > 0 AND total_liabilities >= 0`) to reject degenerate FMP rows.
- **Paper-trading smoke test**: new `scripts/smoke_test.py` round-trips a Sigma-shaped `ExecutionDecision` through `AlpacaPaperBrokerAdapter.submit_execution_decision()` and cancels — proves the database → universe → execution risk → broker → audit-log loop end-to-end. Validated on ACAD.
- **Infra**: Supabase upgraded Free → Pro ($25/mo, 8 GB) on 2026-05-11 after the all-active sweep tripped the free-tier 500 MB read-only lock. Railway auto-deploys disabled; all daily ops run locally for now. CI ruff drift fixed; 298 tests pass.
- Total fixed monthly cost: $52 (FMP Starter $22 + Railway Hobby $5 + Supabase Pro $25).

## 2026-05-13 — Phase 0 Bootstrap
- Initialized repo structure
- Built tpcore skeleton
- Created platform schema migrations
- Ran Alpaca asset ingestion script
- Decision: FMP free tier for now; upgrade to Starter before July backtesting
