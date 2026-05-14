# Short-Term Trading Engine — Unified Platform Master Plan

**Version:** 1.3
**Date:** 2026-05-12
**Execution:** **Local Mac. Railway paused.** Auto-deploys disabled and cron schedules unset on the four Railway services (`ingestion-engine`, `sigma-scheduler`, `reversion-scheduler`, `vector-scheduler`); the new `trade-monitor` service is defined in `railway.json` but not deployed. Daily ops, engine runs, smoke tests, and backtests are all invoked locally from `scripts/`. The architectural decision on Railway vs. another host is deferred until an engine clears the credibility gate.

**Status:** All three engine pipelines (Sigma, Reversion, Vector) plus the live trade monitor (`tpcore.trade_monitor`) are built and unit-tested. RiskGovernor `check_trade()` + startup kill-switch check, AARWriter persistence, LivePaperParityHarness wiring, and the Phase 2 cost gate (`RiskGovernor.check_cost`) are all verified end-to-end (live DB round-trip for AAR; harnesses no-op without live broker creds, by design). Engines read daily bars exclusively from `platform.prices_daily` via `PostgresDataAdapter` — no live-API fallback. Per-run audit timeline lands in `platform.application_log` via `tpcore.logging.DBLogHandler`. All three engines still fail the overfitting-aware credibility gate (Sigma 55, Reversion 45, Vector 45 / 100); none cleared for live capital — paper-trading only. **Immediate next gate:** `scripts/pipeline_smoke_test.py` against the Alpaca paper account at the 2026-05-13 US market open.

---

## 1. Constitution

### 1.1 Product Identity

The Short-Term Trading Engine is a personal, fully automated, multi-strategy trading platform for US equities. It operates on daily timeframes, executes all orders via the Alpaca API, and explicitly does not provide financial advice. The operator oversees; the platform executes.

### 1.2 Core Principles

- **Risk control IS the product.** Position sizing, kill switches, exposure caps, and refusal-to-trade are first-class features — not safety wrappers around a "real" engine.
- **Suspicion is the default.** The platform becomes MORE skeptical in crowded, volatile conditions.
- **AI is auditor, never execution authority.** Deterministic rules govern all orders. AI modules explain, classify, and refuse — they never place, size, or exit positions.
- **Personal-use scope.** Commercial use is blocked. The platform protects ONE operator.
- **Provenance discipline is non-negotiable.** Every data point is timestamped with source and observed-at. Raw text is never persisted unless needed for audit.
- **Engines stay isolated; summary data does not.** Engines share `platform.*` tables with an engine discriminator column. No engine calls another engine directly.
- **Core-first principle.** Every engine consumes shared functionality through `tpcore` modules. Direct vendor SDK imports are prohibited.

### 1.3 Architectural Constraints

- **Time:** All timestamps in UTC. Market calendar provided by `exchange_calendars` (NYSE).
- **Data:** No `yfinance`. Paid sources only where free tiers are insufficient. Survivorship-free backtesting mandatory.
- **Execution:** Fully automated via Alpaca API. Bracket orders wherever possible. Manual execution only for emergency overrides.
- **Build order:** Exactly one engine is built and paper-traded before the next begins.

---

## 2. Shared Core (`tpcore`)

`tpcore` is the stable foundation. All engines depend on it.

### 2.1 Interfaces

- `BaseEnginePlug` ABC — every engine plug inherits from this.
- `BrokerExecutionInterface` — Alpaca adapter implements this.
- `DataProviderInterface` — FMP, Alpaca, SEC EDGAR adapters implement this.

### 2.2 Risk Governor (`tpcore.risk`)

- Per-engine daily loss limit (5%), weekly loss limit (10%), max concurrent positions (8).
- Platform-wide net long exposure cap (60% of total capital).
- `check_trade(engine_id, size, direction) → bool`
- `emergency_kill()` — cancels all orders, flattens positions.
- State persisted in `platform.risk_state`.

### 2.3 After-Action Reports (`tpcore.aar`)

- `AfterActionReport` Pydantic model — engine, trade_id, entry/exit details, P&L, regime tags, rule-compliance flag.
- `AARWriter` writes idempotently to `platform.aar_events`.

### 2.4 Quality & Parity (`tpcore.quality`, `tpcore.parity`)

- `DataQualityScore` + writer → `platform.data_quality_log`
- `ExecutionQualityScore` + writer → `platform.execution_quality_log`
- `LivePaperParityHarness` — compares paper/live fills → `platform.parity_drift_log`

### 2.5 Backtest Integrity (`tpcore.backtest`)

- Provider-agnostic harness.
- `BacktestCredibilityRubric` (0–100) — lookahead, survivorship, PIT fundamentals, regime coverage, out-of-sample, plus the four overfitting-detection categories below.
- **Overfitting detection suite** — automatically run by each engine's backtest script as a "Statistical Validation" section after the comparison table:
    - `Sensitivity sweeps` — parameter perturbation across ±25%, surface flatness scoring (`tpcore/backtest/sensitivity.py`).
    - `Monte Carlo sequence stress tests` — block-bootstrapped trade-sequence shuffling, null distribution of Sharpe, probability of ruin (`tpcore/backtest/monte_carlo.py`).
    - `PSR / DSR / MinBTL` (López de Prado) — Probabilistic Sharpe Ratio, Deflated Sharpe Ratio, Minimum Backtest Length (`tpcore/backtest/statistical_significance.py`).
- Score < 60 → engine cannot trade live.
- Transaction cost model: tier-aware round-trip costs from `platform.liquidity_tiers` via `tpcore/backtest/cost_model.py` (T4 fallback ~1.5% for unknowns); 0.05% per-side slippage is the legacy default still used as a fallback.

**Parameter-search pipeline (`tpcore/backtest/search.py` + `scripts/search_parameters.py`):**

Replaces one-off backtest tuning with a systematic, statistically-rigorous edge-discovery loop. The orchestrator imports each engine's `run_for_search` / `load_*_window_context` / `run_*_with_context` programmatically and never shells out. Per-window data load is amortised across all candidates (~60× per-trial speedup after the panel-sharing refactor).

- **Random search** — uniform sampling over engine-specific parameter ranges (defined in `PARAM_RANGES` per engine). Deliberately narrow ranges keep multiple-testing penalties manageable.
- **Walk-forward validation** — N-year train + M-year holdout, advancing annually; default 3y/1y because the engines' continuous-coverage window is 2018-2023 and the final-holdout reserves 2024-2025.
- **Final held-back validation** — never seen during search, touched exactly once. DSR computed on this slice using the total trial count for the multiple-testing correction.
- **Period-aggregated metrics** (`compute_slice_metrics_from_trades`) — trades sharing an `entry_date` are equal-weighted into one portfolio period return before computing Sharpe / drawdown / DSR. No-op for sequential single-position engines; collapses the ~130 concurrent ticker-month trades into ~24 monthly observations for portfolio strategies like Momentum.
- **Universe loader** — `--universe-tier-max N` pulls all tickers with tier ≤ N from `platform.liquidity_tiers`. T1+T2 = ~1,281 names; T1-T3 = ~2,686 names.
- **Verdict** — `SURVIVED` if DSR ≥ 0.95 AND credibility ≥ 60. `FAILED` prints the top 5 alternatives for the next iteration.

DSR ≥ 0.95 is a hard threshold for daily-frequency strategies with 1000+ observations; it is structurally too strict for monthly portfolio strategies with only 24 held-back periods. For low-frequency engines, the held-back portfolio Sharpe + walk-forward consistency are the real signal even when DSR fails.

### 2.6 Fundamentals & Valuation (`tpcore.fundamentals`, `tpcore.valuation`)

- Earnings quality, FCF trend, insider analysis, comps analysis, moat scorecard.
- DCF, Owner Earnings, Buy Bands — for later engines.
- `ThesisTemplate` — every setup must articulate mispricing, catalyst, and thesis killer.

### 2.7 Tax Overlay (`tpcore.tax`)

- `TaxLotTracker` — FIFO lot assignment, cost basis tracking.
- `WashSaleTracker` — 61-day window, cross-engine wash sale detection, basis adjustment.
- `TaxLossHarvester` — daily scan for "probably failing" positions. Auto-harvest during Q4 (capped at $3,000 net loss). Manual mode otherwise. Forces 31-day re-entry block.

### 2.8 Market Calendar & Scripts

- `tpcore.calendar` — NYSE calendar wrapper.
- `tpcore.scripts.check_imports` — blocks forbidden vendor imports.

---

## 3. Platform Database Schema

All tables live in the `platform` schema. The `engine` column discriminates engine-specific rows.

| Table | Purpose |
| --- | --- |
| `platform.aar_events` | Unified after-action reports |
| `platform.execution_quality_log` | Fill quality per order |
| `platform.data_quality_log` | Data source staleness / integrity |
| `platform.parity_drift_log` | Paper vs live fill drift |
| `platform.risk_state` | Risk Governor state per engine |
| `platform.allocations` | (Stub) Future Allocator capital assignments |
| `platform.forensics_triggers` | (Stub) Future Forensics sprint triggers |
| `platform.tax_lots` | Tax lot FIFO records |

---

## 4. Engine Specifications

All engines share the **5-Plug model:** Setup Detection → Lifecycle Analysis → Execution & Risk Scaling → AAR Logging → Capital Gate.

### 4.1 Sigma — Range Scalping Engine (First Build)

**Mission:** Capture mean-reversion within well-defined, low-volatility price channels on a daily timeframe.

**Setup Detection:**
- Universe filter: Price > $10, avg vol > 1M, ADX(14) < 20, **per-stock CHOP(14) > 38.2**, Bollinger Band width < 30th percentile.
- Score (0–100):
  - Channel Quality (0–40).
  - Entry Precision (0–35).
  - Market Context (0–25) = regime-confirmation (0–15) + VWAP-neutrality (0–10):
    - Per-stock CHOP > 38.2 → **10 pts**; +5 more if CHOP > 61.8 → **15 pts** (strong sideways conviction).
    - Last close within ±1% of 20-day VWAP → **10 pts**, else 0.
- Thresholds: ≥ 70 strong, 50–69 weak, < 50 no trade.

**Rationale (CHOP):** ADX alone can produce false range signals — a young trend can sit below ADX 20 while CHOP has already dropped, signalling that price is no longer truly chopping. CHOP is the second confirmation that the range-trade thesis is alive on the *candidate stock*, not the index.

Earlier drafts of this plan gated Market Context on **SPY-level** CHOP+ADX. The backtest in `sigma/backtest.py` (results in `backtests/chop_backtest_results.json`) falsified that design: the SPY-level gate hurt risk-adjusted returns (Sharpe **−28.4%** vs baseline; max drawdown nearly 2× deeper) while the per-stock gate improved them (Sharpe **+26.2%**, baseline +0.28 → +0.36). All 7 trades the per-stock CHOP gate rejected were baseline losers (each hit the −3% stop, see `backtests/rejected_by_chop.csv`) — the rejection set was clean, not a coin flip. The shipped engine therefore uses per-stock CHOP — the candidate's own data — and the SPY-level path was removed.

**Known weakness — transitional regimes:** When the market is neither cleanly chopping nor cleanly trending (CHOP 38.2–61.8 on the stock, mixed ADX trajectory), Sigma still issues entries that are vulnerable to follow-through breakouts. 2023 was such a year (per-stock-CHOP variant: 25 trades, 18 stop-outs, −27% total return). **Defense for transitional regimes is the position sizing rules and `tpcore.risk.RiskGovernor` — not CHOP.** Specifically: the pre-grad $1,500 cap, the 5% daily / 10% weekly P&L kill switches, and the platform-wide 60% net-long exposure cap are what bound the worst-case loss in a regime that range-scalp can't read.

**Lifecycle Analysis:**
- Phases: Setup, Approaching, Active, Exhaustion.
- Band-ride detector: price closes outside band 2 days → exit.
- Max hold: 3 days without reaching mid-band.

**Execution & Risk:**
- Entries: Market at next open after signal.
- Exits: Sell 50% at mid-band, remaining at opposite band. Hard stop −3%.
- Sizing: Pre-grad cap $1,500, max 4 concurrent positions.
- Bracket orders: take-profit limit + stop-loss.

**AAR Logging:** via `tpcore.aar.AARWriter`.

**Capital Gate:** Hard cap enforcement, graduation (50 trades, 65% win rate, avg return ≥ 1.5%).

**Status (built; Railway paused, runs locally):**
- All five plugs implemented and tested. Scheduler entry: `sigma/scheduler.py`. The Railway service `sigma-scheduler` exists but is unscheduled (no cron, no auto-restart); engine runs are invoked locally during the Railway pause.
- CHOP filter validated by backtest — per-stock variant improves Sharpe by 26%; SPY-level variant falsified and removed.
- Backtest: `sigma/backtest.py` (tier-aware costs from `platform.liquidity_tiers` as of 2026-05-12). Overfitting report: `backtests/sigma_overfitting_report.json`.
- Overfitting diagnostic score **55/100 — BLOCKED**. Extended-window runs show MinBTL gap + DSR deflation as the primary failure modes.
- **Parameter-search verdict (T1+T2, 50 trials × 3 walk-forward windows, 2026-05-12):** held-back 2024-2025 Sharpe **+0.740**, profit factor **+3.71**, max drawdown -8.1%. Walk-forward top-5 all positive across all 3 windows. DSR fails on multiple-testing correction (50 trials × ~150 trades is too few to clear deflation). Marginal real edge, kept as research-only.

### 4.2 Reversion — Mean Reversion Engine (Second Build)

**Mission:** Fade statistically extreme price deviations on a multi-day horizon.

**Setup Detection:**
- Z-score vs 20-MA < −2 (oversold) or > +2 (overbought), RSI extremes, volume climax.
- Fade Score: Statistical Extremity (0–45), Exhaustion Confirmation (0–30), Market Context (0–25).
- Thresholds matched to Sigma.

**Lifecycle Analysis:**
- Monitors reversion progress toward 20-MA.
- Time stop: 5 days without reversion.
- ADX gate: if ADX > 25, engine disabled (trending market).

**Execution & Risk:**
- Entries at market open after signal.
- Targets: Close 75% at 20-MA, remaining at 50-MA.
- Hard stop: −8%.
- Must pass `tpcore.fundamentals.EarningsQualityCheck` — if LOW, trade suppressed.

**Phase 2 enhancement (deferred):** Refine the ADX > 25 shutdown by combining with CHOP — a high-ADX *and* high-CHOP regime (volatile chop, not a clean trend) is the worst environment for fading because reversion to the mean keeps overshooting in both directions. Concretely: if ADX > 25 AND CHOP > 61.8, suppress entries even if Statistical Extremity flags. Not implemented in Phase 1; revisit after Reversion has paper-traded for ≥ 30 trades.

**Earnings-quality gate backtest + combined-filter validation:** `reversion/backtest.py` (results in `backtests/earnings_quality_backtest.json`) compares three variants on the 2018-01-01 → 2025-12-31 window over the 47-name funded universe (FMP Starter, 1,790 quarterly rows). The third variant — the *combined-filter* — was set after `reversion/diagnose_backtest.py` showed HIGH-grade trades and \|Z\|≥3 trades were each individually profitable while the other buckets weren't.

| Metric | Baseline (z≥2.0, no EQ) | Gated (z≥2.0, reject LOW) | **Combined-filter** (z≥3.0, require HIGH) |
| --- | --- | --- | --- |
| Trades | 61 | 34 | **11** |
| Win rate | 42.6% | 41.2% | **54.5%** |
| Avg return / trade | −0.74% | −0.68% | **+2.08%** |
| Sharpe (annualized) | −0.42 | −0.28 | **+0.63** |
| Max drawdown | −55.9% | −33.7% | **−6.1%** |
| Profit factor | 0.74 | 0.76 | **3.69** |

**Conclusion — combined-filter validated; live engine updated.** Tightening to `Z_SCORE_THRESHOLD = 3.0` and `EarningsQualityGrade is HIGH` flips the strategy from a money-loser to profitable: Sharpe swings from −0.42 to +0.63, max drawdown drops from −55.9% to −6.1%, profit factor 0.74 → 3.69. The trade count drops to 11 over 8 years (~1–2 per year) — sparse but high-quality. Live changes: `reversion/models.py:Z_SCORE_THRESHOLD` is now 3.0; `reversion/plugs/lifecycle_analysis.py` blocks any grade that isn't HIGH (was: blocked only LOW). Phase 2 enhancement (ADX > 25 ∧ CHOP > 61.8) remains deferred until ≥30 paper trades accumulate under the new thresholds — at the new firing rate that's a multi-year horizon, so the next concrete step is to monitor live performance and revisit only if real fills diverge from the backtest.

**Graduation:** 10 completed trades, win rate ≥ 55%, avg return ≥ +2%, profit factor > 1.5.

**Rationale:** Reversion fires infrequently by design — extremes occur a few times per year across a liquid universe. The win rate and average return bars are calibrated to the backtest results (54.5% / +2.08%). The trade count is set at 10 to balance statistical confidence against the engine's natural firing rate; requiring ~7 years of data ensures graduation is achievable within a reasonable timeframe while still demanding enough trades to avoid single-trade luck. If the live engine's firing rate differs materially from the backtest, re-evaluate the graduation trade count after 2 years of live paper data.

**Status (built; Railway paused, runs locally):**
- All five plugs implemented and tested. Scheduler entry: `reversion/scheduler.py`. The Railway service `reversion-scheduler` exists but is unscheduled during the Railway pause; engine runs are invoked locally.
- Earnings-quality gate validated; combined filter (HIGH quality + |Z| ≥ 3.0) applied in `reversion/models.py` and `reversion/plugs/lifecycle_analysis.py`.
- Backtest: `reversion/backtest.py` (tier-aware costs as of 2026-05-12). Overfitting report: `backtests/reversion_overfitting_report.json`.
- Overfitting diagnostic score **45/100 — BLOCKED**.
- **Parameter-search verdict (T1+T2 with EQ filter dropped, 50 trials × 3 walk-forward windows, 2026-05-13):** walk-forward top-5 all scored +0.39 to +2.87 across all 3 windows (strong in-sample), but held-back 2024-2025 collapsed to Sharpe **-0.080**, profit factor 0.87. **Classic overfit signature** — the pipeline caught it. Strategy doesn't generalise on the wider universe; DSR=0 correctly rejected the winner. Reversion is shelved pending either (a) a different signal class or (b) the FMP fundamentals coverage expanding far enough to make the original EQ-gated path testable on T2+.

### 4.3 Vector — Momentum Swing Engine (Third Build)

**Mission:** Capture multi-day momentum in fundamentally-backed, catalyst-driven stocks.

**Setup Detection:**
Three-gate model:
1. Value & Quality pre-screen (P/B, D/E, revenue).
2. Catalyst NLP (EDGAR, contracts).
3. Technical trigger (pullback or breakout).

Swing Score: Technical (0–40), Catalyst (0–35), Sentiment (0–25). Thresholds ≥ 65.

**Crash Guard (mandatory):**
- Volatility-scaled sizing: VIX > 25 → 50% size, VIX > 30 → 25% size.
- Trend confirmation (CHOP): require SPY CHOP(14) < 38.2 to confirm a strong directional regime — momentum strategies need a market that's actually trending, not chopping. If CHOP ≥ 38.2 (transitional or sideways) but the setup otherwise triggers, the trade still enters but at **50% size with a warning flag** in the AAR. CHOP < 38.2 → full size; the lower CHOP is, the stronger the trend conviction.
- Post-drawdown cooldown: SPY −10% in 20 days & rebounding → no new entries for 10 days.
- Engine-level circuit breaker: −10% rolling 20-day P&L → freeze for 10 days.

**Execution & Risk:**
- Entries at market open. Hard stop −7%. Profit target +15% or trailing stop after +10%.
- Sizing pre-grad $2,000. Max 5 concurrent positions.

**Backtest results — extended window 1995-01-01 → 2025-12-31:** `vector/backtest.py` (44-name universe, with 1,622 PIT-safe `pb`/`de` rows in `fundamentals_quarterly` and 683 `EARNINGS_BEAT` rows in `catalyst_events`):

| Metric | Value |
| --- | --- |
| Trades | 11 |
| Win rate | 45.5% |
| Avg return / trade | −0.26% |
| Sharpe (annualized) | −0.05 |
| Max drawdown | −13.1% |
| Profit factor | 0.91 |

The 1995-pushed `--start` doesn't change the trade count: bars are present back to 1994 (Tradier merge) but `fundamentals_quarterly` only goes ~10 years back (FMP Starter) and `catalyst_events` starts 2018-01-24 (FMP coverage). Pre-2018 sessions have nothing to gate on. Actual usable window is still 2018-2025.

**VIX-aware crash-guard sizing — implemented.** Plan §4.3's volatility-scaled sizing now fires from a SPY 20-day realized-volatility proxy computed off `platform.prices_daily` (annualized std × √252, expressed as %). Per-trade `size_factor` is 1.0 (default) / 0.5 (RV > 25) / 0.25 (RV > 30) and multiplies `return_pct` so the equity curve and Sharpe reflect the reduced exposure during high-vol regimes. The proxy is also written to each TradeRecord as `rv20_at_entry_pct` for diagnostics. (The CHOP-based trend-confirmation cut from §4.3 is still deferred to a follow-up — it requires a SPY-CHOP feed Vector doesn't yet read.)

**Overfitting verdict — `tpcore.backtest.overfitting.OverfittingDiagnostic` with `n_trials = 30`:** report saved to `backtests/vector_overfitting_report.json`.

* Sensitivity surfaces FLAT on both knobs (PB flatness 0.089, DE flatness 0.143 — well below 0.20). The strategy isn't on a knife-edge.
* PSR(SR > 0) = 0.452, DSR (deflated for 30 trials) = 0.016. The strong deflation collapses any candidate edge once we account for the gate combinatorics.
* MC sequence test: observed Sharpe at the 65th percentile of the bootstrap null (threshold ≥ 90% to claim signal). Probability of ruin 0%.
* MinBTL effectively infinite (Sharpe ≤ 0 — no length suffices to call this real).
* Trades-per-parameter ratio = 1.6 (11 trades / 7 parameters). The diagnostic flags this as the dominant problem: there isn't enough trade evidence relative to the parameter search space to conclude anything either way.

**Credibility (with overfitting bundle): 45/100 — BLOCKED.** Persisted to `platform.data_quality_log` as `backtest_credibility.vector`.

**Primary cause:** thin trade count relative to the parameter search space. The catalyst gate fires only ~once every 1-2 quarters per ticker, and most of those don't co-occur with a Gate-3 technical trigger inside the 5-day window. **Mitigation paths**, in order of cost:
1. **Extend paper-trading window** — let the live engine accumulate live trades; revisit when ≥ 30 trades exist on the live tape.
2. **Broaden the universe** — the 44-name funded universe is a small cross-section of the market; expanding to the next 50–100 liquid names with available fundamentals could roughly double the firing rate without changing the strategy.
3. **Lock parameters as-is** — don't tune. The sensitivity surface is already flat, so any "improvement" from PB/DE tweaks is curve-fitting noise on 11 trades.

The infrastructure is correct; the strategy needs more evidence before the gate will let it graduate.

**Status (built; Railway paused, runs locally):**
- All five plugs implemented and tested. Scheduler entry: `vector/scheduler.py`. The Railway service `vector-scheduler` exists (service ID `6498df68-0a23-4531-85df-f54ba37a1c40`) but is unscheduled during the Railway pause; engine runs are invoked locally.
- Catalyst proxy via FMP `EARNINGS_BEAT` events (683 events across **44 tickers**, 2018–2025) populated in `platform.catalyst_events`. **Critical coverage gap: zero of those 44 tickers are in liquidity_tiers T1+T2.** Vector is therefore untestable on the wider universe until catalyst_events is backfilled. Fundamentals ratios `pb`/`de` backfilled to 152,907 PIT-safe rows across 5,981 tickers in `platform.fundamentals_quarterly` — but fundamentals alone aren't enough; the catalyst-event coverage is the binding constraint.
- VIX-aware crash-guard sizing implemented and verified end-to-end in the backtest (1.0× / 0.5× / 0.25× via SPY 20-day realized-vol proxy).
- Backtest: `vector/backtest.py` (tier-aware costs as of 2026-05-12). Overfitting report: `backtests/vector_overfitting_report.json`. Score **45/100 — BLOCKED**.
- **Parameter-search verdict (T1+T2, 50 trials × 3 walk-forward windows, 2026-05-13):** zero trades on every candidate due to the catalyst-event coverage gap. **Vector is data-blocked, not strategy-blocked.** The strategy cannot be evaluated on this universe until `platform.catalyst_events` is expanded beyond the original 44-ticker set. Re-enabling Vector is gated on a one-time data-ingestion backfill (catalyst events for T1+T2 tickers from FMP earnings-history endpoint), not on any strategy work.

### 4.4 Momentum — Cross-Sectional 12-1 Engine (Fourth Build)

**Mission:** Long-only cross-sectional momentum on a liquid US-equities universe; capture the persistent 12-1 month return-of-returns premium documented in 50+ years of academic literature (Jegadeesh-Titman 1993, Asness-Moskowitz-Pedersen 2013).

**Rationale for adding a fourth engine:** Sigma and Reversion are mean-reversion bets; Vector is catalyst-driven momentum but data-blocked (zero catalyst-event coverage on T1+T2 universe). 2024-2025 has been a momentum-favouring regime, so the existing three-engine bench is regime-mismatched. A regime-matched, simple, well-understood factor engine is the cleanest fourth slot.

**Setup Detection:**
- Universe: T1+T2 from `platform.liquidity_tiers` (~1,281 names with continuous bar coverage over the lookback window). No fundamentals or catalyst-events required.
- Signal: 12-1 month total return, `score(ticker, t) = price(t-skip) / price(t-skip-lookback) - 1`. Default skip=21 trading days, lookback=231 (≈12-1 calendar months).
- Rank survivors; take the top decile (default top 10%).

**Lifecycle Analysis:** Hold to next monthly rebalance. No early exits in Phase 1 (no per-name drawdown circuit breaker, no position-level stop). Phase 2 adds a portfolio-level drawdown circuit breaker (pause new entries when portfolio is > 10% off rolling peak).

**Execution & Risk:**
- Entry: next bar's open after rebalance signal × (1 + tier-aware slippage).
- Exit: bar `hold` days later at close × (1 − tier-aware slippage).
- Equal-weight within the decile (~130 positions). Per-name cap and sector cap deferred to Phase 2.
- Tier-aware round-trip cost via `tpcore.backtest.cost_model.get_round_trip_cost` (already wired).

**Phase 1 backtest results (T1+T2, 50 trials × 3 walk-forward windows, period-aggregated metrics):**

| Metric | Walk-forward (top-5 avg) | Held-back 2024-2025 |
|---|---|---|
| Annual Sharpe | +0.07 to +0.32 | **+1.583** |
| Profit factor | n/a | **+2.796** |
| Max drawdown | n/a | -32.4% (geometric, real) |
| Trades | per-window 200-700 | 1,841 ticker-months → ~24 monthly periods |
| Top-5 evaluated in 3/3 windows | yes — parameter cluster: lookback 200-220, skip 22-30, hold 28-30, decile 0.06-0.18 | |
| Credibility | — | 40/100 |
| DSR (50 trials × 24 periods) | — | 0.0000 |

**Verdict — Phase 1 CONTINUE.** The held-back Sharpe +1.58 / PF +2.80 is the strongest OOS signal across all four engines. The DSR=0 is a structural limitation of López de Prado's deflation at 50 trials with only 24 monthly observations — not a strategy failure. Walk-forward consistency (top-5 all positive across 3/3 windows, tight parameter cluster) is the cleaner evidence of edge.

**Known data caveats:**
1. `platform.prices_daily` is partially-survivorship-clean (~99% of tickers have bars through 2025 vs ~93-95% expected). Major 2023 delistings SIVB / WeWork / Credit Suisse are missing; BBBY shows post-bankruptcy ticker-reuse data as continuous. Walk-forward 2018-2023 is upward-biased; held-back 2024-2025 less so (most major delistings were 2023, so 24-25 universe is mostly real survivors). The `survivorship_inclusive` rubric flag is honestly set False for the momentum credibility score.
2. The DSR ≥ 0.95 threshold is calibrated for daily-frequency strategies (1000+ obs); monthly portfolio strategies with 2 years of held-back history cannot pass it regardless of strategy quality. PSR or a frequency-adjusted DSR threshold is appropriate.

**Status — Phase 2 shipped (2026-05-13):**
- `momentum/backtest.py` — exposes `load_momentum_window_context()` + `run_momentum_with_context()` matching the panel-sharing pattern used by the other engines. CLI supports `--json` / `--trade-log` / parameter overrides.
- Search wired in `scripts/search_parameters.py` (PARAM_RANGES["momentum"] = 4 narrow knobs: lookback_days 200-280, skip_days 15-30, hold_days 15-30, top_decile_pct 0.05-0.20).
- **5 plugs complete**: `momentum/plugs/{setup_detection,lifecycle_analysis,execution_risk,aar_logging,capital_gate}.py`. Session questions route through `tpcore.calendar` per CLAUDE.md convention.
- **Scheduler complete**: `momentum/scheduler.py` orchestrates the monthly rebalance via `AlpacaPaperBrokerAdapter`. Day-market orders only (no brackets — Momentum doesn't use per-name stops). `--force-rebalance` flag overrides the first-trading-day check for kickoff and emergency mid-month rebalances. `--dry-run` flag previews without submitting.
- **Paper-trading kickoff scripted**: `scripts/run_momentum_kickoff.sh` does the one-shot force-rebalance from the validated T1+T2 universe. Verified end-to-end against the live DB on 2026-05-13: produced 55 orders (1 close + 54 opens, ~$985/position on a $99,989 paper account).
- **Looser graduation thresholds than Sigma/Reversion/Vector**: 6 rebalances (≈6 months), Sharpe ≥ 1.0, PF ≥ 1.5. Monthly cadence accrues fewer events per unit time.

**Phase 2.5 status (2026-05-13):**

| # | Item | Status |
|---|---|---|
| 1 | Common-stock-only filter + $5 min-price floor | **✓ Shipped** (commit bf0c5d2) — applied in both `setup_detection.scan` and `backtest._compute_one_rebalance` so live and backtest agree |
| 2 | DBLogHandler wiring → SIGNAL + ORDER_SUBMITTED + EQUITY_SNAPSHOT rows in `application_log` | **✓ Shipped** (commit fa4dcbc) |
| 3 | Drawdown circuit breaker — pause rebalance when portfolio > 10% off 60-day rolling peak | **✓ Shipped** — `MomentumCapitalGate.check_drawdown`, queried from `application_log` EQUITY_SNAPSHOTs |
| 4 | Sector concentration cap | **Deferred** — needs a `platform.ticker_classifications` table + ingestion handler; design note in `docs/superpowers/specs/2026-05-13-tip-sheet-plan.md` |
| — | Trade-monitor integration | **Not required** — Momentum doesn't use per-name stops, so reactive monitoring isn't on the critical path |

End-to-end smoke (`scripts/run_momentum_smoke.sh`): plug unit tests → scheduler dry-run → tip-sheet render. Used as the canonical 'did anything break?' gate before kicking off real rebalances.

**Phase 3 — Rolling-portfolio construction (scoped, no code yet):**

The current monthly all-at-once rebalance matches Jegadeesh-Titman's *paper* presentation. Real production momentum funds (AQR, AlphaArchitect's MOM ETF) instead use the **overlapping rolling-portfolio** construction: each position carries its own ~21-day timer, the ~1/21 of positions that age out each day are rotated to fresh top-decile names, no synchronized rebalance day. Mathematically equivalent in expected return; smoother turnover; faster response to new top-decile entrants.

Two variants — pure timer-rolling (recommended for first build) vs score-decay exit (AQR-style, higher turnover). Validation gate: held-back Sharpe must come within 0.2 of monthly's +1.58, with profit factor and drawdown also competitive. Migration plan runs both schedulers in parallel on sibling paper accounts for ≥60 trading days before retiring monthly.

Full design: `docs/superpowers/specs/2026-05-13-momentum-rolling-construction.md`. **Phase 3 work is NOT in scope until the running monthly paper experiment has at least one full rebalance cycle (June 1, 2026).**

### 4.5 S2 — Short Squeeze Engine (Fifth Build, Satellite)

**Mission:** Detect conditions conducive to short squeezes. Satellite only — permanent 5% capital cap.

**Setup Detection:**
- Layer 0: Short interest > 20% (FINRA, release-date matched), days-to-cover > 5, borrow rate acceleration.
- Layer 1: Social volume spike (ApeWisdom).
- Layer 1.5 (deferred wiring): options-derived signals — IV skew, put/call ratio, gamma-weighted strike concentration. Source data already on disk: `platform.tradier_options_chains` (122,668 contracts across 51 tickers, snapshot 2026-05-10 from the Tradier production API right before that brokerage account closed). Top-15 liquid names will drive the live signal; the rest are reference. **No live S2 consumption yet — data is parked.**
- Squeeze Score ≥ 50 (pre-chat), ≥ 60 (social alert).

**Data Limitations:**
- FINRA data is bi-monthly with 2-week lag. Used for regime detection only — live entry triggered by real-time borrow rates and social signals.

**Lifecycle Analysis:**
- Phases: Accumulation → Breakout → Spike → Exhaustion.
- Exit trail: Tier 1 (close 10% below 5-day high, sell 50%), Tier 2 (close 20% below, sell remaining).
- Instant profit trail at +100% unrealized gain.

**Execution & Risk:**
- Hard stop −7%. Max hold 15 trading days.
- 30-day ticker re-entry lock.

**Graduation:** 5 trades / 6+ months, win rate ≥ 60%, avg return ≥ 30%.

**Status:** Specification only — no engine code. Options data parked: `platform.tradier_options_chains` (122,668 contracts across 51 tickers) is loaded but no plug consumes it.

### 4.6 Catalyst — Event-Driven Engine (Sixth Build)

**Mission:** Capture post-event drift from earnings surprises and contract awards. No binary events, no options.

**Setup Detection:**
- Event Score: Event Quality (0–45), Asymmetry (0–30), Market Context (0–25).
- Only scheduled events: earnings beats with raised guidance, material government contract awards.
- Pre-event anticipation removed — post-event reaction only.

**Entry Gate:**
- Stock must be above 200-day SMA.
- Event ≤ 2 trading days old (staleness gate).

**Execution & Risk:**
- Entry at market open after breakout from pre-event range.
- Hard stop −10%. Profit target event-specific.
- Max 10% of total platform capital allocated to Catalyst.

### 4.7 Sentinel — Macro Defense Engine (Seventh Build)

**Mission:** Protect the platform during recessions. Reformed basket with minimal decay.

**Composition (non-leveraged dominant):**

| Symbol | Weight | Notes |
| --- | --- | --- |
| SH | 35% | |
| PSQ | 25% | |
| TLT | 20% | |
| GLD | 10% | |
| SQQQ | 10% | Tactical, 5-day max hold |

**Activation:**
- Bear Score ≥ 60 for 3 consecutive days with no counter-trend rally > 5%.
- Bear Score 60–79 → allocate up to 10% of platform capital.
- Bear Score 80+ → allocate up to 20%.
- Permanent maximum: 20% of platform capital.

**Safety Overrides:**
- Shallow recession override (Bear Score < 80): reduce SH/PSQ by 50%, increase TLT/GLD.
- VIX > 40: reduce inverse ETFs by 50% (compounding drag spikes).
- SH/PSQ re-evaluated every 30 calendar days.

### 4.8 Research Tools

Non-engine tooling that consumes engine outputs (credibility scores, AARs, signals) to support operator review. Strictly internal — research, not product.

**Operator Dashboard (`dashboard.py`):**
- **Phase 1 (in scope, building now):** single-page local Streamlit web UI replacing the 8 separate shell scripts the operator runs daily. Read-mostly view (header, holdings, equity curve, credibility scorecards, signals + AARs, today's recommendations) + action buttons (daily update, force-rebalance, refresh credibility, smoke test, cancel-all-orders).
- **Chart library**: `streamlit-lightweight-charts-pro` (TradingView Lightweight Charts wrapper) — only Streamlit-compatible option with first-class trade-marker API. Wrapped in a one-file adapter so Plotly fallback is a 1-file swap if maintenance becomes an issue.
- **Subprocess pattern**: short scripts blocking (`subprocess.run`); long scripts (`run_daily_update.sh` 30-45 min) detached via `Popen(start_new_session=True)` + logfile tail so Streamlit worker recycles don't SIGTERM the job.
- **HCI**: typed-confirmation modals on destructive actions, heartbeat indicators on detached jobs, data-freshness timestamps per panel, keyboard shortcuts (`r` refresh, `Esc` modal), accessibility (color + glyph, never color alone).
- **NOT in scope**: order entry (dashboard dispatches scripts, never submits orders directly), public exposure (localhost binding only), authentication (single operator on personal Mac).
- Full design: [`docs/superpowers/specs/2026-05-13-operator-dashboard.md`](superpowers/specs/2026-05-13-operator-dashboard.md). Sequenced **before** Rolling-Momentum (Phase 3) — see that spec for the prioritization rationale.

**Tip Sheet (`scripts/generate_tip_sheet.py`):**
- **Phase 1 — Private operator review tool (in scope).** Terminal-only report per engine: layman description, credibility-rubric breakdown, recent signals from `platform.application_log`, recent trade outcomes from `platform.aar_events`. Credibility gate (≥ 60) enforced by default; `--force` flag permits private review of unproven engines. Mandatory non-removable disclaimer printed on every output. **No public distribution. No web endpoint. No file output.**
- **Phase 2 — Gated publication (deferred).** Adds `--publish` flag. Prerequisites: an engine with credibility ≥ 60 AND ≥ 30 documented paper trades AND disclaimer reviewed by a securities attorney. `--force` is *removed* in `--publish` mode.
- **Phase 3 — Multi-engine roll-up (deferred).** Cross-engine summary view. Prerequisites: two-plus engines have passed Phase 2.

Full design and rationale: [`docs/superpowers/specs/2026-05-13-tip-sheet-plan.md`](superpowers/specs/2026-05-13-tip-sheet-plan.md). Phase 2 / 3 publication gates also tracked in `docs/EDGE_VALIDATION_PLAN.md` as a Phase-4 follow-up to credibility validation.

---

## 5. Platform Services

Status as of 2026-05-14:

- **Allocator** — **built + deployed** (`tpcore/allocator/`). Inverse-volatility weighting with [0.10, 0.50] caps, soft-freeze at 15% drawdown, hard-freeze at 25%. Runs Mondays 13:00 UTC via the `com.michael.trading.allocator` launchd daemon. Bootstrap mode (equal weights) until each engine has ≥20 AARs. Reads engine pnl history via `tpcore.aar.AARReader`.
- **Forensics** — **built + wired into data-operations** (`tpcore/forensics/`). Scans every engine's AAR history for drawdown periods (≥10% / ≥14 days), loss clusters (≥3 consecutive losers), and outlier losses (>3σ below the mean of ≥5 historical trades). Idempotent via fingerprint. On each new trigger, auto-generates a Sprint Dossier template under `docs/sprints/` so the operator has a structured postmortem to fill in. Dashboard's Health tab surfaces open triggers with a "Mark resolved" button. Runs as the final step of `scripts/run_data_operations.sh`.
- **Settlement** — Deferred. Annual distribution (75% to operator, 25% retained). Produces Schedule D-ready tax CSV. Will be built after the first live-trading cycle completes.

---

## 6. Data Architecture

Full database schema and data flow documentation: [`docs/DATABASE_AND_DATAFLOW.md`](DATABASE_AND_DATAFLOW.md).

### 6.1 Live / Production Stack

| Source | Purpose | Cost |
| --- | --- | --- |
| Alpaca (IEX free) | Daily bar **ingest** (→ `platform.prices_daily`), quotes, execution, delisted stock data. Engines read bars from the DB, not from Alpaca live. | $0 (real-time upgrade gated on `ExecutionQualityScore` evidence — see §6.5) |
| FMP **Starter** ($22/mo, active) | Fundamentals, insider, earnings | $22 (Premium $59/mo deferred — see §6.5) |
| Railway **Hobby** ($5/mo, active — currently paused) | Cron schedulers (6 services). Auto-deploys disabled 2026-05-12; all daily ops run locally for now. | $5 |
| Supabase **Pro** ($25/mo, active) | Postgres + pooler. Upgraded 2026-05-11 from free tier after `prices_daily` crossed the 500 MB read-only lock; 8 GB disk gives headroom for the all-active universe. | $25 |
| SEC EDGAR | Form 4 (insider transactions) + 8-K (material events) via `tpcore.sec.SECEdgarAdapter` → `platform.sec_insider_transactions` + `platform.sec_material_events`. Public API, no key — requires `SEC_EDGAR_USER_AGENT` env var per SEC fair-access. Integrated 2026-05-14 (reference implementation of the standard 5-stage data-adapter pipeline). | $0 |
| ApeWisdom | Social sentiment *(spec-only; no adapter code as of 2026-05-14)* | $0 |
| FRED | Macro indicators *(spec-only; no adapter code as of 2026-05-14)* | $0 |
| FINRA / NASDAQ | Short interest (release-date matched) *(spec-only; no adapter code as of 2026-05-14)* | $0 |
| IBorrowDesk | Borrow rates (scraped, fragile) *(spec-only; no adapter code as of 2026-05-14)* | $0 |

**Total fixed monthly cost: $52** (FMP Starter $22 + Railway Hobby $5 + Supabase Pro $25).

### 6.2 Historical / Backtesting Database (Self-Built)

- Alpaca free tier → survivorship-free daily bars (delisted stocks included).
- Tradier historical export → pre-2020 daily bars merged into `platform.prices_daily` (Tradier brokerage account closed; data extracted before closure).
- FMP Starter → quarterly fundamentals, with `pb`/`de` ratios computed via `scripts/compute_fundamental_ratios.py`.
- FMP earnings-beats → `platform.catalyst_events` (Vector's catalyst proxy).
- Self-built corporate-actions pipeline (Alpaca free endpoint) → `platform.corporate_actions` with split + dividend records; AAPL split adjustment verified.
- Built in Phases 0–4. See §6.4 for current row counts and §6.5 for upgrade triggers.

### 6.3 Data Quality Gates

- `DataValidationSuite` — three correctness checks against `platform.prices_daily`: delistings, S&P 500 constituent snapshot, split verification.
- Invoked locally as `python -m tpcore.quality.validation`. Previously scheduled as the Railway Sunday cron `validation-scheduler`; that service was consolidated into the persistent `ingestion-engine` via `platform.ingestion_jobs` (and the engine is currently paused alongside Railway — operator runs the check on demand).
- Capital Gate hook: `tpcore.quality.validation.capital_gate.assert_passed(pool, max_age_days=7)` is consulted by every engine's `assert_can_graduate`. No engine graduates from paper to live without a fresh passing run.
- Design spec: `docs/superpowers/specs/2026-05-10-data-validation-suite-design.md`.
- Current state: **all three checks pass** (delistings 8/8, constituent 58/58, splits 10/10). Five historic delisted tickers (HTZGQ, WLLBQ, LK, SBNYQ, SI) were removed from `delistings.yaml` and `constituents.yaml` on 2026-05-10 after a definitive audit confirmed neither Alpaca free tier nor the Tradier export carries bars for them — they are unresolvable on free-tier data. Re-add the entries when a paid delisted-feed (EODHD survivorship-free, Norgate, or Polygon w/ delisted) is provisioned.

### 6.4 Current Data Infrastructure Status

Verified row counts and coverage (audited 2026-05-14, post-data-layer normalization + catalyst backfill):

| Table | Rows | Notes |
| --- | ---: | --- |
| `platform.prices_daily` | 20,654,889 | **7,694 distinct tickers**, 1994-07-21 → 2026-05-13, survivorship-free (Alpaca SIP `all_active` sweep + Tradier wide-export merge). Default feed switched IEX→SIP 2026-05-13. |
| `platform.fundamentals_quarterly` | 178,608 | **5,984 tickers**, PIT-safe via FMP Starter (~30 quarters/ticker mean). `pb` + `de` populated on 152,907 rows; remaining NULLs are explainable (negative book value, no price on filing date, missing fields). |
| `platform.corporate_actions` | 109,413 | **217 tickers with splits + 3,848 tickers with dividends**. Handler now retries via `@with_retry` (fixes the 2026-05-12 Alpaca-429 cron failure). AAPL split fix verified. |
| `platform.tradier_options_chains` | 122,668 | 51 tickers, snapshot from May 2026 (immediately before the Tradier brokerage account closed). Frozen — parked for future S2. |
| `platform.catalyst_events` | 1,350 | **137 tickers**, `EARNINGS_BEAT` type, 2018–2025. Recurring weekly refresh active via `ops.py --update` `catalyst_refresh` stage (skip-guard: 6-day freshness). Vector engine unblocked. |
| `platform.ticker_classifications` | 13,669 | Asset-class taxonomy (`stock` / `etf` / `spac` / `fund`) + ETF leverage/inverse/category flags for the sentinel engine. Backfilled from Alpaca `/v2/assets` + name-pattern classifier (2026-05-14). |
| `platform.sec_insider_transactions` | 0 → populated on first `sec_filings` ops-stage run; historical backfill from 2018-01-01 documented in `docs/OPERATIONS.md §7.5`. Multi-hour wall time at SEC's 10 req/sec courtesy budget. | Form 4 insider BUY/SELL transactions parsed from SEC EDGAR. T1+T2 stock universe. |
| `platform.sec_material_events` | 0 → populated alongside `sec_insider_transactions` on the same first run. | 8-K material events (one row per item code from the submissions index). T1+T2 stock universe. |
| `platform.data_quality_log` | active | Receives rows from the Data Validation Suite, execution-quality tracker, and engine credibility scorer. |
| `platform.aar_events` | 0 | Schema + writer implemented; populated by live paper trades once they fire. |
| `platform.risk_state` | 1 | Postgres-backed Risk Governor persistence active. |
| `platform.application_log` | active | `DBLogHandler` writes `STARTUP` / `SHUTDOWN` / `INGESTION_*` / `UNIVERSE_SIMULATION` / `SMOKE_ORDER_*` events. 7-day rolling retention enforced per-write. |

All sources free-tier or FMP Starter ($22/month). Hosting on Railway Hobby ($5/month, currently paused) + Supabase Pro ($25/month). Total fixed monthly cost: **$52** (see §6.1). No `yfinance`. The Tradier brokerage account is closed; the options-chain and pre-2020 bar export was completed before closure.

### 6.5 Data Upgrade ROI Gates

Triggers for paid-tier upgrades. The default posture is to stay on the current $52/mo stack (FMP Starter + Railway Hobby + Supabase Pro) until the Parity Harness or Overfitting Diagnostic produces measured evidence that an upgrade's marginal benefit exceeds its cost.

| Trigger | Threshold | Upgrade |
| --- | --- | --- |
| `ExecutionQualityScore` shows realized-slippage cost > Alpaca real-time subscription cost | After 3 months of paper-trading fills are logged | Alpaca Algo Trader Plus ($99/mo) |
| Overfitting credibility scores stay below 60 and 6–12 months of additional live trades fail to close the MinBTL gap | After full paper-trading phase for the affected engine | FMP Premium ($59/mo) |
| Tradier-era price history + FMP Starter fundamentals gap prevents pre-2018 backtesting while the overfitting suite still demands deeper samples | Same trigger as above | FMP Premium |

Current decision: **stay on FMP Starter and Alpaca free**. The overfitting diagnostic is doing its job (every engine fails on trade count, not on shape of edge), so buying more historical depth is the correct lever — but only after the live tape has had a fair chance to add evidence on the cheap.

---

## 7. Tax Overlay

- `TaxLotTracker` records every purchase.
- `WashSaleTracker` prevents cross-engine wash sales.
- `TaxLossHarvester` daily scans "probably failing" positions. Auto-harvests within $3,000 net loss cap during Q4.
- Settlement module generates annual Schedule D CSV.

---

## 8. Platform Operations & Safety

- **Autonomous operations posture (2026-05-14, audit-verified):** every recurring data-layer action is scheduled — 13 stages in `scripts/ops.py:_STAGE_SPECS`, fired daily by `run_data_operations.sh` (launchd 21:30 UTC). 18 dashboard probes flag drift across stages, validation suite, risk governor, managed-service backups (`supabase_backup`), launchd misfires (`missed_data_operations`), disk space, and trade-monitor liveness. Failure paths fire macOS notifications. Capital gate's `EXPECTED_SOURCES` derives from the suite's `KNOWN_CHECK_NAMES` (audit-fix D3-1), so adding a check automatically makes it required for engine graduation. Operator's only remaining recurring duty is reviewing the dashboard; one-time SEC backfill is a single self-verifying command (`--stage sec_filings --backfill`). See `docs/superpowers/pipelines/data_adapter_pipeline.md` for the 5/5 compliance matrix.
- **Kill Switch:** Emergency button → `RiskGovernor.emergency_kill()` → cancels all orders, flattens positions. **Two-layer enforcement:** every engine's `submit_decision` calls `RiskGovernor.check_trade()` (which returns `BLOCK` if `kill_switch_active`); each scheduler also short-circuits at startup before scanning candidates, so a frozen engine consumes zero FMP / Alpaca / DB calls. Verified by `scripts/test_kill_switch.py`.
- **Cumulative Exposure Cap:** Net long ≤ 60% of platform capital.
- **Vacation Mode:** Pauses new entries; exits remain active.
- **Broker Outage Protocol:** Backup manual login path. No secondary automated broker.
- **Performance Benchmark:** SPY total return (Sharpe ratio). Failure = underperformance for 24 consecutive months.
- **Trade Discipline Log:** Daily checklist before first trade.
- **Tradier account:** CLOSED — $500 moved to Alpaca. No inactivity fees.
- **Deploy discipline:** Every Railway deployment must correspond to a commit on `main`. Out-of-band CLI redeploys (`railway redeploy --from-source`, `railway up`) are forbidden — they break the audit trail. `watchPatterns` on each service gate rebuilds to runtime files only (`**/*.py`, `**/*.yaml`, `pyproject.toml`, `railway.json`, `.python-version`); doc / markdown / backtest-output changes don't trigger rebuilds. Build creates a venv at `/app/.venv` and the runtime invokes `/app/.venv/bin/python` directly. Python pinned to 3.11.15 via `.python-version`. See `docs/OPERATIONS.md` §1.
- **AAR persistence:** `tpcore.aar.writer.AARWriter` persists every closed trade to `platform.aar_events` with `(engine, trade_id)` uniqueness + `ON CONFLICT DO NOTHING` idempotency. Pipeline verified end-to-end against the live database via `scripts/test_aar_pipeline.py`.

---

## 9. Build Order

**Hosting note (2026-05-12):** Railway deployment is **deferred until post-edge validation**. Phase 1.5's trade-monitor refactor and Phase 2's cost gate both landed in `railway.json` but neither was applied to live Railway service-instance config; rather than partial-apply, all production execution moved to the operator's local Mac. Re-enabling Railway (or replacing it) is gated on at least one engine clearing the credibility gate (≥ 60/100). Until then, services in the table below describe what's **built** and how they're invoked — not what's running on Railway.

| Phase | Deliverable | Status |
| --- | --- | --- |
| Phase 0 | `tpcore` + platform schema + ingestion script | **Complete** |
| Phase 1 | Sigma engine — full plug implementation | **Complete** |
| Phase 1b | Sigma paper trading (3+ months), Parity Harness active | **Paused** — engine + Parity Harness built; cron firing blocked by the Railway pause. Paper-trading resumes when execution architecture is settled. |
| Phase 2 | Reversion engine | **Complete** — combined filter (HIGH quality + \|Z\| ≥ 3.0) applied. Backtest re-run with tier-aware costs 2026-05-12. |
| Phase 3 | Allocator + Forensics (basic) | **Complete (2026-05-13 / 2026-05-14)** — Allocator service in `tpcore/allocator/` with launchd daemon firing Mondays 13:00 UTC. Forensics in `tpcore/forensics/` wired into the data-operations pipeline, auto-generates Sprint Dossiers, surfaces on dashboard with one-click resolve. Both services read AARs through the shared `tpcore.aar.AARReader`. |
| Phase 4 | Vector engine | **Complete (build); data-blocked (validation)** — engine code shipped, but parameter-search verdict (2026-05-13) showed zero trades on T1+T2 because `platform.catalyst_events` has zero overlap with that universe. Re-enabling requires a catalyst-event backfill, not a code change. |
| Phase 4b | **Momentum engine — Phase 2 (live-shippable)** | **Complete (Phase 2; 2026-05-13)** — 5 plugs + scheduler + Alpaca paper integration. `momentum/backtest.py` produces held-back Sharpe +1.58 / PF 2.80 on T1+T2 2024-2025. Paper kickoff: `scripts/run_momentum_kickoff.sh`. Daily cron pattern: scheduler no-ops on non-rebalance days, fires on the first NYSE session of each month. |
| Phase 5 | S2 (satellite) | **Deferred** — options data parked in `platform.tradier_options_chains` (122,668 rows), no engine code. |
| Phase 6 | Catalyst | **Deferred** — specification only. |
| Phase 7 | Sentinel | **Deferred** — specification only. |
| Cross-cutting | Parameter-search pipeline | **Complete** — `scripts/search_parameters.py` + `tpcore/backtest/search.py`. Random search + walk-forward + final held-back DSR. Panel-sharing context cache (~60× per-trial speedup). Period-aggregated metrics (correct for portfolio strategies). See §2.5. |
| Cross-cutting | Overfitting detection suite | **Complete** — `tpcore/backtest/overfitting.py` wired into all engine backtests. See *Overfitting Diagnostics Status* below. |
| Cross-cutting | Data Validation Suite | **Complete** — `python -m tpcore.quality.validation` runs locally; was previously scheduled as a Railway Sunday cron, consolidated into `platform.ingestion_jobs` for the persistent `ingestion-engine`. |
| Cross-cutting | Corporate-actions pipeline | **Complete** — `scripts/run_corporate_actions_all_active.py` runs locally; previously scheduled as a Sunday cron, now driven via `platform.ingestion_jobs`. |
| Cross-cutting | Maintenance CLI (`scripts/ops.py`) | **Complete** — single-file `--update` / `--check` / `--full` driver for daily + weekly data work. Reuses `tpcore.ingestion.handlers`, writes audit rows to `platform.application_log` under `engine='ops'`. Operator runbook in `docs/OPERATIONS.md` § *Daily Maintenance (via ops CLI)*. |
| Cross-cutting | Trade monitor (Phase 1.5) | **Complete (built)** — `tpcore/trade_monitor.py` consumes Alpaca's `TradingStream`; defined in `railway.json` but not yet deployed (Railway paused). Local invocation: `python -m tpcore.trade_monitor`. |
| Cross-cutting | Cost model (Phase 2) | **Complete** — `platform.spread_observations` + `platform.liquidity_tiers` populated from Corwin-Schultz; `RiskGovernor.check_cost` wired through all three engines; backtests use `tpcore.backtest.cost_model.get_round_trip_cost`. Spec: `docs/EDGE_VALIDATION_PLAN.md`. |

### Overfitting Diagnostics Status

- **Module:** `tpcore/backtest/overfitting.py` — nine tests (DSR, PSR, PBO via CSCV, MinBTL, parameter sensitivity sweep, Monte Carlo sequence stress, noise infusion, regime coverage, trades-per-parameter ratio).
- **Integration:** wired into all four engine backtest scripts (`sigma`, `reversion`, `vector`, `momentum`). Reports saved to `backtests/<engine>_overfitting_report.json`. Credibility consumed by `tpcore.backtest.credibility.BacktestCredibilityRubric.evaluate_with_overfitting()` (70 pts integrity + 30 pts overfitting bundle = 100 total; ≥ 60 required for graduation).

**Parameter-search verdicts (T1+T2 universe, 50 trials × 3 walk-forward windows, period-aggregated metrics; sweeps run 2026-05-12 / 2026-05-13):**

| Engine | Held-back Sharpe | Held-back PF | Held-back DD | Credibility | DSR | Verdict |
|---|---|---|---|---|---|---|
| Sigma | +0.74 | +3.71 | -8.1% | 55 | 0.00 | Marginal real edge — research only |
| Reversion | -0.08 | +0.87 | -8.0% | 45 | 0.00 | Pipeline caught overfit (in-sample +2.87 → OOS -0.08) |
| Vector | — | — | — | — | — | **Data-blocked** — catalyst_events has 0 overlap with T1+T2 |
| **Momentum** | **+1.58** | **+2.80** | -32.4% | 40 | 0.00 | **Strongest OOS signal in the bench** — Phase 1 CONTINUE |

- **Why the 60-pt gate has not been cleared anywhere yet:**
  1. For Sigma/Reversion/Vector: trade-count thinness + DSR multiple-testing correction.
  2. For Momentum: monthly portfolio frequency × 50-trial penalty × 24 held-back observations makes DSR ≥ 0.95 mathematically unreachable regardless of strategy quality. The rubric was calibrated for daily-frequency strategies; using it as-is on monthly strategies is a category error.
- **Forward path:**
  - **Momentum**: paper-trade with small size now; let the live tape become the OOS validation. Re-evaluate credibility under either a frequency-adjusted DSR threshold (≈0.5 for monthly with 24 obs) or PSR (no deflation).
  - **Vector**: backfill `platform.catalyst_events` for T1+T2 tickers via FMP earnings-history endpoint. Single ingestion task. Re-run search after.
  - **Sigma / Reversion**: park. Reversion is overfit-confirmed; Sigma is too marginal to move ahead of Momentum.

---

## 10. Governance

This master plan is the binding specification. Any deviation must be ratified by a new decision entry in `docs/decisions/` following the naming convention `YYYY-MM-DD-topic.md`. All engine code must reference the relevant section of this plan. The `docs/session-log.md` records each build session. The `docs/glossary.md` defines every term.
