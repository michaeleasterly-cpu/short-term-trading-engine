# Sentinel maxDD-Reduction Lab Candidate (SP-E proof case)

**Status:** PRE-REGISTERED (single hypothesis, pinned). The SP-E
validation case proving SP-D's pluggable per-engine success scoring.
**Lane:** engine lane. **Epic:** `2026-05-19-lab-front-half-epic.md` §SP-E.
**Intent:** `fold_existing` (a `MODIFY`-class re-tune of an existing
PAPER engine, not a new engine).
**Readiness checklist:** `docs/superpowers/checklists/lab_candidate_readiness.md`
(every section ticked below; §10 mechanical set at the end).

---

## 1. Single pre-registered primary hypothesis (checklist §1)

**Primary hypothesis (ONE, pre-registered, pinned):** Sentinel's
defensive-basket holdout **maximum drawdown is minimized at the
canonical Bear-Score activation threshold of 60**; lowering the
activation threshold to **55** (earlier activation) does **not** reduce
holdout max drawdown.

**Primary metric / verdict (ONE):** `LabPrimaryMetric.MAXDD_REDUCTION`
(SP-D, declared on `sentinel.backtest.LAB_TARGET.primary_metric`). The
ranking is judged by holdout `max_drawdown` (`<= 0` by construction; a
**shallower / less-negative** drawdown is the better candidate and ranks
first under the existing descending sort). This is **deliberately NOT
Sharpe/DSR-expressible** — Sentinel is a macro-defense overlay whose job
is loss mitigation, not return generation; that is the exact contrast
the Vector pilot §5.1 identified and the reason SP-D had to exist before
SP-E could run.

- **No post-hoc metric shopping.** The success/falsification criterion
  is pinned here *before* the run: the candidate FAILS its hypothesis if
  the `55` variant produces a strictly shallower mean holdout drawdown
  than `60`. A FAIL is logged as a genuine falsification and is **not**
  re-run with tweaked parameters (that would be n_trials laundering).
- **At most ONE pre-declared robustness check:** none beyond the single
  toggle. The `choice:` has exactly two members, `{60, 55}`; the legacy
  `60` arm is the baseline/denominator re-measurement of the live path
  and **claims no edge of its own** (it is not a third hypothesis).
- **Every numeric constant is pinned.** Placeholder scan
  (`TODO`/`TBD`/`???`/`<…>`) on this spec comes back empty.
- **This is NOT a sweep.** The only Lab-sampled value is the single
  `activation_score_threshold` `choice:60,55` toggle. Every other
  Sentinel constant (consecutive-days, rally veto, deep-recession
  threshold, VIX breaker, fade days, basket weights, costs) is a **code
  constant**, never Lab-sampled.

## 2. Feature-flag-variant pattern (checklist §2)

- The new code path lives **only in `sentinel/backtest.py`** behind the
  module-level `_ACTIVATION_THRESHOLD_OVERRIDE`, which **defaults to
  `None`/off** and mirrors the engine ecosystem's existing `_*_OVERRIDE`
  pattern (Momentum `_LOOKBACK_OVERRIDE`, Vector `_PB_CEILING_OVERRIDE`).
  The legacy default is `sentinel.models.ACTIVATION_SCORE_THRESHOLD`
  (60) when no override is supplied.
- The variant is reached by **exactly ONE** `LAB_TARGET.param_ranges`
  key — `activation_score_threshold` — a `choice:60,55` toggle whose
  values are `{legacy_default 60, the one variant 55}`. No env var, no
  config file, no second toggle, no default-on path. (Post-SP-B
  `PARAM_RANGES` is roster-driven, so this lives in the engine-OWNED
  `LAB_TARGET` declaration, **not** in `ops/lab/run.py` — the SP-B
  evolution of the checklist §2 "one PARAM_RANGES key" item.)
- The override is read into the module global and **reset per call** in
  `run_sentinel_with_context` (set to `None` when the key is absent), so
  no module-global state bleeds across Lab trials.
- `default_params()` returns the **legacy default** (`60`) so the
  dossier `param_diff` carries the true `60 → 55` delta.
- **`grep` proof:** the only non-test files changed are
  `sentinel/backtest.py` (the variant + the LAB_TARGET declaration) and
  this spec/checklist-doc. `sentinel/plugs/*`, `sentinel/scheduler.py`,
  `sentinel/models.py`, `ops/lab/run.py`, `scripts/run_all_engines.sh`,
  `ops/platform_pipeline.py`, and every SoT/roster file are **NOT** in
  the diff.

The activation threshold is consumed by the live path via
`SentinelLifecycleAnalysis.walk_states`, which binds
`ACTIVATION_SCORE_THRESHOLD` from `sentinel.models` at import. The
backtest variant does **not** edit the plug: it context-shadows
`sentinel.plugs.lifecycle_analysis.ACTIVATION_SCORE_THRESHOLD` for
**exactly the duration of the backtest's `walk_states` call** and
restores it in a `finally`. The live scheduler never enters
`run_sentinel_with_context`, so its `walk_states` reads the unchanged
module value — byte-identical by construction.

## 3. Byte-identical live path (checklist §3 — the make-or-break proof)

`sentinel/tests/test_lab_activation_threshold_byte_identical.py` pins:

- **C1 committed golden:** `run_sentinel_with_context(ctx, overrides={})`
  `BacktestRunResult` is field-for-field equal to a frozen golden of the
  pre-candidate (legacy) behaviour. The build FAILS if the golden drifts.
- **C2 default-is-legacy:** the result is the legacy golden when the
  override is `None`, when the toggle is omitted from `overrides`, and
  when it is explicitly set to the legacy value `60`.
- **C3 variant-reachable-and-distinct:** turning the toggle to `55`
  changes the result (the branch is wired, not dead).
- **C4 no-cross-trial-leakage:** running the `55` variant then a legacy
  call in the same process yields the legacy golden (the per-call
  module-global reset + the `finally` restore of the shadowed constant).
- **Live-path byte-identical:** a separate assertion proves the live
  `SentinelLifecycleAnalysis.walk_states(breakdowns, spy_close=...)`
  call (the scheduler's call, no override) is identical with the Lab
  flag off — the module constant is restored, no residue.

## 4. n_trials ledger acknowledgement (checklist §4)

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
bypassed. This candidate adds **exactly TWO configurations** to the
ledger sample space (the `{60, 55}` `choice:` arm), and the legacy `60`
arm is a denominator re-measurement, not a third hypothesis. There is
**no hidden grid** — the only Lab-sampled value is the one toggle.

## 5. Roster-targeting prerequisite (checklist §5)

`python -c "from tpcore.engine_profile import lab_targetable_engines as f;
print('sentinel' in f())"` prints `True` (sentinel is PAPER,
non-allocator, not the `lab`/`canary` sentinel). SP-E supplies
Sentinel's previously-missing `LAB_TARGET` declaration so the SP-B
resolver no longer hard-rejects it with the SP-E-pointing message — this
is the SP-E deliverable, declared via the engine-OWNED `LAB_TARGET`
constant, **never** by hand-editing the roster or the Lab dispatch. The
candidate adds **zero** changes to the Lab CLI, dispatch, `tpcore/lab/`,
or any SoT/roster.

## 6. The gate is sacred (checklist §6)

The candidate routes through `python -m ops.lab --candidate
sentinel_maxdd --target-engine sentinel --intent fold_existing` →
`_run_lab_core` → `survived` → dossier → ECR like every other candidate.
The verdict is the **unchanged** `DSR ≥ 0.95 ∧ credibility ≥ 60 ∧
n_trades ≥ 3` floor. **No clause is relaxed.** No
`--dsr-threshold`/`--credibility-threshold` below 0.95/60 is used.
`MAXDD_REDUCTION` changes only **which candidate wins the ranking**,
never **whether it may graduate** — proven by SP-D's
`test_lab_sp_d_make_or_break.py` and SP-E's
`sentinel/tests/test_lab_maxdd_ranking_gate_sacred.py` (the maxDD
ranking re-orders candidates while the per-candidate `survived` verdict
is byte-identical to the Sharpe-ranked run).

## 7. Lab credibility namespacing (checklist §7)

Sentinel's experimental credibility writes under the existing
`backtest_credibility.lab.sentinel_maxdd` namespace via the unchanged
`_lab_credibility_engine_name` (H-S2-3) mechanism — the candidate
introduces **no** code that writes the experimental score under the bare
`sentinel` key, so `graduation_ready(pool, "sentinel")` can never read
it. No new migration, no new table, no new SoT — the ledger and
credibility namespace both ride existing `platform.data_quality_log`.

## 8. Data prerequisites stated honestly (checklist §8)

| Datum | Status | Concrete evidence |
|---|---|---|
| `credit_spread` (BAA10Y) | **LIVE** | `tpcore/fred/adapter.py:60` `("credit_spread","BAA10Y")` in the active `INDICATOR_SERIES`; `tpcore/quality/validation/checks/macro_indicators_freshness.py:38` in `EXPECTED_INDICATORS` (hard-gated by the data-layer acceptance gate — stale ⇒ no `DATA_OPERATIONS_COMPLETE`). Consumed by Sentinel via `score_credit_spread` → `bs.credit_spread_pts`. |
| `hy_spread` (BAMLH0A0HYM2) | **LIVE** | `tpcore/fred/adapter.py:69` re-activated 2026-05-16, in `INDICATOR_SERIES`; freshness check line 39; `tpcore/providers.py:98,120` real ProviderBinding (FRED + static-history-recovery FALLBACK). |
| `platform.macro_indicators` (sahm_rule, industrial_production, initial_claims, yield_curve, vix) | LIVE | All in `EXPECTED_INDICATORS`, freshness-gated. |
| ETF prices (`SH`,`PSQ`,`TLT`,`GLD`,`SQQQ`,`SPY`) `platform.prices_daily` | PARTIAL (documented) | `SH/PSQ/SQQQ` may be missing — the backtest re-weights to available tickers (CLAUDE.md Sentinel note). This is a **pre-existing, documented** caveat affecting `60` and `55` arms **identically**, so it does **not** bias the pre-registered comparison. **No new BLOCKER** is introduced by this candidate. |

The candidate adds **no new strictly-additive read** —
`load_sentinel_window_context` reuses the existing `_fetch_etf_prices` /
`_round_trip_cost_by_ticker` / `SentinelSetupDetection.compute_for_range`
helpers; the legacy path is unaffected.

## 9. Lookahead / point-in-time honesty (checklist §9)

Every signal the variant scores uses the same strictly-backward
Bear-Score breakdowns the live plug uses (`compute_for_range`); the
variant changes only the **activation threshold comparison**, not the
data windows, the sizing, the crash-guard, or the cost model. Degenerate
inputs (empty breakdowns) hit the pinned neutral
empty-`BacktestRunResult` guard. Entry/exit mechanics and the per-ticker
tier round-trip cost model are **unchanged** (long-only defensive ETF
basket — the cost direction is correct).

## 10. Compliance verifications (the `grep`-able set, checklist §10)

- **Exactly one toggle added.** `sentinel.backtest.LAB_TARGET.param_ranges`
  has exactly one key, `activation_score_threshold`, a `choice:60,55`
  whose values are `{legacy 60, the one variant 55}`. No menu.
- **Live path files untouched.** `git diff --name-only` contains no
  `sentinel/plugs/`, `sentinel/scheduler.py`, `sentinel/order_manager.py`,
  `scripts/run_all_engines.sh`, `ops/platform_pipeline.py`, `tpcore/lab/`,
  `ops/lab/__main__.py`, or any SoT/roster file.
- **Characterization golden present + RED-first.**
  `sentinel/tests/test_lab_activation_threshold_byte_identical.py` exists
  with the C1–C4 assertions; the golden is captured from the legacy
  (no-override) code path.
- **Roster target verified.** The `lab_targetable_engines()` one-liner
  prints `True` for `sentinel`.
- **No gate override below the floor.** The intended `python -m ops.lab`
  command carries no `--dsr-threshold`/`--credibility-threshold` below
  0.95/60.
- **n_trials acknowledgement present.** Section 4 above.
- **Single-hypothesis attestation.** ONE primary hypothesis (Section 1);
  the placeholder scan is empty; every constant is pinned.
- **`ruff check` clean** on the added tests; no `yfinance`, no Discord,
  no `print()` residue.

## Self-review

- ONE pre-registered primary hypothesis; ONE primary metric
  (`MAXDD_REDUCTION`); placeholder scan empty; every constant pinned.
- Feature-flag-variant satisfied: off-by-default `_ACTIVATION_THRESHOLD_
  OVERRIDE`, exactly one `choice:60,55` toggle, per-call reset, legacy
  default in `default_params()`.
- Gate sacred: SP-D's pluggable metric never reaches `survived`; proven
  by the SP-E gate-sacred test + SP-D's make-or-break.
- Sentinel live path byte-identical with the flag off: proven by the
  C1–C4 + live-`walk_states` characterization test.
- No other engine touched; `tpcore/lab/target.py` stays engine-free.
</content>
</invoke>
