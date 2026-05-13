# Edge-Discovery and Strategy-Validation Plan

**Status (2026-05-12):** Phase 1 + Phase 1.5 (trade monitor) + Phase 2 (cost model) all complete. Backtests re-run with tier-aware costs — Sigma 55/100, Reversion 45/100, Vector 45/100; none passes the ≥ 60 gate. **Immediate next step: the pipeline smoke test** at the 2026-05-13 US market open. Historical replay (Phase 3) is deferred until smoke + engine architecture decisions land.

## Current Execution Environment

**Local Mac. Railway paused.** The four Railway services (`ingestion-engine`, `sigma-scheduler`, `reversion-scheduler`, `vector-scheduler`) exist but have no cron schedule and `restartPolicyType=NEVER`; the new `trade-monitor` service is in `railway.json` but never reached Railway. All daily ops, engine runs, smoke tests, and backtests are invoked locally from `scripts/`.

The architectural decision on Railway vs. another host is deferred until at least one engine clears the credibility gate. Until then:

- **Immediate next gate** (May 13 open): `scripts/pipeline_smoke_test.py` — confirms the engine → broker → trade_monitor → AAR path round-trips against the live Alpaca paper account.
- **Daily ops:** `scripts/ops.py --full` for refresh + validation + universe sim.
- **One-shot infra check:** `scripts/smoke_test.py` for broker reachability only.
- **Paper trading on demand:** invoke `python sigma/scheduler.py` / `python reversion/scheduler.py` / `python vector/scheduler.py` directly when the operator wants the engines to act.
- **Trade monitor (live):** `python -m tpcore.trade_monitor` in a separate terminal — required for the engines' Tier 2 follow-on logic to fire.

## Objective
Systematically find, validate, and calibrate trading strategies that have a statistically significant edge after realistic costs in the US equities market.

## Context
- Three engines (Sigma, Reversion, Vector) are built and backtested with tier-aware costs (2026-05-12). Credibility scores: Sigma 55, Reversion 45, Vector 45 / 100. None passes the ≥ 60 gate.
- Universe expanded to 7,694 tickers in `platform.prices_daily`; simulate_universe shows Sigma 187 candidates / Reversion 4 / Vector 0 (the Vector zero is a calibration gap on `P/B < 1.5`, not a data issue).
- Cost model is tier-aware via `platform.liquidity_tiers` populated from Corwin-Schultz (`tpcore.backtest.spread_estimator`). T4 default (1.50% round-trip) for unknowns.
- Trade-count thinness vs the parameter search space (MinBTL gap + DSR deflation) remains the binding constraint for all three engines — not cost calibration. The historical replay (Phase 3) is the next lever; deferred until the local-execution smoke loop is proven on the live Alpaca paper account.

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

### Phase 2: Cost Model Build — **Complete (2026-05-12)**

Shipped on Corwin-Schultz alone — the Tradier streaming subscription (B3) was dropped after Tradier was scheduled to deprecate. `platform.liquidity_tiers` is populated from `source = 'corwin_schultz'`. The aggregator in `scripts/assign_liquidity_tiers.py` is source-agnostic; when a real-time quote feed lands later it joins the same table by extending the `--sources` flag.

- **B1** ✓ — `20260512_2100_spread_observations_and_liquidity_tiers.py`. Tables live with 30-day rolling retention on observations.
- **B2** ✓ — `tpcore/backtest/spread_estimator.py` + 6 unit tests. CS estimator + `rank_universe_by_liquidity` populates `spread_observations` with `source='corwin_schultz'`. Ranked 1,435 coarse-pass tickers locally.
- **B3** ✗ wontfix — Tradier streaming deprecated. Future intraday-quote needs are served on-demand (one REST call to `Alpaca /v2/stocks/{symbol}/quotes/latest` at trade time) rather than a persistent service. Spec: revisit if backtest credibility shows CS noise is hurting engine ROI.
- **B4** ✓ — `scripts/assign_liquidity_tiers.py`. Aggregates by median + p95 spread per ticker, assigns tier per the (5/15/50/200 bps) thresholds, upserts. First run: T1:14 / T2:46 / T3:324 / T4:988 / T5:63.
- **B5** ✓ — `tpcore/backtest/cost_model.py` exposes `get_round_trip_cost(pool, ticker)`. T4 default for unknowns (150 bps round-trip). `SimpleCostModel` default bumped from 5 bps to T4 so an unconfigured backtest doesn't silently understate cost. 6 tests.
- **B6** ✓ — `RiskGovernor.check_cost(ticker, expected_edge_pct)` + `check_trade(ticker, expected_edge_pct)` kwargs. The three order managers compute per-trade edge from `assessment.entry_price` and the conservative TP (Sigma: mid-band; Reversion: 20-MA; Vector: profit_target) and thread it through. 6 new tests + back-compat preserved.
- **B7** ✓ — `20260512_2200_parity_drift_log_spread_columns.py` adds `spread_at_order_pct` + `spread_observed_at`. `LivePaperParityHarness.submit_pair` snapshots the latest `spread_observations` row for the ticker before submission and writes it on the drift row. 2 new tests.

### Phase 2 (original, kept for history): Cost Model Build (Weeks 1-2)
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

### Phase 2.5: Parameter-Search Pipeline — **Complete (2026-05-13)**

Built the production edge-discovery substrate that replaces one-off backtest tuning. Three subsystems land together:

1. **Shared search infra (`tpcore/backtest/search.py`)** — `BacktestRunResult` + `SearchTrade` dataclasses, `compute_search_metrics()` that runs the overfitting diagnostic + credibility rubric on any (trades, parameters, price_data) triple, standardised trade-log CSV format.
2. **Per-engine programmatic entry (`{sigma,reversion,vector,momentum}/backtest.py`)** — each engine exposes `load_*_window_context()` (async, heavy I/O — pulls bars + fundamentals + catalysts as needed) and `run_*_with_context()` (sync, pure compute). The orchestrator imports these directly; no subprocess, no stdout parsing. Each engine's CLI also accepts `--json`, `--trade-log <path>`, and parameter-override flags so ad-hoc invocation works.
3. **Orchestrator (`scripts/search_parameters.py`)** — random search + walk-forward + final held-back DSR. Loads window context once per walk-forward window (panel-sharing refactor, ~60× per-trial speedup), then runs all candidates in that window against the shared in-memory context. Period-aggregated metrics: trades sharing an `entry_date` collapse into one portfolio period return before computing Sharpe / drawdown / DSR (correct for both single-position and parallel-position strategies).

**Verdicts produced 2026-05-12 / 2026-05-13:**

| Engine | T1+T2 universe? | Held-back Sharpe | Held-back PF | DSR | Conclusion |
|---|---|---|---|---|---|
| Sigma | yes (1,281) | +0.74 | +3.71 | 0.00 | Marginal real edge — research only |
| Reversion | yes (355 funded) | -0.08 | +0.87 | 0.00 | Pipeline caught overfit (in-sample +2.87 → OOS -0.08) |
| Vector | n/a | — | — | — | **Data-blocked** — `catalyst_events` has 0 overlap with T1+T2 |
| **Momentum** | yes (1,281) | **+1.58** | **+2.80** | 0.00 | **Strongest OOS signal in the bench** |

DSR ≥ 0.95 is structurally too strict for monthly portfolio strategies with only 2 years of held-back data (24 observations × 50-trial penalty makes the bar unreachable regardless of strategy quality). For Momentum the held-back portfolio Sharpe + walk-forward consistency are the real evidence.

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

### Phase 4: First Edge Assessment (Week 4) — **Superseded by Phase 2.5 search pipeline (2026-05-13)**

The original Phase 4 plan was: run a single historical replay, feed trade lists into `OverfittingDiagnostic`, decide go/no-go. That decision gate fired across all four engines via the Phase 2.5 parameter-search pipeline. The verdict table is in Phase 2.5 above.

**Phase 4 decision (2026-05-13):** Momentum is the only engine producing a real OOS edge on the wider universe. Sigma is marginal; Reversion was caught as overfit; Vector is data-blocked. Forward path:

- **Momentum**: paper-trade with small size (Phase 5a below) to validate the +1.58 held-back Sharpe in production. 3 months of paper performance + the existing backtest = the real out-of-sample test, earned from market exposure not from a credibility-checkbox.
- **Vector**: backfill `platform.catalyst_events` for T1+T2 tickers (one-time ingestion task). Re-run search. Decision point on whether to invest further then.
- **Sigma / Reversion**: park. Reversion is overfit-confirmed; Sigma is too marginal to move ahead of Momentum.

### Phase 5a: Momentum Paper Trading — **In progress (2026-05-13)**

1. **5-plug architecture — done.** `momentum/plugs/{setup_detection,lifecycle_analysis,execution_risk,aar_logging,capital_gate}.py`. Session questions route through `tpcore.calendar` per CLAUDE.md.
2. **Scheduler — done.** `momentum/scheduler.py` runs daily; quietly no-ops on non-rebalance days, fires on the first NYSE session of each month. `--force-rebalance` flag bypasses the date check for kickoff and emergency rebalances. Trade-monitor integration *not* required (Momentum has no per-name stops between rebalances).
3. **Paper kickoff — in progress.** `scripts/run_momentum_kickoff.sh` queues the first rebalance against Alpaca paper. Target: 3 months of live paper data accumulating from kickoff date.
4. **Re-evaluate credibility** after paper data lands, under either (a) a frequency-adjusted DSR threshold (~0.5 for monthly with 24 obs) or (b) PSR instead of DSR.

Phase 2.5 follow-ups (post-kickoff, before live capital):
* DBLogHandler wiring to `platform.application_log`
* Drawdown circuit breaker (pause new entries when portfolio > 10% off rolling peak)
* Common-stock-only filter (warrants like `XBPEW` slipped into the smoke output despite the T1+T2 universe)
* Min-price floor ($1) to keep penny stocks out of the decile

### Phase 4 (publication gate — tip sheet): blocked on credibility + attorney review

Separate from the original *First Edge Assessment* (superseded above), this Phase 4 covers the tip-sheet publication path.

- **Phase 1 of tip sheet — Private operator review tool**: build now. Terminal-only output. Credibility gate (≥ 60) enforced by default; `--force` permits private review of unproven engines. Disclaimer printed on every output and is non-removable. No public distribution.
- **Phase 2 of tip sheet — Gated publication**: blocked until ALL of these are true:
  1. At least one engine has credibility ≥ 60 from held-back validation
  2. That engine has accumulated ≥ 30 paper trades with documented outcomes
  3. Disclaimer language has been reviewed by a securities attorney
- **Phase 3 of tip sheet — Multi-engine roll-up**: blocked until two-plus engines have passed Phase 2 gates.

Full design and rationale: `docs/superpowers/specs/2026-05-13-tip-sheet-plan.md`. Publication-policy document (`docs/TIP_SHEET_POLICY.md`) is written alongside the Phase 2 build, not before.

### Phase 5b: Edge-Finding Agent (deferred until Phase 5a outcome)

1. **E1 – Build a hypothesis queue** in `platform.research_queue`.
2. **E2 – Implement a template library** of reusable signal functions from existing plugs.
3. **E3 – Create a worker** that picks up queued hypotheses, runs them through the parameter-search orchestrator, and writes results.
4. **E4 – Variation generator** creates slight modifications of top-performing hypotheses for further testing.

### Pivot Plan (if Momentum paper-trading fails)
- Broaden to less efficient segments (T3+T4 tickers with limits-to-arbitrage).
- Alternative data (options flow, insider filings, macro indicators).
- Multi-asset (ETFs, bonds) for diversification or pairs trading.
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
