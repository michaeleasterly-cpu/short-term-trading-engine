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
    "vector": ["catalyst_window_days", "de_ceiling", "pb_ceiling", "stop_pct", "swing_score_threshold"],
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
    # Membership + iteration order == the T0 literal insertion order.
    assert list(run.PARAM_RANGES) == ["reversion", "vector", "momentum"]
    assert "reversion" in run.PARAM_RANGES
    assert "sentinel" not in run.PARAM_RANGES
    assert len(run.PARAM_RANGES) == 3
    for e in ("reversion", "vector", "momentum"):
        assert set(run.PARAM_RANGES[e]) == set(_T0_PARAM_RANGES_KEYSETS[e])


# ── BINDING CONTRACT pins (spec §2.4 — the §8 highest residual risk) ─────

def test_param_ranges_subscript_undeclared_raises_KEYERROR_not_valueerror():
    """planner.py:694 does PARAM_RANGES.get(ecr.engine, {}). Mapping.get
    catches KeyError ONLY. The lazy __getitem__ MUST re-raise the
    _lab_target_for ValueError as KeyError or that live-adjacent
    MODIFY-ECR validator crashes (spec §2.4, §8-A2)."""
    import ops.lab.run as run
    with pytest.raises(KeyError):
        run.PARAM_RANGES["sentinel"]            # eligible-but-undeclared
    # NOT a ValueError leaking through:
    try:
        run.PARAM_RANGES["sentinel"]
    except KeyError:
        pass
    except ValueError as exc:  # pragma: no cover - regression tripwire
        pytest.fail(f"ValueError leaked (planner.py:694 would crash): {exc}")


def test_param_ranges_get_returns_default_for_undeclared_engine():
    """The exact planner.py:694 call: .get(<undeclared>, {}) == {}."""
    import ops.lab.run as run
    assert run.PARAM_RANGES.get("sentinel", {}) == {}
    assert run.PARAM_RANGES.get("canary", {}) == {}
    assert run.PARAM_RANGES.get("sigma", {}) == {}
    assert run.PARAM_RANGES.get("nope", {}) == {}


def test_lab_target_for_resolves_declared_engines():
    import importlib

    from ops.lab.run import _lab_target_for
    for engine in ("reversion", "vector", "momentum"):
        mod = importlib.import_module(f"{engine}.backtest")
        t = _lab_target_for(engine)
        assert t.run_for_search is mod.run_for_search
        assert t.default_params is mod.default_params


def test_lab_target_for_rejects_non_targetable_with_clear_valueerror():
    from ops.lab.run import _lab_target_for
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(ValueError, match="not Lab-targetable"):
            _lab_target_for(bad)


def test_lab_target_for_rejects_eligible_but_undeclared_sentinel():
    """Sentinel is PAPER (eligible) but exports no LAB_TARGET → the clear
    SP-E/SP-F-pointing message, NOT KeyError/'unknown engine' (spec
    §4.1)."""
    from ops.lab.run import _lab_target_for
    with pytest.raises(ValueError, match="has not.*declared.*LAB_TARGET"):
        _lab_target_for("sentinel")


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
    a = run.sample_parameters("reversion", 8, seed=7)
    b = run.sample_parameters("reversion", 8, seed=7)
    assert a == b and set(a[0]) == set(_T0_PARAM_RANGES_KEYSETS["reversion"])


# ── default_params shim (the sixth surface, spec §0.1 / §2.3) ────────────

def test_default_params_shim_byte_equal_for_declared_engines():
    import importlib

    from ops.engine_sdlc.default_params import default_params
    for engine in ("reversion", "vector", "momentum"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert default_params(engine) == mod.default_params()


def test_default_params_shim_rejects_sentinel_with_clear_message():
    from ops.engine_sdlc.default_params import default_params
    with pytest.raises(ValueError, match="has not.*declared.*LAB_TARGET"):
        default_params("sentinel")


def test_no_import_cycle_default_params_shim_to_run():
    """ops.engine_sdlc.default_params → ops.lab.run is ops→ops, lazy,
    legal, no cycle. Import each first, then exercise."""
    import importlib

    m1 = importlib.import_module("ops.engine_sdlc.default_params")
    m2 = importlib.import_module("ops.lab.run")
    assert m1.default_params("momentum") == \
        importlib.import_module("momentum.backtest").default_params()
    assert callable(m2._lab_target_for)
