# Short-Term Trading Engine — Unified Platform Master Plan

**Version:** 1.2
**Date:** 2026-05-13
**Status:** All three engine schedulers (Sigma, Reversion, Vector) deployed on Railway and online. RiskGovernor `check_trade()` + startup kill-switch check, AARWriter persistence, and LivePaperParityHarness wiring all verified end-to-end (live DB round-trip for AAR; harnesses no-op without live broker creds, by design). As of 2026-05-10, engines read daily bars exclusively from `platform.prices_daily` via `PostgresDataAdapter` — no live-API fallback; schedulers halt with a critical log if the DB is unreachable. Per-run audit timeline lands in `platform.application_log` (7-day rolling retention) via `tpcore.logging.DBLogHandler`. All three engines fail the overfitting-aware credibility gate (60/100); none cleared for live capital — paper-trading only.

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
- Transaction cost model: 0.05% slippage per side for liquid stocks, configurable.

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

**Status (built and deployed):**
- All five plugs implemented and tested. Scheduler deployed on Railway as `sigma-scheduler` (cron: Mon–Fri 22:00 UTC).
- CHOP filter validated by backtest — per-stock variant improves Sharpe by 26%; SPY-level variant falsified and removed.
- Backtest: `sigma/backtest.py`. Overfitting report: `backtests/sigma_overfitting_report.json`.
- Overfitting diagnostic score **50/100 — BLOCKED**. Extended window (1995–2025, 754 trades) revealed −83% max drawdown in pre-2008 regimes; per-trade Sharpe collapsed to 0.03 once the longer window pulls in the dot-com bust and 2008 GFC. Not cleared for live capital. See §9 *Overfitting Diagnostics Status* for the failure-mode breakdown.

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

**Status (built and deployed):**
- All five plugs implemented and tested. Scheduler deployed on Railway as `reversion-scheduler` (cron: Mon–Fri 22:00 UTC).
- Earnings-quality gate validated; combined filter (HIGH quality + |Z| ≥ 3.0) applied in `reversion/models.py` and `reversion/plugs/lifecycle_analysis.py`.
- Backtest: `reversion/backtest.py`. Overfitting report: `backtests/reversion_overfitting_report.json`.
- Overfitting diagnostic score **45/100 — BLOCKED**. Extended window (1995–2025) produced 28 trades. Primary failure modes: trades-per-parameter ratio 5.6 (needs ≥ 10) and a 709-trade MinBTL gap vs the 28 actual. Not cleared for live capital.

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

**Status (built and deployed):**
- All five plugs implemented and tested. Scheduler deployed on Railway as `vector-scheduler` (cron: Mon–Fri 22:00 UTC). Verified Online via `railway status` on 2026-05-10; service ID `6498df68-0a23-4531-85df-f54ba37a1c40`.
- Catalyst proxy via FMP `EARNINGS_BEAT` events (683 events across 44 tickers, 2018–2025) populated in `platform.catalyst_events`. Fundamentals ratios `pb`/`de` backfilled (1,622 PIT-safe rows in `platform.fundamentals_quarterly`).
- VIX-aware crash-guard sizing implemented and verified end-to-end in the backtest (1.0× / 0.5× / 0.25× via SPY 20-day realized-vol proxy).
- Backtest: `vector/backtest.py`. Overfitting report: `backtests/vector_overfitting_report.json`. Score **45/100 — BLOCKED** for live capital (11 trades, trades-per-parameter ratio 1.6, MinBTL effectively infinite). Paper-trading is unblocked: the cron submits paper orders via Alpaca for AAR collection while the credibility score remains below 60.

### 4.4 S2 — Short Squeeze Engine (Fourth Build, Satellite)

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

### 4.5 Catalyst — Event-Driven Engine (Fifth Build)

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

### 4.6 Sentinel — Macro Defense Engine (Sixth Build)

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

---

## 5. Platform Services (Deferred)

These are built only after at least two engines are live.

- **Allocator:** Equal-risk-weighted capital distribution. Performance-chasing explicitly rejected. Primary value is the floor (freezing engines in persistent drawdown).
- **Forensics:** Monitors AARs. Generates Sprint Dossiers on drawdown, loss cluster, or outlier loss.
- **Settlement:** Annual distribution (75% to operator, 25% retained). Produces Schedule D-ready tax CSV.

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
| SEC EDGAR | Point-in-time filings, fundamentals backup | $0 |
| ApeWisdom | Social sentiment | $0 |
| FRED | Macro indicators | $0 |
| FINRA / NASDAQ | Short interest (release-date matched) | $0 |
| IBorrowDesk | Borrow rates (scraped, fragile) | $0 |

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
- Deployed on Railway as `validation-scheduler` (cron: Sunday 06:00 UTC). CLI: `python -m tpcore.quality.validation`.
- Capital Gate hook: `tpcore.quality.validation.capital_gate.assert_passed(pool, max_age_days=7)` is consulted by every engine's `assert_can_graduate`. No engine graduates from paper to live without a fresh passing run.
- Design spec: `docs/superpowers/specs/2026-05-10-data-validation-suite-design.md`.
- Current state: **all three checks pass** (delistings 8/8, constituent 58/58, splits 10/10). Five historic delisted tickers (HTZGQ, WLLBQ, LK, SBNYQ, SI) were removed from `delistings.yaml` and `constituents.yaml` on 2026-05-10 after a definitive audit confirmed neither Alpaca free tier nor the Tradier export carries bars for them — they are unresolvable on free-tier data. Re-add the entries when a paid delisted-feed (EODHD survivorship-free, Norgate, or Polygon w/ delisted) is provisioned.

### 6.4 Current Data Infrastructure Status

Verified row counts and coverage (as of 2026-05-12, post-Phase 1 expansion):

| Table | Rows | Notes |
| --- | ---: | --- |
| `platform.prices_daily` | 20.6M | **7,694 distinct tickers**, 1994-07-21 → 2026-05-11, survivorship-free (Alpaca IEX `all_active` sweep + Tradier wide-export merge). Was 60 tickers / 301k rows pre-Phase 1. |
| `platform.fundamentals_quarterly` | 178,518 | **5,981 tickers**, PIT-safe via FMP Starter (~30 quarters/ticker mean). `pb` + `de` populated on 152,907 rows; 25,560 NULLs are explainable (negative book value, no price on filing date, missing fields). Was 47 tickers / 1,790 rows pre-Phase 1. |
| `platform.corporate_actions` | 109,344 | **217 tickers with splits (250 events) + 3,848 tickers with dividends (109,094 events)**. Handler now supports `universe: all_active`; the original 50-name default still works for back-compat. AAPL split fix verified. |
| `platform.tradier_options_chains` | 122,668 | 51 tickers, snapshot from May 2026 (immediately before the Tradier brokerage account closed). Frozen — parked for future S2. |
| `platform.catalyst_events` | 683 | 44 tickers, all `EARNINGS_BEAT` type, 2018–2025 (FMP coverage starts 2018-01-24). Universe-expansion catalyst backfill not yet run; many new tickers have no event row. |
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

| Phase | Deliverable | Status |
| --- | --- | --- |
| Phase 0 | `tpcore` + platform schema + ingestion script | **Complete** |
| Phase 1 | Sigma engine — full plug implementation | **Complete** |
| Phase 1b | Sigma paper trading (3+ months), Parity Harness active | **In progress** — deployed on Railway as `sigma-scheduler`. Overfitting diagnostics running. |
| Phase 2 | Reversion engine | **Complete** — deployed on Railway as `reversion-scheduler`. Combined filter (HIGH quality + \|Z\| ≥ 3.0) applied. Overfitting diagnostics running. |
| Phase 3 | Allocator + Forensics (basic) | **Deferred** — blocked on paper track record from Sigma + Reversion + Vector. |
| Phase 4 | Vector engine | **Complete** — deployed on Railway as `vector-scheduler` (cron: Mon–Fri 22:00 UTC). VIX-aware sizing implemented. Overfitting diagnostics running; live capital still blocked at 45/100 (paper-trading active). |
| Phase 5 | S2 (satellite) | **Deferred** — options data parked in `platform.tradier_options_chains` (122,668 rows), no engine code. |
| Phase 6 | Catalyst | **Deferred** — specification only. |
| Phase 7 | Sentinel | **Deferred** — specification only. |
| Cross-cutting | Overfitting detection suite | **Complete** — `tpcore/backtest/overfitting.py` wired into all three engine backtests. See *Overfitting Diagnostics Status* below. |
| Cross-cutting | Data Validation Suite | **Complete** — deployed on Railway as `validation-scheduler` (Sun 06:00 UTC). |
| Cross-cutting | Corporate-actions pipeline | **Complete** — deployed as `corporate-actions-scheduler` (Sun 04:00 UTC). |
| Cross-cutting | Maintenance CLI (`scripts/ops.py`) | **Complete** — single-file `--update` / `--check` / `--full` driver for daily + weekly data work. Reuses `tpcore.ingestion.handlers`, writes audit rows to `platform.application_log` under `engine='ops'`. Operator runbook in `docs/OPERATIONS.md` § *Daily Maintenance (via ops CLI)*. |

### Overfitting Diagnostics Status

- **Module:** `tpcore/backtest/overfitting.py` — nine tests (DSR, PSR, PBO via CSCV, MinBTL, parameter sensitivity sweep, Monte Carlo sequence stress, noise infusion, regime coverage, trades-per-parameter ratio).
- **Integration:** wired into all three engine backtest scripts (`sigma/backtest.py`, `reversion/backtest.py`, `vector/backtest.py`). Reports saved to `backtests/<engine>_overfitting_report.json`. Credibility consumed by `tpcore.backtest.credibility.BacktestCredibilityRubric.evaluate_with_overfitting()` (70 pts integrity + 30 pts overfitting bundle = 100 total; ≥ 60 required for graduation).
- **Current scores:** Sigma **50/100**, Reversion **45/100**, Vector **45/100**. All below the 60-point graduation threshold. None of the three engines is cleared for live capital.
- **Primary cause of failure:** trade-count thinness relative to parameter search space. The MinBTL gap and the DSR deflation are the dominant failure modes — every engine has a defensible per-trade edge in-sample, but not enough trades to clear a multiple-testing-corrected significance threshold.
- **Mitigation paths (in order of cost):**
  1. **Extend the paper-trading window** — let the live tape add trade evidence at zero data cost.
  2. **Broaden the universe** — modest one-time effort, no strategy changes.
  3. **Upgrade to FMP Premium ($59/mo)** — only if (1) and (2) plateau without clearing the gate.

  No strategy parameter changes are warranted while trade counts are this small; tuning would be curve-fitting noise.

---

## 10. Governance

This master plan is the binding specification. Any deviation must be ratified by a new decision entry in `docs/decisions/` following the naming convention `YYYY-MM-DD-topic.md`. All engine code must reference the relevant section of this plan. The `docs/session-log.md` records each build session. The `docs/glossary.md` defines every term.
