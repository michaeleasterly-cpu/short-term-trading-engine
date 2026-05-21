# Sentinel — graduated Bear Score (single-spec Lab candidate)

- **Status:** PRE-REGISTERED (single hypothesis, pinned). Candidate-ready artifact.
- **Date:** 2026-05-21
- **Lane:** engine-owned (Lab). Heavy lane.
- **Branch:** `feat/sentinel-bear-score-lab-candidate` (off `origin/main`).
- **Decision record:** `TODO.md` L537-552 — `decision: ADOPT — route via ops.lab`, `intent: fold_existing`, `effort: M`.
- **Route:** `python -m ops.lab --candidate sentinel_bear_score --target-engine sentinel --intent fold_existing` → held-back DSR/credibility graduation gate → ECR. Counts against the cumulative n_trials ledger. The gate is sacred — never relaxed, never bypassed.
- **Binding lens:** the cumulative DSR/n_trials overfit verdict is the platform constraint. This is ONE pre-registered single-primary configuration plus exactly ONE pre-declared robustness check (itself counted as a trial). It is NOT a sweep. The weight × band surface is large by construction; this spec resists that surface explicitly by pinning every constant in code.
- **Sibling candidate (already on this branch's base):** `2026-05-20-sentinel-maxdd-lab-candidate.md` (`sentinel_maxdd`, activation-threshold 60↔55 toggle). This candidate (`sentinel_bear_score`) is **additive** — a second pre-registered candidate sharing the same `LAB_TARGET` declaration through a second `param_ranges` key.

---

## 1. Single pre-registered primary hypothesis (checklist §1)

**Primary hypothesis (ONE, pre-registered, pinned):** replacing Sentinel's
binary Bear-Score activation gate with a **graduated** five-factor
composite (defined in §2) — using **literature-anchored** sub-thresholds
(Sahm ≥ 0.50, CFNAI-MA3 ≤ −0.70, SOS ≥ 0.20 — anti-overfit anchors,
external to this run) and three action bands (0.45 / 0.60 / 0.80) — reduces
the defensive-basket holdout **maximum drawdown** vs the legacy binary
activation, with a Treasuries/gold-first sizing discipline and an
inverse-ETF cap of 25 % of defensive capital.

**Primary metric / verdict (ONE):** `LabPrimaryMetric.MAXDD_REDUCTION`
(SP-D), already declared on `sentinel.backtest.LAB_TARGET.primary_metric`.
Ranking is judged by holdout `max_drawdown` (`<= 0` by construction;
shallower / less-negative drawdown ranks first under the existing
descending sort). This is **deliberately NOT Sharpe/DSR-expressible** —
Sentinel is a macro-defense overlay whose job is loss mitigation, not
return generation (the same SP-E choice that the sibling `sentinel_maxdd`
candidate took, and the contrast Vector pilot §5.1 identified).

- **No post-hoc metric shopping.** The success/falsification criterion is
  pinned here *before* the run: the candidate FAILS if the graduated
  variant does not produce a strictly shallower mean holdout drawdown
  than the legacy binary path on the held-back window. A FAIL is logged
  as a genuine falsification and is **not** re-run with tweaked weights /
  bands / thresholds (that would be n_trials laundering).
- **At most ONE pre-declared robustness check** (see §10): the
  equal-weight ablation `(0.20·Sahm + 0.20·SOS + 0.20·curve + 0.20·CFNAI-MA3 + 0.20·HY-OAS)`.
  Run once; counted as one additional trial against the cumulative ledger
  (§4). Mirrors the Vector pilot §2.6 anti-anchor pattern (the weight set
  is the spec's least-anchored choice, so the equal-weight ablation tests
  that anchor directly).
- **Every numeric constant is pinned.** Placeholder scan
  (`TODO`/`TBD`/`???`/`<…>`) on this spec comes back empty.
- **This is NOT a sweep.** The only Lab-sampled value added by this
  candidate is the single `bear_score_mode` `choice:current,graduated`
  toggle. Every constant of the graduated composite (weights, anchor
  thresholds, action-band cuts, inverse-ETF cap, basket-tier order) is a
  **code constant**, never Lab-sampled. The weight × band surface is
  intentionally not exposed to the sampler.

## 2. The single pre-registered graduated composite spec (exact — no ranges)

All five sub-signals are computed point-in-time from
`platform.macro_indicators` for each `sim_date`, mapped to `[0, 1]`
sub-scores, then combined into ONE composite. **Every constant below is
pinned. There is no grid, no sweep, no per-factor weight menu.**

### 2.1 Eligible window (unchanged from live Sentinel)

The candidate runs on Sentinel's existing `SentinelWindowContext`
backtest window. The graduated composite changes only **how the
activation level is computed and how the basket is sized**, NOT the
window selection, the cost model, the entry/exit timing, or the
crash-guard.

### 2.2 Per-factor [0, 1] sub-scores (point-in-time, strictly backward)

For each `sim_date`, read the latest observation at or before that date
from `platform.macro_indicators` for each of the five canonical names
already wired into `tpcore.fred.adapter.INDICATOR_SERIES`:

| Factor | Indicator name | Anchor threshold | Sub-score formula |
| --- | --- | --- | --- |
| **Sahm** | `sahm_rule` | **≥ 0.50** (Sahm 2019 — original recession-onset trigger) | `clip( (value - 0.20) / (0.80 - 0.20), 0, 1 )` — anchor 0.50 sits at sub-score 0.50; 0.80 saturates to 1.0; 0.20 floors at 0. |
| **SOS state diffusion** | `sos_state_diffusion` (derived) | **≥ 0.20** (Crone/Clayton-Matthews 2005 — 3-mo span fraction of declining states) | `clip( (value - 0.05) / (0.40 - 0.05), 0, 1 )` — anchor 0.20 maps to ≈ 0.43; 0.40 saturates to 1.0. |
| **Yield curve** | `yield_curve` (T10Y2Y) | **inversion = ≤ 0** (Estrella-Mishkin 1998 — recession leading indicator) | `clip( -value / 1.00, 0, 1 )` — inversion 0 ⇒ 0; −1.00 saturates to 1.0; positive (steep) ⇒ 0. |
| **CFNAI-MA3** | `cfnai_ma3` | **≤ −0.70** (Chicago Fed — official recession-onset signal) | `clip( (-value - 0.20) / (1.20 - 0.20), 0, 1 )` — anchor −0.70 maps to 0.50; −1.20 saturates to 1.0; ≥ −0.20 floors at 0. |
| **HY-OAS** | `hy_spread` (BAMLH0A0HYM2 — percent) | **≥ 5.00 %** (~500 bp — corporate credit stress) | `clip( (value - 3.00) / (8.00 - 3.00), 0, 1 )` — anchor 5.00 maps to 0.40; 8.00 saturates to 1.0; ≤ 3.00 floors at 0. |

Missing indicator on a given `sim_date` ⇒ sub-score = 0 for that factor
(the same null-guard the legacy plug uses for missing sub-scorers).

### 2.3 The composite (pinned weights)

`composite(t) = 0.30·sahm + 0.15·sos + 0.20·curve + 0.15·cfnai + 0.20·hy_oas`

Weights are exactly `0.30 / 0.15 / 0.20 / 0.15 / 0.20`, pinned by the
operator's `TODO.md` L537-552 spec. Range is `[0, 1]` by construction
(each sub-score is `[0, 1]`; weights sum to 1.00). **No weight grid. No
`--factor-weights`.**

### 2.4 Action bands (pinned graduated escalation)

| Band | Composite | Action | Basket scale |
| --- | --- | --- | --- |
| 0 | `< 0.45` | **DORMANT** | 0.00 (no defensive position) |
| 1 | `0.45 ≤ c < 0.60` | **LIGHT** | 0.40 of full basket weight |
| 2 | `0.60 ≤ c < 0.80` | **HEAVY** | 0.80 of full basket weight |
| 3 | `c ≥ 0.80` | **DEEP** | 1.00 of full basket weight |

Cuts `0.45 / 0.60 / 0.80` are operator-pinned (`TODO.md` L537-552). The
basket scaling is **monotone non-decreasing** in composite — strictly
graduated escalation, not a binary flip.

### 2.5 Basket composition under the graduated path (pinned)

- **Treasuries / gold first.** Among the legacy basket
  (`SH`, `PSQ`, `TLT`, `GLD`, `SQQQ`), the Treasuries/gold sleeve
  (`TLT + GLD`) is allocated the full requested band weight first. The
  inverse-ETF sleeve (`SH + PSQ + SQQQ`) is then capped at **25 % of
  defensive capital** — strictly enforced post-renormalization.
- **Inverse-ETF cap = 0.25 of defensive capital.** Pinned. If the legacy
  basket's inverse-ETF weight share would exceed 0.25 after the band
  scaling, the surplus is reallocated pro-rata into `TLT` / `GLD` (the
  Treasuries/gold-first rule).
- All other Sentinel mechanics — cost model, entry/exit timing, crash
  guard, capital cap, override/breaker flags — are **unchanged** from the
  legacy code path.

### 2.6 Why these anchors (anti-overfit, external, NOT fitted)

Every anchor threshold above is a **literature-anchored, externally
published recession signal**, not a value fitted on the candidate's own
data. The Sahm rule's 0.50 trigger is the original Sahm (2019)
specification; CFNAI-MA3's −0.70 is the official Chicago Fed
recession-onset threshold; the SOS 3-month span ≥ 0.20 is the
Crone/Clayton-Matthews (2005) state-diffusion threshold; T10Y2Y inversion
(≤ 0) is the Estrella-Mishkin (1998) yield-curve recession indicator;
the HY-OAS 5 % anchor is the standard ~500 bp credit-stress level
(GFC peak ≈ 6 %, calm 2-2.5 %). The composite weights and action bands
are operator-pinned to one specific configuration so the candidate
expresses **exactly one** hypothesis against the cumulative ledger.

## 3. Feature-flag-variant pattern (checklist §2)

- The new code path lives **only in `sentinel/backtest.py`** behind the
  module-level `_BEAR_SCORE_MODE_OVERRIDE`, which **defaults to
  `None`/off** and mirrors `_ACTIVATION_THRESHOLD_OVERRIDE` (the sibling
  `sentinel_maxdd` candidate) and the engine ecosystem's existing
  `_*_OVERRIDE` pattern (Momentum `_LOOKBACK_OVERRIDE`, Vector
  `_PB_CEILING_OVERRIDE`).
- The variant is reached by **exactly ONE** added `LAB_TARGET.param_ranges`
  key — `bear_score_mode` — a `choice:current,graduated` toggle whose
  values are `{legacy_default "current", the one variant "graduated"}`.
  No env var, no config file, no second toggle, no default-on path. The
  pre-existing `activation_score_threshold` toggle (sibling
  `sentinel_maxdd` candidate, MERGED) remains untouched.
- The override is read into the module global and **reset per call** in
  `run_sentinel_with_context` (set to `None` when the key is absent), so
  no module-global state bleeds across Lab trials. The legacy-mode
  return path is byte-identical when the override is `None`.
- `default_params()` returns the **legacy default** (`"current"`) so the
  dossier `param_diff` carries the true `current → graduated` delta.
- **`grep` proof:** the only non-test files changed are
  `sentinel/backtest.py` (the variant + the LAB_TARGET declaration) and
  this spec doc. `sentinel/plugs/*`, `sentinel/scheduler.py`,
  `sentinel/models.py`, `ops/lab/run.py`, `scripts/run_all_engines.sh`,
  `ops/platform_pipeline.py`, and every SoT/roster file are **NOT** in
  the diff.
- **Live path:** the live scheduler (`sentinel/scheduler.py`) never
  enters `run_sentinel_with_context` and never sets the override, so the
  live trading dispatch is **byte-identical** by construction. The
  characterization test (§4) pins this.

## 4. Byte-identical live path (checklist §3 — the make-or-break proof)

`sentinel/tests/test_bear_score_byte_identical.py` pins:

- **C1 committed golden:** `run_sentinel_with_context(ctx, overrides={})`
  `BacktestRunResult` is field-for-field equal to a frozen golden of the
  pre-candidate (legacy) behaviour. The build FAILS if the golden drifts.
- **C2 default-is-legacy:** the result is the legacy golden when the
  override is `None`, when the toggle is omitted from `overrides`, when
  it is explicitly set to `"current"` (and rejecting any other unknown
  value as falling back to `"current"` — strict equality with
  `"graduated"` is the only path into the variant branch).
- **C3 variant-reachable-and-distinct:** turning the toggle to
  `"graduated"` changes the result (the branch is wired, not dead).
- **C4 no-cross-trial-leakage:** running the `"graduated"` variant then
  a legacy call in the same process yields the legacy golden (the
  per-call module-global reset).

TDD RED first: the golden is captured from the legacy (no-override) code
path itself before the variant code exists; the byte-identical contract
is the legacy behaviour, RED on any drift.

## 5. n_trials ledger acknowledgement (checklist §4)

This run records its `--trials` spend to the cumulative ledger
(`tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.sentinel` in
`platform.data_quality_log`), **unconditionally at sample time**, and
the verdict's DSR is deflated against
`tpcore.lab.ledger.cumulative_n_trials("sentinel") + this_run_trials` —
**not** this run's `--trials` in isolation. The author **acknowledges
cumulative (not per-run) DSR deflation**: every prior Lab run against
`sentinel` makes this run's gate strictly harder (monotone-harder); a
candidate that "would have passed at per-run n_trials" is **not** an
argument for relaxing anything. The cumulative ledger is never reset or
bypassed.

This candidate adds **exactly TWO configurations** to the cumulative
`lab_trial_ledger.sentinel` namespace:

1. The primary `bear_score_mode="graduated"` configuration (§2).
2. The ONE pre-declared equal-weight ablation robustness check (§10).

There is **no hidden grid** — the only Lab-sampled value added by this
candidate is the single `bear_score_mode` toggle; every weight, anchor
threshold, band cut, inverse-ETF cap, and basket-tier-order constant is
a CODE CONSTANT, never Lab-sampled.

The sibling `sentinel_maxdd` candidate's `activation_score_threshold`
toggle (also in `LAB_TARGET.param_ranges`) is a SEPARATE candidate's
configuration set; this candidate does not re-claim those trials.

## 6. Roster-targeting prerequisite (checklist §5)

`python -c "from tpcore.engine_profile import lab_targetable_engines as f;
print('sentinel' in f())"` prints `True` (sentinel is PAPER,
non-allocator, not the `lab`/`canary` sentinel). Live-verified
2026-05-21: `lab_targetable_engines()` returns
`('carver', 'catalyst', 'momentum', 'reversion', 'sentinel', 'vector')`.

Sentinel's `LAB_TARGET` declaration already exists (delivered by SP-E /
the sibling `sentinel_maxdd` candidate). This candidate adds **zero**
changes to the Lab CLI, dispatch, `tpcore/lab/`, or any SoT/roster — the
only Lab-side edit is the additional `bear_score_mode` key in the
engine-OWNED `LAB_TARGET.param_ranges` (post-SP-B `PARAM_RANGES` is
roster-driven, so this lives in `sentinel/backtest.py`, NOT in
`ops/lab/run.py`).

## 7. The gate is sacred (checklist §6)

The candidate routes through `python -m ops.lab --candidate
sentinel_bear_score --target-engine sentinel --intent fold_existing` →
`_run_lab_core` → `survived` → dossier → ECR like every other candidate.
The verdict is the **unchanged** `DSR ≥ 0.95 ∧ credibility ≥ 60 ∧
n_trades ≥ 3` floor. **No clause is relaxed.** No
`--dsr-threshold`/`--credibility-threshold` below 0.95/60 is used.

The `TODO.md`-spec'd extras (maxDD reduction ≥ 30 % vs base, ulcer index
improvement, median inverse-ETF hold < 20 d, no single-recession PnL
concentration) ride on `LabPrimaryMetric.MAXDD_REDUCTION` for the
candidate-ranking surface; **the sacred graduation gate itself is
byte-identical** — SP-D's pluggable metric only changes WHICH candidate
wins the ranking, never WHETHER it may graduate, proven by SP-D's
`test_lab_sp_d_make_or_break.py` and Sentinel's
`sentinel/tests/test_lab_maxdd_ranking_gate_sacred.py`. Bespoke
extra-gate clauses (ulcer, inverse-ETF hold concentration) become
candidate-side observability on the dossier, not gate relaxations on the
core `survived` clause.

## 8. Data prerequisites stated honestly (checklist §8)

| Datum | Status | Concrete evidence (live DB, 2026-05-21) |
| --- | --- | --- |
| `sahm_rule` | **LIVE** | `tpcore/fred/adapter.py:68`; `platform.macro_indicators WHERE indicator='sahm_rule'` returns 435 rows, last_date `2026-04-01` (monthly cadence). |
| `yield_curve` (T10Y2Y) | **LIVE** | `tpcore/fred/adapter.py:71`; 9 099 rows, last_date `2026-05-15`. |
| `hy_spread` (BAMLH0A0HYM2 — percent) | **LIVE** | `tpcore/fred/adapter.py:81` (re-activated 2026-05-16); 7 669 rows, last_date `2026-05-14`. |
| `cfnai_ma3` | **WIRED, NOT YET INGESTED** | `tpcore/fred/adapter.py:91` wired by PR #184; `platform.macro_indicators WHERE indicator='cfnai_ma3'` returns **0 rows** as of 2026-05-21. The next macro_indicators ingestion stage run (`platform.macro_indicators` weekly cadence) will populate it — the wiring is committed but the live load is pending. **Operator must trigger one macro_indicators ingestion before launching the Lab probe** (the freshness check at `tpcore/quality/validation/checks/macro_indicators_freshness.py` will gate the operator-initiated load; the substrate is on-disk via FRED API, not blocked). |
| `sos_state_diffusion` (derived from 50 `phci_<state>` Philadelphia Fed indices) | **WIRED, NOT YET INGESTED** | `tpcore/fred/adapter.py:99-115` wires the 50 substrate `phci_<state>` series; `tpcore/ingestion/handlers.py:1392` derives `sos_state_diffusion` via `tpcore.fred.diffusion.compute_sos_diffusion`. PR #216 merged 2026-05-21 (today, commit `5a011a8` — base of this branch). `platform.macro_indicators WHERE indicator='sos_state_diffusion'` returns **0 rows** as of 2026-05-21 (same operator-trigger constraint as `cfnai_ma3`). |
| ETF prices (`SH`, `PSQ`, `TLT`, `GLD`, `SQQQ`, `SPY`) in `platform.prices_daily` | PARTIAL (documented) | Inherits the existing Sentinel caveat (`apply_missing_etf_fallback` — re-weights to available tickers). Affects `current` and `graduated` arms identically — does **not** bias the pre-registered comparison. |

**Pre-probe operator action (NOT in this PR):** before launching
`python -m ops.lab --candidate sentinel_bear_score ...`, the operator
runs one macro_indicators ingestion cycle so `cfnai_ma3` and
`sos_state_diffusion` have rows in `platform.macro_indicators`. The
canonical pipeline (`scripts/run_data_operations.sh` →
`ops.py --update`) already includes the macro_indicators stage; one
cycle suffices because both indicators publish monthly (CFNAI-MA3) /
monthly-derived (SOS).

The strictly-additive PIT read of the five raw indicators is **consumed
only in the graduated branch** of `run_sentinel_with_context`; the
legacy path is unaffected (C1 byte-identical test pins this).

No new migration, no new feed, no new data adapter, no new CLI flag.

## 9. Lookahead / point-in-time honesty (checklist §9)

Every sub-signal the graduated composite reads uses the **most recent
observation at or before `sim_date`** from `platform.macro_indicators`
(forward-fill-on-the-left, never forward-from-the-future). No row dated
after `sim_date` ever enters a sub-score. Degenerate inputs (missing
observation) hit the pinned neutral guard: sub-score = 0.0 (the same
null-policy the legacy plug uses for missing sub-scorers).

Entry / exit mechanics, sizing inside each action band, crash-guard, and
cost model are **unchanged** from the legacy path — the variant changes
*which states activate and at what intensity*, not the trade machinery.
The cost model is validated for the long-only defensive ETF basket
direction; no borrow-rate model is needed.

## 10. The ONE pre-declared robustness check (checklist §1)

> **Robustness check: equal-weight ablation.** Re-run the held-back
> evaluation ONCE with the composite weights flattened to
> `(0.20, 0.20, 0.20, 0.20, 0.20)` instead of the operator-pinned
> `(0.30, 0.15, 0.20, 0.15, 0.20)`. All other constants (anchor
> thresholds, band cuts, inverse-ETF cap, basket-tier order) unchanged.

Mirrors the Vector pilot §2.6 anti-anchor pattern: the weight set is the
spec's least-anchored choice — the anchor thresholds are externally
published, the band cuts are operator-pinned, but the specific
`0.30 / 0.15 / 0.20 / 0.15 / 0.20` weight mix is the most plausible-to-be
-overfit dial. The equal-weight ablation tests that anchor directly.

This is the ONE robustness check; counted as one additional trial in the
cumulative `lab_trial_ledger.sentinel` ledger (§5). Two configurations
total: primary + equal-weight ablation. NOT a sweep.

## 11. Lab credibility namespacing (checklist §7)

Sentinel's experimental credibility writes under the existing
`backtest_credibility.lab.sentinel_bear_score` namespace via the
unchanged `_lab_credibility_engine_name` (H-S2-3) mechanism — the
candidate introduces **no** code that writes the experimental score
under the bare `sentinel` key, so `graduation_ready(pool, "sentinel")`
can never read it. No new migration, no new table, no new SoT — the
ledger and credibility namespace both ride existing
`platform.data_quality_log`.

## 12. Success / falsification criteria (red is red)

- **SURVIVED** iff ALL of: held-back DSR ≥ 0.95 **AND** credibility ≥ 60
  **AND** n_trades ≥ 3 **AND** mean holdout `max_drawdown` of
  `bear_score_mode="graduated"` is strictly shallower (less negative)
  than `bear_score_mode="current"` on the same held-back window. The
  dossier records every clause; ECR proceeds only on SURVIVED +
  `recommended_exit = fold_existing`.
- **FAILED** if ANY clause misses. A FAIL is a genuine, recorded
  falsification of the graduated-Bear-Score hypothesis. It is **NOT**
  re-run with tweaked weights / bands / thresholds (that would be a
  sweep / n_trials laundering). The honest outcome is logged; the next
  Sentinel direction is a separate adjudication.
- The `TODO.md`-spec'd auxiliary observability metrics (maxDD reduction
  ≥ 30 % vs base, ulcer improvement, median inverse-ETF hold < 20 d, no
  single-recession PnL concentration) are reported in the dossier
  alongside the primary verdict for operator review — they do **not**
  relax the sacred core gate, and a SURVIVED dossier that fails one of
  these auxiliary metrics is still SURVIVED on the core but
  `recommended_exit` will reflect operator review (per the ECR's
  human-in-the-loop on Sentinel candidates).

## 13. Compliance verifications (the `grep`-able set, checklist §10)

- **Exactly one toggle added by THIS candidate.**
  `sentinel.backtest.LAB_TARGET.param_ranges` gains exactly one new key,
  `bear_score_mode`, a `choice:current,graduated` whose values are
  `{legacy "current", the one variant "graduated"}`. No menu. The
  pre-existing `activation_score_threshold` toggle (the sibling
  `sentinel_maxdd` candidate) is unchanged.
- **Live path files untouched.** `git diff --name-only` contains no
  `sentinel/plugs/`, `sentinel/scheduler.py`, `sentinel/order_manager.py`,
  `sentinel/models.py`, `scripts/run_all_engines.sh`,
  `ops/platform_pipeline.py`, `tpcore/lab/`, `ops/lab/__main__.py`, or
  any SoT/roster file.
- **Characterization golden present + RED-first.**
  `sentinel/tests/test_bear_score_byte_identical.py` exists with the
  C1–C4 assertions; the golden is captured from the legacy
  (no-override) code path.
- **Roster target verified.** The `lab_targetable_engines()` one-liner
  prints `True` for `sentinel`.
- **No gate override below the floor.** The intended `python -m ops.lab`
  command carries no `--dsr-threshold`/`--credibility-threshold` below
  0.95/60.
- **n_trials acknowledgement present.** §5 above.
- **Single-hypothesis attestation.** ONE primary hypothesis (§1); the
  placeholder scan is empty; every constant is pinned.
- **`ruff check` clean** on the added test; no `yfinance`, no Discord,
  no `print()` residue.

## Self-review

- ONE pre-registered primary hypothesis; ONE primary metric
  (`MAXDD_REDUCTION`); ONE pre-declared robustness check (equal-weight
  ablation); placeholder scan empty; every constant pinned.
- Feature-flag-variant satisfied: off-by-default `_BEAR_SCORE_MODE_OVERRIDE`,
  exactly one new `choice:current,graduated` toggle, per-call reset,
  legacy default in `default_params()`. The sibling `sentinel_maxdd`
  candidate's `activation_score_threshold` toggle is untouched and
  co-exists.
- Gate sacred: SP-D's pluggable metric never reaches `survived`; proven
  by the SP-E gate-sacred test + SP-D's make-or-break.
- Sentinel live path byte-identical with the flag off: proven by the
  C1–C4 characterization test.
- No other engine touched; `tpcore/lab/target.py` stays engine-free.
- Data prereqs stated honestly: code wiring is committed (PRs #184 +
  #216 merged into this branch's base) but live rows for `cfnai_ma3` +
  `sos_state_diffusion` are pending one operator-initiated
  macro_indicators ingestion cycle. This is a STAGE prerequisite to the
  Lab probe, not a blocker to this candidate-ready artifact (this PR
  ships the spec + variant + LAB_TARGET + byte-identical test; the
  probe itself is a separate operator step).
