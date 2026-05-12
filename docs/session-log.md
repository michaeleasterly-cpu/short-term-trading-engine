# Session Log

## 2026-05-12 (continued) — Calendar bug fix + pipeline smoke wired through tpcore.calendar
- Acceptance audit on `scripts/pipeline_smoke_test.py` caught a real contract violation: the market-hours check was a hardcoded `13:30 ≤ UTC minutes ≤ 20:00 ` range, not `tpcore.calendar`. CLAUDE.md + STYLE_GUIDE.md require the calendar.
- Replacing the hardcoded check surfaced a latent bug in `tpcore.calendar`: `session_contains` / `next_open` / `next_close` / `previous_close` passed tz-aware pandas Timestamps (carrying `datetime.timezone.utc`) into `exchange_calendars`, which now validates inputs through `calendar_helpers.parse_date` and reads `ts.tz.key`. Stdlib `datetime.timezone.utc` doesn't expose `.key`, so every call crashed with `AttributeError`.
- Fix: naive UTC Timestamps at the `exchange_calendars` boundary (same wall-clock UTC, no tzinfo to introspect); tz-aware Timestamps remain only for the open/close range comparison in `session_contains`. Stdlib `datetime.timezone.utc` stays the lingua franca — no `ZoneInfo` / `pytz` introduced.
- New `tpcore/tests/test_calendar.py` (11 tests) pins the regression: during/before/after-session, weekend, holiday, naive-input ValueError, `next_open` / `next_close` / `previous_close` stdlib-UTC round-trip, `trading_days_between` arithmetic.
- `scripts/pipeline_smoke_test.py` `SKIPPED` message now reports the live `next_open` timestamp from the calendar — e.g. `"SKIPPED — NYSE session is closed at 2026-05-12T10:27+00:00. Next open per tpcore.calendar: 2026-05-12T13:30+00:00."`
- 322 tests pass, ruff clean.

## 2026-05-12 (continued) — Pipeline smoke test + monitor run-as-module fix
- Local run of `python tpcore/trade_monitor.py` surfaced a sys.path trap: the script's directory ends up on sys.path, and the stdlib's internal `import logging` (via concurrent.futures._base via asyncio) resolves to the project's `tpcore.logging` package. Fix: invoke as `python -m tpcore.trade_monitor`. Updated docstring + `railway.json` startCommand accordingly; verified the monitor connects to `BaseURL.TRADING_STREAM_PAPER` and writes STARTUP + STREAM_CONNECTED to `application_log`.
- New `scripts/pipeline_smoke_test.py` — live end-to-end smoke that submits one Tier 1 BUY bracket on SPY, inserts the matching `open_orders` row, polls for the monitor to mark Tier 1 filled and submit Tier 2, then cancels everything and cleans up. Market-hours gated (13:30–20:00 UTC, Mon–Fri); idempotent across reruns. Documented in `docs/OPERATIONS.md` §10 alongside the existing broker-only `smoke_test.py`.

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
