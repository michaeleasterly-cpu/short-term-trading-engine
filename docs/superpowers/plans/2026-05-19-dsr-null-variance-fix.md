# SP-A2 — DSR Null-Variance Estimator Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the statistically-wrong `1/(n_obs−1)` single-estimator sampling variance in the DSR null-benchmark (`SR₀`) with the paper-correct cross-trial Sharpe-dispersion `V[ŜR_n]` (Bailey & López de Prado, SSRN 2460551), in **both** DSR implementations, wiring the real V at the one production site (the Lab verdict path) so the ungameable graduation gate is genuinely *tightened* — never silently loosened — guarded by a conservative floor.

**Architecture:** `_expected_max_sharpe_under_null` / `_deflated_sharpe_ratio` (`tpcore/backtest/overfitting.py`) and `compute_dsr_for_verdict` (`ops/lab/run.py`) each gain a keyword-only `trial_sharpe_variance: float | None = None`. When supplied, `sr_variance = max(V, 1/(n_obs−1))` (the H-A2-10 conservative floor — tightening-or-equal for every input); when `None`, the legacy `1/(n_obs−1)` value is used **and a structlog WARNING is emitted** (a documented, never-silent approximation). The Lab-verdict path computes V from an **additive non-annualized** `holdout_sharpe_per_period` field on `SliceMetrics` (units-coherent with `compute_dsr_for_verdict`'s per-period `SR̂`; the annualized `sharpe` field is byte-unchanged so ranking/the SP2 oracle are untouched). `OverfittingDiagnostic` derives V from its already-present `trial_returns_matrix` via the existing `_column_sharpes`, gated by `MIN_TRIALS_FOR_V=5`. `norm.ppf`/`_norm_inv`, `EULER_MASCHERONI`, `DSR_PASS_THRESHOLD`, and the SP-A `n_trials` ledger are byte-unchanged. The just-shipped SP-A ledger tests (`test_lab_ntrials_ledger.py`) are deliberately, reviewably re-baselined (NOT the byte-frozen SP2 oracle) because the new kwarg crashes their `_spy` stubs and the V threading legitimately tightens their pinned DSR numerics.

**Tech Stack:** Python 3.11, numpy, scipy.stats.norm, structlog (existing module logger), pytest (`asyncio_mode = auto`, collected `tpcore/tests/` + `scripts/tests/`), the offline `_run_lab_core` stub-harness pattern, `structlog.testing.capture_logs`.

---

## File Structure (locked decomposition)

Every file SP-A2 creates or modifies, and its single responsibility:

| File | Create/Modify | One responsibility |
| --- | --- | --- |
| `tpcore/backtest/overfitting.py` | **Modify** | `_expected_max_sharpe_under_null` + `_deflated_sharpe_ratio` gain keyword-only `trial_sharpe_variance`; the H-A2-10 floor `max(V, 1/(n_obs−1))` + the `None`-fallback structlog WARNING; new `MIN_TRIALS_FOR_V=5` const + `OverfittingDiagnostic._trial_sharpe_variance()` helper (reuses `_column_sharpes`); `run()` threads it. `norm.ppf` (`:117-118`), `EULER_MASCHERONI`, `DSR_PASS_THRESHOLD` BYTE-UNCHANGED. |
| `ops/lab/run.py` | **Modify** | (a) `SliceMetrics` gains an **additive** `holdout_sharpe_per_period: float` field; `compute_slice_metrics_from_trades` computes it as the un-annualized `mean/std(ddof=1)` (the annualized `sharpe` stays byte-identical). (b) `compute_dsr_for_verdict` gains keyword-only `trial_sharpe_variance` with the identical floor/fallback-WARNING treatment; the legacy `1/(n−1)` is removed from the V role and supplied solely by the kwarg. (c) The verdict call site (`:774-775`) computes `V = np.var([t.holdout.holdout_sharpe_per_period for t in trials if not t.error], ddof=1)` (subject to the MIN_TRIALS guard) and passes it. Cross-reference comment in BOTH impls. SP-A ledger read/emit block UNTOUCHED. |
| `tpcore/tests/test_overfitting.py` | **Modify** (append) | The pure-math + diagnostic make-or-break tests: T-WORKED, T-CROSSTRIAL, T-FALLBACK-WARNS, T-STRICTER, T-ORTHO, T-DEGENERATE, T-SIG-COMPAT, T-VN-COHERENCE. Already a collected path; the canonical `overfitting.py` test home. |
| `tpcore/tests/test_lab_ntrials_ledger.py` | **Modify** (highest-scrutiny re-baseline) | Widen the 3 `_spy`/`_spy_dsr` stub signatures to accept `trial_sharpe_variance=None`; deliberately re-baseline the 2 pinned Lab-path DSR assertions (`core2.dsr <= core1.dsr` `:441`, `core.dsr == real_dsr(...)` `:644`) against the V-threaded behavior with an explicit H-LL-7 anti-reversion docstring. T-LEDGER-COMPAT, T-VERDICT-FALLBACK-WARNS. |
| `tpcore/tests/test_lab_dsr_delivered.py` | **Create** | The crux make-or-break Lab-path proofs that need a **dispersed** multi-trial offline harness (the SP-A harness produces a degenerate single-config trial set): T-DELIVERED, T-UNITS-COHERENT. Collected path; the SP2/SP3 `scripts/ops.py`↔`ops` `sys.modules` collision-eviction stanza + lazy in-body `ops.lab.run` imports. |
| `scripts/tests/test_search_parameters_characterization.py` | **UNTOUCHED (byte-frozen)** | The SP2 oracle. T-ORACLE asserts its `git diff origin/main` is EMPTY and its tests stay green by *property*. Never edited; never re-baselined (§5). |

No new table, no migration, no schema column, no new dependency (§9). No edit to any of the 8 forbidden lane files (`tpcore/calendar.py`, `tpcore/risk/*`, `ops/engine_supervisor.py`, `ops/engine_service.py`, `ops/engine_ladder.py`, `tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`), `.github/workflows/*`, the data lane, or `archive/sigma/*`. The SP-A ledger (`tpcore/lab/ledger.py`, `cumulative_n_trials`, the `ops/lab/run.py` ledger read/emit block) is unmodified — the only `ops/lab/run.py` edits are inside `SliceMetrics`/`compute_slice_metrics_from_trades`/`compute_dsr_for_verdict` and the in-`_run_lab_core` V computation, none of which is the ledger read/emit.

---

## Locked names & signatures (used identically across every task — any drift is a plan bug)

```python
# tpcore/backtest/overfitting.py
MIN_TRIALS_FOR_V = 5   # below this the cross-trial variance is too noisy to trust

def _expected_max_sharpe_under_null(
    n_trials: int, n_obs: int, *, trial_sharpe_variance: float | None = None,
) -> float: ...

def _deflated_sharpe_ratio(
    sr: float, n: int, skew: float, kurt: float, n_trials: int,
    *, trial_sharpe_variance: float | None = None,
) -> float: ...

class OverfittingDiagnostic:
    def _trial_sharpe_variance(self) -> float | None: ...
        # None if no matrix / arr.ndim != 2 / shape[1] < MIN_TRIALS_FOR_V
        # else float(np.var(_column_sharpes(arr), ddof=1))

# ops/lab/run.py
@dataclass
class SliceMetrics:
    n_trades: int
    sharpe: float                       # ANNUALIZED — byte-unchanged
    profit_factor: float
    max_drawdown: float
    win_rate: float
    holdout_sharpe_per_period: float = 0.0   # NEW additive — un-annualized

def compute_dsr_for_verdict(
    returns: list[float], n_trials: int,
    *, trial_sharpe_variance: float | None = None,
) -> float: ...
```

- The structlog WARNING event name is **exactly** `"tpcore.overfitting.dsr.null_variance_approximation"` in BOTH impls (one canonical event string).
- `ddof=1` for every variance (`np.var(..., ddof=1)`) — matches the module's `_column_sharpes`/`_per_trade_sharpe` convention.
- `holdout_sharpe_per_period` defaults to `0.0` (additive, every existing `SliceMetrics(...)` positional construction stays valid; the empty-trades early returns `SliceMetrics(0, 0.0, 0.0, 0.0, 0.0)` get a 6th positional `0.0`).
- venv python is **`/Users/michael/short-term-trading-engine/.venv/bin/python`** for every command.
- Every multi-flag shell command runs as written in a single line (operator terminal wraps paste — keep each `Run:` a single line; no raw multi-line paste).
- Git-hygiene per commit: verify `git branch --show-current` == `sp-a2-dsr-variance-clean` FIRST; `git switch` only (never `git checkout <sha|branch>`); transient reverts use `git checkout -- <path>` (never `git stash`); assert `git stash list` is unchanged (empty) at the finish task.

---

### Task 0: Baseline capture + H-A2-* → task map (no production code)

Satisfies: **§12 self-review baseline**, **the full-suite zero-new-failures gate**, **H-A2-1..15 traceability**.

**Files:**
- Create: `/tmp/sp-a2-baseline.txt` (scratch, NOT committed — a throwaway baseline artifact)

- [ ] **Step 1: Verify branch and clean tree**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git stash list`
Expected: empty (no stashes — record this; the finish task re-asserts it unchanged)

- [ ] **Step 2: Capture the current origin/main full-suite baseline**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -40 | tee /tmp/sp-a2-baseline.txt`
Expected: a pass/skip summary line (e.g. `N passed, M skipped`). Record the exact failed/error count (DB-gated tests SKIP locally — that is correct; CI runs them). This count is the **baseline**: the finish task (Task 9) asserts the post-fix suite has **zero NEW failures vs this baseline** (a pre-existing unrelated failure, if any, is not introduced/worsened by SP-A2).

- [ ] **Step 3: Record the H-A2-* → task closure map into the PR/commit body (no repo file changed)**

Confirm this mapping is complete and will be the Task 9 self-review spine:

| Hardening / amended § | Closing task | Pinning test |
| --- | --- | --- |
| §3.1 (kw-only V + floor + WARNING) / H-A2-1 / H-A2-10 | Task 2 | T-WORKED, T-FALLBACK-WARNS, T-STRICTER |
| §3.2 (`_deflated_sharpe_ratio` pass-through) | Task 2 | T-SIG-COMPAT |
| §3.3 (`_trial_sharpe_variance` helper, MIN_TRIALS_FOR_V) / H-A2-8 | Task 3 | T-CROSSTRIAL, T-DEGENERATE |
| §3.4 (`compute_dsr_for_verdict` corrected) / H-A2-9 | Task 5 | T-VERDICT-FALLBACK-WARNS |
| §3.4 obл.1 units (`holdout_sharpe_per_period`) / H-A2-11 | Task 4 (groundwork), Task 6 (proof) | T-UNITS-COHERENT |
| §3.4 verdict-site V wiring / H-A2-9 (delivery) | Task 6 | T-DELIVERED |
| §4 per-caller matrix / H-A2-3 (no fabrication) | Task 3, Task 5 (fallback rows) | T-SIG-COMPAT, T-FALLBACK-WARNS |
| §5 oracle = byte-frozen property contract / H-A2-2 | Task 8 | T-ORACLE |
| §5 H-A2-12 ledger-test re-baseline (editable, reviewed) | Task 7 | T-LEDGER-COMPAT |
| §6 SP-A orthogonality / H-A2-4 | Task 2, Task 8 | T-ORTHO, T-VN-COHERENCE |
| §7 tightening-only safety / H-A2-5 / H-A2-13 | Task 2, Task 8 | T-STRICTER, T-VN-COHERENCE |
| §7.4 cross-lane / H-A2-15 blast-radius | Task 9 | lane-clean assertion, full suite |
| H-A2-6 (γ-detail byte-unchanged) | Task 1 (characterization), Task 2 | T-WORKED (byte-pin) |
| H-A2-7 (two-impl divergence comment) | Task 5 | cross-reference comment present |
| H-A2-14 (`_norm_inv` ε pre-existing, per-impl tolerance) | Task 2 / Task 6 | per-impl tolerance in T-WORKED / T-DELIVERED |

No production code in this task; the map is the binding traceability spine for Task 9's self-review.

- [ ] **Step 4: Commit the plan/decision spine (the plan file itself is committed by the plan author; this step is a no-op marker — proceed to Task 1)**

No commit in Task 0 (no repo change). Proceed.

---

### Task 1: Characterization pin of the current `overfitting.py` DSR numerics (RED→GREEN, lock the byte-unchanged surface)

Satisfies: **H-A2-6 (γ-blend byte-unchanged)**, **§9 non-goals (`norm.ppf`/`EULER`/threshold untouched)** — a guard test that fails if Task 2 accidentally perturbs the untouched math.

**Files:**
- Modify (append): `tpcore/tests/test_overfitting.py`

- [ ] **Step 1: Write the characterization test (asserts the CURRENT fallback math, pre-fix)**

Append to `tpcore/tests/test_overfitting.py`:

```python
# ─── SP-A2: DSR null-variance estimator correction ─────────────────────────
import math as _sp_a2_math

import structlog as _sp_a2_structlog

from tpcore.backtest.overfitting import (
    MIN_TRIALS_FOR_V,
    _column_sharpes,
    _deflated_sharpe_ratio,
    _expected_max_sharpe_under_null,
)


def test_sp_a2_fallback_math_byte_unchanged_no_variance_arg() -> None:
    """H-A2-6 / §9: with NO trial_sharpe_variance the result equals the
    legacy 1/(n_obs-1) formula EXACTLY — the norm.ppf bracket + EULER
    blend are byte-unchanged; only the V semantics change."""
    n_trials, n_obs = 50, 500
    # The exact legacy expression (pre-SP-A2), recomputed inline.
    from scipy.stats import norm
    sr_variance = 1.0 / (n_obs - 1)
    z1 = float(norm.ppf(1.0 - 1.0 / n_trials))
    z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * _sp_a2_math.e)))
    euler = 0.5772156649015329
    legacy = _sp_a2_math.sqrt(sr_variance) * ((1.0 - euler) * z1 + euler * z2)
    got = _expected_max_sharpe_under_null(n_trials, n_obs)
    assert abs(got - legacy) < 1e-12
    # §2.2 worked number for the fallback branch.
    assert abs(got - 0.10190) < 1e-4
```

- [ ] **Step 2: Run it — expect FAIL (the symbols don't exist yet)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_overfitting.py::test_sp_a2_fallback_math_byte_unchanged_no_variance_arg"`
Expected: FAIL — `ImportError: cannot import name 'MIN_TRIALS_FOR_V'` (the SP-A2 symbols are introduced in Task 2/3).

- [ ] **Step 3: No implementation in this task**

This test is RED-pinned now and goes GREEN at the end of Task 2 (it asserts the *unchanged fallback*, which Task 2's `None`-branch must preserve byte-exactly). Do NOT implement here — Task 2 makes it pass. Leave it failing.

- [ ] **Step 4: Commit the RED characterization pin**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add tpcore/tests/test_overfitting.py`

Run: `git commit -m "test(lab-fh): SP-A2 — RED characterization pin of byte-unchanged DSR fallback math"`

---

### Task 2: `_expected_max_sharpe_under_null` + `_deflated_sharpe_ratio` — cross-trial V, floor, loud fallback (RED→GREEN, MAKE-OR-BREAK T-WORKED/T-FALLBACK-WARNS/T-STRICTER)

Satisfies: **§3.1, §3.2, H-A2-1, H-A2-5, H-A2-10, H-A2-14**.

**Files:**
- Modify: `tpcore/backtest/overfitting.py:108-126` (the two functions + a new const)
- Modify (append): `tpcore/tests/test_overfitting.py`

- [ ] **Step 1: Write the failing tests (T-WORKED, T-FALLBACK-WARNS, T-STRICTER, T-ORTHO, T-SIG-COMPAT)**

Append to `tpcore/tests/test_overfitting.py`:

```python
def test_sp_a2_t_worked_cross_trial_variance_pins_2_2_numbers() -> None:
    """T-WORKED (MAKE-OR-BREAK). §2.2: N=50, n_obs=500, V=0.01 ⇒ SR₀≈0.22763;
    fallback ⇒ SR₀≈0.10190; the per-impl ε (H-A2-14: this is the
    overfitting.py / scipy.norm.ppf impl)."""
    sr0_v = _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)
    assert abs(sr0_v - 0.22763) < 1e-4
    sr0_fb = _expected_max_sharpe_under_null(50, 500)
    assert abs(sr0_fb - 0.10190) < 1e-4
    # The candidate-SR=0.15 DSR pair (skew 0, kurt 3, n=500).
    d_bug = _deflated_sharpe_ratio(0.15, 500, 0.0, 3.0, 50)
    d_fix = _deflated_sharpe_ratio(0.15, 500, 0.0, 3.0, 50,
                                   trial_sharpe_variance=0.01)
    assert abs(d_bug - 0.8573) < 1e-3
    assert abs(d_fix - 0.0423) < 1e-3


def test_sp_a2_t_fallback_warns_loud_and_numeric_backward_compat() -> None:
    """T-FALLBACK-WARNS (MAKE-OR-BREAK, H-A2-1). No variance ⇒ legacy
    numeric AND a loud structlog WARNING (never silent)."""
    with _sp_a2_structlog.testing.capture_logs() as logs:
        got = _expected_max_sharpe_under_null(50, 500)
    assert abs(got - 0.10190) < 1e-4
    assert any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        and e.get("log_level") == "warning"
        and e.get("n_trials") == 50 and e.get("n_obs") == 500
        for e in logs
    )


def test_sp_a2_t_fallback_no_warn_when_variance_supplied() -> None:
    """The honest path is silent (no spurious WARNING when V is given)."""
    with _sp_a2_structlog.testing.capture_logs() as logs:
        _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)
    assert not any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        for e in logs
    )


def test_sp_a2_t_stricter_floor_makes_change_tightening_or_equal() -> None:
    """T-STRICTER (MAKE-OR-BREAK, H-A2-10). Over a grid incl. the
    low-dispersion / degenerate band, DSR_with_V ≤ DSR_fallback + 1e-12
    — the floor max(V, 1/(n_obs-1)) makes the change provably
    tightening-or-equal for EVERY input (never looser)."""
    for n_obs in (250, 500, 1000):
        d_fb = _deflated_sharpe_ratio(0.15, n_obs, 0.0, 3.0, 50)
        for v in (0.0, 1e-9, 1e-6, 0.0005, 0.001, 0.01, 0.04, 0.10):
            d_v = _deflated_sharpe_ratio(0.15, n_obs, 0.0, 3.0, 50,
                                         trial_sharpe_variance=v)
            assert d_v <= d_fb + 1e-12, (n_obs, v, d_v, d_fb)


def test_sp_a2_t_ortho_v_and_n_compose_multiplicatively() -> None:
    """T-ORTHO (§6). Hold V fixed, sweep n_trials ⇒ SR₀ monotone-up in N
    (the untouched Φ⁻¹ bracket); hold N fixed, increase V ⇒ SR₀
    monotone-up in V. They multiply."""
    base = _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)
    more_n = _expected_max_sharpe_under_null(2000, 500, trial_sharpe_variance=0.01)
    assert more_n > base                       # monotone in N (SP-A term)
    more_v = _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.04)
    assert more_v > base                       # monotone in V (SP-A2 term)
    # Multiplicative separability: SR0(N,V) / SR0(N,V0) is N-independent.
    r1 = (_expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.04)
          / _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01))
    r2 = (_expected_max_sharpe_under_null(200, 500, trial_sharpe_variance=0.04)
          / _expected_max_sharpe_under_null(200, 500, trial_sharpe_variance=0.01))
    assert abs(r1 - r2) < 1e-9


def test_sp_a2_t_sig_compat_positional_calls_still_work() -> None:
    """T-SIG-COMPAT. Every legacy positional call still type-checks/runs
    (keyword-only addition is non-breaking) — the 5 engine call shapes."""
    assert _expected_max_sharpe_under_null(20, 250) >= 0.0     # reversion shape
    assert _deflated_sharpe_ratio(0.1, 250, 0.0, 3.0, 20) >= 0.0
    assert _deflated_sharpe_ratio(0.1, 250, 0.0, 3.0, 1) == 0.0  # N=1 short-circuit
    assert _expected_max_sharpe_under_null(50, 1) == 0.0         # n_obs<2 short-circuit
```

- [ ] **Step 2: Run them — expect FAIL**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py -k sp_a2`
Expected: FAIL — `ImportError: cannot import name 'MIN_TRIALS_FOR_V'` / `_expected_max_sharpe_under_null() got an unexpected keyword argument 'trial_sharpe_variance'`.

- [ ] **Step 3: Implement — replace `overfitting.py:108-126` exactly**

In `tpcore/backtest/overfitting.py`, replace the block from `def _expected_max_sharpe_under_null(n_trials: int, n_obs: int) -> float:` through the end of `_deflated_sharpe_ratio` (lines 108-126) with:

```python
MIN_TRIALS_FOR_V = 5  # H-A2-10: below this the cross-trial variance is too
                      # noisy to trust as a selection-bias estimate.


def _expected_max_sharpe_under_null(
    n_trials: int,
    n_obs: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    """Expected max sample Sharpe across ``n_trials`` trials under the null.

    Bailey & López de Prado (2014), SSRN 2460551, eqn for SR₀:
        SR₀ = √V · ((1−γ)·Φ⁻¹[1−1/N] + γ·Φ⁻¹[1−1/(N·e)])
    where **V = V[ŜR_n] is the cross-trial variance of the per-trial
    Sharpe estimates across the N searched trials** (selection-bias
    dispersion), NOT the single-estimator sampling variance.

    ``trial_sharpe_variance`` — pass V[ŜR_n] computed from the sweep's
    per-trial Sharpe vector (the statistically-correct path). When
    ``None`` (a count-only / single-strategy caller that has no trial
    vector), fall back to the single-estimator null approximation
    ``1/(n_obs-1)`` AND emit a structlog WARNING — this branch is a
    documented approximation, never silent (§1.3, H-A2-1).

    The H-A2-10 floor ``max(V, 1/(n_obs-1))`` makes the change provably
    tightening-or-equal for every input: a low-dispersion / degenerate
    sweep can NOT loosen the (already-too-lenient) legacy bar. See the
    sibling impl ``ops/lab/run.py::compute_dsr_for_verdict`` — both must
    stay coherent (H-A2-7); the V-term is the cross-trial dispersion,
    ``ddof=1``, distinct from the multiple-testing count ``n_trials``.
    """
    if n_trials <= 1 or n_obs < 2:
        return 0.0
    floor = 1.0 / (n_obs - 1)  # legacy single-estimator value — now a FLOOR
    if trial_sharpe_variance is not None:
        # H-A2-10: honest cross-trial dispersion is used ONLY when it makes
        # the gate the SAME OR HARDER. A low-dispersion / degenerate sweep
        # (V < 1/(n_obs-1)) must NOT loosen the bar — clamp up to the floor.
        sr_variance = max(float(trial_sharpe_variance), floor)
    else:
        sr_variance = floor  # KNOWN APPROXIMATION — not the paper's V
        logger.warning(
            "tpcore.overfitting.dsr.null_variance_approximation",
            reason="no per-trial Sharpe vector available; using "
                   "single-estimator 1/(n_obs-1) instead of "
                   "cross-trial V[SR_n]",
            n_trials=n_trials,
            n_obs=n_obs,
        )
    z1 = float(norm.ppf(1.0 - 1.0 / n_trials))
    z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
    return math.sqrt(sr_variance) * (
        (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
    )


def _deflated_sharpe_ratio(
    sr: float,
    n: int,
    skew: float,
    kurt: float,
    n_trials: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    threshold = _expected_max_sharpe_under_null(
        n_trials, n, trial_sharpe_variance=trial_sharpe_variance
    )
    return _psr_per_trade(sr, threshold, n, skew, kurt)
```

(The `z1`/`z2` `norm.ppf` lines are byte-identical to the original `:117-118` — only re-wrapped onto their own statements; the math is unchanged. `EULER_MASCHERONI`/`DSR_PASS_THRESHOLD` untouched.)

- [ ] **Step 4: Run the Task 1 + Task 2 tests — expect PASS**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py -k "sp_a2 or test_sp_a2_fallback_math_byte_unchanged"`
Expected: PASS (all SP-A2 Task 1/2 tests green; the Task 1 RED pin now goes GREEN — the fallback branch preserved the legacy number byte-exactly).

- [ ] **Step 5: Run the full overfitting suite — no regression**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py`
Expected: all pass (the existing `dsr_passes`/`dsr_above_0_90` tests use `n_trials=5`, NO matrix ⇒ the byte-identical fallback ⇒ they do NOT flip — H-A2-15).

- [ ] **Step 6: ruff + check_imports + isort**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/ scripts/ ops/`
Expected: `All checks passed!` (imports stdlib/3rd-party before first-party — the `import math as _sp_a2_math` / `import structlog as _sp_a2_structlog` / `from tpcore...` ordering must satisfy isort I001).

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found` (overfitting.py stays engine-free).

- [ ] **Step 7: Commit**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add tpcore/backtest/overfitting.py tpcore/tests/test_overfitting.py`

Run: `git commit -m "feat(lab-fh): SP-A2 — cross-trial V + floor + loud fallback in _expected_max_sharpe_under_null"`

---

### Task 3: `OverfittingDiagnostic._trial_sharpe_variance` helper + `run()` wiring (RED→GREEN, MAKE-OR-BREAK T-CROSSTRIAL/T-DEGENERATE)

Satisfies: **§3.3, H-A2-3 (no fabrication — None when no matrix), H-A2-4 (V-trial-count vs n_trials logged side-by-side), H-A2-8 (ddof=1, never raise)**.

**Files:**
- Modify: `tpcore/backtest/overfitting.py` (add the helper near `_column_sharpes`; thread it in `run()` at `:365`)
- Modify (append): `tpcore/tests/test_overfitting.py`

- [ ] **Step 1: Write the failing tests (T-CROSSTRIAL, T-DEGENERATE)**

Append to `tpcore/tests/test_overfitting.py`:

```python
def _sp_a2_make_trial_matrix(col_means, *, n_obs=250, seed=0):
    """T×N matrix with controlled per-column Sharpe dispersion."""
    rng = np.random.default_rng(seed)
    cols = []
    for m in col_means:
        c = rng.normal(m, 0.02, n_obs)
        cols.append(c)
    return np.column_stack(cols)


def test_sp_a2_t_crosstrial_matrix_changes_dsr_via_v() -> None:
    """T-CROSSTRIAL (MAKE-OR-BREAK). Supplying a trial_returns_matrix with
    KNOWN per-column Sharpe dispersion makes OverfittingDiagnostic's DSR
    equal the DSR computed from that V, and STRICTLY different from the
    no-matrix fallback run on the same winner (i.e. it tightens)."""
    returns = list(np.random.default_rng(1).normal(0.01, 0.02, 200))
    trades = _make_trades(returns)
    matrix = _sp_a2_make_trial_matrix(
        [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12], n_obs=200, seed=3)
    diag_v = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=7,
        trial_returns_matrix=matrix,
    )
    rep_v = diag_v.run()
    diag_fb = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=7,
    )
    rep_fb = diag_fb.run()
    expected_v = float(np.var(_column_sharpes(matrix), ddof=1))
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    from tpcore.backtest.overfitting import _moments, _per_trade_sharpe
    sk, ku = _moments(pnls)
    want = _deflated_sharpe_ratio(
        _per_trade_sharpe(pnls), pnls.size, sk, ku, 7,
        trial_sharpe_variance=expected_v,
    )
    assert abs(rep_v.dsr_value - want) < 1e-9
    assert rep_v.dsr_value != rep_fb.dsr_value           # V actually changed DSR
    assert rep_v.dsr_value <= rep_fb.dsr_value + 1e-12   # tightening direction


def test_sp_a2_t_degenerate_identical_columns_and_too_few_cols() -> None:
    """T-DEGENERATE (H-A2-8). All-identical columns ⇒ V=0 ⇒ floored, no
    crash. < MIN_TRIALS_FOR_V columns ⇒ helper returns None ⇒ fallback +
    WARNING (no raise). < 2-D / empty ⇒ None."""
    returns = list(np.random.default_rng(2).normal(0.01, 0.02, 120))
    trades = _make_trades(returns)
    identical = np.column_stack([np.full(120, 0.01)] * 6)
    rep_ident = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=6,
        trial_returns_matrix=identical,
    ).run()
    assert 0.0 <= rep_ident.dsr_value <= 1.0              # no crash, bounded
    few = _sp_a2_make_trial_matrix([0.0, 0.05, 0.10], n_obs=120, seed=4)
    assert few.shape[1] < MIN_TRIALS_FOR_V
    diag_few = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=3,
        trial_returns_matrix=few,
    )
    assert diag_few._trial_sharpe_variance() is None      # too few cols
    diag_none = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=3,
    )
    assert diag_none._trial_sharpe_variance() is None      # no matrix
```

- [ ] **Step 2: Run them — expect FAIL**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py -k "t_crosstrial or t_degenerate"`
Expected: FAIL — `AttributeError: 'OverfittingDiagnostic' object has no attribute '_trial_sharpe_variance'`.

- [ ] **Step 3: Implement the helper + thread it in `run()`**

In `tpcore/backtest/overfitting.py`, add this method to the `OverfittingDiagnostic` class body (place it immediately before `def run(self)` at `:357`):

```python
    def _trial_sharpe_variance(self) -> float | None:
        """V[ŜR_n] across the N searched trials, from the SAME per-column
        Sharpe vector PBO already uses (``_column_sharpes``). One canonical
        Sharpe-vector definition; no second estimator. ``None`` when no
        matrix / not 2-D / < MIN_TRIALS_FOR_V columns (the §3.1 floor at
        1/(n_obs-1) then keeps the gate safe — H-A2-10). Never raises
        (module contract: sub-tests never raise)."""
        if self._trial_matrix is None:
            return None
        arr = (
            self._trial_matrix.values
            if isinstance(self._trial_matrix, pd.DataFrame)
            else np.asarray(self._trial_matrix)
        )
        if arr.ndim != 2 or arr.shape[1] < MIN_TRIALS_FOR_V:
            return None
        col_sharpes = _column_sharpes(arr)
        return float(np.var(col_sharpes, ddof=1))
```

Then in `run()` replace the single line `:365`:

```python
        dsr = _deflated_sharpe_ratio(sr_internal, n, skew, kurt, self._n_trials) if n >= 2 else 0.0
```

with:

```python
        trial_sharpe_var = self._trial_sharpe_variance()  # None ⇒ documented fallback
        if trial_sharpe_var is not None:
            # H-A2-4: the V-source trial count and the multiple-testing
            # count are deliberately distinct estimands — log side-by-side
            # so any divergence is visible, never silently reconciled.
            _v_arr = (
                self._trial_matrix.values
                if isinstance(self._trial_matrix, pd.DataFrame)
                else np.asarray(self._trial_matrix)
            )
            logger.info(
                "tpcore.overfitting.dsr.v_n_trial_population",
                v_trial_count=int(_v_arr.shape[1]),
                n_trials=self._n_trials,
            )
        dsr = (
            _deflated_sharpe_ratio(
                sr_internal, n, skew, kurt, self._n_trials,
                trial_sharpe_variance=trial_sharpe_var,
            )
            if n >= 2
            else 0.0
        )
```

- [ ] **Step 4: Run the tests — expect PASS**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py -k "t_crosstrial or t_degenerate"`
Expected: PASS.

- [ ] **Step 5: Full overfitting suite — no regression**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py`
Expected: all pass (existing PBO tests still pass — `_column_sharpes` is reused, not changed; PBO path untouched).

- [ ] **Step 6: ruff + check_imports**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/ scripts/ ops/`
Expected: `All checks passed!`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 7: Commit**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add tpcore/backtest/overfitting.py tpcore/tests/test_overfitting.py`

Run: `git commit -m "feat(lab-fh): SP-A2 — OverfittingDiagnostic._trial_sharpe_variance + run() wiring (T-CROSSTRIAL)"`

---

### Task 4: `SliceMetrics.holdout_sharpe_per_period` additive field (RED→GREEN, H-A2-11 groundwork — units coherence)

Satisfies: **§3.4 obл.1 (H-A2-11 CRITICAL): the per-period field — ranking-neutral, oracle-neutral, the only honest way to deliver §3.4**.

**Files:**
- Modify: `ops/lab/run.py` (`SliceMetrics` `:197-213`, `compute_slice_metrics_from_trades` `:236-273`)
- Modify (append): `tpcore/tests/test_overfitting.py` (a small in-process unit — `compute_slice_metrics_from_trades` is importable; this stays in the collected overfitting test file with the lazy `ops.lab.run` import + the collision-eviction guard local to the test)

- [ ] **Step 1: Write the failing test (per-period field is the un-annualized Sharpe; annualized `sharpe` byte-unchanged)**

Append to `tpcore/tests/test_overfitting.py`:

```python
def test_sp_a2_slice_metrics_per_period_field_is_unannualized() -> None:
    """H-A2-11. compute_slice_metrics_from_trades exposes an additive
    holdout_sharpe_per_period == mean/std(ddof=1) (NO √periods_per_year);
    the annualized `sharpe` field is byte-IDENTICAL (rankings/oracle
    untouched)."""
    # Lazy import + collision-eviction guard (scripts/ops.py vs ops/).
    import sys as _sys
    for _m in [m for m in list(_sys.modules)
               if m == "ops" or m.startswith("ops.")]:
        if not hasattr(_sys.modules[_m], "__path__"):
            del _sys.modules[_m]
    from datetime import date as _date

    import ops.lab.run as _lr

    class _T:
        def __init__(self, d, p):
            self.entry_date = d
            self.pnl_pct = p

    rng = np.random.default_rng(7)
    rs = [float(x) for x in rng.normal(0.01, 0.02, 30)]
    trades = [_T(_date(2022, 1, 3) + timedelta(days=i), r)
              for i, r in enumerate(rs)]
    span_days = 364
    sm = _lr.compute_slice_metrics_from_trades(trades, span_days)
    arr = np.array(rs, dtype=float)
    want_pp = float(arr.mean() / arr.std(ddof=1))
    assert abs(sm.holdout_sharpe_per_period - want_pp) < 1e-12
    # Annualized sharpe = per-period · √periods_per_year, byte-unchanged.
    ppy = 30 / (span_days / 365.25)
    assert abs(sm.sharpe - want_pp * math.sqrt(ppy)) < 1e-9
    # Empty-trades path keeps the additive default (0.0), no crash.
    sm0 = _lr.compute_slice_metrics_from_trades([], 1)
    assert sm0.holdout_sharpe_per_period == 0.0
```

(`math` and `timedelta` are already imported at the top of `test_overfitting.py` via the SP-A2 block / existing `from datetime import date, timedelta`; `np` is module-level.)

- [ ] **Step 2: Run it — expect FAIL**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_overfitting.py::test_sp_a2_slice_metrics_per_period_field_is_unannualized"`
Expected: FAIL — `AttributeError: 'SliceMetrics' object has no attribute 'holdout_sharpe_per_period'`.

- [ ] **Step 3: Add the additive field to `SliceMetrics` (`ops/lab/run.py:197-213`)**

Replace the `SliceMetrics` dataclass body (the field list + `to_dict`) so it reads exactly:

```python
@dataclass
class SliceMetrics:
    """Metrics computed on a trade-log slice (typically the holdout window)."""

    n_trades: int
    sharpe: float
    profit_factor: float
    max_drawdown: float
    win_rate: float
    # SP-A2 / H-A2-11: the UN-annualized per-period Sharpe
    # (mean/std(ddof=1), the same quantity BEFORE the √periods_per_year
    # factor). Additive + ranking-neutral + oracle-neutral: the
    # annualized `sharpe` above is byte-IDENTICAL; this field exists
    # ONLY so compute_dsr_for_verdict's V-term is units-coherent with
    # its per-period SR̂ (annualized V would inflate SR₀ by ≈ppy).
    holdout_sharpe_per_period: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "sharpe": float(self.sharpe) if math.isfinite(self.sharpe) else 0.0,
            "profit_factor": float(self.profit_factor) if math.isfinite(self.profit_factor) else 0.0,
            "max_drawdown": float(self.max_drawdown),
            "win_rate": float(self.win_rate),
            "holdout_sharpe_per_period": (
                float(self.holdout_sharpe_per_period)
                if math.isfinite(self.holdout_sharpe_per_period) else 0.0
            ),
        }
```

- [ ] **Step 4: Compute it in `compute_slice_metrics_from_trades` (`ops/lab/run.py`)**

In `compute_slice_metrics_from_trades`, replace the Sharpe block (lines `:254-260`):

```python
    if period_returns_arr.std(ddof=1) > 0 and n_periods > 1:
        sharpe = float(
            period_returns_arr.mean() / period_returns_arr.std(ddof=1)
            * math.sqrt(periods_per_year)
        )
    else:
        sharpe = 0.0
```

with:

```python
    if period_returns_arr.std(ddof=1) > 0 and n_periods > 1:
        # SP-A2 / H-A2-11: the per-period (un-annualized) Sharpe is the
        # base quantity; the annualized `sharpe` is it × √periods_per_year
        # — the annualized expression is byte-IDENTICAL to before.
        sharpe_per_period = float(
            period_returns_arr.mean() / period_returns_arr.std(ddof=1)
        )
        sharpe = float(sharpe_per_period * math.sqrt(periods_per_year))
    else:
        sharpe_per_period = 0.0
        sharpe = 0.0
```

Then change the final `return SliceMetrics(...)` (lines `:270-273`) to pass the new field:

```python
    return SliceMetrics(
        n_trades=len(trades), sharpe=sharpe, profit_factor=pf,
        max_drawdown=max_dd, win_rate=win_rate,
        holdout_sharpe_per_period=sharpe_per_period,
    )
```

(The two early `return SliceMetrics(0, 0.0, 0.0, 0.0, 0.0)` guards at `:237`/`:248` are left as-is — the additive default `0.0` makes them valid; `holdout_sharpe_per_period` defaults to `0.0`.)

- [ ] **Step 5: Run the test — expect PASS**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_overfitting.py::test_sp_a2_slice_metrics_per_period_field_is_unannualized"`
Expected: PASS.

- [ ] **Step 6: Prove ranking-neutrality / oracle-neutrality is not yet broken (quick guard)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/test_search_parameters_characterization.py`
Expected: all pass (the annualized `sharpe` is byte-unchanged ⇒ `_score_for_ranking`/rankings/the oracle properties hold; the additive field is untouched by ranking).

- [ ] **Step 7: ruff + check_imports**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/ scripts/ ops/`
Expected: `All checks passed!`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 8: Commit**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add ops/lab/run.py tpcore/tests/test_overfitting.py`

Run: `git commit -m "feat(lab-fh): SP-A2 — additive SliceMetrics.holdout_sharpe_per_period (H-A2-11 units groundwork)"`

---

### Task 5: `compute_dsr_for_verdict` — keyword-only V, floor, loud fallback, cross-ref (RED→GREEN, T-VERDICT-FALLBACK-WARNS, H-A2-7/H-A2-9)

Satisfies: **§3.4 obл.1 (the second DSR impl corrected coherently), obл.3 (cross-reference comment), H-A2-7, H-A2-9 (signature), H-A2-14 (per-impl ε)**.

**Files:**
- Modify: `ops/lab/run.py` (`compute_dsr_for_verdict` `:423-447`)
- Create: `tpcore/tests/test_lab_dsr_delivered.py`

- [ ] **Step 1: Create the new collected test file with the eviction stanza + write T-VERDICT-FALLBACK-WARNS**

Create `tpcore/tests/test_lab_dsr_delivered.py`:

```python
"""SP-A2 — DSR null-variance fix: Lab-verdict-path delivery proofs.

Collected path (``tpcore/tests`` is in pyproject ``testpaths``). The
``scripts/ops.py`` vs ``ops/`` package collision is acute once a test
imports ``ops.lab.run``: a non-package ``ops`` cached by an earlier
full-suite test would shadow ``ops.lab.run``. Mirror
``tpcore/tests/test_lab_ntrials_ledger.py``: evict any cached
non-package ``ops`` at module load and keep every ``ops.lab`` / ``ops``
import lazy/in-body.
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import structlog

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]


def test_sp_a2_t_verdict_fallback_warns_and_byte_identical() -> None:
    """T-VERDICT-FALLBACK-WARNS. Direct two-arg call (no
    trial_sharpe_variance) is byte-identical to pre-SP-A2 AND emits the
    single documented WARNING. Per-impl ε (H-A2-14: this is the
    compute_dsr_for_verdict / Acklam _norm_inv impl)."""
    import ops.lab.run as lr
    rng = np.random.default_rng(0)
    returns = [float(x) for x in rng.normal(0.015, 0.01, 40)]
    # Recompute the legacy (pre-SP-A2) expression inline: e_max bracket
    # with the OLD 1/(n-1) folded into denom.
    arr = np.asarray(returns, dtype=float)
    sr = float(arr.mean() / arr.std(ddof=1))
    n = len(arr)
    skew = float(((arr - arr.mean()) ** 3).mean() / (arr.std() ** 3))
    kurt = float(((arr - arr.mean()) ** 4).mean() / (arr.std() ** 4))
    EULER = 0.5772156649015329
    e_max = ((1.0 - EULER) * lr._norm_inv(1.0 - 1.0 / 37)
             + EULER * lr._norm_inv(1.0 - 1.0 / (37 * math.e)))
    denom = math.sqrt(
        max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr ** 2), 1e-12)
        / max(n - 1, 1)
    )
    z = (sr - e_max) / denom
    legacy = float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    with structlog.testing.capture_logs() as logs:
        got = lr.compute_dsr_for_verdict(returns, n_trials=37)
    assert abs(got - legacy) < 1e-12
    assert any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        and e.get("log_level") == "warning"
        for e in logs
    )


def test_sp_a2_t_verdict_v_arg_tightens_and_no_warn() -> None:
    """Supplying trial_sharpe_variance applies the floor and is silent
    (no spurious WARNING); the V path is ≤ the fallback (tightening)."""
    import ops.lab.run as lr
    rng = np.random.default_rng(1)
    returns = [float(x) for x in rng.normal(0.02, 0.01, 40)]
    d_fb = lr.compute_dsr_for_verdict(returns, n_trials=50)
    with structlog.testing.capture_logs() as logs:
        d_v = lr.compute_dsr_for_verdict(
            returns, n_trials=50, trial_sharpe_variance=0.01)
    assert d_v <= d_fb + 1e-12
    assert not any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        for e in logs
    )
```

- [ ] **Step 2: Run them — expect FAIL**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_dsr_delivered.py`
Expected: FAIL — `compute_dsr_for_verdict() got an unexpected keyword argument 'trial_sharpe_variance'` (and the fallback test fails because no WARNING is emitted yet).

- [ ] **Step 3: Rewrite `compute_dsr_for_verdict` (`ops/lab/run.py:423-447`)**

Replace the entire `compute_dsr_for_verdict` function body (from `def compute_dsr_for_verdict(returns: list[float], n_trials: int) -> float:` through its `return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))`) with:

```python
def compute_dsr_for_verdict(
    returns: list[float],
    n_trials: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    """Deflated Sharpe Ratio corrected for the total number of search
    trials. Returns a probability ≥ 0.0; ≥ 0.95 is the "survived"
    threshold. Same formula as
    :func:`tpcore.backtest.overfitting._expected_max_sharpe_under_null`
    — the two impls MUST stay coherent (H-A2-7).

    ``trial_sharpe_variance`` — V[ŜR_n], the **cross-trial** variance
    of the per-trial *per-period* Sharpe estimates across the searched
    trials (``ddof=1``; the same per-period space as ``sr`` below — NOT
    the annualized ``SliceMetrics.sharpe`` — H-A2-11). When ``None`` (a
    count-only / non-Lab caller, e.g. the SP2 oracle's two-arg call),
    fall back to the single-estimator ``1/(n-1)`` approximation AND emit
    a structlog WARNING — documented, never silent (§1.3, H-A2-1). The
    H-A2-10 floor ``max(V, 1/(n-1))`` makes the change tightening-or-
    equal for every input. The V-source trial population and ``n_trials``
    (the SP-A cumulative selection budget) are deliberately distinct
    estimands (H-A2-4/§6) — the floor bounds the residual seam (H-A2-13).
    """
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sr = float(arr.mean() / arr.std(ddof=1)) if arr.std(ddof=1) > 0 else 0.0
    n = len(arr)
    skew = float(((arr - arr.mean()) ** 3).mean() / (arr.std() ** 3)) if arr.std() > 0 else 0.0
    kurt = (
        float(((arr - arr.mean()) ** 4).mean() / (arr.std() ** 4))
        if arr.std() > 0 else 3.0
    )
    # Threshold from López de Prado (Deflated Sharpe Ratio, eqn 8/9). Same
    # formula as tpcore/backtest/overfitting.py.
    EULER = 0.5772156649015329
    e_max_bracket = (
        (1.0 - EULER) * _norm_inv(1.0 - 1.0 / max(n_trials, 1))
        + EULER * _norm_inv(1.0 - 1.0 / (max(n_trials, 1) * math.e))
    )
    floor = 1.0 / max(n - 1, 1)  # legacy single-estimator value — now a FLOOR
    if trial_sharpe_variance is not None:
        # H-A2-10: clamp up to the floor — an honest low-dispersion sweep
        # must NOT loosen the (already-too-lenient) legacy bar.
        sr_variance = max(float(trial_sharpe_variance), floor)
    else:
        sr_variance = floor  # KNOWN APPROXIMATION — not the paper's V
        logger.warning(
            "tpcore.overfitting.dsr.null_variance_approximation",
            reason="no per-trial Sharpe vector available; using "
                   "single-estimator 1/(n-1) instead of cross-trial "
                   "V[SR_n]",
            n_trials=n_trials,
            n_obs=n,
        )
    # The √V factor is now supplied solely by the V-term (the legacy
    # 1/(n-1) is REMOVED from the V role — it conflated within-strategy
    # estimation noise into the selection-bias term, the same defect
    # expressed differently). The non-normality term stays in `denom`.
    e_max = math.sqrt(sr_variance) * e_max_bracket
    denom = math.sqrt(
        max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr ** 2), 1e-12)
    ) / math.sqrt(max(n - 1, 1))
    if denom <= 0:
        return 0.0
    z = (sr - e_max) / denom
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
```

Confirm `logger` is module-level in `ops/lab/run.py` (structlog). If the module has no `logger`, add at module top (after the existing imports, before the first function): `logger = structlog.get_logger(__name__)` and `import structlog` in the import block (stdlib/3rd-party-before-first-party isort order). Verify first:

Run: `grep -n "^logger = \|^import structlog\|structlog.get_logger" ops/lab/run.py`
Expected: shows an existing `logger`/`structlog` binding. If ABSENT, add `import structlog` (3rd-party block) + `logger = structlog.get_logger(__name__)` (module scope) in this step.

> **Note (H-A2-15 / oracle):** the legacy two-arg numeric is preserved on the fallback branch (`floor == 1/(n-1)`, `e_max == √floor·bracket`, `denom` unchanged) — algebraically identical to the original `denom = sqrt(var/ (n-1))`, `e_max = bracket`, `z = (sr - e_max)/denom` ⇒ same `z`. Verified by `test_sp_a2_t_verdict_fallback_warns_and_byte_identical` (< 1e-12). This is what keeps the SP2 oracle green by *property* AND numerically on its two-arg call (§5).

- [ ] **Step 4: Run the new tests — expect PASS**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_dsr_delivered.py`
Expected: PASS (both — fallback byte-identical + WARNING; V path tightens + silent).

- [ ] **Step 5: ruff + check_imports**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/ scripts/ ops/`
Expected: `All checks passed!`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 6: Commit**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add ops/lab/run.py tpcore/tests/test_lab_dsr_delivered.py`

Run: `git commit -m "feat(lab-fh): SP-A2 — compute_dsr_for_verdict cross-trial V + floor + loud fallback (H-A2-9)"`

---

### Task 6: Wire real V at the verdict call site — the delivered tightening (RED→GREEN, MAKE-OR-BREAK T-DELIVERED + T-UNITS-COHERENT)

Satisfies: **§3.4 obл.1+obл.2 (verdict-site V wiring), §4 conclusion (the ONE production site that tightens), H-A2-9 (delivery), H-A2-11 (per-period, NOT annualized)**.

**Files:**
- Modify: `ops/lab/run.py` (`_run_lab_core`, the verdict call `:774-775`)
- Modify (append): `tpcore/tests/test_lab_dsr_delivered.py`

- [ ] **Step 1: Write the failing tests — a DISPERSED multi-trial offline harness (the SP-A harness is degenerate)**

Append to `tpcore/tests/test_lab_dsr_delivered.py`:

```python
def _install_dispersed_harness(monkeypatch, lab_run, *, per_trial_returns,
                                held_returns, cred_score=80):
    """Offline harness with a DISPERSED trial set (the SP-A harness
    produces a single repeated config ⇒ V≈0 ⇒ can't prove tightening).
    Each evaluated candidate gets a distinct return series so the trials
    list carries a real cross-trial per-period Sharpe dispersion; the
    final held-back winner replay uses `held_returns`."""
    from datetime import date as _date

    from tpcore.backtest.credibility import CredibilityScore

    _rubric = CredibilityScore(
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True,
        score=cred_score,
    )

    class _Trade:
        def __init__(self, d, p):
            self.entry_date = d
            self.pnl_pct = p

    seq = {"i": 0}

    def _mk(returns):
        class _RR:
            credibility_score = cred_score
            credibility_rubric = _rubric
            trade_log = [
                _Trade(_date(2022, 1, 3) + timedelta(days=k), r)
                for k, r in enumerate(returns)
            ]
        return _RR()

    def _ctx_runner(context, *, overrides=None):
        rs = per_trial_returns[seq["i"] % len(per_trial_returns)]
        seq["i"] += 1
        return _mk(rs)

    async def _ctx_loader(*a, **k):
        return object()

    async def _runner(*a, **k):
        return _mk(held_returns)   # the held-back winner replay

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _ctx_loader)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)

    async def _fake_write_cred(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write_cred, raising=True)


def _ns(output, *, trials, seed=0):
    import argparse
    return argparse.Namespace(
        engine="reversion", trials=trials, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None,
    )


async def test_sp_a2_t_delivered_lab_verdict_strictly_tightened(
        monkeypatch, tmp_path) -> None:
    """T-DELIVERED (MAKE-OR-BREAK, the crux pin). With ≥ MIN_TRIALS_FOR_V
    dispersed trials, the Lab verdict DSR is STRICTLY LOWER than the same
    run with the V path disabled — a real numeric tightening, not inert
    plumbing."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    rng = np.random.default_rng(11)
    # 8 distinct candidate return series (real cross-trial dispersion).
    per_trial = [
        [float(x) for x in rng.normal(m, 0.012, 40)]
        for m in (0.002, 0.006, 0.010, 0.014, 0.018, 0.022, 0.026, 0.030)
    ]
    held = [float(x) for x in rng.normal(0.02, 0.012, 40)]

    seen = {}
    real = lab_run.compute_dsr_for_verdict

    def _cap(r, *, n_trials, trial_sharpe_variance=None):
        seen["v"] = trial_sharpe_variance
        return real(r, n_trials=n_trials,
                    trial_sharpe_variance=trial_sharpe_variance)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _cap)
    _install_dispersed_harness(monkeypatch, lab_run,
                               per_trial_returns=per_trial, held_returns=held)

    class _Pool:
        def acquire(self):
            raise AssertionError("legacy path must not touch a pool")
        async def close(self):
            ...

    async def _fake_build(url, *, read_only, **k):
        return _Pool()

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    # candidate=None ⇒ legacy non-ledger path (effective_n_trials =
    # args.trials); the V wiring is orthogonal to the SP-A ledger.
    core = await lab_run._run_lab_core(
        _ns(tmp_path / "d.csv", trials=8, seed=1), candidate=None)
    assert not isinstance(core, int)
    assert seen["v"] is not None                       # real V threaded
    dsr_with_v = core.dsr
    dsr_fallback = real(held, n_trials=core.effective_n_trials)
    assert dsr_with_v < dsr_fallback - 1e-9            # STRICTLY tightened


async def test_sp_a2_t_units_coherent_v_uses_per_period_not_annualized(
        monkeypatch, tmp_path) -> None:
    """T-UNITS-COHERENT (MAKE-OR-BREAK, H-A2-11). The V fed at the verdict
    site is np.var of the NON-annualized holdout_sharpe_per_period. A
    fixture whose annualized sharpe differs from per-period by a known
    √ppy: using the annualized field would inflate SR₀ past a tripwire
    (DSR≈0); the per-period field keeps it sane."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    rng = np.random.default_rng(13)
    per_trial = [
        [float(x) for x in rng.normal(m, 0.012, 40)]
        for m in (0.004, 0.008, 0.012, 0.016, 0.020, 0.024, 0.028, 0.032)
    ]
    held = [float(x) for x in rng.normal(0.02, 0.012, 40)]

    captured = {}
    real = lab_run.compute_dsr_for_verdict

    def _cap(r, *, n_trials, trial_sharpe_variance=None):
        captured["v"] = trial_sharpe_variance
        return real(r, n_trials=n_trials,
                    trial_sharpe_variance=trial_sharpe_variance)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _cap)
    _install_dispersed_harness(monkeypatch, lab_run,
                               per_trial_returns=per_trial, held_returns=held)

    async def _fake_build(url, *, read_only, **k):
        class _P:
            async def close(self): ...
        return _P()

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    core = await lab_run._run_lab_core(
        _ns(tmp_path / "u.csv", trials=8, seed=2), candidate=None)
    assert not isinstance(core, int)
    v = captured["v"]
    assert v is not None
    # The per-period dispersion of these 8 series is O(1e-3..1e-2); the
    # ANNUALIZED dispersion would be ≈ppy× larger (ppy≈40 here ⇒ ~40×).
    # Assert V is in the per-period band, NOT the annualized band.
    assert v < 0.5, ("V looks annualized (units bug regressed): "
                     f"{v}")
    # And the realized verdict DSR is finite/sane (not the DSR≈0-always
    # collapse the annualized bug causes).
    assert 0.0 <= core.dsr <= 1.0
```

- [ ] **Step 2: Run them — expect FAIL**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_dsr_delivered.py -k "t_delivered or t_units_coherent"`
Expected: FAIL — `seen["v"]` / `captured["v"]` is `None` (the verdict site does NOT yet compute or pass V).

- [ ] **Step 3: Wire V at the verdict call site (`ops/lab/run.py:768-775`)**

In `_run_lab_core`, replace the verdict block (the `if spend_ts is not None: … effective_n_trials = …` through `dsr = compute_dsr_for_verdict(held_period_returns, n_trials=effective_n_trials)`) so the `effective_n_trials` derivation is **unchanged** (SP-A ledger untouched) and only the DSR call gains V:

```python
    if spend_ts is not None:
        cumulative = await cumulative_n_trials(
            _ledger_pool, args.engine, spend_ts)
        effective_n_trials = cumulative + args.trials
    else:
        effective_n_trials = args.trials
    # SP-A2 / H-A2-9 + H-A2-11: V[ŜR_n] is the cross-trial dispersion of
    # the per-trial *per-period* (NON-annualized) holdout Sharpes across
    # this run's searched trials — the same per-period space as
    # compute_dsr_for_verdict's internal SR̂. NOT t.holdout.sharpe (that
    # is ANNUALIZED — feeding it would inflate SR₀ by ≈periods_per_year).
    # Guarded by MIN_TRIALS_FOR_V (H-A2-10): too few non-errored trials ⇒
    # None ⇒ the documented 1/(n-1) fallback + WARNING inside the call.
    # H-A2-4: V-source trial count and the SP-A cumulative n_trials are
    # deliberately distinct estimands — logged side-by-side, not
    # silently reconciled (the floor bounds the residual seam, H-A2-13).
    from tpcore.backtest.overfitting import MIN_TRIALS_FOR_V
    _pp_sharpes = [
        t.holdout.holdout_sharpe_per_period
        for t in trials
        if not t.error
    ]
    if len(_pp_sharpes) >= MIN_TRIALS_FOR_V:
        trial_sharpe_var: float | None = float(
            np.var(np.asarray(_pp_sharpes, dtype=float), ddof=1)
        )
        logger.info(
            "tpcore.lab.dsr.v_n_trial_population",
            v_trial_count=len(_pp_sharpes),
            n_trials=effective_n_trials,
        )
    else:
        trial_sharpe_var = None
    dsr = compute_dsr_for_verdict(
        held_period_returns,
        n_trials=effective_n_trials,
        trial_sharpe_variance=trial_sharpe_var,
    )
```

Confirm `np` and `logger` are in scope in `ops/lab/run.py` (both used elsewhere in the module: `np` at `:243`, `logger` confirmed/added in Task 5 Step 3). The `from tpcore.backtest.overfitting import MIN_TRIALS_FOR_V` is a **lazy in-body import** (no module-level engine/tpcore-heavy import added; consistent with the file's lazy-import discipline).

- [ ] **Step 4: Run the tests — expect PASS**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_dsr_delivered.py -k "t_delivered or t_units_coherent"`
Expected: PASS — `seen["v"]` is a real float; `dsr_with_v < dsr_fallback − 1e-9` (strict tightening); V in the per-period band.

- [ ] **Step 5: Run the whole new file — no regression**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_dsr_delivered.py`
Expected: all pass.

- [ ] **Step 6: ruff + check_imports**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/ scripts/ ops/`
Expected: `All checks passed!` (lazy-import inside the function body is acceptable; isort applies to module-level only).

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 7: Commit**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add ops/lab/run.py tpcore/tests/test_lab_dsr_delivered.py`

Run: `git commit -m "feat(lab-fh): SP-A2 — wire real per-period cross-trial V at the Lab verdict site (T-DELIVERED, the crux)"`

---

### Task 7: SP-A ledger-test re-baseline — **HIGHEST-SCRUTINY TASK** (RED→GREEN, MAKE-OR-BREAK T-LEDGER-COMPAT, H-A2-12)

> **⚠ TOP-RISK TASK — REQUIRES THE MOST REVIEWER SCRUTINY.** This task **edits just-shipped SP-A safety/anti-laundering tests** (`tpcore/tests/test_lab_ntrials_ledger.py`, PR #93). It is ordered *after* Tasks 2/6 deliberately: the re-baseline is justified ONLY by the demonstrated tightening proven by T-DELIVERED + the byte-identical-fallback proof. The reviewer MUST confirm: (a) the `.dsr` numerics moved because V *legitimately tightens* the gate (a genuine hardening), NOT because the fix weakened anything; (b) every re-baselined assertion carries an explicit H-LL-7 anti-reversion docstring stating WHY the number changed; (c) the `_spy` widening is signature-only (no behavior change to the stub); (d) this task does NOT worsen the **known pre-existing collection-time-eviction defect (task #148)** in this file's module-load `sys.modules` stanza — if the stanza must be touched at all, prefer **per-test eviction scoping** over broadening the module-load loop.

Satisfies: **§5 H-A2-12 scope correction (editable ledger tests, deliberate reviewed re-baseline — distinct from the byte-frozen oracle), H-A2-12, the SP-A make-or-break surface survival**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (3 `_spy` signatures `:404`/`:621`/`:735`; 2 pinned assertions `:441`/`:644`)

- [ ] **Step 1: Run the SP-A suite UNCHANGED — observe the EXACT crash (RED, the justification baseline)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py 2>&1 | tail -30`
Expected: ≥4 tests **ERROR/FAIL** with `TypeError: _spy_dsr() got an unexpected keyword argument 'trial_sharpe_variance'` (and `_spy()` likewise) — because Task 6's production call site now passes `trial_sharpe_variance=` into the monkeypatched stub. Record which tests crash; this is the documented H-A2-12 crash, and the precise before/after justification for the re-baseline.

- [ ] **Step 2: Widen the 3 `_spy` stub signatures (signature-only — no behavior change)**

In `tpcore/tests/test_lab_ntrials_ledger.py`:

At `:404-406` replace:

```python
    def _spy_dsr(r, *, n_trials):
        seen_n_trials.append(n_trials)
        return real_dsr(r, n_trials=n_trials)
```

with:

```python
    def _spy_dsr(r, *, n_trials, trial_sharpe_variance=None):
        # SP-A2: the production call site now passes
        # trial_sharpe_variance=. Widen the stub signature so it does
        # not raise TypeError; forward it so the wrapped real DSR sees
        # the same V the production path computed (H-A2-12).
        seen_n_trials.append(n_trials)
        return real_dsr(r, n_trials=n_trials,
                        trial_sharpe_variance=trial_sharpe_variance)
```

At `:621-623` replace:

```python
    def _spy(r, *, n_trials):
        seen.append(n_trials)
        return real_dsr(r, n_trials=n_trials)
```

with:

```python
    def _spy(r, *, n_trials, trial_sharpe_variance=None):
        # SP-A2 H-A2-12 — signature widening (see _spy_dsr note above).
        seen.append(n_trials)
        return real_dsr(r, n_trials=n_trials,
                        trial_sharpe_variance=trial_sharpe_variance)
```

At `:735-737` replace:

```python
    def _spy(r, *, n_trials):
        seen.append(n_trials)
        return real_dsr(r, n_trials=n_trials)
```

with:

```python
    def _spy(r, *, n_trials, trial_sharpe_variance=None):
        # SP-A2 H-A2-12 — signature widening (legacy non-Lab path: the
        # production site passes trial_sharpe_variance=None here since
        # candidate is None ⇒ the offline harness yields < MIN_TRIALS).
        seen.append(n_trials)
        return real_dsr(r, n_trials=n_trials,
                        trial_sharpe_variance=trial_sharpe_variance)
```

- [ ] **Step 3: Re-run — observe the now-NUMERIC failures (the pinned `.dsr` assertions)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py 2>&1 | tail -30`
Expected: the `TypeError`s are gone; now `test_second_candidate_same_target_gets_strictly_larger_n_trials` (`core2.dsr <= core1.dsr` `:441`) and the first-ever-run test (`core.dsr == real_dsr(returns, n_trials=37)` `:644`) may FAIL on the numeric pin (the offline harness builds a degenerate single-config trial set ⇒ `_pp_sharpes` from the SP-A harness may be < MIN_TRIALS_FOR_V ⇒ `trial_sharpe_var=None` ⇒ fallback). **Determine empirically which of the two assertions actually moved** from the tail output before editing — re-baseline ONLY the assertion that genuinely changed; leave a still-true assertion untouched.

- [ ] **Step 4: Re-baseline ONLY the moved assertion(s) with an explicit H-LL-7 anti-reversion docstring**

For `:441` (`assert core2.dsr <= core1.dsr`): this is a **monotone-direction** assertion, not a golden numeric. The SP-A2 floor + V threading preserve "more trials ⇒ DSR no higher on identical returns" (V is N-independent; the floor never loosens). If it still holds, **leave it byte-unchanged** and add a one-line comment above it: `# SP-A2: still holds — V is N-independent and floored, monotone-in-N preserved (H-LL-7).` If (and only if) Step 3 shows it moved, replace it with the empirically-correct relation AND prepend this docstring block to the test function body (right after the existing docstring):

```python
    # ── SP-A2 H-LL-7 RE-BASELINE (deliberate, reviewed — NOT a
    #    weakening). The pinned DSR relation changed because SP-A2
    #    threads the real cross-trial per-period V[ŜR_n] into
    #    compute_dsr_for_verdict (Bailey & LdP SSRN 2460551), a genuine
    #    *tightening* of the selection-bias bar — the previous number
    #    encoded the statistically-wrong 1/(n-1) approximation. This is
    #    an editable SP-A test (NOT the byte-frozen SP2 oracle). Do NOT
    #    revert toward the old value: that would re-introduce the
    #    corrected defect (anti-reversion guard). Justification:
    #    T-DELIVERED in tpcore/tests/test_lab_dsr_delivered.py proves the
    #    direction is a tightening; the fallback branch is byte-identical
    #    (T-VERDICT-FALLBACK-WARNS). ──────────────────────────────────
```

For `:644` (`assert core.dsr == real_dsr(returns, n_trials=37)`): post-SP-A2 the production LHS may pass a real `trial_sharpe_variance` while the RHS `real_dsr(returns, n_trials=37)` is a two-arg fallback call. Re-baseline to compare like-for-like by passing the SAME V the production path used. Replace:

```python
    # Same returns + same n_trials ⇒ DSR identical to pre-SP-A path.
    assert core.dsr == real_dsr(returns, n_trials=37)
```

with:

```python
    # ── SP-A2 H-LL-7 RE-BASELINE (deliberate, reviewed — NOT a
    #    weakening). Pre-SP-A2 this pinned the bare 1/(n-1)-fallback
    #    number. SP-A2 threads real cross-trial per-period V at the
    #    verdict site (a genuine tightening; T-DELIVERED proves the
    #    direction). The honest like-for-like pin compares the verdict
    #    DSR against real_dsr called with the SAME V the production path
    #    used (None ⇒ the offline harness yielded < MIN_TRIALS_FOR_V
    #    non-errored trials ⇒ documented fallback, byte-identical). Do
    #    NOT revert toward the old equality without the V arg — that
    #    would mask the corrected defect. Editable SP-A test, NOT the
    #    byte-frozen SP2 oracle (§5 / H-A2-12). ───────────────────────
    from tpcore.backtest.overfitting import MIN_TRIALS_FOR_V
    _pp = [t.holdout.holdout_sharpe_per_period
           for t in core.ranked and []]  # placeholder removed below
```

Then immediately correct the like-for-like to use the harness reality: the SP-A `_install_offline_harness` produces ONE repeated config across windows, so the non-errored per-period Sharpes are NOT ≥ `MIN_TRIALS_FOR_V` distinct trials ⇒ the production path took `trial_sharpe_var=None`. Therefore the correct, honest re-baseline is the two-arg fallback equality **kept as-is**:

```python
    # Harness reality: _install_offline_harness replays one repeated
    # config ⇒ fewer than MIN_TRIALS_FOR_V non-errored trials ⇒ the
    # production verdict site passes trial_sharpe_variance=None ⇒ the
    # documented 1/(n-1) fallback, BYTE-IDENTICAL to pre-SP-A2. So the
    # original equality still holds verbatim (the WARNING is a logging
    # side-effect, numerically inert — T-VERDICT-FALLBACK-WARNS).
    assert core.dsr == real_dsr(returns, n_trials=37)
```

(Net effect: `:644` is most likely **unchanged** because the SP-A harness is degenerate ⇒ fallback ⇒ byte-identical; the H-LL-7 docstring documents *why it is still correct under SP-A2*. Only re-write the assertion value if Step 3 empirically shows it moved — if the harness ever yields ≥ `MIN_TRIALS_FOR_V` distinct trials, replace the RHS with `real_dsr(returns, n_trials=37, trial_sharpe_variance=<the V the production path logged>)` and keep the H-LL-7 block. The reviewer MUST see Step 3's tail output to confirm which branch applies — do not guess.)

> **Reviewer gate:** before accepting this task, demand the Step 1 + Step 3 pytest tail outputs. The re-baseline is legitimate ONLY if (i) Step 1 showed the `TypeError` crash, (ii) Step 3 showed exactly which numeric moved (or that none did and the change is docstring-only), (iii) every edited assertion carries the H-LL-7 block, (iv) the module-load eviction stanza (`:24-26`) is NOT broadened (task #148 not worsened).

- [ ] **Step 5: Run the full SP-A ledger suite — expect GREEN (T-LEDGER-COMPAT)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py`
Expected: all pass — no `TypeError`, the (possibly unchanged) `.dsr` assertions hold under SP-A2, the H-LL-7 docstrings present.

- [ ] **Step 6: Assert task #148 not worsened (the module-load eviction stanza is byte-unchanged)**

Run: `git diff origin/main -- tpcore/tests/test_lab_ntrials_ledger.py | grep -nE '^\+|^-' | grep -E 'sys.modules|__path__|del sys.modules' || echo "eviction stanza untouched"`
Expected: `eviction stanza untouched` (the `:24-26` module-load loop is NOT in the diff — task #148 is neither fixed nor worsened here; this task only widens `_spy` signatures + adds H-LL-7 docstrings).

- [ ] **Step 7: ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/`
Expected: `All checks passed!`

- [ ] **Step 8: Commit (the highest-scrutiny commit — explicit re-baseline message)**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add tpcore/tests/test_lab_ntrials_ledger.py`

Run: `git commit -m "test(lab-fh): SP-A2 — deliberate reviewed re-baseline of SP-A ledger tests (H-A2-12, H-LL-7 anti-reversion; widen _spy, justified by T-DELIVERED tightening)"`

---

### Task 8: T-VN-COHERENCE + T-ORACLE — the cross-run seam bound + the byte-frozen oracle (RED→GREEN, MAKE-OR-BREAK T-ORACLE)

Satisfies: **§5 (oracle = byte-frozen property contract, H-A2-2), §6 + H-A2-4 + H-A2-13 (the SP-A↔SP-A2 V/N coherence seam as an executable bounded-limitation contract), H-A2-15 (blast-radius)**.

**Files:**
- Modify (append): `tpcore/tests/test_overfitting.py` (T-VN-COHERENCE — pure-math)
- Modify (append): `tpcore/tests/test_lab_dsr_delivered.py` (T-ORACLE)

- [ ] **Step 1: Write T-VN-COHERENCE (the H-A2-13 floor bounds the seam) in `test_overfitting.py`**

Append to `tpcore/tests/test_overfitting.py`:

```python
def test_sp_a2_t_vn_coherence_floor_bounds_the_cross_run_seam() -> None:
    """T-VN-COHERENCE (H-A2-4/H-A2-13). Holding a small single-run
    dispersion fixed while growing the cumulative N: SR₀ still increases
    monotonically via the untouched Φ⁻¹ bracket (SP-A's anti-laundering
    term is NOT defeated by a small √V), AND the H-A2-10 floor at
    1/(n_obs-1) is active so a tight fine-grid sweep cannot drive SR₀
    below the fallback. Encodes the §6/H-A2-13 accepted-limitation as an
    executable contract."""
    tiny_v = 1e-6  # a degenerately-tight fine-grid sweep
    n_obs = 250
    prev = -1.0
    for cum_n in (50, 200, 800, 2000):
        sr0_v = _expected_max_sharpe_under_null(
            cum_n, n_obs, trial_sharpe_variance=tiny_v)
        sr0_fb = _expected_max_sharpe_under_null(cum_n, n_obs)
        # Monotone-up in the cumulative N (SP-A bracket not defeated).
        assert sr0_v > prev
        prev = sr0_v
        # The floor: tiny V can NOT drop SR₀ below the legacy fallback.
        assert sr0_v >= sr0_fb - 1e-12
```

- [ ] **Step 2: Write T-ORACLE (byte-frozen empty diff + green) in `test_lab_dsr_delivered.py`**

Append to `tpcore/tests/test_lab_dsr_delivered.py`:

```python
import subprocess


def test_sp_a2_t_oracle_byte_unmodified_and_green() -> None:
    """T-ORACLE (MAKE-OR-BREAK, §5/H-A2-2). The SP2 characterization
    oracle stays BYTE-UNMODIFIED (empty diff vs origin/main) AND green by
    property. Distinct from the editable SP-A ledger tests (Task 7,
    H-A2-12) — conflating them was the original spec defect."""
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--",
         "scripts/tests/test_search_parameters_characterization.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=True,
    )
    assert diff.stdout.strip() == "", (
        "SP2 oracle was modified — §5 invariant violated:\n" + diff.stdout
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "scripts/tests/test_search_parameters_characterization.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert run.returncode == 0, (
        "SP2 oracle property/parity contract went RED under SP-A2 — that "
        "is a real regression in the fix, NOT a signal to edit the "
        "oracle:\n" + run.stdout[-3000:] + run.stderr[-2000:]
    )
```

- [ ] **Step 3: Run them — expect FAIL then PASS**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py -k t_vn_coherence "tpcore/tests/test_lab_dsr_delivered.py::test_sp_a2_t_oracle_byte_unmodified_and_green"`
Expected: PASS (T-VN-COHERENCE passes against the Task 2 floor; T-ORACLE passes — the oracle file is in the File-Structure UNTOUCHED row, never edited by any task, and its properties hold under the fix). If T-VN-COHERENCE fails first because the function is new-shaped, it is already implemented by Task 2 — it should pass directly; if T-ORACLE's diff is non-empty, a prior task wrongly touched the oracle — STOP and revert that edit (do not edit the oracle).

- [ ] **Step 4: Full overfitting + delivered suites — no regression**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_overfitting.py tpcore/tests/test_lab_dsr_delivered.py`
Expected: all pass.

- [ ] **Step 5: ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/`
Expected: `All checks passed!` (`import subprocess` is stdlib — keep it in the stdlib import block of `test_lab_dsr_delivered.py` to satisfy isort I001; move the top-of-file `import subprocess` up with `import sys` if ruff flags ordering).

- [ ] **Step 6: Commit**

Run: `git branch --show-current`
Expected: `sp-a2-dsr-variance-clean`

Run: `git add tpcore/tests/test_overfitting.py tpcore/tests/test_lab_dsr_delivered.py`

Run: `git commit -m "test(lab-fh): SP-A2 — T-VN-COHERENCE (floored cross-run seam) + T-ORACLE (byte-frozen SP2 oracle)"`

---

### Task 9: Full-gate (CI-exact) + lane/scope assertion + self-review + finish branch

Satisfies: **§7 safety, §7.4 cross-lane, §9 non-goals, §12 self-review, all preservation gates, the zero-new-failures invariant from Task 0**.

**Files:**
- Modify: none (verification + branch finish only)

- [ ] **Step 1: Full test suite (CI-exact) — zero NEW failures vs the Task 0 baseline**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -40`
Expected: pass/skip summary with **no new failures/errors vs `/tmp/sp-a2-baseline.txt`** (Task 0 Step 2). DB-gated tests SKIP locally (correct; CI runs the full suite — the SP-A DB-gated tests are irrelevant to SP-A2's worktree-safe merge). Diff the failed/error count against the baseline; any delta MUST be explained (expected: zero delta — SP-A2 adds only passing tests + deliberately re-baselined-green SP-A tests).

- [ ] **Step 2: CI-exact ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/ scripts/ ops/`
Expected: `All checks passed!`

- [ ] **Step 3: CI-exact forbidden-imports (tpcore stays engine-free)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 4: Engine-manifest consistency (no roster/testpath drift)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python scripts/gen_engine_manifest.py --check`
Expected: exit 0 (SP-A2 adds no engine; the manifest is unchanged).

- [ ] **Step 5: SP2 oracle byte-frozen + green (the §5 enforcement)**

Run: `git log --oneline origin/main..HEAD -- scripts/tests/test_search_parameters_characterization.py`
Expected: empty output (no SP-A2 commit touched the oracle).

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_ntrials_ledger.py tpcore/tests/test_overfitting.py tpcore/tests/test_lab_dsr_delivered.py`
Expected: all pass/skip — the oracle green by property (UNMODIFIED), the deliberately-re-baselined SP-A ledger suite green, the SP-A2 make-or-break tests green.

- [ ] **Step 6: Lane / scope assertion — no forbidden-file or data-lane diff**

Run: `git diff --name-only origin/main...HEAD`
Expected: ONLY these paths appear — `docs/superpowers/plans/2026-05-19-dsr-null-variance-fix.md`, `tpcore/backtest/overfitting.py`, `ops/lab/run.py`, `tpcore/tests/test_overfitting.py`, `tpcore/tests/test_lab_ntrials_ledger.py`, `tpcore/tests/test_lab_dsr_delivered.py`.

Run: `git diff --name-only origin/main...HEAD | grep -E 'tpcore/calendar\.py|tpcore/risk/|ops/engine_supervisor\.py|ops/engine_service\.py|ops/engine_ladder\.py|tpcore/supervisor_state\.py|tpcore/trade_monitor\.py|\.github/|archive/sigma/|data-provider-lifecycle|data_feed_change_request' && echo "LANE VIOLATION — FORBIDDEN FILE TOUCHED" || echo "lane clean"`
Expected: `lane clean` (grep matches nothing ⇒ the `||` branch fires). Also assert `scripts/tests/test_search_parameters_characterization.py` is NOT in the name-only list.

- [ ] **Step 7: Assert the SP-A ledger read/emit + thresholds are byte-unchanged (§9 non-goals)**

Run: `git diff origin/main -- ops/lab/run.py | grep -E 'cumulative_n_trials|record_trial_spend|DSR_PASS_THRESHOLD|dsr_threshold=|norm.ppf|n_trials=effective_n_trials' | grep '^-' || echo "ledger/threshold/ppf lines not removed"`
Expected: `ledger/threshold/ppf lines not removed` (no deletion of the SP-A ledger read, no threshold change; the `effective_n_trials` derivation is preserved — only the DSR call gained the V kwarg).

Run: `git diff origin/main -- tpcore/backtest/overfitting.py | grep -E 'DSR_PASS_THRESHOLD|EULER_MASCHERONI =' | grep -E '^\+|^-' || echo "threshold + EULER byte-unchanged"`
Expected: `threshold + EULER byte-unchanged`.

- [ ] **Step 8: Assert `git stash list` unchanged (git-hygiene)**

Run: `git stash list`
Expected: empty (identical to Task 0 Step 1 — no stash was ever used; transient reverts, if any, used `git checkout --`).

- [ ] **Step 9: Finish the development branch**

Use **superpowers:finishing-a-development-branch** to integrate the work: verify `git branch --show-current` == `sp-a2-dsr-variance-clean` FIRST; open a PR (body ends with the standard Claude Code footer); on CI-green, worktree-safe squash-merge with `--delete-branch` per the standing git-hygiene method. CI runs the full suite (the SP-A DB-gated tests run there and must stay green — the deliberate re-baseline in Task 7 keeps them green by tightening, not weakening). Tests/code MUST NEVER run real `git`/`gh` against the working repo — the finish-branch flow handles integration; T-ORACLE's `subprocess git diff` is read-only (`git diff`, no mutation) and runs against the worktree, which is acceptable per the read-only exception.

---

## Self-Review

### 1. Spec coverage (every amended § + H-A2-1..15 → a task)

| Spec item | Task | Pinning test |
| --- | --- | --- |
| §1 authoritative formula (V = cross-trial dispersion) | T2 | T-WORKED |
| §2.1 the defect (`1/(n_obs−1)`) | T1, T2 | T-WORKED, byte-unchanged pin |
| §2.2 worked numbers (0.10190/0.22763, DSR 0.8573/0.0423) | T2 | T-WORKED |
| §3.1 kw-only V + floor + WARNING (`max(V, 1/(n_obs−1))`, MIN_TRIALS_FOR_V) | T2, T3 | T-WORKED, T-FALLBACK-WARNS, T-STRICTER |
| §3.2 `_deflated_sharpe_ratio` pass-through | T2 | T-SIG-COMPAT |
| §3.3 `_trial_sharpe_variance` helper (reuse `_column_sharpes`) | T3 | T-CROSSTRIAL, T-DEGENERATE |
| §3.4 obл.1 BOTH impls coherent + per-period field (H-A2-11) | T4 (field), T5 (impl), T6 (wire) | T-UNITS-COHERENT, T-DELIVERED |
| §3.4 obл.2 V/N coherence logged | T3, T6 | T-VN-COHERENCE |
| §3.4 obл.3 cross-reference comment in both impls | T2 (docstring), T5 (docstring) | reviewer-verified |
| §4 per-caller matrix (fallback rows, no fabrication, H-A2-3) | T3, T5 | T-FALLBACK-WARNS, T-SIG-COMPAT |
| §4 conclusion — Lab verdict path is the delivery vehicle (H-A2-9) | T6 | T-DELIVERED |
| §5 oracle = byte-frozen property contract (no re-baseline, H-A2-2) | T8 | T-ORACLE |
| §5 H-A2-12 ledger-test deliberate reviewed re-baseline | T7 | T-LEDGER-COMPAT |
| §6 SP-A orthogonality (`√V` × untouched bracket) | T2 | T-ORTHO |
| §7 tightening-only safety (floor) / H-A2-5 / H-A2-13 | T2, T8 | T-STRICTER, T-VN-COHERENCE |
| §7.4 cross-lane (additive kw-only, no concurrent edit) | T9 | lane-clean assertion |
| §8 every make-or-break test | T1–T8 | (mapped below) |
| §9 non-goals (`norm.ppf`/threshold/ledger/no unify/no fabricate/oracle/forbidden files) | T1, T7, T9 | byte-pin, lane assertion, §9 grep |
| §10 H-register (H-A2-1..8) | T2/T3/T5/T8 | per row |
| Hardening Addendum H-A2-9..15 | T6/T4/T7/T8/T2/T9 | per row (Task 0 map) |

H-A2-1..15 closure: **H-A2-1** T2 (T-FALLBACK-WARNS) + T5 (T-VERDICT-FALLBACK-WARNS); **H-A2-2** T8 (T-ORACLE); **H-A2-3** T3/T5 (None when no matrix, no fabrication); **H-A2-4** T3/T6 (V/N logged side-by-side) + T8 (T-VN-COHERENCE); **H-A2-5** T2 (T-STRICTER docstring forbids reversion); **H-A2-6** T1 (byte-unchanged γ-blend pin); **H-A2-7** T2/T5 cross-ref docstrings; **H-A2-8** T3 (`ddof=1`, None never raise — T-DEGENERATE); **H-A2-9** T6 (T-DELIVERED — the delivery vehicle); **H-A2-10** T2 (floor + MIN_TRIALS_FOR_V) — T-STRICTER; **H-A2-11** T4 (per-period field) + T6 (T-UNITS-COHERENT); **H-A2-12** T7 (highest-scrutiny re-baseline) — T-LEDGER-COMPAT; **H-A2-13** T8 (T-VN-COHERENCE bounds the seam via the floor); **H-A2-14** T2/T6 (per-impl ε tolerances, no cross-impl equality, no `_norm_inv` unify); **H-A2-15** T9 (full-suite blast-radius + lane-clean + threshold/ppf byte-pin).

Make-or-break map: **T-WORKED → T2**, **T-CROSSTRIAL → T3**, **T-FALLBACK-WARNS → T2**, **T-VERDICT-FALLBACK-WARNS → T5**, **T-DELIVERED → T6**, **T-UNITS-COHERENT → T6**, **T-LEDGER-COMPAT → T7**, **T-VN-COHERENCE → T8**, **T-STRICTER → T2**, **T-ORTHO → T2**, **T-DEGENERATE → T3**, **T-ORACLE → T8**, **T-SIG-COMPAT → T2**.

### 2. Placeholder scan

Searched for `TBD`/`TODO`/`fill in`/`add appropriate`/`add validation`/`similar to Task N`/`implement later`/`handle edge cases`/`<placeholder>`/`???` — none present. Every code step contains complete copy-pasteable code: both full function replacements in `overfitting.py`, the full `compute_dsr_for_verdict` rewrite, the full `SliceMetrics`/`compute_slice_metrics_from_trades` edits, the full verdict-site V block, every complete test, the full new `test_lab_dsr_delivered.py` (with its eviction stanza + dispersed harness written out in full, not "see Task N"). The one conditional in Task 7 Step 4 ("re-baseline ONLY the moved assertion") is *deliberately empirical-evidence-gated* — both branches are written out in full (the leave-unchanged + comment branch AND the replace-with-H-LL-7-block branch); the implementer picks based on the pytest tail output, which is the correct, honest disposition for re-baselining a just-shipped safety test (guessing the new number blind would be the defect).

### 3. Type / name consistency

- `MIN_TRIALS_FOR_V` — defined T2 (`tpcore/backtest/overfitting.py`), imported lazily in T6 (`ops/lab/run.py` verdict block) and used in T3/T6/T7 tests identically (`= 5`).
- `trial_sharpe_variance: float | None = None` — identical keyword-only signature in `_expected_max_sharpe_under_null` (T2), `_deflated_sharpe_ratio` (T2), `compute_dsr_for_verdict` (T5), the 3 widened `_spy` stubs (T7), and the T6 `_cap` capture wrappers.
- `holdout_sharpe_per_period: float = 0.0` — defined T4 (`SliceMetrics`), set in `compute_slice_metrics_from_trades` (T4), read at the verdict site (T6: `t.holdout.holdout_sharpe_per_period`) and in T6's T-UNITS-COHERENT assertion — identical spelling everywhere (NOT `holdout_per_period_sharpe` / `sharpe_per_period` as a field; the local var in `compute_slice_metrics_from_trades` is `sharpe_per_period` and is assigned INTO the `holdout_sharpe_per_period=` field — no field-name drift).
- WARNING event string `"tpcore.overfitting.dsr.null_variance_approximation"` — byte-identical in both impls (T2, T5) and all three asserting tests (T2 T-FALLBACK-WARNS, T5 T-VERDICT-FALLBACK-WARNS).
- The `scripts/ops.py`↔`ops` collision-eviction stanza in `test_lab_dsr_delivered.py` (T5) is byte-identical to the verified `test_lab_ntrials_ledger.py:24-26` precedent; Task 7 explicitly does NOT broaden the existing module-load stanza (task #148 not worsened).
- `_norm_inv` (Acklam, `ops/lab/run.py`) vs `norm.ppf` (scipy, `overfitting.py`) are NOT unified (H-A2-14): T-WORKED pins the `overfitting.py` impl, T-VERDICT-FALLBACK-WARNS/T-DELIVERED pin the `compute_dsr_for_verdict` impl — per-impl tolerance, never cross-impl equality.

Fixes applied inline during review: none required on the second pass — no inconsistency found.

### Spec-gap flag

No amended § (§3.1/§3.4/§4/§5/§6/§8) and no H-A2-1..15 is left without a task + pinning test (table above). One **scope note (not a gap)**: §4's `compute_search_metrics` / 5-engine `OverfittingDiagnostic` rows are *honestly fallback-only* — the plan does NOT add a task to thread a matrix into `compute_search_metrics` (the spec explicitly states no production code passes a non-None `trial_returns_matrix`; fabricating a vector is the §9/H-A2-3 non-goal). Those sites are covered by T-SIG-COMPAT (they still compile + run on the warned fallback) — the correct, honest disposition, called out here rather than silently. The §3.3 `OverfittingDiagnostic` seam is built + tested (T-CROSSTRIAL) as the correct future-caller path even though no current production caller exercises it — that is the spec's explicit intent (the seam is correct + useful for SP-B/C), not gold-plating.
