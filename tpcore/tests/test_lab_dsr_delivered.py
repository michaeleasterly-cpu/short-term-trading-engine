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

import numpy as np
import structlog


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
