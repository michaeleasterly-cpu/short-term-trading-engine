# Session Log

## 2026-05-12 (continued) — Trade monitor built (Phase 1.5 complete)
- M1: Alembic migration `20260512_0000_create_open_orders.py` creates `platform.open_orders` (id, engine, trade_id, ticker, order_type, alpaca_order_id, status, fill_price, filled_at, decision_data jsonb) with `UNIQUE (engine, trade_id, order_type)` + partial index on `alpaca_order_id` for the monitor's hot path.
- M4: `AlpacaPaperBrokerAdapter.submit_tier1_only(...)` — single-bracket primitive returning the placed `Order`. `submit_execution_decision` retained as deprecated wrapper for the smoke test.
- M3: All three engine order managers (Sigma, Reversion, Vector) refactored — submit Tier 1 only via the new primitive, persist `decision` + `assessment` JSON to `platform.open_orders`. `TPCORE_SCAN_ONLY` guard removed.
- M2: `tpcore/trade_monitor.py` — `TradeMonitor` class consuming Alpaca `TradingStream`, reactive Tier 2 submission on Tier 1 fill, AAR + `risk_state` write on Tier 2 close, crash-safe via `reconcile_pending_on_startup`, exponential-backoff reconnect loop. `python tpcore/trade_monitor.py` CLI entry.
- M5: `tpcore/tests/test_trade_monitor.py` — 13 tests covering helpers (`_decimal`, `_aware`, `_resolve_tier2_take_profit`, `_row_from_record`), Sigma Tier 1 → Tier 2 submission, Vector no-tier2 path, Tier 2 fill → AAR + risk_state, unmatched fills ignored, cancellation flow. All pass.
- M6: `trade-monitor` service added to `railway.json` (`restartPolicyType=ALWAYS`). Railway deploy verification deferred (Railway is paused).
- Full suite: **311 passing, 4 skipped**. Ruff + forbidden-imports green.

## 2026-05-12 (continued) — Scan-only guard + trade-monitor spec
- Attempted `scripts/start_paper_trading.py`: surfaced a real engine bug. The order managers (Sigma, Reversion, Vector) call `broker.submit_execution_decision` which submits both Tier 1 + Tier 2 sequentially. Tier 2 is an opposing-side limit (LONG → SELL at upper band); Alpaca rejects with `cannot open a short sell while a long buy order is open`. Tier 1 lands as an orphan with no managed Tier 2 exit. Architectural gap: the engines were designed assuming a live worker would react to fills; that worker was never built.
- Added a `TPCORE_SCAN_ONLY=true` env-var guard to all three order managers — runs gates + governor + signal logging, then returns `None` before any broker call. Defense-in-depth so a cron / manual run can't accidentally submit while the trade monitor is missing.
- Wrote design spec `docs/superpowers/specs/2026-05-12-trade-monitor-design.md` for the live `TradeMonitor` worker: Alpaca `TradingStream` consumer, new `platform.open_orders` table, engine refactor to Tier 1-only submission, crash-safe rehydration. Queued as the next implementation block.
- Two orphan Tier 1 orders (YUMC) from intermediate runs were cancelled at the broker.

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
