# Edge-Discovery and Strategy-Validation Plan

**Status:** Phase 1 complete (2026-05-12). Phase 2 (cost model build) ready to start.

## Objective
Systematically find, validate, and calibrate trading strategies that have a statistically significant edge after realistic costs in the US equities market.

## Context
- Three engines (Sigma, Reversion, Vector) are built but fail the overfitting credibility gate (50, 45, 45 / 100).
- The universe is limited to ~50 large-cap tickers; the simulation showed zero candidates.
- The cost model currently uses a flat 0.05% slippage assumption.
- We need to expand the universe, build a real cost model from market data, and then run a historical replay to get an honest edge assessment.

## Two-Track Validation
- **Track A – Infrastructure Validation:** Prove the pipeline works end-to-end (orders → fills → AARs → risk checks).
- **Track B – Strategy Edge Validation:** Prove a specific set of filters produces positive risk-adjusted returns after realistic costs.

No strategy graduates from paper to live capital until it passes the full overfitting diagnostic (credibility ≥ 60).

## Implementation Steps

### Phase 1: Universe Expansion — **Complete (2026-05-12)**
1. **A1 – Full Alpaca asset list ingestion** ✓
   - `daily_bars` job runs with `universe: all_active`, `min_price=5.0`, `min_volume=250000`.
   - Handler `_handle_daily_bars_all_active` in `tpcore/ingestion/handlers.py`; local driver `scripts/run_daily_bars_all_active.py`.
   - Last sweep: 8,297 active assets enumerated → 533 passed coarse → 2,665 rows upserted (2026-05-12).
2. **A2 – Tradier historical bar extraction + ingest** ✓
   - `scripts/extract_tradier_full.py` produced `data/tradier_export/tradier_bars_full.csv` (1.07 GB, 22.36M rows, 8,640 symbols).
   - `scripts/ingest_tradier_csv.py` merged into `platform.prices_daily` with Inf/overflow guards (≈50k bad-data rows skipped, 0.23% of source). Latest run: 20.56M rows attempted, 7,710 tickers seen.
3. **A3 – Universe simulation** ✓
   - `scripts/simulate_universe.py` rewritten to batched SQL — 32 min → 57 s.
   - Result (2026-05-12, 7,694-ticker universe): **Sigma 187, Reversion 4, Vector 0**.
   - Vector zero is a calibration issue, not data: 65% of 1,435 coarse survivors fail on `P/B < 1.5` alone (current market well above that ceiling — AAPL P/B 38.85). See "Pivot Plan" below for the recalibration vs. pivot decision.
   - The script also persists a `UNIVERSE_SIMULATION` row to `platform.application_log` with the full candidate lists; the smoke-test workflow reads from there.

### Phase 1.5: Trade Monitor — **Complete (2026-05-12)**

**Status:** All six work items landed; spec `docs/superpowers/specs/2026-05-12-trade-monitor-design.md` realized.

- **M1** ✓ — Alembic migration `20260512_0000_create_open_orders.py` applied. Table `platform.open_orders` (id, engine, trade_id, ticker, order_type, alpaca_order_id, status, fill_price, filled_at, decision_data jsonb, created_at, updated_at) with unique (engine, trade_id, order_type) and indexes on alpaca_order_id (partial, NOT NULL) and (engine, status).
- **M2** ✓ — `tpcore/trade_monitor.py` consuming Alpaca's `TradingStream`. `on_trade_update` matches fills by `alpaca_order_id`, updates the row, submits Tier 2 reactively (BUY bracket at the engine's far target, same hard stop as Tier 1), writes the AAR on Tier 2 close, bumps `risk_state`. Crash-safe via `reconcile_pending_on_startup` which queries broker state for each `'pending'` row.
- **M3** ✓ — `sigma/order_manager.py`, `reversion/order_manager.py`, `vector/order_manager.py` all submit Tier 1 only and persist `decision` + `assessment` to `platform.open_orders`. `TPCORE_SCAN_ONLY` guard removed.
- **M4** ✓ — `AlpacaPaperBrokerAdapter.submit_tier1_only(ticker, qty, side, take_profit_price, stop_loss_price, client_order_id, engine_id)` returns the placed `Order`. `submit_execution_decision` is kept as a back-compat wrapper for the smoke test only.
- **M5** ✓ — `tpcore/tests/test_trade_monitor.py` with 13 tests: unit (helpers) + integration (Sigma Tier 1 fill triggers Tier 2 submission, Vector tier1 fill stays single-leg, Tier 2 fill writes AAR and bumps risk_state, unmatched fills are silently skipped, cancellation flips status).
- **M6** ✓ — `trade-monitor` service added to `railway.json` (`restartPolicyType=ALWAYS`, persistent). Railway deploy verification is deferred until Railway is re-enabled; locally the monitor is invokable via `python tpcore/trade_monitor.py`.

### Phase 2: Cost Model Build (Weeks 1-2)
1. **B1 – Schema migration**
   - Create `platform.spread_observations` and `platform.liquidity_tiers` according to the master plan.
2. **B2 – Corwin-Schultz bootstrap (free, immediate)**
   - Implement the Corwin-Schultz spread estimator in `tpcore/backtest/spread_estimator.py`.
   - Run it against the expanded universe to produce a **liquidity ranking** (not final tiers).
   - This ranking tells the streaming adapter which 200 tickers to observe first.
3. **B3 – Tradier streaming WebSocket client**
   - Build `tpcore/data/tradier_streaming.py`.
   - On Monday morning (Week 2), subscribe to real-time quotes for the top-ranked 200 tickers.
   - Record bid-ask spreads to `platform.spread_observations`.
   - Expand to the next liquidity band each subsequent week.
4. **B4 – Provisional tier assignment**
   - After 5 full trading days of streaming (end of Week 2), compute median/p95 spreads.
   - Assign provisional tiers (T1–T5) with the `provisional = true` flag.
5. **B5 – Tier-aware backtest cost model**
   - Modify `tpcore/backtest/cost_model.py` to use tier-specific round-trip costs.
6. **B6 – Pre-trade cost check**
   - Integrate liquidity tier into the Risk Governor: suppress trade if expected cost > strategy edge.
7. **B7 – Parity Harness spread logging**
   - Log bid-ask spread at order time alongside fill price.

### Phase 3: Infrastructure Validation via Historical Replay (Weeks 3-4)
1. **C1 – Historical replay script**
   - Build `scripts/replay_history.py`.
   - Loop over historical trading days (2019-01-01 to 2025-12-31).
   - Run the production engine schedulers (`sigma/scheduler.py`, etc.) with a simulated clock.
   - Use the **stable tier-specific spread costs** derived from real Tradier data to simulate fills.
   - Write AARs, application logs, risk state, and parity drift logs.
2. **C2 – Replay execution**
   - Run the replay for the full universe.
   - Collect trade records and P&L.

### Phase 4: First Edge Assessment (Week 4)
1. **D1 – Compute strategy metrics**
   - For each engine, calculate Sharpe ratio, maximum drawdown, profit factor, and win rate from the replay.
2. **D2 – Run the overfitting diagnostic**
   - Feed the trade list into `OverfittingDiagnostic`.
   - Check credibility score (≥ 60?), DSR, MinBTL, trades-per-parameter, etc.
3. **D3 – Decision gate**
   - If any engine passes the credibility gate → proceed to Phase 5 (Edge-Finding Agent).
   - If no engine passes → pivot to systematic search for new strategy classes (see Pivot Plan below).

### Phase 5: Edge-Finding Agent (only if Phase 4 passes)
1. **E1 – Build a hypothesis queue** in `platform.research_queue`.
2. **E2 – Implement a template library** of reusable signal functions from existing plugs.
3. **E3 – Create a worker** that picks up queued hypotheses, runs them through the historical replay, and writes results.
4. **E4 – Variation generator** creates slight modifications of top-performing hypotheses for further testing.

### Pivot Plan (if no edge found)
- Broaden the search to less efficient market segments (micro-caps, OTC, etc.) if liquidity data supports it.
- Investigate alternative data sources (options flow, insider filings, macro indicators) for novel edges.
- Consider multi-asset strategies (ETFs, bonds) for diversification.
- The platform's infrastructure remains a rigorous test environment for any new idea.

## Deliverables and Timeline
- Monday evening (Week 1): Expanded universe ingested.
- Tuesday evening (Week 1): Corwin-Schultz bootstrap built, streaming adapter ready.
- Friday evening (Week 2): 5 days of streaming quotes collected; provisional tiers assigned; backtests re-run with those tiers (variance check only, not final).
- Week 3: Streaming adapter observes the next liquidity band; stable tiers gradually replace provisional ones.
- Monday (Week 4): Historical replay built.
- Thursday (Week 4): Historical replay completed.
- Friday (Week 4): First reliable cost-calibrated edge assessment produced.

The role of this Claude session is to execute the plan step-by-step, building any missing components and running the required simulations. The session should operate autonomously, reporting progress and results back to you. The plan does not require building any new strategies from scratch—only running the three existing engines against the expanded data and calibrated cost model to determine if they have an edge.
