# SP-A2 — DSR Null-Variance Estimator Correction (Hardened Spec)

**Epic:** Lab front-half (`docs/superpowers/specs/2026-05-19-lab-front-half-epic.md`). SECOND sub-project — a statistical-correctness fix that **tightens** the one ungameable gate. Sequenced after SP-A (cross-candidate n_trials ledger, merged `96e6ce6`, PR #93).
**Status:** spec hardened, NOT yet implemented (controller runs expert-harden → writing-plans → subagent-driven-exec next).
**Lane:** engine lane only. `tpcore/backtest/overfitting.py` is shared tpcore — see §7.4 cross-lane note (no concurrent data-lane edit in flight; verified).
**Memory lens:** `project_ml_research_track` (the DSR/n_trials overfit verdict is THE binding platform constraint — this fix makes that constraint statistically *correct*, not merely present), `project_lab_front_half_epic`, `feedback_use_official_docs` (the formula is grounded in the authoritative Bailey & López de Prado source, §1, not assumed knowledge), `feedback_no_shortcuts_100_pct`.
**Origin:** data-lane code-sweep Finding #1, operator-flagged as this epic's next phase (real-money paper→live impact). Engine-lane-owned per the handoff; the file is shared tpcore.

---

## 1. Authoritative basis (cited formula + our variable mapping)

**Citation.** David H. Bailey & Marcos López de Prado, *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality*, Journal of Portfolio Management 40 (5), 94–107 (2014). SSRN 2460551. Authoritative copy: `https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf`. Restated in López de Prado, *Advances in Financial Machine Learning* (Wiley, 2018), Ch. 8. Grounded for this spec on 2026-05-19 via the paper landing + two independent restatements (the Bailey-hosted PDF; the Wikipedia formal statement; the Marti/López-de-Prado restatement) — all three agree on the V-term definition; no discrepancy found (§1.3).

### 1.1 The Deflated Sharpe Ratio

DSR is the Probabilistic Sharpe Ratio (PSR) evaluated against a **deflated benchmark** `SR₀` instead of zero:

```
DSR = Φ( (SR̂ − SR₀) · √(T − 1)  /  √(1 − γ̂₃·SR₀ + (γ̂₄ − 1)/4 · SR₀²) )
```

where `Φ` = standard-normal CDF, `SR̂` = the observed (non-annualized, per-observation) Sharpe of the selected strategy, `T` = number of return observations, `γ̂₃` = skewness, `γ̂₄` = (raw Pearson) kurtosis.

### 1.2 The expected maximum Sharpe under the null (SR₀) — the load-bearing equation

```
SR₀  =  √( V[ŜR_n] )  ·  ( (1 − γ)·Φ⁻¹[ 1 − 1/N ]  +  γ·Φ⁻¹[ 1 − 1/(N·e) ] )
```

with `γ` = Euler–Mascheroni ≈ 0.5772156649, `e` = Euler's number, `N` = number of (independent) trials, `Φ⁻¹` = inverse standard-normal CDF.

**The V term — verbatim from the authoritative sources.** `V[ŜR_n]` is **"the variance across the trials"** — i.e. *the cross-sectional (cross-trial) variance of the Sharpe-ratio estimates computed ACROSS the N tested configurations*. Both independent restatements state this explicitly: "𝐕[{ŜR_n}] is the variance across the trials, and N the number of independent trials"; and "DSR is more accurate than methods based on Šidák correction, because DSR takes into account the dispersion across trials, V[ŜR_n]." It is **NOT** the single-estimator sampling variance of one Sharpe. The whole point of DSR over Šidák is that it ingests this cross-trial *dispersion*: a search over near-identical configs (small `V`) deserves a smaller selection-bias bar than a search over wildly heterogeneous configs (large `V`), even at the same `N`.

### 1.3 Our variable mapping (paper symbol → platform symbol) and the single approximation we keep

| Paper | Platform (`tpcore/backtest/overfitting.py`) | Notes |
| --- | --- | --- |
| `SR̂` | `_per_trade_sharpe(pnls)` (`:74`) | per-trade/event-time Sharpe (module docstring `:27-31` — per-trade space, deliberately not the annualized `statistical_significance` path) |
| `T` | `n` = trade count (`run()` `:359`, `actual` `:372`) | per-trade observation count |
| `N` | `n_trials` (post-SP-A: the **effective cumulative** from the ledger) | §6 — orthogonal to this fix; unchanged |
| `γ` | `EULER_MASCHERONI` (`:67`) | unchanged |
| `Φ⁻¹[1−1/N]`, `Φ⁻¹[1−1/(N·e)]` | `norm.ppf(...)` (`:117-118`) | **correct — explicitly NOT touched (§9)** |
| `γ̂₃`, `γ̂₄` | `_moments()` (`:83-93`) | skew, raw kurtosis (normal = 3) |
| **`V[ŜR_n]`** | **`sr_variance` (`:116`) — TODAY HARD-CODED `1/(n−1)` (THE DEFECT)** | must become the cross-trial Sharpe-vector variance §3 |

**The one documented approximation we deliberately retain.** When a caller genuinely cannot supply a cross-trial Sharpe vector (single-strategy verdict path, §4), there is no `V[ŜR_n]` to compute. The literature's degenerate fallback — and the platform's existing behavior — is the single-estimator null sampling variance `Var(SR̂ | SR=0) ≈ 1/(T−1)`. This is **a known approximation, not the formula**: it conflates within-strategy estimation noise with cross-strategy selection dispersion and is only defensible when `N` is small / the trial set is degenerate. Post-fix it is retained **only** on the explicit fallback path, **named as an approximation in code, and emitted as a structlog WARNING** so a count-only caller is *visibly approximating, never silently* (§3, H-A2-1).

---

## 2. The exact defect + worked numerical example

### 2.1 The defect

`tpcore/backtest/overfitting.py:108-119`:

```python
def _expected_max_sharpe_under_null(n_trials: int, n_obs: int) -> float:
    if n_trials <= 1 or n_obs < 2:
        return 0.0
    sr_variance = 1.0 / (n_obs - 1)                       # ← DEFECT: single-estimator sampling var
    z1 = float(norm.ppf(1.0 - 1.0 / n_trials))            # ← correct, leave (§9)
    z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * math.e))) # ← correct, leave (§9)
    return math.sqrt(sr_variance) * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)
```

`sr_variance` is set to the single-estimator null sampling variance `1/(n_obs−1)`. The paper requires `V[ŜR_n]` = the **cross-trial variance of the per-trial Sharpe estimates across the N searched trials**. These are different quantities:

- `1/(n_obs−1)` → **0 as n_obs → ∞**. With long backtests the selection-bias bar `SR₀` collapses toward 0 — i.e. the more data you have, the *weaker* the multiple-testing correction becomes. That is exactly backwards in spirit: more observations should sharpen each Sharpe estimate, but they say *nothing* about how dispersed the N strategies' Sharpes were — which is what selection bias is about.
- `V[ŜR_n]` is governed by the heterogeneity of the search space and the per-strategy estimation noise, and does **not** vanish with n_obs.

**Effect (the operator-flagged risk):** with realistic large `n_obs`, `SR₀` is **understated** → the DSR `z = (SR̂ − SR₀)·√(T−1)/√(...)` is **overstated** → DSR passes too easily → a future tuned candidate could spuriously clear the 0.95 live gate. Not yet exploited (all five engines currently FAIL DSR — §7). This is a latent correctness defect that the SP-A ledger's larger effective `N` does NOT fix (SP-A scales the `Φ⁻¹` bracket via `N`; the `√V` factor is the orthogonal, still-wrong multiplier — §6).

### 2.2 Worked numerical example (computed, not asserted)

`N = 50` trials. Compare the buggy `√(1/(n_obs−1))` factor to a realistic cross-trial Sharpe-dispersion `√V`:

| n_obs | `1/(n_obs−1)` | `√V` (bug) | `SR₀` (bug) |
| --- | --- | --- | --- |
| 250 | 0.004016 | 0.06337 | **0.14425** |
| 500 | 0.002004 | 0.04477 | **0.10190** |
| 1000 | 0.001001 | 0.03164 | **0.07202** |

| cross-trial `sd(SR̂_n)` | `V` (true) | `SR₀` (true) |
| --- | --- | --- |
| 0.05 | 0.00250 | **0.11382** |
| 0.10 | 0.01000 | **0.22763** |
| 0.20 | 0.04000 | **0.45526** |

At `n_obs = 500`, `N = 50`: the bug yields `SR₀ = 0.102`; a realistic cross-trial dispersion `sd = 0.10` yields `SR₀ = 0.228` — **2.23× larger**. The understatement *grows* with n_obs (at 1000 obs the bug's `SR₀ = 0.072`, ~3× too low vs `sd = 0.10`).

**Spurious-pass mechanism** (candidate per-trade `SR̂ = 0.15`, `n_obs = 500`, `N = 50`, skew 0, kurt 3):

- bug `SR₀ = 0.10190` → DSR = **0.8573**
- correct `SR₀ = 0.22763` (`sd = 0.10`) → DSR = **0.0423**

The gate is `DSR ≥ 0.95`. Here neither clears 0.95 (consistent with §7 — all engines currently fail), but the bug pushes DSR **0.857 vs 0.042** — a >20× probability inflation in the gate-relevant statistic. At a higher candidate Sharpe or lower n_obs the bug's branch crosses 0.95 while the correct branch does not: that is the precise spurious-promotion path the fix forecloses. (Reproduction: the §8 T-WORKED test pins these exact numbers.)

---

## 3. The API change

### 3.1 `_expected_max_sharpe_under_null` — accept the cross-trial variance, keyword-only, documented fallback

```python
def _expected_max_sharpe_under_null(
    n_trials: int,
    n_obs: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    """Expected max sample Sharpe across ``n_trials`` trials under the null.

    Bailey & López de Prado (2014), SSRN 2460551, eqn for SR₀:
        SR₀ = √V · ((1−γ)·Φ⁻¹[1−1/N] + γ·Φ⁻¹[1−1/(N·e)])
    where **V = V[ŜR_n] is the cross-trial variance of the per-trial Sharpe
    estimates across the N searched trials** (selection-bias dispersion),
    NOT the single-estimator sampling variance.

    ``trial_sharpe_variance`` — pass V[ŜR_n] computed from the sweep's
    per-trial Sharpe vector (the statistically-correct path). When ``None``
    (a count-only / single-strategy caller that has no trial vector), fall
    back to the single-estimator null approximation ``1/(n_obs-1)`` AND emit
    a structlog WARNING — this branch is a documented approximation, never
    silent (§1.3, H-A2-1).
    """
    if n_trials <= 1 or n_obs < 2:
        return 0.0
    floor = 1.0 / (n_obs - 1)  # the legacy single-estimator value — now a FLOOR, never the V
    if trial_sharpe_variance is not None:
        # H-A2-10: the honest cross-trial dispersion is used ONLY when it
        # makes the gate the SAME OR HARDER. A low-dispersion / degenerate
        # sweep (V < 1/(n_obs-1), or V≈0 for near-identical configs) must
        # NOT loosen the already-too-lenient bar — clamp up to the floor.
        sr_variance = max(float(trial_sharpe_variance), floor)
    else:
        sr_variance = floor  # KNOWN APPROXIMATION — not the paper's V
        logger.warning(
            "tpcore.overfitting.dsr.null_variance_approximation",
            reason="no per-trial Sharpe vector available; using single-estimator "
                   "1/(n_obs-1) instead of cross-trial V[SR_n]",
            n_trials=n_trials, n_obs=n_obs,
        )
    z1 = float(norm.ppf(1.0 - 1.0 / n_trials))
    z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
    return math.sqrt(sr_variance) * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)
```

- **Keyword-only, default `None`** ⇒ every existing positional caller compiles unchanged and gets the *documented + warned* approximation — backward-compatible by construction, never a silent behavior flip.
- The `z1`/`z2` lines (`:117-118`) are **byte-identical** — line 118 `norm.ppf(1-1/(n_trials*e))` is correct per the finding and is explicitly out of scope (§9).
- **`max(V, 1.0/(n_obs−1))` (H-A2-10) — not `max(V, 0.0)`.** The original `max(...,0.0)` is *unsafe*: it would let a degenerate/low-dispersion sweep drive `SR₀→0` and DSR→PSR-vs-0 (numerically 0.9996 at SR=0.15 — a catastrophic *loosening* of a live gate). The floor at the legacy `1/(n_obs−1)` makes the change provably "tightening or equal" for **every** input: where real dispersion exceeds the floor (the production case at large n_obs) the gate hardens correctly; where it doesn't, the gate is *no looser than today* (the worst case is the status quo, never worse). "V=0 ⇒ no selection bias" is rejected as statistically false at large n_obs — zero observed dispersion over few trials means the estimator has no information, which must be treated conservatively, not as a licence to zero out the bar. The genuine N=1 case is unaffected (the `n_trials <= 1` short-circuit returns 0.0 *before* V is read — DSR degrades to PSR exactly, correct: no selection over one trial).

### 3.2 `_deflated_sharpe_ratio` — thread the variance through

```python
def _deflated_sharpe_ratio(
    sr: float, n: int, skew: float, kurt: float, n_trials: int,
    *, trial_sharpe_variance: float | None = None,
) -> float:
    threshold = _expected_max_sharpe_under_null(
        n_trials, n, trial_sharpe_variance=trial_sharpe_variance)
    return _psr_per_trade(sr, threshold, n, skew, kurt)
```

Keyword-only pass-through; existing 5-positional-arg callers unchanged (warned-approximation path).

### 3.3 `OverfittingDiagnostic` — derive V from the *already-present* `trial_returns_matrix`

`OverfittingDiagnostic.__init__` **already accepts** `trial_returns_matrix: np.ndarray | pd.DataFrame | None` (`:333`, today consumed only by `_run_pbo` via `_column_sharpes`). This is *exactly the per-trial Sharpe-vector source the fix needs* — no new constructor parameter. In `run()` (`:365`):

```python
trial_sharpe_var = self._trial_sharpe_variance()  # None if no matrix
dsr = _deflated_sharpe_ratio(
    sr_internal, n, skew, kurt, self._n_trials,
    trial_sharpe_variance=trial_sharpe_var,
) if n >= 2 else 0.0
```

New private helper (reuses the existing `_column_sharpes`, the same per-column Sharpe vector PBO already computes — one canonical Sharpe-vector definition, no second estimator):

```python
MIN_TRIALS_FOR_V = 5  # H-A2-10: below this the cross-trial variance is too
                      # noisy to trust as a selection-bias estimate

def _trial_sharpe_variance(self) -> float | None:
    if self._trial_matrix is None:
        return None
    arr = (self._trial_matrix.values
           if isinstance(self._trial_matrix, pd.DataFrame)
           else np.asarray(self._trial_matrix))
    if arr.ndim != 2 or arr.shape[1] < MIN_TRIALS_FOR_V:
        # < MIN_TRIALS_FOR_V columns → the cross-trial variance is
        # statistically unreliable (N=2..4 sample variance is wildly
        # noisy) → documented fallback (the §3.1 floor at 1/(n_obs-1)
        # keeps the gate safe — H-A2-10).
        return None
    col_sharpes = _column_sharpes(arr)              # one Sharpe per searched trial
    return float(np.var(col_sharpes, ddof=1))       # V[ŜR_n] across the N trials
```

**Trial-set consistency invariant (H-A2-4, load-bearing — CLARIFIED by expert-harden).** The variance is the dispersion over the trial *sample* available at the call site; `n_trials` is the multiple-testing *count* (post-SP-A: the cumulative selection budget). These are deliberately **distinct estimands** in the SR₀ formula — `√V` is a dispersion scale, `Φ⁻¹[1−1/N]` is the order-statistic count over the selection budget — and are NOT required to be the same number (the original "must equal that N" wording was over-strong and would have *forbidden* the correct SP-A composition). The honest, statistically-defensible construction: V is estimated from the most representative trial sample to hand (the current sweep's N columns / the `trials` list at the verdict site) and `n_trials` carries SP-A's cumulative budget; **the V-source trial count and `n_trials` MUST be logged side-by-side at every site so any divergence is visible, never silently reconciled** (H-A2-4). Where a matrix *is* supplied to `OverfittingDiagnostic`, the helper computes V over the matrix's columns and `run()` emits the (V-trial-count, `self._n_trials`) pair at INFO; no assertion that they are equal (a deliberate cumulative-N caller is correct, not an error). Fabricating a cumulative-spanning Sharpe vector to force equality is an explicit non-goal (§9, H-A2-3) — a representative-sample dispersion scale is the honest estimator. See §6 for the full SP-A composition rule.

### 3.4 `compute_dsr_for_verdict` (`ops/lab/run.py:423`) — the second DSR implementation; the per-trial vector IS available at its production call site

> **CORRECTED by expert-harden 2026-05-19 (H-A2-9, CRITICAL).** The original draft asserted this path "structurally has no per-trial Sharpe vector — by construction it sees only the held-back winner, never the N sweep trials." **Independent code trace falsified that.** `compute_dsr_for_verdict` is called from exactly ONE production site, `ops/lab/run.py:769`, inside `_run_lab_core`. At that exact line, `trials: list[TrialResult]` (populated at `:711`, already consumed by `rank_candidates(trials)` at `:723`) is fully in scope; **every `TrialResult` carries `holdout.sharpe` — a real per-trial OOS Sharpe across the N searched configs.** A genuine cross-trial Sharpe vector is therefore *structurally present in the same function* that computes the verdict DSR. The path is count-only only because of the *current function signature*, NOT because the data is unavailable — this is precisely the data-lane Finding #1 instruction ("pass var(per-trial Sharpe vector) derived from the sweep's EXISTING trial structure — the sweep HAS it"). Treating it as legitimately count-only would render SP-A2 **inert plumbing** (see H-A2-9 / the corrected §4 conclusion).

This is a **separate, independent DSR implementation** (not a caller of `overfitting.py`), used by the SP-A Lab-verdict path. It receives `held_period_returns` (the *single winning candidate's* held-back period-return series) + `effective_n_trials` (a count from the SP-A ledger). Three correctness obligations:

1. **Thread the real cross-trial V (the load-bearing fix — H-A2-9).** `compute_dsr_for_verdict` gains a keyword-only `trial_sharpe_variance: float | None = None`, applying the identical `√V`/fallback treatment as `_expected_max_sharpe_under_null` (§3.1). The production call site `ops/lab/run.py:774-775` MUST compute `V` from the in-scope `trials` list and pass it. **UNITS CORRECTION (H-A2-11, CRITICAL — independent expert-harden 2026-05-19).** `t.holdout.sharpe` (`SliceMetrics.sharpe`, `ops/lab/run.py:255-258`) is **ANNUALIZED** (`mean/std·√(periods_per_year)`), whereas `compute_dsr_for_verdict`'s own `SR̂` (`:431`) is the **per-period (non-annualized)** Sharpe of `held_period_returns`. Putting `np.var(annualized_sharpes)` into the same SR₀ formula as a per-period `SR̂` is a units mismatch that inflates V by ≈`periods_per_year` (≈25–250× for typical holdouts) ⇒ a nonsensically huge SR₀ ⇒ DSR≈0 always (gate not "tightened", *broken*). The implementer MUST compute V from a **per-period (non-annualized) per-trial Sharpe** consistent with `SR̂`. **`TrialResult` retains only the scalar `SliceMetrics` — NOT the per-trial holdout period-return vector** (`ops/lab/run.py:342-351`); and `periods_per_year` is not persisted either, so de-annualizing `t.holdout.sharpe` post-hoc is not possible from current state. The implementer MUST add a `holdout_sharpe_per_period: float` field to `SliceMetrics` (computed in `compute_slice_metrics_from_trades` as the *un-annualized* `period_returns_arr.mean()/period_returns_arr.std(ddof=1)`, the same quantity before the `·√periods_per_year` factor at `:255-258` — a pure additive field, the annualized `sharpe` stays byte-identical so `_score_for_ranking`/rankings/the §5 oracle are untouched), carry it through `_evaluate_candidate_with_context`, and at the verdict site compute `V = np.var([t.holdout.holdout_sharpe_per_period for t in trials if not t.error], ddof=1)`. The "same per-trial Sharpe `rank_candidates` aggregates" claim is **withdrawn**: `rank_candidates` ranks on the annualized `_score_for_ranking(t.holdout)`, which is the wrong space for the DSR V-term. One canonical *per-period* Sharpe definition (the new field), derived once in `compute_slice_metrics_from_trades`, used for the V-term only. This is a `SliceMetrics`/`compute_slice_metrics_from_trades` structural change — in scope for SP-A2 (it is the only honest way to deliver the §3.4 obligation), additive, ranking-neutral, oracle-neutral. The current `e_max` (`:441-442`, pure `Φ⁻¹` bracket) gains the `√V` factor exactly as the overfitting.py path; the legacy `1/(n−1)` folded into `denom` (`:444`) is **removed from the V role** (it conflates within-strategy estimation noise into the selection-bias term — the same defect, expressed differently) and the V-term is supplied solely by `trial_sharpe_variance`. When `trial_sharpe_variance is None` (the oracle's direct two-arg call; any non-Lab caller), fall back to the documented `1/(n−1)` **with the explicit "KNOWN APPROXIMATION" comment + one structlog WARNING** — identical to §3.1, never silent. The `None`-default keyword-only addition keeps every existing positional/two-arg call (the oracle, the ledger tests) compiling and on the warned-fallback path unchanged (signature backward-compat by construction; this is what preserves the §5 oracle property-contract — see corrected §5).
2. **V/N trial-population coherence (H-A2-4, made explicit — see corrected §6).** The `V` is the dispersion over **this run's** N trial configs; `effective_n_trials` is the SP-A **cumulative** selection budget (a different, larger trial population). The honest, statistically-defensible construction (not a silent mismatch): `V` is a *dispersion-scale estimate from a representative trial sample* (this run's sweep), while `N` is the *multiple-testing count* (the cumulative budget) — these are **distinct estimands** in the SR₀ formula (`√V` is a scale, the `Φ⁻¹[1−1/N]` bracket is the order-statistic count); pairing this-run V with cumulative N is the principled choice **and MUST be documented in code + the dossier + logged** (the V-source trial count and the N value emitted side by side at the verdict site, so any divergence is visible, never silently reconciled — H-A2-4). A min-trials guard (H-A2-10) governs when this-run V is trustworthy enough to use at all.
3. **No silent divergence:** a code comment in BOTH implementations cross-references the other and states the V-term semantics (cross-trial dispersion, `ddof=1`, the V/N estimand split), so a future reader cannot "fix" one and miss the other.

(Unifying the two DSR implementations into one is **explicitly out of scope** — §9; it would change the SP-A characterization surface and is a separate refactor. SP-A2 corrects the variance *semantics in both* AND wires the real V at the one production site that structurally has the vector — it is NOT inert.)

---

## 4. Per-caller migration matrix (which sites CAN supply the real V, which legitimately cannot)

| Call site | Reaches DSR via | Per-trial Sharpe **vector** available there? | SP-A2 disposition |
| --- | --- | --- | --- |
| `tpcore/backtest/search.py:196` `compute_search_metrics` → `OverfittingDiagnostic` | overfitting.py | **Not today** — instantiates `OverfittingDiagnostic` with no `trial_returns_matrix`. The sweep that produced the winner *does* iterate N configs but `compute_search_metrics` is called per-winner with only that winner's trades. | Threads `trial_returns_matrix` IF the orchestrator already assembles an N-column trial matrix (it assembles one for PBO in some paths — verify at impl); otherwise documented-fallback + WARNING. **No fabrication of a vector** where one doesn't exist. |
| `reversion/backtest.py:1515` `OverfittingDiagnostic(... n_trials=20)` | overfitting.py | **No** — locked-config diagnostic, single winner's trades, no matrix. | Documented fallback + WARNING. Legitimately count-only (frozen-param post-hoc diagnostic, not a live search). |
| `reversion/backtest.py:1382-1416` sweep (`build_report`, NOT `OverfittingDiagnostic`) | NOT a DSR call site (`build_report`/`statistical_significance`) | A per-trial sweep structure exists here (z×mode) but it does **not** flow into `_deflated_sharpe_ratio`. | Out of scope — not a DSR site. Noted so impl does not mistake it for one. |
| `vector/backtest.py:1259` `OverfittingDiagnostic(... trial_returns_matrix=None)` | overfitting.py | **No** — explicitly `None`. | Documented fallback + WARNING. |
| `vector/backtest.py:964`, `momentum/backtest.py:526`, `sentinel/backtest.py:374` → `compute_search_metrics` | overfitting.py | **No** matrix passed. | Documented fallback + WARNING. |
| `canary/backtest.py` | **No DSR call at all** (heartbeat engine, non-graduating by spec §4b — never calls credibility/DSR). | N/A | Untouched. Confirmed by grep (no `OverfittingDiagnostic`/`compute_dsr`/`overfitting`). |
| `ops/lab/run.py:774-775` `compute_dsr_for_verdict(held_period_returns, …)` (SP-A verdict path; `scripts/search_parameters.py` re-export shim) | the **separate** `compute_dsr_for_verdict` impl | **YES — corrected (H-A2-9).** The call is inside `_run_lab_core` where `trials: list[TrialResult]` is in scope, already consumed by `rank_candidates`. The vector is structurally present; the original draft's "structurally count-only" claim was falsified by trace. **But `t.holdout.sharpe` is ANNUALIZED — wrong space for the V-term (H-A2-11); a new per-period `SliceMetrics` field is required (§3.4 obл.1 corrected).** | **Thread real V** — `np.var([t.holdout.holdout_sharpe_per_period for t in trials if not t.error], ddof=1)` (the new non-annualized field, NOT `t.holdout.sharpe`), passed as `trial_sharpe_variance`, subject to the H-A2-10 min-trials guard and the H-A2-12 ledger-test signature fix. This is the ONE production site where SP-A2 actually tightens the gate. |
| `archive/sigma/backtest.py` | archived (RETIRED) | N/A | **Not touched** — archived engine, frozen. |
| `tpcore/templates/engine_template/backtest.py` | template | N/A | Template inherits the new keyword-only signature with the documented default; if it instantiates `OverfittingDiagnostic` it gets the warned fallback like any count-only caller. Update the template comment to point new engines at threading `trial_returns_matrix` when they have a sweep. |

**Conclusion (CORRECTED by expert-harden — H-A2-9, the crux).** The original draft concluded "*Zero* current call site has a live per-trial Sharpe vector flowing into DSR" and that SP-A2's immediate effect is only "correctness-where-a-matrix-exists + loud-approximation-everywhere-else." **Independent per-call-site trace falsified the load-bearing half of that conclusion:**

- For the 5 engine `OverfittingDiagnostic` paths (via `compute_search_metrics` or the reversion direct call) the draft is **correct**: no production code anywhere passes a non-`None` `trial_returns_matrix` (`git grep trial_returns_matrix=` ⇒ the sole hit is `vector/backtest.py:1266`, explicitly `=None`). `compute_search_metrics` does not even *expose* the parameter. These sites legitimately take the documented fallback+WARNING. **But for these the §5 oracle is irrelevant and the gate is genuinely NOT tightened — the engine credibility numbers do not move.**
- For the **Lab verdict path** (`compute_dsr_for_verdict` @ `ops/lab/run.py:769`) the draft is **WRONG**: the per-trial Sharpe vector IS structurally in scope (`trials` list, `t.holdout.sharpe`). This is the SP-A Lab orchestrator — the one path the Lab front-half epic exists to harden, the one path that produces graduation-relevant verdicts, and the one the SP2 oracle exercises.

**Therefore SP-A2's deliverable is reframed (binding):** SP-A2 MUST wire real cross-trial V into the **Lab-verdict path** (§3.4 obligation 1, H-A2-9) so the gate is *demonstrably tightened on a real engine's trial dispersion* (pinned by T-DELIVERED, §8). Leaving the verdict path on the fallback because "no matrix" would make the entire sub-project **inert plumbing + a log line** — the precise CRITICAL failure mode the brief names. The `OverfittingDiagnostic` keyword-only seam (§3.3) remains correct *and useful for future matrix-supplying callers* (and for SP-B/C), but it is NOT the delivery vehicle today; the verdict-path wiring IS. The fix's immediate effect is: (a) the **Lab verdict DSR strictly drops** for any real (non-degenerate) sweep dispersion — the gate genuinely tightens where it matters; (b) the 5 engine `compute_search_metrics` paths loudly declare the documented fallback (visibility, no numeric change — honestly stated, no overclaim); (c) the V<1/(n_obs−1) loosening regime (H-A2-10) is closed by a guard so the "tightening only" safety property is *made true*, not merely asserted.

---

## 5. The SP2 characterization-oracle resolution (THE decision)

**Investigated artifact:** `scripts/tests/test_search_parameters_characterization.py` (338 lines, sacred per the brief — byte-unmodified + green through the entire SDLC + SP-A epics).

**What it actually asserts about the DSR path** (read in full, `test_compute_dsr_for_verdict_bounds_and_monotone`, lines 82-92):

```python
d_flat   = sp.compute_dsr_for_verdict(flat,   n_trials=200)
d_strong = sp.compute_dsr_for_verdict(strong, n_trials=200)
assert 0.0 <= d_flat <= 1.0 and 0.0 <= d_strong <= 1.0          # BOUNDS
assert d_strong >= d_flat                                        # MONOTONE in signal
assert sp.compute_dsr_for_verdict(strong, n_trials=2000) <= d_strong + 1e-9  # MONOTONE in n_trials
```

The `amain` smoke tests (`test_amain_smoke_survived_verdict`, `test_amain_lab_path_namespaces_credibility`) set `dsr_threshold = 0.0` (fully permissive — *any* DSR passes) and assert only `rc == 0`, the persisted credibility `engine_name`, and override-isolation (O2). The module docstring states its purpose verbatim: *"capture what the un-refactored script DOES (not what it should do)"* — but the only DSR thing it pins is **three mathematical PROPERTIES** (bounds, monotone-in-signal, monotone-in-N). **It pins NO golden DSR numeric. It does NOT snapshot an end-to-end search output's DSR value.** `_norm_inv` is pinned only as a *mathematical property* (`Φ⁻¹(0.975) ≈ 1.95996`, with an explicit comment that this is "a mathematical property … NOT an implementation snapshot").

**RESOLUTION — the oracle is a PROPERTY/PARITY CONTRACT, not a numeric characterization snapshot. The SP-A2 fix is fully compatible with it AS-IS. No re-baseline is required, and none is permitted (re-baselining a contract that doesn't move would be noise that masks a future real regression).**

Principled reasoning:

1. **The fix preserves every asserted property.** Bounds: PSR is a CDF ∈ [0,1] regardless of `SR₀` (an honest `V` only changes *where* `SR₀` lands, never the codomain). Monotone-in-signal: a higher `SR̂` strictly increases the DSR `z` for any fixed `SR₀` — unchanged. Monotone-in-n_trials: `SR₀` is still increasing in `N` via the untouched `Φ⁻¹` bracket (§9); a larger `√V` is an N-independent positive scalar, so `n_trials=2000 ⇒ DSR ≤ n_trials=200` still holds. The §8 test plan includes T-ORACLE which *runs the oracle unmodified and asserts green* — proving the contract is honored, not bypassed.
2. **`compute_dsr_for_verdict` is the one DSR path the oracle exercises, and it is the structurally-count-only path (§3.4, §4) that legitimately stays on the documented `1/(n−1)` fallback.** SP-A2 changes its *observable numeric* by ≤ a logging side-effect only (the fallback math is byte-identical; only a WARNING + comment are added). The oracle's three properties are mathematically invariant under "add a structlog warning" — so even the path the oracle touches is property-stable *and* numerically unchanged on the fallback branch.
3. **The brief's worst-case ("a correctness fix silently suppressed to keep an oracle green") cannot arise here, because the oracle never constrained the buggy numeric in the first place.** There is no green snapshot encoding `1/(n−1)` to protect. The fix is not gated, not opt-in, not suppressed: it ships as the default-correct behavior wherever a matrix exists, and the oracle stays byte-unmodified and green by *property*, not by accident.
4. **Contrast — the rejected alternative.** Had the oracle pinned a golden DSR float, the principled call would be a *deliberate, reviewed re-baseline*: update the snapshot in the same commit, with a before/after table, the §1 statistical justification, and a docstring forbidding silent reversion (the SP-A H-LL-7 pattern). We investigated specifically to determine which regime applies and it is unambiguously the property-contract regime — so we take the stronger, no-change resolution and **explicitly forbid** introducing a numeric snapshot here (a numeric snapshot of a fallback-path approximation would *itself* be a defect — it would cement the approximation as a contract).

**Binding instruction to the implementer:** `scripts/tests/test_search_parameters_characterization.py` stays **byte-unmodified**. Its diff vs `origin/main` MUST remain empty (the standing epic invariant). T-ORACLE (§8) is the make-or-break proof that this holds. If any property assertion in it ever goes red under the fix, that is a real regression in the fix, NOT a signal to edit the oracle.

**Scope correction (H-A2-12, CRITICAL — independent expert-harden 2026-05-19).** This §5 resolution covers **only** the SP2 characterization oracle (`scripts/tests/test_search_parameters_characterization.py`). It does NOT clear `tpcore/tests/test_lab_ntrials_ledger.py` — a *separate* SP-A MAKE-OR-BREAK test file that the original spec wrongly asserted was "kept compiling and unchanged by the `None`-default" (§3.4 obл.1, now corrected). That file is **NOT** monkeypatch-free: it replaces `compute_dsr_for_verdict` with a `_spy(r, *, n_trials)` stub at three sites (lines 408, 625, 739) then drives `_run_lab_core`. Post-SP-A2 the production call site passes the new `trial_sharpe_variance=` kwarg into the *replaced* symbol ⇒ **`TypeError`, ≥4 MAKE-OR-BREAK SP-A ledger tests crash** (`test_second_candidate_same_target…`, the first-ever-run reduction test, the legacy-path test, the append-only test). The `None` default on the real function is irrelevant — the real function is not what the stub-driven call hits. Additionally `core.dsr == real_dsr(returns, n_trials=37)` (`:644`) and `core2.dsr <= core1.dsr` (`:441`) pin the Lab-path DSR numeric; threading real V on the LHS while the RHS is a fallback two-arg call breaks the equality unless the fixture's trial count is `< MIN_TRIALS_FOR_V` (which the harness does NOT guarantee — `_install_offline_harness` builds 40-return windows). **These tests ARE editable (not the byte-unmodified oracle) and MUST be updated in the SAME commit:** (a) widen each `_spy`/`_spy_dsr` stub signature to `def _spy(r, *, n_trials, trial_sharpe_variance=None)` so the production call site does not crash; (b) re-derive the pinned `core.dsr == …`/`core2.dsr <= core1.dsr` references against the *new* V-threaded behavior, with a before/after note in the commit and a docstring forbidding silent reversion (the SP-A H-LL-7 re-baseline pattern — a *deliberate, reviewed* re-baseline of an editable test, NOT a suppression). T-LEDGER-COMPAT (§8) is the make-or-break that pins this. Distinguishing the byte-frozen oracle (no re-baseline, §5) from the editable ledger tests (deliberate reviewed re-baseline, H-A2-12) is the correct, honest disposition — conflating them was the original spec defect.

---

## 6. Composition with SP-A (orthogonality proof)

SP-A (PR #93) makes `n_trials` the **effective cumulative** count: `effective_n_trials = cumulative_n_trials(target) + args.trials` (`ops/lab/run.py:764-770`), fed as the `n_trials` argument. In the `SR₀` equation:

```
SR₀  =  √V  ·  ( (1−γ)·Φ⁻¹[1 − 1/N]  +  γ·Φ⁻¹[1 − 1/(N·e)] )
        └┬─┘    └──────────────── the N term ────────────────┘
       SP-A2                         SP-A
```

- **SP-A owns `N`** — the `Φ⁻¹` bracket. SP-A2 does **not** touch `N`, `norm.ppf`, or the bracket (§9). The SP-A ledger and its `effective_n_trials` are **not altered** (explicit non-goal §9).
- **SP-A2 owns `√V`** — the multiplicative scale factor. It is **N-independent**: a positive scalar multiplying the SP-A bracket.
- They compose multiplicatively into the *same* `SR₀`, are mathematically separable (a scalar × a function-of-N), and neither's correctness depends on the other's value. Both correctly flow into the one DSR. T-ORTHO (§8) pins this: holding the per-trial variance fixed and varying `effective_n_trials` reproduces SP-A's monotone-in-N behavior; holding `N` fixed and varying V reproduces SP-A2's monotone-in-V behavior; the two effects multiply.
- **Trial-set consistency at the Lab-verdict site (H-A2-4).** The SP-A `effective_n_trials` is a *cumulative* count spanning many runs; the per-trial Sharpe vector (where available) is a *single sweep's* N columns. These N's are NOT the same number, and that is *correct*: the paper's `Φ⁻¹` bracket takes the cumulative selection-budget `N` (SP-A's anti-laundering semantic — the right multiple-testing count), while `√V` is a *dispersion scale* estimated from whatever representative trial sample is available. The Lab-verdict path (`compute_dsr_for_verdict`) is structurally count-only (§4) so it takes the documented `1/(n−1)` fallback for `√V` and SP-A's cumulative `N` for the bracket — the two are independently correct on that path. Where a single-sweep matrix *is* threaded into `OverfittingDiagnostic`, V is the dispersion of that sweep's N columns while `n_trials` may be a different (e.g. cumulative) N: the §3.3 helper computes V over the matrix it has and the inconsistency is *logged, not silently reconciled* (H-A2-4) — the honest behavior, since fabricating a cumulative-spanning Sharpe vector would be worse than an honest scale estimate from a representative sweep.

---

## 7. Blast radius / live-safety (the safety argument for shipping the tightening)

1. **Direction of the change is tightening ONLY ABOVE a guarded floor — CORRECTED by expert-harden (H-A2-10, CRITICAL).** The original draft asserted the change "can only *raise or leave unchanged* `SR₀`." **This was numerically falsified.** `SR₀` scales with `√V`; the fallback uses `√(1/(n_obs−1))`. For `V < 1/(n_obs−1)` the corrected `SR₀` is **LOWER** than the bug ⇒ DSR **HIGHER** ⇒ the gate **LOOSENS**. At a realistic `n_obs=500, N=50`, any sweep whose cross-trial Sharpe **sd < 0.0448** (i.e. tightly-clustered configs — *common* in fine-grid local search) yields a *looser* gate; a **degenerate all-identical sweep ⇒ V=0 ⇒ SR₀=0 ⇒ DSR collapses to PSR-vs-0** (numerically: candidate SR=0.15, n=500 ⇒ DSR **0.9996** vs the bug's 0.8573 — a catastrophic loosening that could spuriously clear the sacred 0.95 gate). The §3.1 `max(V,0.0)` guard does NOT address this — it guards negativity, not the V<1/(n−1) loosening band, and "V=0 ⇒ no selection bias" is statistically WRONG at large n_obs (zero observed dispersion over few trials = the estimator has no information, not "no selection"). **Binding correction (H-A2-10):** SP-A2 MUST add a guarded floor — when the trustworthy cross-trial V (from ≥ `MIN_TRIALS_FOR_V` non-errored trials) is computed, use `sr_variance = max(V, 1.0/(n_obs−1))` (never let the honest correction make the gate *looser* than today's already-too-lenient bar); when `< MIN_TRIALS_FOR_V` trials or no vector, take the documented `1/(n_obs−1)` fallback + WARNING. With this floor the change is **provably tightening (≥) for every input** — the §7 safety property becomes *true by construction*, not asserted. Pinned by the strengthened T-STRICTER (§8) over a grid that explicitly includes the low-dispersion / degenerate band.
2. **Nothing can be spuriously promoted live by this change, now or at ship.** Per CLAUDE.md + `project_ml_research_track`, **all five live engines currently FAIL the DSR/credibility gate** (positive OOS edge ~0.78–1.26 but DSR/credibility is the binding constraint; no engine has graduated; LIVE is reserved, paper-only mandate). Tightening a gate that everything already fails cannot flip anything to PASS. The only reachable outcomes are: a currently-failing engine *fails harder* (correct), or a count-only site starts *emitting a WARNING* (visibility only). There is **no path** by which SP-A2 promotes anything to live.
3. **It closes a latent future hole.** The risk was prospective: a *future* tuned candidate (or an LLM hypothesis under #242) clearing the gate via the understated bar. SP-A2 forecloses that before it can be exercised — shipping the tightening *now*, while everything fails anyway, is the zero-risk window to correct it.
4. **No runtime/perf surface.** `np.var` over an N-column Sharpe vector that PBO already computes (`_column_sharpes`) is O(T·N) already paid by PBO; the fallback path adds one log line. No new dependency, no schema, no table.
5. **Forbidden-file untouched (lane discipline):** no `tpcore/calendar.py`, `tpcore/risk/*`, `ops/engine_supervisor.py`, `ops/engine_service.py`, `ops/engine_ladder.py`, `tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`, `.github/workflows/*`. SP-A ledger (`tpcore/lab/ledger.py`, `ops/lab/run.py` ledger block) unmodified except the §3.4 WARNING+comment additions inside `compute_dsr_for_verdict` (a DSR function, in-scope; the ledger read/emit is not touched).

### 7.4 Cross-lane note (shared `tpcore/backtest/overfitting.py`)

`git log --oneline -8 origin/main -- tpcore/backtest/overfitting.py` → a **single** commit (`672fd18 feat(tpcore): nine-test overfitting diagnostic + engine wirings`). No pending data-lane work on the file; no concurrent edit in flight. Per the handoff, Finding #1 is **engine-lane-owned**. The file is shared tpcore, so: (a) `Never modify tpcore without checking all engines that consume it` — done, §4 enumerates all 5 + search + lab + template + archived; (b) the change is additive + keyword-only (no consumer breaks); (c) flag for the controller: if the data lane later needs `overfitting.py`, the keyword-only additive shape minimizes merge risk, but SP-A2 should land before any data-lane DSR work to avoid a semantic conflict on the V-term. **No blocking cross-lane concern; one coordination flag recorded.**

---

## 8. TDD-able test plan sketch (make-or-break called out)

All tests in a **collected** path: `tpcore/tests/test_overfitting.py` (already collected, the canonical home) for the pure-math + diagnostic tests; the oracle-green proof runs the existing collected `scripts/tests/test_search_parameters_characterization.py` unmodified.

- **T-WORKED — the §2.2 numbers are pinned (MAKE-OR-BREAK).** `_expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)` ≈ `0.22763`; `_expected_max_sharpe_under_null(50, 500)` (fallback) ≈ `0.10190`; the candidate-SR=0.15 DSR pair (0.8573 vs 0.0423). Proves the fix uses cross-trial dispersion, not `1/(n−1)`, with the exact worked example.
- **T-CROSSTRIAL — V comes from the trial vector (MAKE-OR-BREAK).** Build a synthetic T×N `trial_returns_matrix` with KNOWN per-column Sharpe dispersion; assert `OverfittingDiagnostic(...).run().dsr_value` equals the DSR computed with `trial_sharpe_variance = var(_column_sharpes(matrix), ddof=1)` and is **strictly different** from the no-matrix (fallback) run on the same winner — i.e. supplying the matrix actually changes (tightens) DSR via V.
- **T-FALLBACK-WARNS (MAKE-OR-BREAK).** With no matrix / `trial_sharpe_variance=None`, assert (a) the result equals the legacy `1/(n−1)` value (numeric backward-compat on the fallback branch) AND (b) a `tpcore.overfitting.dsr.null_variance_approximation` WARNING is emitted (capture via `structlog` testing capture). Proves the approximation is loud, never silent (H-A2-1).
- **T-VERDICT-FALLBACK-WARNS.** `compute_dsr_for_verdict([...], n_trials=N)` (direct two-arg call, no `trial_sharpe_variance`) returns byte-identical to pre-SP-A2 (its fallback math is unchanged) AND emits the single documented WARNING. Proves §3.4 obligation 1 fallback branch with zero numeric drift.
- **T-DELIVERED — the gate is genuinely tightened on the Lab verdict path (MAKE-OR-BREAK, the crux-#1 pin).** Drive `_run_lab_core` (offline harness, ≥`MIN_TRIALS_FOR_V` non-errored trials with a real, non-degenerate per-period Sharpe dispersion) and assert the resulting `core.dsr` is **STRICTLY LOWER** than the same run with the V path disabled (fallback) — `core.dsr_with_V < core.dsr_fallback − ε` on a representative engine fixture. Not "unchanged-with-a-warning": a real numeric tightening. Pairs with an indicative before/after in the dossier. This is the test that proves SP-A2 is NOT inert plumbing.
- **T-UNITS-COHERENT (MAKE-OR-BREAK, H-A2-11).** Assert the per-trial Sharpe fed into V is the **non-annualized** `holdout_sharpe_per_period` and is in the SAME space as `compute_dsr_for_verdict`'s internal `SR̂`: build trials whose annualized `t.holdout.sharpe` differs from the per-period value by a known `√periods_per_year`, and assert V is computed from the per-period field (a fixture where using the annualized field would inflate SR₀ past a tripwire and the test catches it). Proves the units bug cannot regress.
- **T-LEDGER-COMPAT (MAKE-OR-BREAK, H-A2-12).** Run the full `tpcore/tests/test_lab_ntrials_ledger.py` and assert green AFTER the `_spy` signature widening + reviewed re-baseline; assert the three `_spy` stubs now accept `trial_sharpe_variance` (the production call site no longer raises `TypeError`); assert the re-baselined `core.dsr`/`core2.dsr<=core1.dsr` reflect the V-threaded behavior and carry the H-LL-7 anti-reversion docstring. Proves the SP-A ledger surface survives the tightening deliberately, not accidentally.
- **T-VN-COHERENCE (H-A2-4/H-A2-13).** Pin the documented residual cross-run laundering bound: holding a small single-run dispersion fixed while growing the cumulative `N`, assert SR₀ still increases monotonically via the `Φ⁻¹` bracket (SP-A's anti-laundering term is not defeated by a small `√V`) AND that the H-A2-10 floor at `1/(n_obs−1)` is active so a tight fine-grid sweep cannot drive SR₀ below the fallback. Encodes the §6 / H-A2-13 accepted-limitation statement as an executable contract.
- **T-STRICTER (anti-regression, MAKE-OR-BREAK).** For a representative engine fixture (reversion-shaped trades + a realistic N-column sweep matrix), assert `DSR_with_V ≤ DSR_fallback + 1e-12` over a grid of n_obs ∈ {250, 500, 1000} and realistic dispersions — the gate only ever got STRICTER, never looser. Directly encodes the §7 safety argument.
- **T-ORTHO.** Holding `trial_sharpe_variance` fixed, sweeping `n_trials` reproduces SP-A monotone-in-N; holding `n_trials` fixed, increasing V monotonically increases `SR₀`; the two multiply. Pins §6 orthogonality.
- **T-DEGENERATE.** N-column matrix with all-identical columns ⇒ `V = 0` ⇒ `SR₀ = 0` ⇒ DSR == PSR-vs-0 (correct: zero dispersion ⇒ no selection bias). `arr.shape[1] < 2` ⇒ helper returns `None` ⇒ fallback+WARNING (no crash).
- **T-ORACLE — the sacred contract still green, byte-unmodified (MAKE-OR-BREAK).** Assert `git diff origin/main -- scripts/tests/test_search_parameters_characterization.py` is EMPTY, then run that file's tests and assert all green. This is the §5 resolution's enforcement: the property/parity contract is honored, not bypassed; no re-baseline occurred.
- **T-SIG-COMPAT.** Every existing positional call (`_deflated_sharpe_ratio(sr,n,skew,kurt,n_trials)`, `_expected_max_sharpe_under_null(n_trials,n_obs)`) still type-checks and runs (keyword-only addition is non-breaking) — a small import+call smoke over each of the 5 engine call sites' arg shapes.

---

## 9. Non-goals (explicit)

- **DO NOT touch line 118 `norm.ppf(1 − 1/(n_trials·e))`** (nor `:117`). The `Φ⁻¹` bracket is correct per the finding and per §1.2; out of scope.
- **DO NOT weaken `DSR_PASS_THRESHOLD`** (0.95, `:56`) or any other threshold. The gate constant is byte-identical; only the `√V` semantics change.
- **DO NOT alter the SP-A ledger** — `tpcore/lab/ledger.py`, `cumulative_n_trials`, `record_trial_spend`, the `ops/lab/run.py` ledger read/emit block, the `lab_trial_ledger.*` source. The only `ops/lab/run.py` edit is the §3.4 WARNING + comment **inside `compute_dsr_for_verdict`** (a DSR function).
- **NO new table, NO schema migration, NO new dependency.** Pure-math + a logging line + a keyword-only signature.
- **DO NOT unify the two DSR implementations** (`overfitting.py` vs `compute_dsr_for_verdict`). De-duplication is a separate refactor that would perturb the SP-A characterization surface; SP-A2 only corrects V-semantics and makes both approximations loud.
- **DO NOT fabricate a per-trial Sharpe vector** where one does not exist (e.g. synthesizing N pseudo-trials at the single-winner Lab-verdict site). The documented fallback is the honest answer there; a fabricated vector would be a worse defect than the original (H-A2-2).
- **DO NOT modify** `scripts/tests/test_search_parameters_characterization.py` (byte-unmodified invariant, §5), the 7 forbidden files, `.github/workflows/*`, the data lane, or `archive/sigma/*`.
- **NOT SP-B/SP-C/SP-D** (roster targeting / readiness checklist / pluggable scoring). NOT #242.

---

## 10. §Hardening register

| ID | Risk → binding correction |
| --- | --- |
| **H-A2-1** | *A count-only caller silently keeps `1/(n−1)` and an operator believes DSR was correctly deflated.* **Correction:** the fallback branch is (a) `None`-defaulted keyword-only so it is *deliberate*, (b) carries a `# KNOWN APPROXIMATION — not the paper's V` comment, and (c) emits a `tpcore.overfitting.dsr.null_variance_approximation` structlog WARNING with `n_trials/n_obs`. Pinned by T-FALLBACK-WARNS + T-VERDICT-FALLBACK-WARNS. The fallback is never silent. |
| **H-A2-2** | *Oracle-suppression / silently gating the fix to keep a snapshot green* (the brief's named worst case). **Correction:** §5 investigated the oracle in full — it pins PROPERTIES (bounds/monotone), no golden DSR numeric. The fix preserves all properties; the oracle stays byte-unmodified and green by *property*, T-ORACLE proves the diff is empty AND the tests pass. The fix is the default behavior, not opt-in/gated/suppressed. Introducing a numeric snapshot of the fallback approximation is itself forbidden (§9). |
| **H-A2-3** | *Cross-trial Sharpe vector unavailable at a call site, so a "fix" fabricates one (e.g. resamples the winner's trades into N pseudo-trials).* **Correction:** §4 enumerates every site; the structurally-winner-only sites (single-winner diagnostics, `compute_dsr_for_verdict`) take the *documented* fallback. Fabricating a vector is an explicit non-goal (§9). The fallback is honest; a fabricated V would be a worse defect than the original bug. |
| **H-A2-4** | *`n_trials` and the variance computed over DIFFERENT trial sets ⇒ a self-inconsistent SR₀.* The paper's `V[ŜR_n]` is the variance over the SAME N trials `n_trials` counts. **Correction:** where a matrix is threaded into `OverfittingDiagnostic`, V is computed over that matrix's N columns and any mismatch with `self._n_trials` (e.g. an SP-A cumulative N) is *logged, not silently reconciled* (§3.3/§6). The Lab-verdict path is count-only ⇒ fallback (no inconsistency reachable). The honest dispersion-scale-from-a-representative-sample is preferred over fabricating a cumulative-spanning vector (ties to H-A2-3). T-ORTHO pins the separability. |
| **H-A2-5** | *A future reviewer/operator "fixes" the (correct) harder failures back to `1/(n−1)` thinking the gate "broke".* **Correction:** §7 states the tightening is correct and live-safe (all engines already fail; nothing can be spuriously promoted); §1 cites the authoritative V definition; T-STRICTER asserts the gate only ever got stricter with a docstring forbidding the reversion (SP-A H-LL-7 pattern). The honest-behavior invariant is explicit. |
| **H-A2-6** | *Non-normality / γ-detail drift* — the paper's SR₀ uses the Euler–Mascheroni Gumbel-tail approximation for E[max of N Gaussians]; mis-stating γ or the `(1−γ)Φ⁻¹[1−1/N] + γΦ⁻¹[1−1/(Ne)]` blend silently changes the bar. **Correction:** §1.2 quotes the exact blend from two independent restatements (no source discrepancy found, §1); `EULER_MASCHERONI` (`:67`) and the `Φ⁻¹` bracket (`:117-118`) are **byte-unmodified** (§9) — SP-A2 changes ONLY `√V`. The non-normality enters via `_psr_per_trade`'s `skew/kurt` term (`:102`), also unchanged. The Gaussian-trials assumption is the paper's, retained as-is; recorded here as the accepted modeling boundary. |
| **H-A2-7** | *Two independent DSR implementations diverge over time* (`overfitting.py` vs `compute_dsr_for_verdict`). **Correction:** §3.4 mandates a cross-reference comment in BOTH stating the Lab-verdict path is *deliberately* count-only/fallback; unifying them is an explicit out-of-scope follow-up (§9), recorded so it is a known, tracked debt rather than a silent divergence. T-VERDICT-FALLBACK-WARNS pins the Lab path's numeric stability. |
| **H-A2-8** | *`np.var` ddof choice / degenerate matrix crashes the rollup.* **Correction:** `ddof=1` (sample variance, consistent with `_column_sharpes`/`_per_trade_sharpe` `ddof=1` convention, `:77/:210`); `arr.ndim != 2 or shape[1] < 2 ⇒ None ⇒ documented fallback` (never raise — honors the module contract "sub-tests never raise", `:24-25/:494`); `max(V,0.0)` guards float underflow. Pinned by T-DEGENERATE. |
| **H-A2-9 … H-A2-15** | Added by the independent expert-harden — see the **Hardening Addendum** at the end of this spec (the canonical register for the adversarial pass). H-A2-11/H-A2-12 are CRITICAL and have corrected §3.4/§4/§5/§8 in-body; H-A2-13 resolves the SP-A↔SP-A2 V/N coherence seam as a bounded, documented limitation. |

---

## 11. Reused-vs-new

| Concern | Decision |
| --- | --- |
| Per-trial Sharpe vector source | **REUSE** `OverfittingDiagnostic._trial_matrix` (already a constructor param `:333`) + `_column_sharpes` (`:208`, the one canonical per-column Sharpe estimator PBO already uses). No new input, no second Sharpe definition. |
| Variance estimator | `np.var(col_sharpes, ddof=1)` — `ddof=1` matches the module's existing sample-stat convention (`_per_trade_sharpe`/`_column_sharpes`). |
| Signature evolution | Keyword-only `trial_sharpe_variance: float \| None = None` — additive, backward-compatible (SP-A H-LL pattern of "the input grows, the gate expression is byte-identical", applied to the V input). |
| Approximation disclosure | **REUSE** the existing module `logger` (`:51`, structlog) — one WARNING event, no new logging surface. |
| Oracle | **REUSE** unmodified — it is a property/parity contract the fix satisfies (§5). No re-baseline. |
| SP-A composition | **REUSE** SP-A's `effective_n_trials` as the `N` term verbatim; SP-A2 only multiplies in the orthogonal `√V`. |
| NEW (minimal) | One keyword-only param (×2 functions), one private helper `_trial_sharpe_variance`, two WARNING call sites + cross-reference comments. No table, no schema, no dependency, no new file. |

---

## 12. Self-review

- **Placeholder scan:** no TODO/TBD/`???`/`<placeholder>` — every section concrete. File:line citations verified against the read code at `origin/main` post-#93 (`tpcore/backtest/overfitting.py:56/67/74/108-119/122-126/208-212/333/359/365/372`, `ops/lab/run.py:423-446/764-770`, `scripts/tests/test_search_parameters_characterization.py:82-92`, the 5 engine call sites in §4).
- **Internal consistency:** the V-term definition (cross-trial Sharpe dispersion `V[ŜR_n]`, §1.2), the defect (`1/(n_obs−1)`, §2.1), the API (`trial_sharpe_variance` kw-only, §3), the per-site disposition (§4), the oracle resolution (property-contract, no re-baseline, §5), the SP-A orthogonality (`√V` × the untouched `Φ⁻¹` bracket, §6), and the H-register all reference the *same* equation and the *same* call-site inventory. The worked numbers in §2.2 are reproduced exactly by T-WORKED in §8.
- **The oracle decision is unambiguous (§5):** investigated → it pins properties not a numeric → the fix preserves all properties → **byte-unmodified, no re-baseline, T-ORACLE enforces empty diff + green**. The rejected alternative (deliberate reviewed re-baseline) is documented with the precise reason it does not apply. The brief's worst case (silent suppression) is shown structurally impossible here.
- **Scope:** spec-only; single sub-project (SP-A2); SP-B/C/D and #242 excluded (§9). Engine lane; 7 forbidden files + `.github/workflows/*` + data lane + `archive/sigma/*` + the sacred oracle untouched (§7.4/§9). SP-A ledger untouched except the in-scope WARNING inside `compute_dsr_for_verdict`.
- **Every requirement → a test:** authoritative formula→T-WORKED; V from the trial vector→T-CROSSTRIAL; loud fallback→T-FALLBACK-WARNS/T-VERDICT-FALLBACK-WARNS; gate-only-stricter→T-STRICTER; SP-A orthogonality→T-ORTHO; degenerate safety→T-DEGENERATE; oracle honored→T-ORACLE; backward-compat→T-SIG-COMPAT.
- **Genuine constraints surfaced (not hand-waved):** (a) there are TWO independent DSR implementations and the second (`compute_dsr_for_verdict`) folds the same `1/(n−1)` approximation into `denom` rather than `e_max` — both must be made loud (§3.4, H-A2-7); (b) NO current call site threads a live per-trial Sharpe vector into DSR — the correct path is enabled by construction but the *immediate* effect is correctness-where-a-matrix-exists + loud-approximation-everywhere-else (§4), stated honestly rather than overclaiming an immediate numeric change at every engine; (c) the SP-A cumulative `N` and a single-sweep `V` are deliberately different trial sets and that is correct, not a bug (H-A2-4/§6).

---

## 13. Readiness

This spec is **ready for expert-harden → writing-plans → subagent-driven-exec**. The statistical correctness is grounded in the authoritative Bailey & López de Prado source (§1, two independent restatements agree, no discrepancy); the worked example is computed and pinned (§2.2 / T-WORKED); the central SP2-oracle tension is resolved decisively and unambiguously (§5: property-contract, byte-unmodified, no re-baseline, enforced by T-ORACLE); the per-call-site vector-availability matrix is concrete (§4); SP-A composition is proven orthogonal (§6); live-safety is the strongest possible (a tightening of a gate everything already fails — §7). No blocking cross-lane concern on the shared `overfitting.py` (§7.4; one coordination flag: SP-A2 should land before any data-lane DSR work). Nothing is blocking. **NOTE: superseded by the Hardening Addendum below — re-verify after the H-A2-11/12 amendments before writing-plans.**

---

## Hardening Addendum (adversarial expert-harden 2026-05-19)

Independent adversarial gate on the author's self-harden. Verified every claim against the real post-#93/#95/#96/#97 code at the worktree (`tpcore/backtest/overfitting.py` line citations all accurate; the §2.2 worked numbers reproduce **exactly** — 0.14425/0.10190/0.07202, 0.11382/0.22763/0.45526, DSR pair 0.8573 vs 0.0423). Three CRITICAL defects found; spec body corrected in §3.4/§4/§5/§8. Verdict: **CRITICAL GAPS — amended, re-verify.**

| ID | Risk → spec amendment → closing plan task/test |
| --- | --- |
| **H-A2-9 (re-confirmed, CORRECT)** | Crux #1 partial verdict. Trace confirms: the **Lab-verdict path** (`compute_dsr_for_verdict` @ `ops/lab/run.py:774-775`, inside `_run_lab_core`) DOES have a structurally-in-scope per-trial vector (`trials` list, built `:717`, consumed by `rank_candidates` `:729`); `TrialResult.holdout` is a `SliceMetrics` with `.sharpe` (`:201/:349`). The 5 engine `compute_search_metrics`/`OverfittingDiagnostic` paths genuinely have **no** matrix (`compute_search_metrics` `tpcore/backtest/search.py:196` does not even expose the param; the *only* non-test `trial_returns_matrix` ref is `vector/backtest.py:1266` `=None`). The author's §3.4/§4 reframe (deliver via the Lab-verdict path, fallback+WARN the engine paths) is the correct delivery vehicle and is **NOT inert** — *conditional on H-A2-11/12 below being fixed*. → Closed by **T-DELIVERED** (§8). |
| **H-A2-11 (NEW, CRITICAL — crux #1 would-be-inert/broken)** | **Units mismatch silently breaks the "delivered" path.** `t.holdout.sharpe` is ANNUALIZED (`mean/std·√periods_per_year`, `:255-258`); `compute_dsr_for_verdict`'s `SR̂` is per-period (`:431`). `np.var(annualized)` into the per-period SR₀ formula inflates V by ≈`periods_per_year` (25–250×) ⇒ SR₀ explodes ⇒ DSR≈0 always ⇒ the gate is not "tightened", it is *destroyed* (every candidate fails for the wrong reason — a different inert/broken failure mode than "unchanged"). Additionally `TrialResult` retains only the scalar `SliceMetrics`, NOT the per-trial period-return vector, and `periods_per_year` is not persisted — so post-hoc de-annualization is impossible from current state. **Amendment:** §3.4 obл.1 corrected — add an additive `holdout_sharpe_per_period` field to `SliceMetrics` (the un-annualized `mean/std(ddof=1)`, computed in `compute_slice_metrics_from_trades`; the annualized `sharpe` stays byte-identical so `_score_for_ranking`/rankings/the §5 oracle are untouched), thread it through `_evaluate_candidate_with_context`, and compute `V` from it at the verdict site. §4 row + the "same Sharpe `rank_candidates` aggregates" claim corrected/withdrawn. → Closed by **T-UNITS-COHERENT** (§8, MAKE-OR-BREAK) + **T-DELIVERED**. |
| **H-A2-12 (NEW, CRITICAL — false backward-compat claim crashes the SP-A suite)** | The spec asserted the `None`-default keeps "the ledger tests compiling and unchanged" (§3.4 obл.1). **Falsified by trace.** `tpcore/tests/test_lab_ntrials_ledger.py` monkeypatch-replaces `compute_dsr_for_verdict` with `def _spy(r, *, n_trials)` at lines 408/625/739, then drives `_run_lab_core`. Post-SP-A2 the production call site passes `trial_sharpe_variance=` into the *replaced stub* ⇒ `TypeError` ⇒ ≥4 MAKE-OR-BREAK SP-A ledger tests **crash** (not flip). Also `core.dsr == real_dsr(returns, n_trials=37)` (`:644`) and `core2.dsr <= core1.dsr` (`:441`) pin the Lab-path DSR numeric and break when real V is threaded on the LHS only. `_install_offline_harness` builds 40-return windows so the `MIN_TRIALS_FOR_V` guard does NOT rescue them. **Amendment:** §5 widened with the H-A2-12 scope correction — these tests are editable (NOT the byte-frozen oracle); SAME-commit (a) widen the three `_spy` stub signatures to accept `trial_sharpe_variance=None`, (b) deliberate reviewed re-baseline of the pinned `.dsr` references with before/after + an H-LL-7 anti-reversion docstring. The byte-frozen-oracle (no re-baseline) vs editable-ledger-test (reviewed re-baseline) distinction is the correct disposition; conflating them was the original defect. → Closed by **T-LEDGER-COMPAT** (§8, MAKE-OR-BREAK) + **T-ORACLE** (proves the oracle, distinctly, stays byte-frozen). |
| **H-A2-13 (NEW, Important — the SP-A↔SP-A2 V/N coherence seam; #2c)** | The single most important statistical call. Post-SP-A `n_trials` is the **cumulative** budget (spans many runs); the threaded `V` is **this run's** single-sweep dispersion. Numerically stressed: a tight fine-grid late-stage sweep (cross-trial sd=0.03) paired with cumulative N=2000 yields SR₀≈**0.103** — *lower than even the buggy fallback* at the same cumN (0.154). So pairing a small single-run `√V` with a large cumulative `N` **can understate selection bias and is a residual cross-run laundering channel** — the very SP-A threat model, re-opened by the SP-A2 V-term. The self-harden's "log both and proceed" (H-A2-4) makes it *visible* but does NOT *bound* it. **Resolution (decisive — accepted, bounded, documented limitation, NOT silently proceeded):** the H-A2-10 floor `sr_variance = max(V, 1/(n_obs−1))` is the bound — it guarantees the V-term can never drive SR₀ below the legacy fallback (the cumulative-N `Φ⁻¹` bracket still scales SR₀ up with the full SP-A budget, so the anti-laundering term is **not defeated**, only the *additional* tightening from a small-V sweep is forgone). The honest statement, now binding in §6/§3.4: *single-run V is a representative dispersion **scale** estimator paired with the cumulative selection-budget **count** N; these are distinct estimands; the floor bounds the residual so the worst case is "no looser than the legacy bar at the cumulative N", never a laundering win; fabricating a cumulative-spanning Sharpe vector to force V/N equality is rejected (worse defect, H-A2-3). Accepted limitation: SP-A2 does not *additionally* tighten beyond the floor when a sweep is degenerately tight — it is provably no-worse-than-legacy there and materially tighter where the honest dispersion exceeds the floor.* The floor's protective `n_obs` at the Lab-verdict path is the **held-back period count** (often small ⇒ a high floor ⇒ strong protection) — this is incidental, so the floor (not the small n_obs) is the load-bearing guarantee, made explicit here. → Closed by **T-VN-COHERENCE** + **T-STRICTER** (§8). |
| **H-A2-10 (re-confirmed, CORRECT)** | The self-harden's `max(V, 1/(n_obs−1))` floor + `MIN_TRIALS_FOR_V=5` guard is statistically defensible as "tightening-or-equal for every input" — numerically verified the V<floor loosening band exists and the floor closes it; the degenerate V=0⇒DSR≈0.9996 catastrophe is real and the floor prevents it. One honest caveat now stated (H-A2-13): the floor rescues monotone-safety but NOT the original leniency in the degenerate-tight-sweep regime — the spec must (and now does, §4 conclusion + H-A2-13) state "no worse, materially tighter where V>floor" rather than over-claim a full fix. Accepted. |
| **H-A2-14 (NEW, Minor — `_norm_inv` divergence is pre-existing, do not "fix")** | `overfitting.py` uses `scipy.stats.norm.ppf`; `compute_dsr_for_verdict` uses a self-contained Acklam `_norm_inv` (`ops/lab/run.py:450`). They differ at the ~1e-9 level. SP-A2 must NOT "unify" them (out of scope §9, H-A2-7) and the §8 numeric pins (T-WORKED, T-DELIVERED) must tolerate the two impls' ε at their respective sites (assert per-impl, not cross-impl equality). Recorded so a future reviewer does not mistake the pre-existing ε for an SP-A2 regression. → Closed by per-impl tolerance in §8 tests (T-WORKED pins the `overfitting.py` impl; T-DELIVERED/T-LEDGER-COMPAT pin the `compute_dsr_for_verdict` impl). |
| **H-A2-15 (Blast-radius sweep — confirmed clean except H-A2-12)** | Full-suite golden-DSR sweep: `test_overfitting.py:93/:395` (`dsr_passes`/`dsr_above_0_90`, `n_trials=5`, NO matrix) take the byte-identical fallback ⇒ do NOT flip. `test_statistical_significance.py:59/69` exercise a *different* module (`tpcore.backtest.statistical_significance`, annualized space — NOT touched by SP-A2) ⇒ unaffected. `test_lab_ntrials_ledger.py:526-527` are direct two-arg calls ⇒ fallback, unchanged. The ONLY flips/crashes are the `_spy`-driven `test_lab_ntrials_ledger.py` tests (H-A2-12). The §5 property-contract decision for the SP2 oracle **survives** (it pins properties, no golden numeric; the fix preserves bounds/monotone-in-signal/monotone-in-N). Blast-radius / non-goals integrity: confirmed line-118 `norm.ppf` untouched, `DSR_PASS_THRESHOLD` untouched, no table; lane-clean (`git log` on `overfitting.py` → single commit `672fd18`, no concurrent data-lane edit). Both DSR impls corrected coherently (§3.1 overfitting.py + §3.4 `compute_dsr_for_verdict`). → Closed by **T-ORACLE** + **T-SIG-COMPAT** + **T-LEDGER-COMPAT**. |

**Verdict.** The author's self-harden (H-A2-10 floor, MIN_TRIALS guard, H-A2-4 clarification, §3.4 second-impl) is sound *as far as it goes* but contained two CRITICAL latent defects that would have made the "delivered tightening" either **broken** (H-A2-11 units mismatch ⇒ DSR≈0 always) or **crash the SP-A MAKE-OR-BREAK suite** (H-A2-12 false backward-compat claim), plus one Important under-bounded statistical seam (H-A2-13 V/N coherence). All three are now amended in-body with closing tests. The crux-#1 verdict: SP-A2 **does deliver a genuine tightening on the Lab-verdict path — but ONLY after H-A2-11 (per-period V field) and H-A2-12 (ledger-test re-baseline) are implemented as now specified**; without them it is broken/inert, exactly the failure the brief names. Indicative before/after on a real engine's dispersion (N=50, n_obs=500, candidate SR=0.15): bug DSR **0.857** → honest cross-trial sd=0.10 DSR **0.042** (>20× gate-statistic deflation). **CRITICAL GAPS — amended, re-verify before writing-plans.**
