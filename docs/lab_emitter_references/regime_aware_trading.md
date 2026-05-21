# Regime-Aware Trading — Mandatory Reference (v1.0)

**Purpose.** Teach the LLM finder how to read the current market regime BEFORE proposing any hypothesis, and how to condition the hypothesis on the regime so it's NOT a McLean-Pontiff-decay-class textbook re-implementation. Operator binding 2026-05-21: the finder must "**read and understand not a static trading environment but one that also has to adjust with the market**" then follow the workflow.

**Position in the bundle stack.** Mandatory-always-include alongside `dsr_ntrials_discipline.md`. Read AFTER `market_structure_primer.md` (which describes the environment statically) and BEFORE the strategy-design bundles (Carver, Chan).

---

## 1. The regime taxonomy this codebase uses

The Path B `MarketSnapshot.market_regime` field (spec §4.2) decomposes the current state into 5 axes — all derivable from already-ingested tables (no new data dependency). Reconciled against `market_structure_primer.md` §8:

| Axis | States | Source | Threshold logic |
|---|---|---|---|
| `vol_regime` | `calm / normal / stress / crisis` | `macro_indicators.vix` | <15 / 15–20 / 20–30 / ≥30 (VIX bands) |
| `trend_regime` | `range / trend_up / trend_down` | `prices_daily` on SPY | SPY 200d slope sign ∧ ADX(14) > 25 → trend_up/down per slope sign; else range |
| `macro_regime` | `expansion / slowing / contraction` | `macro_indicators.sahm_rule` + `cfnai_ma3` + `yield_curve` | Sahm ≥0.50 → contraction; CFNAI-MA3 < -0.70 → contraction; yield-curve <0 (6mo-leading) → slowing; else expansion |
| `sentiment_regime` | `extreme_bull / neutral / extreme_bear` | `aaii_sentiment` × `fear_greed` | AAII bull-bear spread > 50% AND F&G > 75 → extreme_bull; AAII bull-bear < -30% AND F&G < 25 → extreme_bear; else neutral |
| `cycle_position` | tuple of `earnings_season / fomc_week / opex_week / year_end / normal` | XNYS calendar + `earnings_calendar` + Fed calendar | Multi-tag; co-occurrence allowed (e.g., earnings_season + opex_week) |

`regime_tuple_id = SHA12((vol, trend, macro, sentiment) sorted tuple)` — the 4-axis hash that the per-regime n_trials ledger keys on. `cycle_position` is EXCLUDED from the hash (too high-cardinality; would shatter the ledger across 32 tag combinations).

**Net: ~108 plausible regime tuples** (4 × 3 × 3 × 3) in the 4-axis hash. Many are economically improbable (e.g., `crisis × trend_up × expansion × extreme_bull` rarely occurs). The realised distribution is sparse — empirically ~15-25 distinct tuples in any 5-year window.

---

## 2. The behavioural prior per regime

The finder's job is to propose hypotheses **conditional on the regime**. The unconditional hypothesis "mean-reversion in pairs trading" is decayed; the conditional "mean-reversion in pairs trading IS THE WINNING STRATEGY in range × calm × expansion × neutral regime" is empirically much more defensible (Chan 2013 ch. 1).

### 2.1 Vol regime priors (Hasbrouck 2007 ch. 6 + Bollerslev 1986 GARCH lit)

- **`calm`** (VIX < 15): mean-reversion strategies dominate. Cross-sectional dispersion is compressed; pairs/relative-value hypotheses have the largest pool of stationary residuals. Momentum strategies underperform (low cross-sectional dispersion = low momentum signal-to-noise).
- **`normal`** (VIX 15–20): both mean-reversion and momentum work; the cross-section's higher moments are most "well-behaved" — OLS_HAC_NW assumptions hold most reliably here.
- **`stress`** (VIX 20–30): correlations spike (the "single factor" regime — Cont 2001); diversification fails. Momentum / trend-following are the historically-winning strategy class. Sentinel-style defensive engines should be activating.
- **`crisis`** (VIX ≥ 30): correlations near 1; nothing works in a portfolio sense. Cash + defensive ETFs are the rational position. Mean-reversion strategies SHORT vol (sell into spikes) historically work BUT carry blow-up tail risk (LTCM, Aug 2007, Aug 2015 ETF flash crash, Mar 2020).

### 2.2 Trend regime priors (Lo-MacKinlay 1988 variance ratio + Carver 2015 ch. 7)

- **`range`** (SPY slope ~ 0 OR ADX < 25): mean-reversion. Variance-ratio test (Lo-MacKinlay) returns < 1 on most names — confirming auto-correlation < 0 = mean-reverting. Carver §7's vol-targeted multi-forecast IS a `range` regime construct.
- **`trend_up`** (positive 200d slope + ADX > 25): momentum dominates. The classic 12-1 momentum factor (Jegadeesh-Titman 1993) works ONLY in trending regimes — McLean-Pontiff decay is largely a regime-mismatch issue, not a true decay.
- **`trend_down`** (negative slope + ADX > 25): same momentum logic but on the short side. Long-only equity engines (this codebase's Momentum) should reduce position size (Carver §6 vol-targeting handles this mechanically).

### 2.3 Macro regime priors (Estrella-Mishkin 1998 + Sahm 2019 + Crone-Clayton-Matthews 2005)

- **`expansion`** (yield curve positive + Sahm < 0.50 + CFNAI-MA3 > 0): normal-cycle regime. Cross-sectional anomalies (value, quality, low-vol) historically work but with publication-decay. Earnings momentum (Catalyst engine) has its best edge here.
- **`slowing`** (yield curve <0 with 6mo-lead): leading-indicator regime. Defensive sectors (utilities, staples) outperform. Sentinel-style activation thresholds STARTING to fire but not yet at full conviction.
- **`contraction`** (Sahm ≥0.50 OR CFNAI-MA3 ≤ -0.70): recessionary regime. Defensive baskets (the Sentinel design target) carry their best risk-adjusted returns. Long-only equity strategies should de-risk to <50% gross exposure.

### 2.4 Sentiment regime priors (contrarian indicators — AAII history)

- **`extreme_bull`** (AAII bull-bear > 50% AND F&G > 75): historical contrarian signal — subsequent 4-12 week returns mean-revert NEGATIVE. The finder should propose hypotheses BIASED to mean-reversion / fade-the-rally here.
- **`extreme_bear`** (AAII bull-bear < -30% AND F&G < 25): historical contrarian signal — subsequent 4-12 week returns mean-revert POSITIVE. Hypotheses BIASED to long-side mean-reversion.
- **`neutral`**: no contrarian signal; sentiment doesn't condition the hypothesis.

### 2.5 Cycle-position priors (calendar effects + event-driven)

- **`earnings_season`**: idiosyncratic risk explodes; cross-sectional strategies underperform (idio washes out signal). The finder should DOWN-WEIGHT cross-sectional hypotheses during earnings season OR condition them on `(NOT earnings_season)`.
- **`fomc_week`**: pre-FOMC drift was the Lucca-Moench 2015 *JF* edge (24-hour pre-announcement window); post-2012 decay; post-COVID unclear. A finder proposing this MUST check current-data persistence (do not assume textbook result).
- **`opex_week`**: gamma effects from dealer hedging; Friday volatility structurally elevated. Strategies trading through opex Friday face mechanical drag.
- **`year_end`**: tax-loss / window-dressing effects. Mostly decayed for T1/T2. The finder should NOT propose year-end-effect hypotheses without a justification for why decay doesn't apply now.

---

## 3. The workflow — collect, analyse, find, automate (regime-aware)

**This is the operator's binding workflow (2026-05-21).** The 4-step loop in the Path B spec §3.2 maps:

### 3.1 Step 1 — COLLECT (Phase A)

Read `MarketSnapshot`. Specifically:

1. Read `market_regime` FIRST. Note `regime_tuple_id`.
2. Read `cumulative_n_trials_by_regime(target_engine, regime_tuple_id)` AND aggregate `cumulative_n_trials(target_engine)` — both must be under deflation floor for the gate to even consider an emission.
3. Read the 14+ ingested substrate tables via the snapshot's read paths. Identify which substrates are MOST relevant to the current regime (per §2 priors above).
4. Read the bundle excerpts you've been given — `carver_systematic_trading.md` for trend/range; `chan_algorithmic_trading.md` for mean-reversion + pairs; `market_structure_primer.md` for microstructure context.

### 3.2 Step 2 — ANALYSE (Phase B, ≤ 10 turns × ≤ 4 tool calls)

Pre-register turn 1 the pair-roster (if `coint` will be used), the label_window_days, and the primary metric. Spend tool calls:

1. **OLS_HAC_NW** for any time-series regression. NEVER raw OLS. Bandwidth defaults to `floor(0.75 * T^(1/3))`.
2. **rolling_spearmanr / rolling_pearsonr** for IC-stability tests (the IC must be stable across rolling windows — see Harvey-Liu-Zhu 2016 t≥3.0 cutoff).
3. **fama_macbeth** for cross-sectional regression with industry/size controls (the Fama-French 1992 design is the textbook clean baseline).
4. **adfuller** for stationarity (mean-reversion pre-screen).
5. **coint** ONLY on pre-registered pairs (≤3 calls/run) — never post-hoc pair mining (C(500,2) = 124,750 selection-bias trap).
6. **variance_ratio** (Lo-MacKinlay 1988) for confirming mean-reverting vs random-walk behaviour of the residual.
7. **hurst_exponent** + **ljung_box** for serial-correlation diagnostics.
8. **cost_net_simulation** (THE BINDING OUTCOME GATE) — every `ProposedSpec` MUST pass this. The gate reads `cost_net_sharpe`, not gross. A 6-bp gross-alpha edge nets ~-2 bps after costs and FAILS the gate. The LLM MUST project costs honestly using snapshot's `spread_observations` + `dollar_volume`.

### 3.3 Step 3 — FIND (Phase C, ≤ 3 emissions per run)

The HYPOTHESIS shape (the LLM's emitted `ProposedSpec`):

- `primary_hypothesis` — one sentence. NOT "trade momentum" — "trade 1-month-reversal in range × calm regime, conditioned on AAII < -30%."
- `cost_assumption_bps_roundtrip` — MUST be set (default 8 T1, 12 T2). Lying about costs WILL be caught at gate-time by `cost_net_simulation`.
- `regime_tuple_id` — MUST match the current snapshot (no "this works in different regime" hand-waving).
- `analysis_evidence_refs` — every claim in `rationale` MUST cite a tool result. Trained knowledge supplements but never load-bears.
- `falsification_criterion` — Popper-style: what observation would falsify this hypothesis? If the answer is "nothing observable," the hypothesis is unfalsifiable + rejected at validator.

The HYPOTHESIS quality bar:
- Is it conditional on the current regime, or is it unconditional? (Conditional preferred — McLean-Pontiff decay applies less to conditional re-implementations.)
- Is it a novel composite, or a textbook re-implementation? (Novel preferred; textbook re-implementations of pre-2010 anomalies are decay candidates.)
- Does it require data we don't ingest? (Honest answer: flag the gap, suggest a DFCR. Don't propose a hypothesis requiring 13F data.)
- Does its `cost_net_sharpe` clear the SP-A gate? (If not, the gate rejects — but the LLM should pre-self-check via `cost_net_simulation` to avoid wasted emissions counted against per-regime + aggregate ledger.)

### 3.4 Step 4 — AUTOMATE (Phase D-F, autonomous)

After emission:
- SP-G `emit_once_with_auto_promote` runs the fence stack (ledger pre-check → EmittedSpec validate → record_trial_spend → render → enforce_diff_scope → validate_no_gate_override → gh pr create --draft → gh pr ready → gh pr merge).
- CI gate runs (DSR ≥ 0.95 AND credibility ≥ 60 AND cost_net_sharpe positive). Pass → ECR-MODIFY auto-issued; engine SDLC moves LAB → PAPER.
- Phase E monitor surfaces `LiveOutcome` to §12 dashboard. Bleed cap ($5k per-engine; $15k global) is mechanical capital safety.
- Phase F1 (operator posts `verdict='success'` via §12) → `outcome_proven=True`. Phase F2 (bleed-cap breach OR `verdict='failure'` OR inactivity-timeout) → auto-retire via ECR-RETIRE.

The LLM's job at Step 4 = nothing. The infrastructure carries the spec through. The LLM's quality at Step 1-3 determines whether the engine survives Step 4.

---

## 4. The behavioural diff between Path A and Path B (what regime-aware Path B unlocks)

| | Path A (rejected) | Path B (current) |
|---|---|---|
| Snapshot | Static — same shape every emission | Regime-conditioned — snapshot's `market_regime` changes the LLM's hypothesis distribution |
| Ledger | Single counter per target | Per-regime counter + aggregate hard fence (laundering-protected) |
| Workflow | LLM proposes; operator approves each | LLM proposes + auto-promotes within the regime's fresh budget; operator audits OUTCOMES |
| Trigger | Operator command only | Event-driven: `LAB_LEDGER_CAPACITY_AVAILABLE` (regime-capacity refresh) + `REGIME_CHANGE_OBSERVED` (new regime = new search space) |
| Outcome gate | "Spec reaches PAPER" | Mechanical bleed-budget + operator-discretion verdict ("I know it when I see it") |

The regime-awareness is what makes Path B's autonomy honest at scale. Without regime-conditioning, the autonomous loop's per-emission "fresh budget" claim is laundering. WITH regime-conditioning + aggregate fence (constraint 17), the autonomous loop has a defensible reason to spend more trials than a single ledger would allow — because economically-distinct regimes have economically-distinct return-generating processes.

---

## 5. Persona-level rules for the LLM (regime-conditional)

1. **READ the regime FIRST.** Before any tool call, before any hypothesis: state the current `regime_tuple_id` + the 4-axis breakdown + the priors (§2) that apply.
2. **CONDITION the hypothesis on the regime.** Unconditional hypotheses are McLean-Pontiff candidates. Conditional hypotheses are NOT.
3. **CHECK the per-regime + aggregate ledger.** Both must be under deflation floor. If aggregate is exhausted (target has had 200+ trials cumulatively), do NOT propose against that target — propose against a different target where the ledger is fresh.
4. **PROJECT cost honestly via `cost_net_simulation`.** Self-reject if cost-net Sharpe is below the deflation floor BEFORE wasting an emission.
5. **CITE every claim.** `analysis_evidence_refs` MUST be populated for every numeric in the rationale.
6. **ACKNOWLEDGE the regime axis in the falsification criterion.** "This hypothesis is falsified if the cost_net_sharpe in regime X over the holdout window is below Y" — the regime is part of the falsifier.
7. **NEVER propose ENGINE-ADD in v1.** v1.5 scope. v1 emissions are `fold_existing` (existing engine MODIFY) or `promote_new` (new param-arm against existing engine slot). The `engine_template` scaffold has empty bodies; the autonomous loop cannot fill them.

---

## References (literature)

- Cont, R. (2001). "Empirical Properties of Asset Returns: Stylized Facts and Statistical Issues." *Quantitative Finance* 1(2).
- Bollerslev, T. (1986). "Generalized Autoregressive Conditional Heteroskedasticity." *Journal of Econometrics* 31(3).
- Jegadeesh, N., Titman, S. (1993). "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency." *Journal of Finance* 48(1).
- Lo, A. W., MacKinlay, A. C. (1988). "Stock Market Prices Do Not Follow Random Walks: Evidence from a Simple Specification Test." *Review of Financial Studies* 1(1).
- Sahm, C. (2019). "Direct Stimulus Payments to Individuals." Brookings Institution.
- Estrella, A., Mishkin, F. S. (1998). "Predicting U.S. Recessions: Financial Variables as Leading Indicators." *Review of Economics and Statistics* 80(1).
- Crone, T. M., Clayton-Matthews, A. (2005). "Consistent Economic Indexes for the 50 States." *Review of Economics and Statistics* 87(4).
- Lucca, D. O., Moench, E. (2015). "The Pre-FOMC Announcement Drift." *Journal of Finance* 70(1).
- McLean, R. D., Pontiff, J. (2016). "Does Academic Research Destroy Stock Return Predictability?" *Journal of Finance* 71(1).
- Harvey, C. R., Liu, Y., Zhu, H. (2016). "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1).
- Fama, E. F., French, K. R. (1992). "The Cross-Section of Expected Stock Returns." *Journal of Finance* 47(2).

## In-codebase pointers

- `tpcore/lab/llm_finder/snapshot.py::compute_market_regime` — Path B spec §4.2 implementation.
- `tpcore/lab/llm_finder/regime.py` — the 5-axis classifier.
- `tpcore/lab/ledger.py` — per-regime + aggregate cumulative ledger (constraint 14 + 17).
- `tpcore/fred/adapter.py` — INDICATOR_SERIES (the regime-input substrate).
- `dsr_ntrials_discipline.md` (this directory) — the multiple-testing fences regime-conditioning operates within.
- `market_structure_primer.md` (this directory) — the static environment grounding §8 axis decomposition.
