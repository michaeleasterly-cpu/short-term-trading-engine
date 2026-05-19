"""SP-A2 — DSR null-variance fix: Lab-verdict-path delivery proofs.

Collected path (``tpcore/tests`` is in pyproject ``testpaths``). The
``scripts/ops.py`` vs ``ops/`` package collision is acute once a test
imports ``ops.lab.run``.

DEVIATION (test-isolation, empirically proven in SP-A2 T4): we do NOT use
the module-load ``del sys.modules`` eviction stanza the plan's literal
Step-1 code shows. Mirror ``tpcore/tests/test_lab_no_gate_poison.py:25``:
a plain in-body ``import ops.lab.run`` with NO eviction guard (green in
the full single-process suite). The literal eviction stanza, run in the
full suite, EVICTS the ``scripts/ops.py``↔``ops/`` shadow the already-
collected SP2-oracle ``sp`` monkeypatch binds to → silently breaks 2
SP2-oracle tests. The guard is the perturbation, not the import. The
plan's intent + every assertion below are kept byte-identical.
"""
from __future__ import annotations

import math
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
import structlog

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_oracle_base_ref() -> str:
    """Resolve the byte-frozen base ref for the SP2-oracle diff. Prefer
    ``origin/main`` (the ref CI's `actions/checkout` PR clone actually
    has — a bare local ``main`` does NOT exist there, only the PR HEAD +
    remote-tracking refs). Fall back to a local ``main`` for a plain dev
    clone. ``pytest.skip`` (NOT a confusing ``CalledProcessError`` exit
    128) if neither base ref resolves in this checkout (shallow / offline
    / unfetched remote-tracking ref) — the byte-frozen gate must never
    hard-error on a checkout that lacks the base ref entirely.

    DEVIATION (mirror-not-reuse, precedent cited): this is a local mirror
    of the canonical resolver ``_resolve_sp3_base`` in
    ``scripts/tests/test_sp3_scope_confined.py`` (same ref-tuple, same
    ``git rev-parse --verify --quiet`` mechanism, same skip-not-fail
    policy). It is mirrored — NOT imported — because ``tpcore/`` must not
    take a ``scripts/`` (cross-lane / test-to-test) dependency; the
    minimal shape is reproduced here per the SP3 precedent. Unlike SP3
    this returns the ref NAME (not a fork-point merge-base): the
    byte-frozen contract compares the oracle against the integration
    ref's tree directly (the original ``git diff origin/main``
    semantics), so ANY oracle modification still bites loudly."""
    for ref in ("origin/main", "main"):
        rev = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        if rev.returncode == 0:
            return ref
    pytest.skip(
        "origin/main (and fallback main) unresolvable — cannot assert "
        "SP2-oracle byte-frozen in this checkout"
    )


def test_sp_a2_t_verdict_fallback_warns_and_byte_identical() -> None:
    """T-VERDICT-FALLBACK-WARNS. Direct two-arg call (no
    trial_sharpe_variance) is byte-identical to pre-SP-A2 AND emits the
    single documented WARNING. Per-impl ε (H-A2-14: this is the
    compute_dsr_for_verdict / Acklam _norm_inv impl)."""
    import ops.lab.run as lab_run
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
    e_max = ((1.0 - EULER) * lab_run._norm_inv(1.0 - 1.0 / 37)
             + EULER * lab_run._norm_inv(1.0 - 1.0 / (37 * math.e)))
    denom = math.sqrt(
        max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr ** 2), 1e-12)
        / max(n - 1, 1)
    )
    z = (sr - e_max) / denom
    legacy = float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    with structlog.testing.capture_logs() as logs:
        got = lab_run.compute_dsr_for_verdict(returns, n_trials=37)
    assert abs(got - legacy) < 1e-12
    assert any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        and e.get("log_level") == "warning"
        for e in logs
    )


def test_sp_a2_t_verdict_v_below_floor_clamps_equal_and_no_warn() -> None:
    """FLOOR-CLAMP/EQUAL case. With trial_sharpe_variance STRICTLY BELOW
    the floor 1/(n-1) the H-A2-10 clamp fires ⇒ sr_variance == floor ⇒
    √(sr_variance/floor) == 1.0 ⇒ d_v is bit-EQUAL to the fallback
    (tightening-OR-equal, the floor lower-bound). Supplying V is still
    silent (no spurious fallback WARNING). This pins the clamp path; the
    strict-tightening (V>floor) bite is the SEPARATE test below."""
    import ops.lab.run as lab_run
    rng = np.random.default_rng(1)
    returns = [float(x) for x in rng.normal(0.02, 0.01, 40)]
    n = len(returns)
    floor = 1.0 / (n - 1)  # ≈ 0.025641 for n=40
    v_below = 0.01
    assert v_below < floor  # clamp path, NOT real-V
    d_fb = lab_run.compute_dsr_for_verdict(returns, n_trials=50)
    with structlog.testing.capture_logs() as logs:
        d_v = lab_run.compute_dsr_for_verdict(
            returns, n_trials=50, trial_sharpe_variance=v_below)
    # Clamped to the floor ⇒ EXACTLY equal (bit-equal, the OR-equal arm).
    assert d_v == d_fb
    assert not any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        for e in logs
    )


def test_sp_a2_t_verdict_v_above_floor_strictly_tightens_and_no_warn() -> None:
    """STRICT-TIGHTENING case (the verdict-path analog of T2's
    T-STRICTER — it must genuinely bite). With trial_sharpe_variance
    STRICTLY GREATER than the floor 1/(n-1), √(sr_variance/floor) > 1.0
    ⇒ e_max > bracket ⇒ a REAL DSR reduction: d_v STRICTLY < d_fb (NOT
    merely ≤). This exercises the real-V path, NOT the floor clamp.
    Supplying V is still silent (no spurious fallback WARNING)."""
    import ops.lab.run as lab_run
    rng = np.random.default_rng(1)
    returns = [float(x) for x in rng.normal(0.02, 0.01, 40)]
    n = len(returns)
    floor = 1.0 / (n - 1)  # ≈ 0.025641 for n=40
    v_above = 0.10
    # Real-V path (NOT the clamp): V strictly above the floor ⇒
    # √(V/floor) > 1 ⇒ a strictly higher e_max ⇒ strictly lower DSR.
    assert v_above > floor
    assert math.sqrt(v_above / floor) > 1.0
    d_fb = lab_run.compute_dsr_for_verdict(returns, n_trials=50)
    with structlog.testing.capture_logs() as logs:
        d_v = lab_run.compute_dsr_for_verdict(
            returns, n_trials=50, trial_sharpe_variance=v_above)
    # STRICT: a genuine DSR reduction on the real-V path (not bit-equal).
    assert d_v < d_fb
    assert not any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        for e in logs
    )


# ── SP-A2 T6: the ONE production site that delivers the tightening ──────────


def _install_dispersed_harness(monkeypatch, lab_run, *, per_trial_returns,
                                held_returns, cred_score=80):
    """Offline harness with a DISPERSED trial set (the SP-A harness
    produces a single repeated config ⇒ V≈0 ⇒ can't prove tightening).
    Each evaluated candidate gets a distinct return series so the trials
    list carries a real cross-trial per-period Sharpe dispersion; the
    final held-back winner replay uses `held_returns`.

    DEVIATION (plan-vs-real, intent + every assertion preserved): the
    plan's literal harness dates ALL trades from 2022-01-03 and used a
    single ``_mk``. But the real ``_run_lab_core`` (verified) slices
    per-trial outcomes to the WALK-FORWARD holdout window
    ([2020-12-31, 2021-12-30] for the plan's _ns span) and the held-back
    winner replay to the FINAL-holdout window ([2022-01-01, 2022-12-31]).
    Dating per-trial trades in 2022 ⇒ EMPTY holdout_trades for every
    trial ⇒ all holdout_sharpe_per_period == 0.0 ⇒ ZERO cross-trial
    dispersion ⇒ V below floor ⇒ the plan's own stated intent (">= MIN_
    TRIALS_FOR_V dispersed trials" with "real cross-trial per-period
    Sharpe dispersion", T-DELIVERED docstring) is unrealizable. Aligned
    to the real code: per-trial trades land in the walk-forward holdout
    window, the held-back replay in the final-holdout window. The plan's
    intent + every T-DELIVERED / T-UNITS-COHERENT assertion are byte-
    identical. (See also the matching per_window_trials>=MIN_TRIALS_FOR_V
    fix in _ns: the literal per_window_trials=4 < MIN_TRIALS_FOR_V=5
    would force the fallback, also defeating the plan's stated intent.)
    """
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

    def _mk(returns, base):
        class _RR:
            credibility_score = cred_score
            credibility_rubric = _rubric
            trade_log = [
                _Trade(base + timedelta(days=k), r)
                for k, r in enumerate(returns)
            ]
        return _RR()

    def _ctx_runner(context, *, overrides=None):
        rs = per_trial_returns[seq["i"] % len(per_trial_returns)]
        seq["i"] += 1
        # per-trial trades INSIDE the walk-forward holdout window
        # ([2020-12-31, 2021-12-30] for the plan's _ns span).
        return _mk(rs, _date(2021, 1, 4))

    async def _ctx_loader(*a, **k):
        return object()

    async def _runner(*a, **k):
        # the held-back winner replay — INSIDE the final-holdout window
        # ([2022-01-01, 2022-12-31] for the plan's _ns span).
        return _mk(held_returns, _date(2022, 1, 3))

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
    # DEVIATION (plan-vs-real): plan's literal per_window_trials=4 is
    # < MIN_TRIALS_FOR_V (=5) ⇒ the trials list never reaches the H-A2-10
    # threshold ⇒ V is always None ⇒ the fallback fires ⇒ "STRICTLY
    # tightened" (T-DELIVERED) is unprovable. Aligned to == trials so the
    # single walk-forward window yields >= MIN_TRIALS_FOR_V non-errored
    # dispersed trials. Plan intent + every assertion byte-identical.
    return argparse.Namespace(
        engine="reversion", trials=trials, per_window_trials=trials,
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
    plumbing.

    SELF-DISCRIMINATING (SP-A2 T6 review #1): the per-trial means are
    NARROWED so the cross-trial V lands in the CONTINUOUS DSR band
    (recomputed against the real walk-forward-sliced harness: means
    0.018,0.019,…,0.025 / σ=0.012 / n=40 / seed=11 ⇒ V≈0.075370,
    floor 1/(n-1)≈0.025641 so this is the real-V path NOT the clamp,
    DSR_with_v≈0.001820, DSR_fallback≈0.652677). Because DSR_with_v is
    NON-SATURATED (strictly > 0), this single test now catches BOTH
    failure modes: (a) inert plumbing (V never threaded / no tightening
    — the `seen["v"] is not None` + strict-`<` assertions) AND (b) the
    H-A2-11 annualized-units regression (a swap to the annualized
    `.sharpe` inflates V≫0.1 ⇒ DSR→0.0 ⇒ the magnitude-sensitive
    `dsr_with_v > 1e-9` assertion FAILS). It no longer delegates
    H-A2-11 detection solely to the sibling T-UNITS-COHERENT."""
    import ops.lab.run as lab_run
    # DEVIATION (plan-vs-real): the plan's literal T6 body also did
    # `from tpcore.lab.context import LabContext`, but the legacy
    # candidate=None path uses NO LabContext — it is dead (ruff F401).
    # Removed; the verdict-path behaviour + every assertion unchanged.
    rng = np.random.default_rng(11)
    # 8 distinct candidate return series (real cross-trial dispersion).
    # NARROW means (review #1): V lands in the continuous DSR band
    # (≈0.075, strictly above the 1/(n-1) floor ⇒ real-V path, and
    # DSR_with_v strictly in (1e-9, DSR_fallback)) so the non-saturation
    # assertion below is magnitude-sensitive to the annualized bug.
    per_trial = [
        [float(x) for x in rng.normal(m, 0.012, 40)]
        for m in (0.018, 0.019, 0.020, 0.021, 0.022, 0.023, 0.024, 0.025)
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
    # DEVIATION (plan-vs-real, intent + assertions preserved): the plan's
    # literal harness only fakes ``tpcore.db.build_asyncpg_pool``, but the
    # legacy candidate=None credibility-persist path deliberately opens its
    # OWN ad-hoc ``asyncpg.create_pool`` (run.py:951, the H-S3-8 byte-
    # identical-legacy invariant — NOT routed through build_asyncpg_pool).
    # Mirror the canonical legacy-path fake from
    # test_lab_ntrials_ledger.py::test_legacy_non_lab_path_emits_and_reads
    # _no_ledger so no socket is opened; the build_asyncpg_pool patch +
    # every plan assertion below stay byte-identical.
    import asyncpg

    class _AdHoc:
        async def close(self) -> None: ...

    async def _fake_create_pool(*a, **k):
        return _AdHoc()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool,
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
    # NON-SATURATED / magnitude-sensitive (review #1): V is in the
    # continuous band so DSR_with_v is strictly positive. The H-A2-11
    # annualized-units regression inflates V≫0.1 ⇒ DSR collapses to 0.0
    # ⇒ this assertion FAILS — T-DELIVERED is now self-discriminating.
    assert dsr_with_v > 1e-9                            # NON-saturated


async def test_sp_a2_t_units_coherent_v_uses_per_period_not_annualized(
        monkeypatch, tmp_path) -> None:
    """T-UNITS-COHERENT (MAKE-OR-BREAK, H-A2-11). The V fed at the verdict
    site is np.var of the NON-annualized holdout_sharpe_per_period. A
    fixture whose annualized sharpe differs from per-period by a known
    √ppy: using the annualized field would inflate SR₀ past a tripwire
    (DSR≈0); the per-period field keeps it sane."""
    import ops.lab.run as lab_run
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
    # DEVIATION (see test_sp_a2_t_delivered_... above): legacy
    # candidate=None persists via an ad-hoc asyncpg.create_pool — fake it
    # so no socket; plan intent + every assertion below unchanged.
    import asyncpg

    class _AdHoc:
        async def close(self) -> None: ...

    async def _fake_create_pool(*a, **k):
        return _AdHoc()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool,
                        raising=True)

    core = await lab_run._run_lab_core(
        _ns(tmp_path / "u.csv", trials=8, seed=2), candidate=None)
    assert not isinstance(core, int)
    v = captured["v"]
    assert v is not None
    # DEVIATION (plan-vs-real, numeric constant only — intent + non-
    # vacuity preserved): the plan's literal `< 0.5` mis-estimated the
    # per-period magnitude for the REAL walk-forward-sliced series. With
    # these 8 dispersed fixtures the verified numbers are: per-period V
    # ≈ 0.5996, annualized V ≈ 24.07 (the ANNUALIZED dispersion is
    # ≈ppy²/-ish larger — ppy≈40 here; the inflation factor that H-A2-11
    # warns destroys the gate). The discriminating band is wide open
    # between them; `< 1.0` passes the real per-period V and STILL FAILS
    # non-vacuously if a maintainer swaps to the annualized `.sharpe`
    # (24.07 ≫ 1.0). The plan's intent ("V is in the per-period band,
    # NOT the annualized band") + the non-vacuity are byte-identical.
    assert v < 1.0, ("V looks annualized (units bug regressed): "
                      f"{v}")
    # And the realized verdict DSR is finite/sane (not the DSR≈0-always
    # collapse the annualized bug causes).
    assert 0.0 <= core.dsr <= 1.0


def test_sp_a2_t_oracle_byte_unmodified_and_green() -> None:
    """T-ORACLE (MAKE-OR-BREAK, §5/H-A2-2). The SP2 characterization
    oracle stays BYTE-UNMODIFIED (empty diff vs origin/main) AND green by
    property. Distinct from the editable SP-A ledger tests (Task 7,
    H-A2-12) — conflating them was the original spec defect.

    DEVIATION (house style / plan Step 5): ``import subprocess`` lives in
    the module-top stdlib block (isort I001), NOT in the test body as the
    plan's literal shows; ``sys``/``REPO_ROOT`` are likewise module-level
    (the established ``Path(__file__).resolve().parents[2]`` repo pattern).
    The ``git diff`` invocation is the plan's exact literal and is
    READ-ONLY (no mutation of the working repo). Plan intent + both
    assertions byte-identical.

    REVIEW NOTE (SP-A2 T8, Minor — robustness): the base ref is now
    resolved via ``_resolve_oracle_base_ref`` (the mirrored canonical
    SP3 resolver) and the diff runs with ``check=False``: an unresolvable
    ``origin/main``/``main`` ⇒ a clean ``pytest.skip`` instead of a
    confusing ``CalledProcessError`` (exit 128). When the base IS
    resolvable (the normal local + GitHub-Actions ``actions/checkout``
    case) the behavior is IDENTICAL: a non-empty diff (oracle modified)
    still fails this assertion loudly — the byte-frozen contract is
    UNWEAKENED."""
    base = _resolve_oracle_base_ref()
    diff = subprocess.run(
        ["git", "diff", base, "--",
         "scripts/tests/test_search_parameters_characterization.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
    )
    assert diff.returncode == 0 and diff.stdout.strip() == "", (
        "SP2 oracle was modified — §5 invariant violated "
        f"(git diff {base} rc={diff.returncode}):\n"
        + diff.stdout + diff.stderr
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
