# Edge-Discovery and Strategy-Validation Plan

**Status:** Ready for execution. Platform infrastructure is live; strategies are unproven.

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

### Phase 1: Universe Expansion (Start immediately, complete by Monday evening)
1. **A1 – Full Alpaca asset list ingestion**
   - Update the `daily_bars` ingestion job to process `universe: all_active`.
   - Set coarse filter: price > $5, avg daily volume > 250K.
   - Default cost for unknown tickers: T4 (1.50% round-trip).
   - Upsert into `platform.prices_daily`.
2. **A2 – Tradier historical bar extraction**
   - Run `scripts/extract_tradier.py` against all available US equities.
   - Write `tradier_bars_full.csv` to disk.
3. **A3 – Universe simulation**
   - Run `scripts/simulate_universe.py` against the expanded `prices_daily`.
   - Verify that candidate counts for Sigma, Reversion, Vector are non-zero.

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
