# Vector — sector-relative composite score (single-spec Lab candidate)

- **Status:** hardened spec + implementation (one-PR ship per the lean cadence — `feedback_cut_process_overhead_ship`).
- **Date:** 2026-05-20
- **Lane:** engine-owned (Lab). Heavy lane.
- **Branch:** `lab/vector-composite` (off `origin/main` @ `3dc431b`).
- **Decision record:** `TODO.md` "Deep-research spike adjudication — Lab-candidate backlog (2026-05-19)", Vector item (~L626) — `decision: ADOPT — route via ops.lab`, `intent: fold_existing`, `effort: M`. This spec is the 2026-05-20 refresh of the 2026-05-19 pilot (commit `0a94414` on `lab-candidates-rollthrough`); the abstract Lab Candidate Readiness checklist (`docs/superpowers/checklists/lab_candidate_readiness.md`) cites the pilot as its canonical worked example. §13 below walks the 10-section checklist against this refresh.
- **Route:** `python -m ops.lab --candidate vector_composite --target-engine vector --intent fold_existing` → held-back DSR/credibility graduation gate → ECR (`python -m ops.engine_sdlc`). Counts against n_trials. The gate is sacred — never relaxed, never bypassed.
- **Binding lens:** the DSR/n_trials overfit verdict is THE platform constraint. This is ONE pre-registered single-primary configuration plus at most ONE pre-declared robustness check (itself counted as a trial). It is NOT a sweep. The deep-research reports' `--family-weights` menus are the n_trials hazard and are explicitly rejected — single config only.

## 0. Post-pilot context (2026-05-19 → 2026-05-20)

Material context that has changed between the pilot and this refresh:

- **`carver` shipped (#149/#151/#154 merged 2026-05-20)** — first engine ever real-ADDed via the SDLC planner. `_PROFILE` now contains `carver(dispatch_order=6, LifecycleState.LAB)`. `lab_targetable_engines()` returns `(reversion, vector, momentum, sentinel, carver)`. Vector remains a valid `--target-engine`.
- **SP-D pluggable scoring (#135) + SP-E Sentinel `MAXDD_REDUCTION` (#136) on main** — Vector's PBO/family-variance clauses remain expressible on the existing `LabResult` dossier path (Sharpe/DSR/trade-count/CSCV-PBO + the §2.6 ablation); no SP-D extension needed for this candidate. The Sentinel-style non-Sharpe path is reserved for the next Sentinel Lab candidate.
- **SP-G LLM spec-emitter shipped (#146 spec, #152 build)** — distinct downstream from this candidate; the spec-emitter will eventually *propose* Lab candidates, this *is* a Lab candidate that still routes through the same sacred gate.
- **SDLC ECR `source=existing_code` (#153)** — irrelevant here; we are `fold_existing`-MODIFY-via-Lab-dossier, not an ADD.
- **Lean-cadence ship discipline (`feedback_cut_process_overhead_ship`)** — pilot was spec-only; this refresh combines the spec + implementation + build PR into ONE deliverable.

---

## 1. Problem

Vector's live signal is a hard **AND-gate** of three independent gates,
evaluated point-in-time in `vector/backtest.py::_run` and mirrored in
`vector/plugs/setup_detection.py`:

1. **Value & quality** — most-recent `fundamentals_quarterly` row with
   `filing_date <= sim_date` must satisfy `pb < 1.5 AND de < 3 AND
   revenue > $500M` AND `last_close >= 200-SMA`.
2. **Catalyst** — at least one `earnings_events` (`EARNINGS_BEAT`) row within
   ±`catalyst_window_days` of `sim_date`.
3. **Technical trigger** — pullback to 10/20-MA on volume > 1.2× avg OR
   breakout above 50-MA on volume > 1.5× avg.

A name trades only if it clears **all three** simultaneously. This is the
documented Vector failure: the conjunction collapses trade count to a level
where the held-back DSR cannot accumulate statistical power. The last search
top OOS was +1.257 but **FAILED the DSR gate** (CLAUDE.md engine roster;
TODO.md adjudication). The structural fix with the most cross-spike evidence
(both `deep-research-report.md` and `deep-research-report2.md`, expert-reviewed
2026-05-19) is to **replace the AND-gate with ONE fixed-weight composite
score** standardized cross-sectionally, with **top-decile selection** — a
graded ranking, not a binary conjunction. This converts three pass/fail
filters into one continuous score, so marginal names that fail one gate by a
hair but rank in the top decile overall can trade, restoring trade count
without abandoning the value/catalyst/technical thesis.

This Lab candidate **tests that structural fix once, honestly, against the
sacred held-back gate**. A FAIL genuinely falsifies the most-evidenced Vector
structural fix (red is red — see §9).

---

## 2. The single pre-registered composite spec (exact — no ranges)

All three family scores are computed point-in-time on the eligible universe
for each `sim_date`, then combined into ONE composite, then the top decile is
selected. **Every constant below is pinned. There is no grid, no sweep, no
per-family weight menu.**

### 2.1 Eligible universe (unchanged from live Vector)

The candidate set per `sim_date` is exactly Vector's current eligible
universe: tickers present in BOTH `fundamentals_quarterly` (pb/de/revenue all
non-null) AND `earnings_events` (`event_type='EARNINGS_BEAT'`), intersected
with the Lab `--universe-tier-max` selection when supplied, with ≥ `SMA_200+5`
bars of price history at `sim_date`. This is identical to
`load_vector_window_context`'s `eligible` derivation — **the composite changes
how names are scored/selected, NOT which names are eligible.** No new feed is
required for eligibility.

### 2.2 Per-family raw signals (point-in-time)

For each eligible ticker `t` at `sim_date`:

- **Value family `v_raw(t)`** — from the PIT `fundamentals_quarterly` row
  (latest `filing_date <= sim_date`):
  `v_raw = -z(pb) - 0.5 * z(de)` computed AFTER cross-sectional
  standardization (see §2.3). Conceptually: cheaper (lower P/B) and
  less-levered (lower D/E) is better. The `-0.5` weight on D/E preserves the
  live engine's relative emphasis (P/B is the primary value axis in the
  current Gate 1; D/E is the secondary leverage guard). Names with any of
  pb/de/revenue null are **dropped from the candidate set for that
  `sim_date`** (same as the live `_passes_gate1` null-guard — no imputation,
  which would be a silent lookahead/quality hazard).
- **Catalyst family `c_raw(t)`** — the catalyst family is
  `earnings_events + insider-cluster` (TODO.md adjudication). Defined as the
  sum of two PIT sub-signals (both clipped to be non-negative; the composite
  is a long-conviction score):
  - `earn_signal = max(0, magnitude_pct)` of the most-recent `EARNINGS_BEAT`
    `earnings_events` row whose `event_date` lies in the **PIT-safe window**
    `[sim_date - catalyst_window_days, sim_date]` (NOT the live ±window —
    see §6, H-VC-6: the live backtest's documented forward half-window is a
    deliberate anticipation signal for the AND-gate; the composite uses a
    strictly-backward window so the score carries zero lookahead and the
    held-back DSR is honest). `0.0` if none.
  - `insider_signal = 1.0` iff an **insider cluster** is present, else `0.0`.
    **Insider cluster (pinned definition):** ≥ 2 **distinct** `insider_name`
    values with `transaction_type = 'BUY'` in `platform.sec_insider_transactions`
    for `t` with `filing_date` in `[sim_date - 30, sim_date]` (strictly
    backward, 30 calendar-day window — the literature-standard non-routine
    cluster window, also used by the Catalyst candidate per TODO.md). "Distinct
    insiders" is the cluster-ness proxy; SELL rows are ignored. No
    routine-exclusion sub-filter is added (the platform has no Form-4
    transaction-code routine flag; adding one would be a new feed = scope
    creep / a second hypothesis — explicitly out of scope, §10).
  - `c_raw = z(earn_signal) + z(insider_signal)` after cross-sectional
    standardization (§2.3). The two sub-signals are summed pre-standardization
    only conceptually; **each is standardized independently then summed** so a
    universe with zero insider clusters that day does not blow up the z of a
    degenerate constant column (§2.3 handles the zero-variance guard).
- **Technical family `tech_raw(t)`** — Vector's existing trigger logic
  (`_technical_trigger`) yields a categorical trigger or `None`. Map to a
  graded raw signal: `breakout_above_50ma → 1.0`, `pullback_to_10ma → 0.7`,
  `pullback_to_20ma → 0.6`, `None → 0.0`. (Ordering mirrors the live engine's
  implicit preference: a confirmed breakout is the strongest technical state;
  a 10-MA pullback is tighter than a 20-MA pullback.) Then
  `t_z = z(tech_raw)` (§2.3).

### 2.3 Cross-sectional standardization + the sector-relative method

**Sector-relative standardization is specified by the adjudication. The data
audit (this spec, §8) proves there is NO GICS/sector/industry source anywhere
in `platform.*` — confirmed via `information_schema.columns` (zero
sector-named columns), consistent with TODO.md's recorded "sector-neutral has
no GICS source" finding for the Reversion #171-175 work.** This is a genuine
data BLOCKER for *naive* sector-relative standardization. It is resolved here
with ONE pre-registered fallback that keeps the spec single (NOT a choice
swept at runtime):

> **PRE-REGISTERED SECTOR METHOD (pinned): cross-sectional standardization
> over the full eligible universe per `sim_date` (the "single-group"
> degenerate sector partition).** Every family raw signal is z-scored across
> ALL eligible tickers on that `sim_date`:
> `z(x_i) = (x_i - mean(x)) / std(x, ddof=1)`, with the zero-variance guard
> `std <= 1e-9 → z := 0.0` for every name (a degenerate column carries no
> cross-sectional information; forcing it to 0 is the honest neutral).
> When a future GICS/sector feed is onboarded through the data-provider
> lifecycle, the partition can be refined to per-sector groups **as a new,
> separately-pre-registered Lab candidate** — it is NOT a knob of this one.

Rationale for choosing single-group over a PCA-implied-group port (the
Reversion #171-175 substitute): (a) Vector's composite is a
cross-sectional **ranking** problem; a PCA-residual group structure is the
right tool for a market/sector-neutral *matched book* (Reversion's residual
fade), not for a fixed-weight long-conviction composite — importing it here
would be a second, unrelated hypothesis and a fresh n_trials hazard;
(b) full-universe standardization is the **most conservative** sector
treatment: it makes the weakest claim (no sector structure asserted), so a
SURVIVED verdict is not an artifact of a fitted grouping; (c) it requires zero
new feeds, keeping "Data prereq: none beyond live feeds" (TODO.md) true.
This is an honest, pre-registered fallback for a real BLOCKER — not a
hand-wave (see §8 for the precise gap statement).

### 2.4 The composite + selection (pinned)

`composite(t) = 0.35 * v_z(t) + 0.40 * c_z(t) + 0.25 * tech_z(t)`

where `v_z`, `c_z`, `tech_z` are the standardized family scores from §2.2–2.3.
Weights are exactly `0.35 / 0.40 / 0.25` (value / catalyst / technical), as
pinned by the adjudication. **No weight grid. No `--family-weights`.**

`c_z` is itself the standardized sum `z(earn_signal) + z(insider_signal)`
(§2.2); the family-level weight `0.40` applies to that combined catalyst
family, so earnings and insider-cluster jointly own the largest weight,
matching the deep-research thesis that the *catalyst* family is Vector's
edge axis.

**Selection rule (pinned):** on each `sim_date`, rank eligible names by
`composite(t)` descending; the **top decile** (`ceil(0.10 * N_eligible)`,
minimum 1) is the selected set. The engine then enters from the selected set
using Vector's existing single-position-at-a-time, first-match, next-bar-open
machinery and unchanged exit rules (hard stop / target / trailing / max-hold).
The top-decile rule replaces the AND-gate `if not pass: continue` chain —
nothing else in `_simulate_trade`, sizing, crash-guard, or cost model changes.

### 2.5 The ONE pre-registered long-only vs long-short choice (+ justification)

The two deep-research reports disagree (long-only composite vs dollar-neutral
long-short). The adjudication mandates: **pick ONE, do NOT test both.**

> **PINNED CHOICE: LONG-ONLY (top-decile longs, no short leg).**

Justification against the binding constraints (not report optimism):

1. **Live-safety / fold_existing semantics.** The candidate's `intent` is
   `fold_existing` into Vector. Vector is a long-only Alpaca bracket-order
   per-trade engine (`vector/order_manager.py`, `BaseOrderManager`,
   take-profit + stop-loss). A long-short variant would not be a *fold* —
   it would be a different engine (short borrow, locate, dollar-neutral
   sizing, no bracket-stop symmetry), violating "the candidate is a Lab
   experiment, NOT a live-roster change; nothing in the live
   dispatch/roster/SoT changes."
2. **Single-hypothesis discipline.** Long-short doubles the moving parts
   (short construction, neutralization target, borrow cost model) — each is
   an implicit extra researcher degree of freedom that inflates the true
   n_trials the DSR must deflate. Long-only is the minimal change that tests
   the *composite-vs-AND-gate* hypothesis in isolation.
3. **Cost-model fidelity.** The existing Vector backtest cost model
   (`tpcore.backtest.cost_model` tier round-trip costs, slippage per side)
   is calibrated for long entries/exits. There is no borrow-rate /
   short-rebate model in the Vector path; bolting one on would be an
   unvalidated cost assumption inside a gate that must be honest.
4. **The thesis is preserved.** The reports' shared core claim is that a
   *graded composite beats the binary AND-gate*. That claim is fully
   testable long-only via top-decile selection; the long-short leg is an
   orthogonal "can we also harvest the bottom decile" question — a separate
   future candidate, not this one.

### 2.6 Pre-declared robustness check (ONE, counted as a trial — NOT a sweep)

Exactly ONE robustness check is pre-registered. It is run **once**, reported
in the dossier, and **counted as one additional trial** in the n_trials
accounting (§5). It is NOT a parameter the Lab samples.

> **Robustness check: catalyst-family ablation.** Re-run the held-back
> evaluation ONCE with the catalyst family forced to its earnings-only
> sub-signal (`c_raw = z(earn_signal)`, insider-cluster term zeroed),
> weights unchanged. Purpose: the "**no family > 70% of score variance**"
> gate clause (§5) requires evidence that no single family (and within
> catalyst, no single sub-signal) dominates. This ablation is the pre-
> declared instrument for that clause. It does not change the primary
> verdict; it produces the family-variance evidence the gate consumes.

Two configurations total: the primary composite + this ONE ablation ⇒ the
candidate honestly contributes its own internal trial budget on top of the
Lab's `--trials` sampling correction (§5). No third configuration exists.

---

## 3. Live-safety design (feature-flag-OFF ⇒ byte-identical live path)

This is the make-or-break invariant.

### 3.1 Off-by-default feature flag in `vector/backtest.py`

A new module-level override `_COMPOSITE_MODE_OVERRIDE: str | None = None`
mirrors the existing `_*_OVERRIDE` pattern (e.g.
`_SWING_SCORE_THRESHOLD_OVERRIDE`). A pure accessor
`_composite_mode() -> str` returns `"composite"` iff the override is exactly
the string `"composite"`, else `"and_gate"` (the legacy default — the default
when the override is `None`).

- When `_composite_mode() == "and_gate"` (the default, and the value when no
  Lab override is supplied), `_run` / `run_vector_with_context` /
  `run_for_search` execute the **existing AND-gate code path verbatim**. The
  composite code is in a branch that is never entered.
- When `_composite_mode() == "composite"` (set ONLY by an explicit Lab
  param override), `_run` selects via the §2 composite/top-decile path.

The flag is read the same way the existing overrides are read in
`run_vector_with_context` (the `overrides` dict → module global, reset each
call). No environment variable, no config file, no default-on path anywhere.

### 3.2 The live roster path is untouched

`vector/plugs/setup_detection.py`, `vector/scheduler.py`,
`vector/order_manager.py`, `vector/plugs/*` are **NOT modified by this
candidate**. The composite lives ONLY in `vector/backtest.py` behind the
flag. The live engine dispatch (`scripts/run_all_engines.sh`,
`ops/platform_pipeline.py`, `engine-service`) is unchanged. The candidate is
a backtest-only Lab experiment.

### 3.3 The characterization test that pins byte-identical (T-C)

A new test `vector/tests/test_composite_flag_byte_identical.py` (added in the
build session, not here) asserts:

- **C1 (default path unchanged):** for a fixed `VectorWindowContext` fixture,
  `run_vector_with_context(ctx, overrides={...legacy keys only...})` returns a
  `BacktestRunResult` **field-for-field equal** to the same call on the
  pre-candidate `vector/backtest.py` (pinned via a committed golden, exactly
  as `scripts/search_parameters.py`'s oracle pins the Lab CLI). No legacy key
  may change behaviour.
- **C2 (flag default is and_gate):** `_composite_mode()` returns `"and_gate"`
  when `_COMPOSITE_MODE_OVERRIDE is None` AND when `overrides` omits the
  toggle AND when `overrides={"composite_mode": "and_gate"}`.
- **C3 (composite is reachable & distinct):** with
  `overrides={"composite_mode": "composite"}` the result differs (trade set
  changes) — proves the branch is wired, not dead.
- **C4 (no leakage):** running C3 then C1 in the same process yields C1's
  golden — the module global is reset per call (no cross-trial state bleed,
  mirroring the existing `_*_OVERRIDE` reset discipline).

### 3.4 Lab credibility namespacing (H-S2-3, reused as-is)

`ops/lab/run.py::_lab_credibility_engine_name` already persists Lab
credibility under `lab.<candidate>` (here: `backtest_credibility.lab.
vector_composite`) whenever `candidate is not None` (a `python -m ops.lab`
run). `graduation_ready(pool, "vector")` reads `backtest_credibility.vector`
and can **never** read the experimental score. **No change to this mechanism
is required or made** — it is verified-correct for `vector` already (the same
code path momentum/reversion use). The spec's only obligation is to NOT
introduce any code that writes the experimental score under the bare
`vector` key.

---

## 4. Lab integration (ONE PARAM_RANGES toggle; no CLI/dispatch change)

### 4.1 The ONE new toggle

Add exactly one key to `PARAM_RANGES["vector"]` in `ops/lab/run.py`:

```python
"composite_mode": (0, 0, "choice:and_gate,composite"),
```

`_sample_value`'s existing `choice:` branch
(`kind.startswith("choice:")`) already supports this with **no change to the
sampler**. The `(0, 0, ...)` low/high tuple is the established placeholder for
choice specs (the values are ignored for `choice:`). With this key in the
ranges, the Lab samples `composite_mode ∈ {and_gate, composite}` per trial.

For the `vector_composite` candidate run, the candidate's
`--param-overrides` pins `{"composite_mode": "composite"}` so the candidate's
trials all exercise the composite path; the AND-gate value remains in the
range so the toggle is honest and the legacy default is still expressible
(C2). (The reports' rejected `--family-weights` menu is NOT added — weights
are constants in `vector/backtest.py`, §2.4, never sampled.)

### 4.2 How `run_for_search` / `run_vector_with_context` honor it

`run_vector_with_context` already reads each known override key into a module
global and resets it per call. Add `composite_mode` to that block exactly
like `swing_score_threshold`:

```python
global _COMPOSITE_MODE_OVERRIDE
_COMPOSITE_MODE_OVERRIDE = (
    str(overrides["composite_mode"]) if "composite_mode" in overrides else None
)
```

`run_for_search` delegates to `run_vector_with_context`, so it inherits the
behaviour with no change. `default_params()` and `VECTOR_OVERRIDE_KEYS` gain
`"composite_mode"` (default `"and_gate"`) so the SP3 O1 `default_params`
seam reports the live default and `param_diff` carries the real
`and_gate → composite` delta in the dossier.

### 4.3 No CLI / dispatch / contract change

`vector` is **already** a valid `--target-engine` choice in
`ops/lab/__main__.py` and `ops/lab/run.py`, already in `_runner_for` /
`_context_loader_for` / `_context_runner_for`, already in `PARAM_RANGES`.
Unlike sentinel/catalyst (which would need new dispatch arms), **this
candidate needs ZERO changes to the Lab contract, CLI, dispatch, or
`tpcore/lab/`**. The only Lab-side edit is the single `PARAM_RANGES["vector"]`
key (§4.1).

---

## 5. The held-back gate + n_trials discipline (preserved/strengthened; sacred)

The graduation gate for this candidate is **exactly** the TODO.md
adjudication bar, restated and never relaxed:

| Clause | Threshold | Source / how expressed |
| --- | --- | --- |
| Held-back DSR | **≥ 0.95** | `ops/lab/run.py::compute_dsr_for_verdict(held_period_returns, n_trials=args.trials)` — the existing Lab DSR, deflated for the total sampled trials. |
| Credibility | **≥ 60** | `final_result.credibility_score` (the `CredibilityScore.score`); `survived` already ANDs `>= args.credibility_threshold` (default 60). |
| PBO | **≤ 0.20** | See §5.1 — strengthened from the platform default 0.50 to the adjudication's 0.20; reported in the dossier and gate-checked. |
| Held-back trades | **≥ 150** | `core.held_metrics.n_trades` (`SliceMetrics.n_trades`, in `LabResult.held_metrics`). Strengthened from the Lab's structural `>= 3` floor to the adjudication's `>= 150`. |
| Candidate count | **≥ 3× current gate-model candidate count** | Trade-count proxy: the composite must select/trade ≥ 3× the AND-gate's held-back trade count on the SAME held-back window. Measured by running the AND-gate (`composite_mode=and_gate`) once on the held-back window as the denominator (this is the pre-declared robustness check's sibling measurement — see §5.2; it does NOT add a third primary config, it reuses the C1/ablation runs' AND-gate evaluation). |
| Family variance | **no family > 70% of score variance** | From the §2.6 catalyst-ablation: decompose held-back composite variance into the value/catalyst/technical contributions; assert `max(share) <= 0.70`. The ablation supplies the catalyst-vs-rest evidence. |

**The gate is preserved-or-strengthened on every clause. No clause is
relaxed. The gate is never bypassed — the candidate routes through
`python -m ops.lab` → `_run_lab_core` → `survived` → dossier → ECR exactly
like every other candidate.**

### 5.1 PBO ≤ 0.20 — how it is honestly expressed (the Sentinel contrast)

This is the "dossier-metrics-suffice-for-this-gate" hardening point. Verified
against the code:

- The Lab's primary verdict (`survived` in `_run_lab_core`) is
  `DSR >= 0.95 AND credibility >= 60 AND n_trades >= 3`. DSR is the
  CSCV-of-record's deflation correction (`compute_dsr_for_verdict`,
  `n_trials=args.trials`).
- `OverfittingDiagnostic` computes a true CSCV/PBO **only when a
  `trial_returns_matrix` is supplied** (`tpcore/backtest/overfitting.py`
  `_run_pbo`); `vector/backtest.py` currently calls it with
  `trial_returns_matrix=None`, so `pbo_skipped_reason` is set and
  `CredibilityScore.pbo_passes` is `False`/skipped in the single-run path.
- **Why Vector's gate IS expressible on the existing dossier+DSR path
  (unlike Sentinel):** Vector's gate clauses are all Sharpe/DSR/trade-count
  /variance quantities that are *already computed on the Lab held-back
  slice* — `held_metrics` (n_trades, sharpe, profit_factor, max_drawdown,
  win_rate), `dsr`, `credibility_score`, plus the ablation-derived variance
  decomposition. PBO≤0.20 is satisfiable from the **Lab's own walk-forward
  trial matrix**: `_run_lab_core` already evaluates `per_window_trials`
  candidates per window producing per-trial holdout return series; the build
  task (T5) assembles those per-trial holdout `period_returns` into the
  `trial_returns_matrix` and passes it through so `cscv_pbo` runs and
  `pbo_value` lands in `LabResult.credibility_rubric` / dossier — a genuine
  CSCV-PBO, gate-checked at `≤ 0.20`. Sentinel's adjudication bar (maxDD
  reduction %, ulcer index, inverse-ETF median hold, recession-PnL
  concentration) is **NOT** expressible on the Sharpe/DSR dossier and would
  need a bespoke metrics path — Vector's is not. This is confirmed by code
  inspection: every Vector gate clause maps to an existing or
  trivially-derivable `LabResult` field; none requires a non-Sharpe metric
  family.
- If, for the held-back window, the CSCV matrix is degenerate (a window
  produced < the CSCV minimum of evaluable trials), PBO is **reported as
  skipped with its reason AND the candidate FAILS the gate** (skipped ≠
  pass — red is red; the gate may not be silently waived). This is a
  pre-registered fail-closed rule, not a runtime decision.

### 5.2 n_trials accounting (honest, pinned)

- The Lab's `--trials` (the sampled parameter combinations) is fed verbatim
  into `compute_dsr_for_verdict(..., n_trials=args.trials)` — the DSR is
  already deflated for the search breadth. The candidate run uses the
  `ops.lab.__main__` default `--trials 40` (or an explicit larger value);
  whatever is used is the honest deflation N.
- This candidate adds **exactly TWO configurations** to the engine-lane
  research ledger: (1) the primary composite spec, (2) the ONE pre-declared
  catalyst-ablation robustness check (§2.6). The AND-gate denominator run
  for the "≥3× candidate count" clause (§5) is **not a third hypothesis** —
  it is a measurement of the existing live baseline on the held-back
  window, reusing the `composite_mode=and_gate` evaluation, claiming no
  edge of its own.
- **No sweep.** `composite_mode` is the only added Lab-sampled key, and its
  two values are the legacy default + the single new spec — not a parameter
  grid. Weights, the sector method, the cluster window, the top-decile
  fraction, the long-only choice are ALL constants in code (§2), never
  sampled. Recording this candidate against n_trials is honest precisely
  because there is no hidden grid.

---

## 6. Data prerequisites

| Prereq | Status | Evidence |
| --- | --- | --- |
| `earnings_events` (catalyst earnings sub-signal) | **READY** | 13,848 rows / 1,104 tickers, 2018-01-10→2026-05-15, `EARNINGS_BEAT` (live DB query, 2026-05-19). |
| `sec_insider_transactions` (insider-cluster sub-signal) | **READY** | 646,107 Form-4 line rows / 1,300 tickers, 2018-01-02→2026-05-15, `transaction_type ∈ {BUY,SELL}` (BUY=278,368). Cluster = ≥2 distinct `insider_name` BUY rows / 30d backward window is directly queryable. (Live DB query, 2026-05-19. Matches TODO.md Catalyst note: "646,107 Form-345 rows".) |
| `fundamentals_quarterly` pb/de/revenue (value family) | **READY** | 152,832 complete rows / 5,792 tickers (live DB query, 2026-05-19). |
| Price panels / technical (technical family) | **READY** | Unchanged from live Vector; `_precompute` SMAs/volume. |
| **Sector / GICS / industry source (sector-relative standardization)** | **BLOCKER — RESOLVED by pre-registered fallback** | See §6.1. |

### 6.1 The sector-source BLOCKER (precise gap + the resolving fallback)

**Precise gap:** `platform.*` has **zero** columns named like
`sector`/`industry`/`gics`/`classification` — verified by
`SELECT table_name, column_name FROM information_schema.columns WHERE
table_schema='platform' AND (column_name ILIKE '%sector%' OR ... )` returning
**[] (empty)** on the live DB, 2026-05-19. `platform.ticker_classifications`
is `{asset_class ∈ stock/etf/fund, etf_*}` — it carries NO sector. This
exactly matches the TODO.md record that "sector-neutral has no GICS source so
PCA-implied groups substitute" (Reversion #171-175). A *naive* sector-relative
standardization (z-score within GICS sector) is therefore **not implementable
on current data** and must not be hand-waved.

**Resolution (NOT a hand-wave, NOT a sweep):** the spec pre-registers a
single sector method (§2.3): **full-eligible-universe cross-sectional
standardization** — the degenerate single-group sector partition. This is
the most conservative sector treatment (asserts no sector structure), needs
zero new feeds (keeps TODO.md's "Data prereq: none beyond live feeds" true),
and is a SINGLE pinned method — not a runtime choice and not swept. A
genuine per-sector refinement is explicitly deferred to a **future,
separately-pre-registered** Lab candidate contingent on a GICS feed being
onboarded through the data-provider lifecycle (out of scope here, §10). This
keeps the candidate a sound single-spec experiment despite the BLOCKER.

> **NOTE for the controller:** This is a genuine BLOCKER on the *literal*
> "sector-relative" wording of the adjudication, resolved by an honest
> conservative single-spec fallback. The candidate remains a sound,
> non-hand-waved single-spec Lab experiment. If the operator requires
> *true* per-sector standardization, that is a data-lane prerequisite (a
> GICS feed onboarding) and a different, later candidate — it is NOT a
> blocker on running THIS conservative single-spec experiment, which
> faithfully tests the composite-vs-AND-gate hypothesis.

---

## 7. Reused-vs-new ledger

**Reused (no change):**
- `ops/lab/run.py` walk-forward engine, `_run_lab_core`, `survived`,
  `compute_dsr_for_verdict`, `_lab_credibility_engine_name` (H-S2-3
  namespacing), `_runner_for`/`_context_*_for` vector arms, `_sample_value`
  `choice:` branch.
- `ops/lab/__main__.py` CLI (`vector` already a valid `--target-engine`),
  `LabContext`, `LabCandidate`/`LabResult`, `ops/lab/dossier.py`.
- `vector/backtest.py` `_simulate_trade`, sizing, crash-guard, cost model,
  PIT fundamentals, `VectorWindowContext`/`load_vector_window_context`,
  `compute_search_metrics`.
- `vector/plugs/*`, `vector/scheduler.py`, `vector/order_manager.py` — **NOT
  touched** (live-safety, §3.2).
- `tpcore.backtest.overfitting` CSCV/PBO, `CredibilityScore`,
  `graduation_ready`.
- ECR: `python -m ops.engine_sdlc` (unchanged).

**New (this candidate):**
- `vector/backtest.py`: `_COMPOSITE_MODE_OVERRIDE`, `_composite_mode()`, the
  §2 composite scorer + top-decile selector branch in `_run`, the
  `composite_mode` override wiring in `run_vector_with_context`, the new
  insider-cluster loader (a `sec_insider_transactions` query added to
  `load_vector_window_context` — strictly additive, only consumed in the
  composite branch), `composite_mode` in `VECTOR_OVERRIDE_KEYS`/
  `default_params()`.
- `ops/lab/run.py`: ONE key `"composite_mode"` in `PARAM_RANGES["vector"]`.
- `vector/tests/test_composite_flag_byte_identical.py` (the §3.3
  characterization test, C1–C4).
- `vector/tests/test_composite_scorer.py` (unit tests for the §2 scorer:
  z-score zero-variance guard, PIT backward windows, cluster definition,
  top-decile selection, weight constants).
- This spec.

**No new migrations, no new feeds, no new data adapters, no CLI flags, no
dispatch/daemon/SoT/roster changes.**

---

## 8. Failure modes + Hardening register (H-VC-*)

| ID | Risk | Hardening |
| --- | --- | --- |
| **H-VC-1** | Composite code subtly changes the live/legacy backtest path (the make-or-break invariant). | Off-by-default flag (§3.1); the C1/C4 characterization test (§3.3) pins `BacktestRunResult` field-for-field equal to a committed pre-candidate golden for ALL legacy-key calls; build FAILS if the golden drifts. `vector/plugs/*` & scheduler untouched (§3.2). |
| **H-VC-2** | n_trials inflation via a hidden grid (the platform constraint). | `composite_mode` is the ONLY added Lab-sampled key; weights/sector-method/cluster-window/decile/long-only are CODE CONSTANTS (§2), never sampled. Exactly 2 configs (primary + 1 ablation) recorded against n_trials (§5.2). The rejected `--family-weights` menu is explicitly NOT added (§4.1). A test asserts `PARAM_RANGES["vector"]` gained exactly one key and it is the choice toggle. |
| **H-VC-3** | Sector-source BLOCKER hand-waved. | §6.1 states the exact gap (information_schema = []) + the ONE pre-registered conservative fallback (full-universe single-group standardization), zero new feeds; true per-sector deferred to a separate future candidate. The fallback is pinned in code, not a runtime choice. |
| **H-VC-4** | The graduation gate relaxed or bypassed. | §5 restates every clause preserved-or-strengthened (PBO 0.50→0.20, trades 3→150); routes through `python -m ops.lab` → `_run_lab_core` → `survived` → dossier → ECR like every candidate; experimental credibility namespaced `lab.vector_composite` (H-S2-3, reused) so `graduation_ready(pool,"vector")` can never read it. No `--credibility-threshold`/`--dsr-threshold` override below the gate is permitted in the run command. |
| **H-VC-5** | PBO/family-variance not actually expressible ⇒ silent gate gap (the Sentinel failure mode). | §5.1 verifies by code inspection every clause maps to an existing/trivially-derivable `LabResult` field; PBO uses the Lab's own per-trial walk-forward holdout returns as the CSCV matrix (T5); degenerate CSCV ⇒ **FAIL** (skipped ≠ pass, pre-registered fail-closed). Family-variance from the §2.6 ablation. |
| **H-VC-6** | Lookahead via the catalyst forward half-window or insider-filing-date timing. | The composite uses STRICTLY-BACKWARD windows: earnings `[sim_date - w, sim_date]`, insider `[sim_date - 30, sim_date]` (§2.2) — NOT the live AND-gate's documented ±window. Entry remains next-bar-open. The held-back DSR is therefore lookahead-honest. A unit test pins that no row with date > `sim_date` ever enters a score. |
| **H-VC-7** | Insider-cluster degenerate column blows up z-scores when a `sim_date` has no clusters. | Per-family zero-variance guard `std<=1e-9 → z:=0.0` for every name (§2.3); `insider_signal` standardized independently before being summed into `c_z`; unit-tested. |
| **H-VC-8** | Module-global override bleeds across Lab trials (the existing `_*_OVERRIDE` hazard). | `_COMPOSITE_MODE_OVERRIDE` reset per `run_vector_with_context` call exactly like `_SWING_SCORE_THRESHOLD_OVERRIDE`; C4 characterization test pins no cross-trial bleed. |
| **H-VC-9** | Candidate accidentally treated as a live-roster change. | §3.2 + §10 non-goals; no edits to dispatch/roster/SoT/scheduler/plugs; spec-only change here; build session adds backtest-only code behind the flag. |
| **H-VC-10** | `_build_lab_result` `default_params` seam misses `composite_mode` ⇒ wrong `param_diff`. | `composite_mode` added to `VECTOR_OVERRIDE_KEYS` + `default_params()` (default `"and_gate"`) so `param_diff` carries the true `and_gate → composite` delta (§4.2); unit-tested via the SP3 O1 `default_params(args.engine)` path. |
| **H-VC-11** | Lane/collision: touching a forbidden file. | This change is spec-only. The build session touches ONLY `vector/backtest.py`, `vector/tests/*`, `ops/lab/run.py` `PARAM_RANGES`. It does NOT touch `tpcore/calendar.py`, `tpcore/risk/*`, `ops/engine_supervisor.py`, `ops/engine_service.py`, `ops/engine_ladder.py`, `tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`, or any data-SDLC spec/checklist (read-only symmetry refs only). |

---

## 9. Success / falsification criteria (red is red)

- **SURVIVED** iff ALL of: held-back DSR ≥ 0.95 **AND** credibility ≥ 60
  **AND** PBO ≤ 0.20 (CSCV, not skipped) **AND** held-back trades ≥ 150
  **AND** composite held-back trade count ≥ 3× AND-gate held-back trade
  count **AND** no family > 70% of held-back composite variance. The dossier
  records every clause; ECR proceeds only on SURVIVED + `recommended_exit =
  fold_existing`.
- **FAILED** if ANY clause misses. A FAIL is a genuine, recorded
  falsification of the most cross-spike-evidenced Vector structural fix
  (composite-vs-AND-gate). It is NOT re-run with tweaked weights (that would
  be a sweep / n_trials laundering). The honest outcome is logged; the next
  Vector direction is a separate adjudication.
- PBO **skipped** (degenerate CSCV) is treated as **FAILED**, not waived
  (§5.1, pre-registered fail-closed).

---

## 10. Non-goals

- **NOT a live-roster change.** No edits to `vector/plugs/*`,
  `vector/scheduler.py`, `vector/order_manager.py`, `scripts/run_all_engines.sh`,
  `ops/platform_pipeline.py`, any SoT/roster, or the live dispatch. The live
  Vector path is byte-identical with the flag off (the C1 characterization
  test is the proof obligation).
- **NOT a parameter sweep.** No weight grid, no `--family-weights`, no
  multi-value `--pca-*`/decile/window menu. ONE pinned spec + ONE pre-declared
  ablation.
- **NOT a long-short engine.** Long-only is pinned (§2.5).
- **NOT true per-sector standardization** (no GICS feed exists). The
  conservative single-group fallback is pinned; per-sector is a future
  separate candidate gated on a GICS feed onboarding (data lane).
- **NOT a Sentinel/Catalyst change.** Those are separate candidates with
  their own specs and (for Sentinel) a non-Sharpe gate.
- **NOT touching any of the 8 forbidden files** or any data-SDLC
  spec/checklist (read-only symmetry references only).
- **NOT a gate relaxation.** Every clause preserved or strengthened (§5).

---

## 11. T0–Tn TDD task decomposition

Each task is test-first (write/extend the failing test, then the minimal
code to pass). The build is subagent-driven per the standing workflow;
spec-compliance review then a separate fresh-context code-quality review per
task.

- **T0 — Characterization golden (live-safety baseline first).** Before any
  composite code: capture a committed golden of
  `run_vector_with_context(ctx, overrides={legacy keys})` `BacktestRunResult`
  for a fixed fixture. Write `test_composite_flag_byte_identical.py::C1`
  (RED — no golden yet → make it the pinned oracle). This locks the
  byte-identical contract BEFORE the feature exists.
- **T1 — Feature flag, default off.** Add `_COMPOSITE_MODE_OVERRIDE` +
  `_composite_mode()`; wire `composite_mode` into `run_vector_with_context`
  override block, `VECTOR_OVERRIDE_KEYS`, `default_params()`. Tests C2
  (default = `and_gate`) + C4 (no cross-trial bleed) GREEN; C1 still GREEN
  (flag off ⇒ unchanged). H-VC-1, H-VC-8, H-VC-10.
- **T2 — Composite scorer (pure).** Implement §2.2–2.4: per-family raw
  signals, the §2.3 zero-variance-guarded cross-sectional z-score
  (single-group), the `0.35/0.40/0.25` composite, top-decile selector — all
  pure functions. `test_composite_scorer.py`: weight constants, z guard,
  PIT strictly-backward windows, cluster definition (≥2 distinct BUY
  insiders / 30d backward), top-decile = `ceil(0.10*N)` min 1. H-VC-3,
  H-VC-6, H-VC-7.
- **T3 — Insider-cluster loader.** Add the additive
  `sec_insider_transactions` query to `load_vector_window_context`
  (consumed ONLY in the composite branch). Test: loader returns
  per-ticker dated BUY rows; AND-gate path unaffected (C1 still GREEN).
- **T4 — Wire the composite branch into `_run`.** `_composite_mode() ==
  "composite"` ⇒ score+top-decile selection replacing the AND-gate
  `continue` chain; everything downstream (`_simulate_trade`, sizing,
  crash-guard, cost) unchanged. Test C3 (composite reachable & distinct)
  GREEN; C1/C4 still GREEN. H-VC-1.
- **T5 — Lab PARAM_RANGES toggle + CSCV-PBO matrix.** Add the ONE
  `"composite_mode"` choice key to `PARAM_RANGES["vector"]`. Assemble the
  Lab per-trial walk-forward holdout `period_returns` into the
  `trial_returns_matrix` so `OverfittingDiagnostic` computes a real
  CSCV-PBO that lands in `LabResult.credibility_rubric`/dossier. Test:
  exactly one key added & it is the choice toggle; PBO populated (not
  skipped) on a non-degenerate fixture; degenerate ⇒ FAIL. H-VC-2, H-VC-5.
- **T6 — Pre-declared catalyst ablation + family-variance.** Implement the
  §2.6 ONE ablation run and the held-back composite-variance decomposition
  feeding the "no family > 70%" gate clause. Test: variance shares sum to 1,
  `max <= 0.70` assertion wired into the verdict, ablation counted as one
  trial in the ledger.
- **T7 — Gate assembly + dossier.** Extend the verdict so SURVIVED requires
  ALL §5 clauses (DSR≥0.95 ∧ cred≥60 ∧ PBO≤0.20 ∧ trades≥150 ∧ ≥3×AND-gate
  ∧ family≤70%); skipped-PBO ⇒ FAIL. Test the full §9 truth table on
  fixtures. H-VC-4, H-VC-5.
- **T8 — End-to-end Lab run (the actual candidate).** Run
  `python -m ops.lab --candidate vector_composite --target-engine vector
  --intent fold_existing --param-overrides '{"composite_mode":"composite"}'`
  against the live DB. Record the dossier verdict (SURVIVED/FAILED) honestly.
- **T9 — ECR step.** On SURVIVED only, route the recommendation through
  `python -m ops.engine_sdlc` (ECR). On FAILED, record the falsification;
  STOP (no re-run with tweaked weights — that is a sweep).
- **Tn — Verification-before-completion.** Full test suite green; the C1
  characterization golden unchanged; `grep` confirms no edits to the 8
  forbidden files or any data-SDLC spec/checklist; n_trials ledger shows
  exactly the primary + one ablation; spec-only obligation honored for this
  change.

---

## 12. Self-review (brainstorming / writing-plans gate)

- **Placeholder scan:** no `TODO`/`TBD`/`???`/`<...>` placeholders; every
  constant pinned (weights `0.35/0.40/0.25`, decile `0.10`, insider window
  `30d`, D/E sub-weight `0.5`, PBO `0.20`, trades `150`, family `70%`,
  zero-var eps `1e-9`).
- **Internal consistency:** the sector-source BLOCKER (§6.1) is consistently
  resolved by the §2.3 pinned fallback and reflected in §8 H-VC-3, §10, and
  the controller note. The long-only choice (§2.5) is consistent with the
  `fold_existing` intent (§1) and the no-borrow-model cost reuse (§7). The
  "≥3× candidate count" clause (§5) reuses the AND-gate denominator without
  adding a third hypothesis (§5.2) — consistent with H-VC-2.
- **Scope:** spec-only this change; build touches only
  `vector/backtest.py`, `vector/tests/*`, one `PARAM_RANGES` key. No
  forbidden files; no data-SDLC edits.
- **Ambiguity:** the one genuine ambiguity (literal "sector-relative" with
  no GICS feed) is resolved by an explicit pre-registered single fallback +
  a flagged controller note — not hand-waved.
- **Every requirement → a task:** composite spec→T2/T4; live-safety
  byte-identical→T0/T1/T4 + C1–C4; PARAM_RANGES toggle→T5; gate
  preserved/strengthened→T5/T6/T7; n_trials discipline→T5/T6 + ledger;
  data prereqs→T3 + §6 (BLOCKER stated); H-VC register→§8; T-decomposition
  →§11; non-goals→§10.
- **Gate sacredness:** every clause preserved-or-strengthened (§5); never
  bypassed (H-VC-4); experimental credibility namespaced (H-S2-3, reused).
- **Lane-clean:** engine lane only; the out-of-scope paths enumerated in §13.11 (parallel-session-owned `ops/engine_sdlc/`, `tpcore/lab/llm_emitter/`, `catalyst/`, `tpcore/engine_profile.py`, `tpcore/tests/test_engine_sdlc_planner.py`, `tpcore/tests/test_engine_lifecycle_consistency.py`) and any data-SDLC spec/checklist are read-only symmetry references and untouched.

---

## 13. Lab Candidate Readiness checklist walk-through (the §0-introduced 10 non-optional sections)

`docs/superpowers/checklists/lab_candidate_readiness.md` is the abstracting checklist this spec's pilot inspired. Walking each of its 10 sections against this 2026-05-20 refresh:

**§1 Single pre-registered primary hypothesis.** ✅ This spec, ONE primary metric (held-back DSR ≥ 0.95 ∧ cred ≥ 60 ∧ PBO ≤ 0.20 ∧ n_trades ≥ 150 ∧ ≥3× AND-gate ∧ family ≤ 70% — §5 truth table). At most ONE pre-declared robustness check (§2.6 catalyst ablation). Placeholder scan empty (§12). Every numeric constant pinned (§2). NOT a sweep (§10).

**§2 Feature-flag-variant pattern.** ✅ `_COMPOSITE_MODE_OVERRIDE` in `vector/backtest.py` only (§3.1). Exactly ONE new `PARAM_RANGES["vector"]` key (`"composite_mode": (0, 0, "choice:and_gate,composite")` — §4.1). Override reset per call (§4.2, H-VC-8). `VECTOR_OVERRIDE_KEYS` + `default_params()` gain the new key with the legacy default (§4.2, H-VC-10).

**§3 Byte-identical live path.** ✅ `vector/tests/test_composite_flag_byte_identical.py` C1–C4 (§3.3). C1 pins `BacktestRunResult` field-for-field against a committed pre-candidate golden; C2 default-is-and_gate; C3 composite reachable+distinct; C4 no cross-trial leakage. Golden captured RED-first (§11 T0).

**§4 n_trials ledger acknowledgement.** ✅ Cumulative-not-per-run DSR deflation per §5.2 / SP-A `tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.vector`. Exactly TWO configurations added to the ledger (primary + one catalyst ablation). No hidden grid; weights/sector-method/cluster-window/decile/long-only are code constants (§2).

**§5 Roster-targeting prerequisite (post-SP-B).** ✅ `vector ∈ lab_targetable_engines()` — verified post-carver (the roster now returns `(reversion, vector, momentum, sentinel, carver)`). No edits to Lab CLI / dispatch / `tpcore/lab/` / any SoT/roster (§4.3). Only Lab-side edit is the ONE `PARAM_RANGES["vector"]` key.

**§6 Gate is sacred — preserved or strengthened.** ✅ §5 truth table: DSR ≥ 0.95 (preserved), cred ≥ 60 (preserved), PBO ≤ 0.20 (STRENGTHENED from platform default 0.50), n_trades ≥ 150 (STRENGTHENED from structural floor 3), ≥3× AND-gate (added), family ≤ 70% (added). No `--credibility-threshold`/`--dsr-threshold` override below the gate in the run command (H-VC-4). PBO-skipped ⇒ FAIL (§5.1, pre-registered fail-closed).

**§7 Lab credibility namespacing.** ✅ The candidate writes its experimental credibility under `backtest_credibility.lab.vector_composite` via the existing `_lab_credibility_engine_name` H-S2-3 mechanism (§3.4). No new code that writes the experimental score under the bare `vector` key. No new migrations / no new tables.

**§8 Data prerequisites stated honestly.** ✅ §6 prereq table with concrete row counts + sample dates from live DB queries (2026-05-19, to be re-verified at probe-time). The sector-source BLOCKER is stated precisely (`information_schema.columns` returned empty for sector/industry/gics/classification on `platform.*`) and resolved by ONE pre-registered conservative fallback (§2.3 single-group full-universe standardization). The strictly-additive insider-cluster read in `load_vector_window_context` is consumed only in the composite branch (§3.2, H-VC-1).

**§9 Lookahead / point-in-time honesty.** ✅ Earnings window `[sim_date - w, sim_date]` strictly backward; insider-cluster window `[sim_date - 30, sim_date]` strictly backward; the composite does NOT use the live AND-gate's documented ±window (H-VC-6). Zero-variance guard `std ≤ 1e-9 → z := 0.0` (§2.3, H-VC-7). Entry/exit mechanics, sizing, crash-guard, cost model unchanged (§2.4, §7).

**§10 Compliance verifications (grep-able).** ✅
- Exactly one `PARAM_RANGES["vector"]` key added (`composite_mode` choice toggle).
- Live path files untouched: no diff in `vector/plugs/`, `vector/scheduler.py`, `vector/order_manager.py`, `scripts/run_all_engines.sh`, `ops/platform_pipeline.py`, `tpcore/lab/`, `ops/lab/__main__.py`, or any SoT/roster file.
- Characterization golden present + RED-first.
- `vector ∈ lab_targetable_engines()` verified by one-liner.
- No gate override below the floor in the intended run command.
- n_trials acknowledgement paragraph in §5.2.
- Single-hypothesis attestation in §12 self-review.
- `ruff check carver/` semantics applied to `vector/tests/test_composite_flag_byte_identical.py` and any new test file.

### 13.11 Out-of-scope paths (2026-05-20 — current parallel-session ownership)

H-VC-11 in §8 lists the lane-clean files this candidate MUST NOT touch. The 2026-05-20 list (replacing the pilot's "8 forbidden files"):

- `ops/engine_sdlc/` (any file) — parallel session is shipping the autonomous-criteria-set gate (`feat/autonomous-lab-criteria`).
- `tpcore/lab/llm_emitter/` — SP-G build (#152) just shipped; downstream future work.
- `catalyst/` — catalyst PAPER activation is queued (parallel-session).
- `tpcore/engine_profile.py` — engine roster (ECR-only, hook-blocked).
- `tpcore/tests/test_engine_sdlc_planner.py`.
- `tpcore/tests/test_engine_lifecycle_consistency.py`.

The build session touches ONLY: `vector/backtest.py`, `vector/tests/*`, ONE new `PARAM_RANGES["vector"]` key in `ops/lab/run.py`, this spec doc.
