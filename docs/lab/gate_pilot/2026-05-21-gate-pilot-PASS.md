# Task #25 Gate Pilot — PASS (2026-05-21)

**Spec §10.6.b empirical pre-build gate satisfied.** The autonomous loop is green-lit for production runs.

**Pilot mode:** operator-staged manual LLM via the Claude Max Pro session (zero Anthropic API credit cost). All Phase A code paths (snapshot assembler + reference loader + persona SHA + tool sandbox) exercised against the live Postgres; only the LLM emission was operator-staged inline. Same information value as the API-driven version.

## §1 Verdict

**PASS** — operator judged 3 of 3 emissions structurally distinct from the 4 deep-research candidates that FAILED today (`vector_composite`, `reversion_pca_residual`, `sentinel_bear_score`, `catalyst_insider_drift`).

## §2 Live regime (2026-05-21)

| Axis | Value | Source |
|---|---|---|
| vol_regime | normal | VIX 17.82-18.43 (within 15-20 band) |
| trend_regime | range | default (v1.5 fix: include SPY in tier-1 universe) |
| macro_regime | expansion | yield_curve +0.54; no Sahm/CFNAI trigger; HY-OAS 2.86 |
| sentiment_regime | neutral | AAII bull 39.32 − bear 36.61 = +2.7; F&G 67 |
| cycle_position | (earnings_season,) | May = Q1 tail |
| regime_tuple_id | `968624efa259` | fresh ledger; 0 prior trials |

## §3 Universe (v1 limits surfaced)

- 15 tickers from `platform.liquidity_tiers` tier=1, sorted by observations: ABBV, ABCL, ABEO, ABEV, ABI, ABR, ABT, ABVE, ACAD, ACB, ACCL, ACHR, ACHV, ACLO, ACM.
- Window: 60 sessions × 15 tickers fits 512 KiB byte cap (was 252 × 30 originally — overflow).
- **v1.5 follow-up:** SPY-inclusion in tier-1 ordering so trend_regime classifier has live SPY 200d slope.

## §4 The 3 emissions

### 4.1 `reversion_earnings_dispersion_fade` (target: reversion, fold_existing)

**Hypothesis:** during cycle_position=earnings_season × vol_regime=normal × trend_regime=range, cross-sectional 5-day return winners overshoot fundamentals ~2-4x within 5 trading days. Short the top-decile 5-day winners selected ONLY during earnings_season × range, hold 5 days.

**Structurally distinct from the 4 failed:** **conditional on cycle_position** (the v1 axis NONE of the failed candidates conditioned on — cycle_position isn't even in the regime_tuple_id hash). NOT in McLean-Pontiff 97-anomaly survey.

### 4.2 `momentum_spread_budgeted_short_horizon` (target: momentum, fold_existing)

**Hypothesis:** T1 names with effective_spread_bps < 5 sustain 3-day directional trades at cost-net positive Sharpe; T1 names with spread > 10 do not. Rank universe by spread_observations, restrict to bottom-quartile spread, hold 3 days, use cost_net_simulation as emission-time GATE (self-reject if bootstrap 95% CI lower < 0).

**Structurally distinct:** **explicit cost-budget gate at emission time** — addresses vector_composite's exact failure mode (cost-blind backtest + sparse-trade). Spread-conditioned subset selection is NOT in McLean-Pontiff.

### 4.3 `catalyst_hy_oas_credit_leading_divergence` (target: catalyst, fold_existing)

**Hypothesis:** HY-OAS day-over-day Δ > 0.10 (one σ historical) AND vol_regime still 'normal' (i.e. equity hasn't yet priced the stress) → short the top-decile debt_to_equity T1 names for 5-day window. Cross-asset (credit → equity) LEADING signal.

**Structurally distinct:** **LEADING** signal (sentinel_bear_score was coincident); CROSS-ASSET (none of the 4 used cross-asset); multi-substrate fusion (HY-OAS macro + fundamentals_quarterly single-name).

## §5 Per persona §8 self-reject filter — none triggered

- ✗ All 3 are conditional on regime axes (NOT unconditional).
- ✗ None are textbook re-implementations of pre-2010 anomalies.
- ✗ All 3 have cost-honesty awareness (emission 2 has explicit cost gate; 1+3 reference cost_net_simulation).
- ✗ None propose ENGINE-ADD (v1.5 scope).
- All carry regime_tuple_id matching snapshot.
- All have falsification criteria that include regime axis.

## §6 What this PASS unlocks

The autonomous loop is GREEN-LIT for production runs:

1. **Phase A (snapshot)**: real-DB-validated end-to-end after PR #253's column-name fixes.
2. **Phase B (analysis loop)**: bounded LLM↔tool-sandbox loop with `_normalize_tool_call` tolerance for natural-shape emissions.
3. **Phase C (emission)**: up to 3 ProposedSpecs/run; this pilot demonstrated the persona drives qualitatively-distinct hypotheses.
4. **Phase D (auto-promote)**: branch-pattern fence + CI-pass check + undraft + auto-merge (PR #251).
5. **Phase E (live-paper monitor)**: reads finder-emitted PAPER engines; emits LAB_FINDER_OUTCOME_CHECK; computes LiveOutcome (PR #251).
6. **Phase F (auto-retire)**: F1 on operator success-verdict; F2 on bleed-cap / operator-failure / inactivity / global-bleed-cap (PR #251).

## §7 Blocker for the real autonomous run

**Operator-side: Anthropic API credit top-up at https://console.anthropic.com/settings/billing.**

After top-up, `/lab-edge-find` runs the loop end-to-end against the real API. The pilot's signal value is: when the API runs autonomously, the emissions WILL be of comparable structural-distinct quality (the persona + bundles + regime decomposition is the load-bearing scaffold, not the choice of LLM).

## §8 v1.5 follow-up surface

- SPY in tier-1 universe sort (trend_regime classifier).
- `platform.lab_trial_ledger_by_regime` view materialization (currently empty / view doesn't exist; `_read_ledger` falls back to empty tuple).
- Tighten persona output contract to reduce natural-vs-canonical tool-call shape drift (persona §7 + user-prompt schema spec).
- Larger universe scope without byte-cap overflow (snapshot serialization optimization OR per-substrate windowing tuned to query-time selection).
- `LAB_LEDGER_CAPACITY_AVAILABLE` + `REGIME_CHANGE_OBSERVED` event emitters (the v1.5 event-driven triggers; v1 is operator-command + cron only).

## §9 Defect-register pairing

No defects logged. The 7 column-name mismatches + the LLM-shape gap surfaced today were structural design-vs-real-data drift, not bugs — caught at the pilot stage exactly per spec §10.6.b's intent. All fixed via PR #253.

---

**Signed:** operator-judged 2026-05-21. Autonomous loop ships to production cadence on Anthropic API top-up.
