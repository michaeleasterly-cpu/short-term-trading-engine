"""SP-B — _lab_target_for resolver + lazy PARAM_RANGES Mapping + the
BINDING ValueError→KeyError re-raise contract (spec §2.4, the §8
highest residual risk) + the default_params shim + no-import-cycle.

Char-before-refactor: the *_for callables return the engine's
run_for_search/load_*/run_* symbols for the declared three and raise for
an unknown engine; PARAM_RANGES supports in/get/iteration-order/len/set.
These pins hold pre- AND post-refactor (the refactor is provably
behaviour-preserving on the declared three).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) so ``import ops.lab.run``
# resolves the real ops/ package (the ops-shadow single-process rule).
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

pytestmark = pytest.mark.xdist_group("ops_shadow")

# T0 byte-parity baseline: PARAM_RANGES keysets captured on the un-refactored
# tree (Task 0). Inlined deliberately — a 3-key snapshot of a fully-known
# constant needs no file artifact (YAGNI), and a runtime fixture must never
# live under the docs/ plans tree (#252 docs-to-reality).
_T0_PARAM_RANGES_KEYSETS: dict[str, list[str]] = {
    "reversion": ["max_hold_days", "stop_pct", "volume_climax_multiplier", "z_threshold"],
    "vector": ["de_ceiling", "earnings_window_days", "pb_ceiling", "stop_pct", "swing_score_threshold"],
    "momentum": ["hold_days", "lookback_days", "skip_days", "top_decile_pct"],
}


# ── CHARACTERIZATION pins (true pre- AND post-refactor) ──────────────────

def test_seam_funcs_return_declared_engine_symbols():
    import importlib

    import ops.lab.run as run
    for engine in ("reversion", "vector", "momentum"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert run._runner_for(engine) is mod.run_for_search
        assert callable(run._context_loader_for(engine))
        assert callable(run._context_runner_for(engine))


def test_seam_funcs_raise_valueerror_on_unknown_engine():
    import ops.lab.run as run
    for fn in (run._runner_for, run._context_loader_for,
               run._context_runner_for):
        with pytest.raises(ValueError):
            fn("nope")


def test_param_ranges_membership_iteration_len_and_set():
    import ops.lab.run as run
    # SP-E: sentinel gained a declared LAB_TARGET, so it is now a
    # declared PARAM_RANGES member (after the T0 trio, dispatch_order).
    # carver (the first LAB engine ever planner-ADDed; dispatch_order=6)
    # ships its LAB_TARGET from day-zero so SP-B's roster-driven resolver
    # picks it up the moment _PROFILE includes it.
    assert list(run.PARAM_RANGES) == [
        "reversion", "vector", "momentum", "sentinel", "carver", "catalyst"]
    assert "reversion" in run.PARAM_RANGES
    assert "sentinel" in run.PARAM_RANGES         # SP-E: now declared
    assert "carver" in run.PARAM_RANGES            # LAB target, day-zero
    assert "catalyst" in run.PARAM_RANGES          # PAPER 2026-05-20 (H-S3-12)
    assert len(run.PARAM_RANGES) == 6
    # reversion PCA-residual Lab candidate (spec
    # docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-candidate.md
    # §4.3): EXACTLY ONE new key added to reversion — the signal_mode
    # choice toggle. Same pattern as vector_composite + momentum.
    assert set(run.PARAM_RANGES["reversion"]) == (
        set(_T0_PARAM_RANGES_KEYSETS["reversion"]) | {"signal_mode"})
    # vector_composite Lab candidate (spec
    # docs/superpowers/specs/2026-05-20-vector-composite-lab-candidate.md
    # §4.1, H-VC-2): EXACTLY ONE new key added to vector — the
    # composite_mode choice toggle. The T0 keyset + that ONE key is the
    # post-candidate complete set. No hidden grid (the spec's binding
    # n_trials discipline). If a future Lab candidate adds another key,
    # update this assertion in the SAME PR that lands the candidate.
    assert set(run.PARAM_RANGES["vector"]) == (
        set(_T0_PARAM_RANGES_KEYSETS["vector"]) | {"composite_mode"})
    # momentum vol-managed Lab candidate (spec
    # docs/superpowers/specs/2026-05-20-momentum-vol-managed-lab-candidate.md
    # §4.1, H-MVM-2): EXACTLY ONE new key added to momentum — the
    # vol_managed_mode choice toggle. Same pattern as vector_composite.
    assert set(run.PARAM_RANGES["momentum"]) == (
        set(_T0_PARAM_RANGES_KEYSETS["momentum"]) | {"vol_managed_mode"})
    # SP-E: sentinel's pre-registered toggles. Sibling candidates:
    #   * `activation_score_threshold` — sentinel_maxdd (spec
    #     docs/superpowers/specs/2026-05-20-sentinel-maxdd-lab-candidate.md)
    #   * `bear_score_mode` — sentinel_bear_score (spec
    #     docs/superpowers/specs/2026-05-21-sentinel-bear-score-lab-candidate.md)
    assert set(run.PARAM_RANGES["sentinel"]) == {
        "activation_score_threshold", "bear_score_mode"}
    # carver's six pre-registered toggles (spec §6 PARAM_RANGES).
    assert set(run.PARAM_RANGES["carver"]) == {
        "trend_fast", "trend_slow", "value_lookback_months",
        "meanrev_window", "annualized_vol_target", "idm_cap"}


# ── BINDING CONTRACT pins (spec §2.4 — the §8 highest residual risk) ─────

def test_param_ranges_subscript_undeclared_raises_KEYERROR_not_valueerror(
        monkeypatch):
    """planner.py:694 does PARAM_RANGES.get(ecr.engine, {}). Mapping.get
    catches KeyError ONLY. The lazy __getitem__ MUST re-raise the
    _lab_target_for ValueError as KeyError or that live-adjacent
    MODIFY-ECR validator crashes (spec §2.4, §8-A2).

    SP-E: sentinel is now declared, so the *eligible-but-undeclared*
    branch is exercised via a synthetic PAPER engine (the SP-B clockwork
    pattern) — a package-less roster member with no LAB_TARGET."""
    import ops.lab.run as run
    import tpcore.engine_profile as ep

    fake = ep.EngineProfile(
        engine="phantompaper", cadence=ep.Cadence.DAILY,
        dispatch_order=7, lifecycle_state=ep.LifecycleState.PAPER)
    monkeypatch.setattr(ep, "_PROFILE", {**ep._PROFILE, "phantompaper": fake})

    with pytest.raises(KeyError):
        run.PARAM_RANGES["phantompaper"]        # eligible-but-undeclared
    # NOT a ValueError leaking through:
    try:
        run.PARAM_RANGES["phantompaper"]
    except KeyError:
        pass
    except ValueError as exc:  # pragma: no cover - regression tripwire
        pytest.fail(f"ValueError leaked (planner.py:694 would crash): {exc}")


def test_param_ranges_get_returns_default_for_undeclared_engine(monkeypatch):
    """The exact planner.py:694 call: .get(<undeclared>, {}) == {}.

    SP-E: sentinel is declared now; the eligible-but-undeclared default
    is exercised via the synthetic PAPER engine. Non-targetable engines
    (canary/sigma/nope) still return the default via the other branch."""
    import ops.lab.run as run
    import tpcore.engine_profile as ep

    fake = ep.EngineProfile(
        engine="phantompaper", cadence=ep.Cadence.DAILY,
        dispatch_order=7, lifecycle_state=ep.LifecycleState.PAPER)
    monkeypatch.setattr(ep, "_PROFILE", {**ep._PROFILE, "phantompaper": fake})
    assert run.PARAM_RANGES.get("phantompaper", {}) == {}
    assert run.PARAM_RANGES.get("canary", {}) == {}
    assert run.PARAM_RANGES.get("sigma", {}) == {}
    assert run.PARAM_RANGES.get("nope", {}) == {}


def test_lab_target_for_resolves_declared_engines():
    import importlib

    from ops.lab.run import _lab_target_for
    # SP-E: sentinel joined the declared set (its LAB_TARGET resolves
    # exactly like the others through the SP-B roster resolver).
    for engine in ("reversion", "vector", "momentum", "sentinel"):
        mod = importlib.import_module(f"{engine}.backtest")
        t = _lab_target_for(engine)
        assert t.run_for_search is mod.run_for_search
        assert t.default_params is mod.default_params


def test_lab_target_for_rejects_non_targetable_with_clear_valueerror():
    from ops.lab.run import _lab_target_for
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(ValueError, match="not Lab-targetable"):
            _lab_target_for(bad)


def test_lab_target_for_resolves_sentinel_post_sp_e():
    """SP-E deliverable: sentinel is PAPER AND now declares a LAB_TARGET
    (the activation-threshold toggle, primary_metric=MAXDD_REDUCTION) so
    the resolver RESOLVES it (it no longer hard-rejects with the
    SP-E-pointing message — SP-E IS that resolution)."""
    import sentinel.backtest as sb
    from ops.lab.run import _lab_target_for
    from tpcore.lab.target import LabPrimaryMetric

    t = _lab_target_for("sentinel")
    assert t.run_for_search is sb.run_for_search
    assert t.default_params is sb.default_params
    assert t.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION


def test_lab_target_for_rejects_eligible_but_undeclared_synthetic(
        monkeypatch):
    """The eligible-but-undeclared branch (post-SP-E, every real
    targetable engine is declared, so this is exercised via a synthetic
    package-less PAPER roster member): the clear SP-E/SP-F-pointing
    message, NOT the 'not Lab-targetable' roster-gate rejection and NOT a
    raw KeyError/'unknown engine' (spec §4.1)."""
    import tpcore.engine_profile as ep
    from ops.lab.run import _lab_target_for

    fake = ep.EngineProfile(
        engine="phantompaper", cadence=ep.Cadence.DAILY,
        dispatch_order=7, lifecycle_state=ep.LifecycleState.PAPER)
    monkeypatch.setattr(ep, "_PROFILE", {**ep._PROFILE, "phantompaper": fake})
    with pytest.raises(ValueError) as ei:
        _lab_target_for("phantompaper")
    msg = str(ei.value)
    # Propagated THROUGH the roster gate (not rejected as non-targetable).
    assert "not Lab-targetable" not in msg
    assert "phantompaper" in msg


# ── HARDENING: declared engine whose <engine>.backtest fails to import ───
# (spec §2.3 / Edge case 7 / §2.4 / §8-A2 / §8-HARDEN-T4) — the resolver's
# import-failure catch is now (ImportError, SyntaxError), not just
# ModuleNotFoundError. This is the ONLY fence on the post-SP-B
# planner.py:693 lazy-import path (`PARAM_RANGES.get(ecr.engine, {})` on
# the live-adjacent MODIFY-ECR validator): a non-ModuleNotFoundError
# ImportError or a SyntaxError anywhere in a declared engine's transitive
# backtest import surface MUST surface as KeyError off the Mapping, never
# the raw import error, or that validator crashes.


@pytest.mark.parametrize(
    ("exc_factory", "label"),
    [
        (lambda: ImportError("cannot import name 'missing_y' from 'x'"), "ImportError"),
        (lambda: SyntaxError("invalid syntax"), "SyntaxError"),
    ],
)
def test_declared_engine_import_failure_is_keyerror_not_raw(
    monkeypatch, exc_factory, label
):
    """A declared+targetable engine (reversion) whose ``reversion.backtest``
    import raises a non-ModuleNotFoundError ``ImportError`` OR a
    ``SyntaxError`` must: (a) ``PARAM_RANGES['reversion']`` → ``KeyError``
    (NOT ImportError/SyntaxError/ValueError), (b) ``PARAM_RANGES.get(...,
    {}) == {}`` (the exact planner.py:693 live-adjacent call), and (c) a
    direct ``_lab_target_for`` raise the clear ``ValueError`` naming the
    engine + that its backtest failed to import/parse. Pins the
    planner-path no-crash guarantee for the broadened catch."""
    import importlib

    import ops.lab.run as run

    real_import = importlib.import_module

    def _fake_import(name, *a, **kw):
        if name == "reversion.backtest":
            raise exc_factory()
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _fake_import)

    # (a) subscript → KeyError, NOT the raw import error or a ValueError.
    with pytest.raises(KeyError):
        run.PARAM_RANGES["reversion"]
    try:
        run.PARAM_RANGES["reversion"]
    except KeyError:
        pass
    except (ImportError, SyntaxError, ValueError) as exc:  # pragma: no cover
        pytest.fail(
            f"{label} leaked as {type(exc).__name__} "
            f"(planner.py:693 .get() would crash): {exc}"
        )

    # (b) the exact planner.py:693 call cleanly returns the default.
    assert run.PARAM_RANGES.get("reversion", {}) == {}

    # (c) the direct resolver raises the clear, actionable ValueError
    #     (names the engine + that its backtest failed to import/parse).
    with pytest.raises(
        ValueError, match=r"reversion.*backtest.*failed to import/parse"
    ):
        run._lab_target_for("reversion")


@pytest.mark.parametrize(
    "bad_target",
    [object(), 42, "not-a-labtarget", {"param_ranges": {}}],
    ids=["object", "int", "str", "dict"],
)
def test_declared_engine_malformed_lab_target_is_keyerror_not_attributeerror(
    monkeypatch, bad_target
):
    """A declared+targetable engine (reversion) whose ``reversion.backtest``
    imports fine but exposes a ``LAB_TARGET`` that is NOT a ``LabTarget``
    instance must: (a) ``_lab_target_for('reversion')`` raise the clear
    ``ValueError`` (names the engine + that LAB_TARGET is not a LabTarget),
    (b) ``PARAM_RANGES['reversion']`` → ``KeyError`` (NOT the
    ``AttributeError`` that ``.param_ranges`` on a non-LabTarget would
    raise — that class is exactly the unhandled leak onto the
    live-adjacent planner.py:693 ``PARAM_RANGES.get(ecr.engine, {})``
    MODIFY-ECR validator), and (c) the exact planner.py:693 call
    ``.get('reversion', {}) == {}``. The resolver — not an out-of-band
    CI isinstance test — is the ONLY fence (spec §2.3 / EC / §2.4 /
    §8-HARDEN-T4)."""
    import importlib
    import types

    import ops.lab.run as run

    real_import = importlib.import_module

    def _fake_import(name, *a, **kw):
        if name == "reversion.backtest":
            fake = types.ModuleType("reversion.backtest")
            fake.LAB_TARGET = bad_target  # present, non-None, NOT a LabTarget
            return fake
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _fake_import)

    # (a) the direct resolver raises the clear, actionable ValueError.
    with pytest.raises(
        ValueError, match=r"reversion.*LAB_TARGET.*not a LabTarget"
    ):
        run._lab_target_for("reversion")

    # (b) subscript → KeyError, NOT AttributeError (the leak class) /
    #     ValueError / anything else.
    with pytest.raises(KeyError):
        run.PARAM_RANGES["reversion"]
    try:
        run.PARAM_RANGES["reversion"]
    except KeyError:
        pass
    except (AttributeError, ValueError) as exc:  # pragma: no cover
        pytest.fail(
            f"malformed LAB_TARGET leaked as {type(exc).__name__} "
            f"(planner.py:693 .get() would crash): {exc}"
        )

    # (c) the exact planner.py:693 live-adjacent call cleanly returns {}.
    assert run.PARAM_RANGES.get("reversion", {}) == {}


def test_sample_parameters_clear_error_on_bad_engine():
    import ops.lab.run as run
    with pytest.raises(ValueError, match="not Lab-targetable"):
        run.sample_parameters("canary", 4, seed=0)
    # Declared engine still samples deterministically (no behaviour drift).
    # Reversion's keyset includes the new ``signal_mode`` toggle landed
    # by the PCA-residual Lab candidate; the post-candidate complete
    # set is T0 keys + {"signal_mode"} (spec §4.3).
    a = run.sample_parameters("reversion", 8, seed=7)
    b = run.sample_parameters("reversion", 8, seed=7)
    assert a == b and set(a[0]) == (
        set(_T0_PARAM_RANGES_KEYSETS["reversion"]) | {"signal_mode"}
    )


# ── default_params shim (the sixth surface, spec §0.1 / §2.3) ────────────

def test_default_params_shim_byte_equal_for_declared_engines():
    import importlib

    from ops.engine_sdlc.default_params import default_params
    # SP-E: sentinel joined the declared set; the shim is byte-equal to
    # its backtest.default_params() exactly like the other engines.
    for engine in ("reversion", "vector", "momentum", "sentinel"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert default_params(engine) == mod.default_params()


def test_default_params_shim_resolves_sentinel_post_sp_e():
    """SP-E: sentinel declares a LAB_TARGET, so the shim resolves it to
    its pre-registered toggles' legacy defaults (the dossier param-diff
    seam). The sentinel_bear_score candidate (spec
    docs/superpowers/specs/2026-05-21-sentinel-bear-score-lab-candidate.md)
    added the `bear_score_mode` toggle alongside the SP-E
    `activation_score_threshold`."""
    from ops.engine_sdlc.default_params import default_params
    assert default_params("sentinel") == {
        "activation_score_threshold": 60,
        "bear_score_mode": "current",
    }


def test_no_import_cycle_default_params_shim_to_run():
    """ops.engine_sdlc.default_params → ops.lab.run is ops→ops, lazy,
    legal, no cycle. Import each first, then exercise."""
    import importlib

    m1 = importlib.import_module("ops.engine_sdlc.default_params")
    m2 = importlib.import_module("ops.lab.run")
    assert m1.default_params("momentum") == \
        importlib.import_module("momentum.backtest").default_params()
    assert callable(m2._lab_target_for)
