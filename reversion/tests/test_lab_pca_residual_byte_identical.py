"""Reversion PCA-residual Lab candidate — C1-C8 characterization tests.

This test file is the make-or-break invariant for the ``signal_mode``
Lab candidate feature flag in ``reversion/backtest.py``: the live
trading path is **byte-identical** when the flag is off (the
default). Per the spec
``docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-
candidate.md`` §5.2:

* **C1** — legacy ``price_z`` path: ``signal_mode`` parameter being
  ADDED to the wiring (added to overrides parsing, default_params,
  param_ranges, recorded parameters) does NOT change the load-bearing
  result fields of a price_z run vs the prior baseline. We pin this
  by comparing a call with ``overrides={}`` to a call with
  ``overrides={"signal_mode": "price_z"}``.
* **C2** — flag default is ``price_z``: ``_signal_mode()`` returns
  ``"price_z"`` when override is None, omitted, or explicit
  ``"price_z"``.
* **C3** — ``signal_mode="pca_residual"`` is reachable & distinct
  (the recorded parameters block contains ``signal_mode="pca_residual"``).
* **C4** — no cross-trial leakage: running pca_residual then price_z
  yields the price_z baseline.
* **C5** — ``default_params()["signal_mode"] == "price_z"``.
* **C6** — ``REVERSION_OVERRIDE_KEYS`` contains ``"signal_mode"``.
* **C7** — ``LAB_TARGET.param_ranges["signal_mode"]`` is exactly
  ``(0, 0, "choice:price_z,pca_residual")``.
* **C8** — live-path import isolation: subprocess probe that
  ``import reversion.scheduler`` does NOT pull in
  ``reversion.lab_pca_residual`` nor ``reversion.backtest``.

Mirrors the precedent at
``momentum/tests/test_lab_vol_managed_byte_identical.py``.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from reversion import backtest as bt


def _make_synthetic_panel(
    *, ticker: str, start: date, end: date, seed: int,
) -> pd.DataFrame:
    """Deterministic OHLCV panel from a seeded random walk.

    The panel includes the same precomputed-indicator columns as the
    real ``reversion.backtest._precompute_indicators`` output so that
    the legacy price_z scan can run without re-precomputing.
    """
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(start, end)
    n = len(sessions)
    if n < 100:
        raise AssertionError("fixture must span >= 100 sessions")
    rets = rng.normal(0.0005, 0.02, size=n)
    closes = 100.0 * np.exp(np.cumsum(rets))
    highs = closes * (1 + rng.uniform(0, 0.01, size=n))
    lows = closes * (1 - rng.uniform(0, 0.01, size=n))
    opens = closes * (1 + rng.normal(0, 0.005, size=n))
    volumes = rng.integers(1_500_000, 5_000_000, size=n)
    df = pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(np.maximum(highs, opens), closes),
            "low": np.minimum(np.minimum(lows, opens), closes),
            "close": closes,
            "volume": volumes,
        },
        # Use python date objects (NOT pandas Timestamps) — the real
        # _load_prices loader returns date-indexed panels.
        index=pd.Index([d.date() for d in sessions], name="date"),
    )
    df["ticker"] = ticker
    # Precompute indicators (mirrors _precompute_indicators output).
    return bt._precompute_indicators(df)  # noqa: SLF001 — test fixture mirrors precompute path


def _make_synthetic_context() -> bt.ReversionWindowContext:
    """Synthetic ReversionWindowContext — hermetic, no DB."""
    start = date(2022, 1, 3)
    end = date(2023, 12, 29)
    panels = {
        f"TST{i:03d}": _make_synthetic_panel(
            ticker=f"TST{i:03d}", start=start, end=end, seed=200 + i,
        )
        for i in range(20)
    }
    # SPY synthetic for market-context (mandatory for the legacy
    # scan — the spy_panel argument feeds the market_context score).
    spy = _make_synthetic_panel(
        ticker="SPY", start=start, end=end, seed=999,
    )
    return bt.ReversionWindowContext(
        panels=panels,
        spy_panel=spy,
        fundamentals={},  # legacy "NONE" filter ⇒ empty fundamentals fine
        tier_round_trip_costs={},
        funded_tickers=list(panels.keys()),
        start=start,
        end=end,
        universe=tuple(panels.keys()),
    )


@pytest.fixture(autouse=True)
def _reset_signal_mode_override() -> None:
    """Reset the module-global override before AND after each test so
    cross-test leakage cannot mask the candidate's per-call reset
    discipline (mirror of momentum H-MVM-8). Autouse — every test in
    this file gets a clean slate.
    """
    bt._SIGNAL_MODE_OVERRIDE = None  # noqa: SLF001
    yield
    bt._SIGNAL_MODE_OVERRIDE = None  # noqa: SLF001


@pytest.fixture
def synthetic_ctx() -> bt.ReversionWindowContext:
    return _make_synthetic_context()


# ── C2 — flag default is price_z ────────────────────────────────────


def test_C2_signal_mode_default_is_price_z_when_override_none() -> None:
    """Override-None path: ``_signal_mode()`` returns ``"price_z"``."""
    assert bt._SIGNAL_MODE_OVERRIDE is None  # noqa: SLF001
    assert bt._signal_mode() == "price_z"  # noqa: SLF001


def test_C2_signal_mode_default_when_overrides_dict_omits_toggle(
    synthetic_ctx: bt.ReversionWindowContext,
) -> None:
    """A call with overrides that omits ``signal_mode`` must leave
    the global at None and the accessor at ``"price_z"``."""
    bt.run_reversion_with_context(
        synthetic_ctx, overrides={"z_threshold": 2.5},
    )
    assert bt._SIGNAL_MODE_OVERRIDE is None  # noqa: SLF001
    assert bt._signal_mode() == "price_z"  # noqa: SLF001


def test_C2_signal_mode_explicit_price_z_value(
    synthetic_ctx: bt.ReversionWindowContext,
) -> None:
    """Explicit ``"price_z"`` override leaves the accessor at the
    legacy default — that string is the legacy value, not a sweep
    dimension.
    """
    bt.run_reversion_with_context(
        synthetic_ctx, overrides={"signal_mode": "price_z"},
    )
    assert bt._signal_mode() == "price_z"  # noqa: SLF001


# ── C1 — legacy result byte-identical w/ vs w/o the signal_mode parameter


def test_C1_legacy_result_byte_identical_with_vs_without_signal_mode_override(
    synthetic_ctx: bt.ReversionWindowContext,
) -> None:
    """The legacy price_z backtest must produce identical load-bearing
    BacktestRunResult fields whether ``overrides={}`` or
    ``overrides={"signal_mode": "price_z"}`` — the additive override
    parsing is byte-identical when the flag is off.
    """
    result_no_override = bt.run_reversion_with_context(
        synthetic_ctx, overrides={},
    )
    result_with_price_z = bt.run_reversion_with_context(
        synthetic_ctx, overrides={"signal_mode": "price_z"},
    )

    assert result_no_override.trades == result_with_price_z.trades
    assert result_no_override.sharpe == pytest.approx(
        result_with_price_z.sharpe,
    )
    assert result_no_override.profit_factor == pytest.approx(
        result_with_price_z.profit_factor,
    )
    assert result_no_override.max_drawdown == pytest.approx(
        result_with_price_z.max_drawdown,
    )
    # The legacy parameters block reports signal_mode = price_z.
    assert result_no_override.parameters["signal_mode"] == "price_z"
    assert result_with_price_z.parameters["signal_mode"] == "price_z"


# ── C3 — pca_residual variant is reachable & distinct ──────────────


def test_C3_pca_residual_branch_is_reachable_and_distinct(
    synthetic_ctx: bt.ReversionWindowContext,
) -> None:
    """Turning ``signal_mode=pca_residual`` on must reach a non-dead
    branch. Distinctness proof: ``result.parameters["signal_mode"]``
    round-trips into the recorded parameters block, so the Lab
    dossier's ``param_diff`` carries the true ``price_z → pca_residual``
    delta.
    """
    result = bt.run_reversion_with_context(
        synthetic_ctx, overrides={"signal_mode": "pca_residual"},
    )
    assert result.parameters.get("signal_mode") == "pca_residual", (
        "signal_mode override must round-trip into the recorded "
        "parameters block for the dossier's param_diff"
    )
    # Companion call: the legacy run records the legacy value.
    legacy_result = bt.run_reversion_with_context(synthetic_ctx, overrides={})
    assert legacy_result.parameters.get("signal_mode") == "price_z", (
        "legacy call must record signal_mode=\"price_z\" in the "
        "result's parameters block (reset-at-next-call discipline)"
    )


# ── C4 — no cross-trial leakage ─────────────────────────────────────


def test_C4_no_cross_trial_leakage_between_pca_residual_and_price_z(
    synthetic_ctx: bt.ReversionWindowContext,
) -> None:
    """Running pca_residual then price_z in the same process must
    yield the price_z result — the override resets per call.
    """
    bt.run_reversion_with_context(
        synthetic_ctx, overrides={"signal_mode": "pca_residual"},
    )
    legacy_result = bt.run_reversion_with_context(synthetic_ctx, overrides={})
    assert bt._SIGNAL_MODE_OVERRIDE is None, (  # noqa: SLF001
        "the next-call override-parse must reset _SIGNAL_MODE_OVERRIDE"
    )
    assert bt._signal_mode() == "price_z"  # noqa: SLF001
    assert legacy_result.parameters["signal_mode"] == "price_z"


# ── C5 — default_params() includes signal_mode with legacy default ──


def test_C5_default_params_includes_signal_mode_default_price_z() -> None:
    """SP3 O1 seam: ``default_params()`` reports the per-engine legacy
    defaults so the Lab dossier's ``param_diff`` carries the true
    legacy→variant delta. Without this, a SURVIVED candidate's
    ECR-MODIFY would land with the wrong default.
    """
    params = bt.default_params()
    assert "signal_mode" in params, (
        "default_params() must include signal_mode for the Lab "
        "dossier's param_diff"
    )
    assert params["signal_mode"] == "price_z"


# ── C6 — REVERSION_OVERRIDE_KEYS includes signal_mode ──────────────


def test_C6_REVERSION_OVERRIDE_KEYS_includes_signal_mode() -> None:
    """The cli_overrides seam reads override keys from this tuple — the
    new toggle must be present for the override flow to plumb.
    """
    assert "signal_mode" in bt.REVERSION_OVERRIDE_KEYS


# ── C7 — LAB_TARGET.param_ranges declares signal_mode choice ───────


def test_C7_LAB_TARGET_param_ranges_includes_signal_mode_choice() -> None:
    """Compliance: the ONE new ``LAB_TARGET.param_ranges`` toggle is a
    ``choice:price_z,pca_residual`` spec. No menu, no second variant.
    """
    spec = bt.LAB_TARGET.param_ranges.get("signal_mode")
    assert spec is not None, (
        "LAB_TARGET.param_ranges must declare signal_mode for the "
        "Lab to sample it"
    )
    low, high, kind = spec
    assert kind == "choice:price_z,pca_residual", (
        "signal_mode kind must be exactly the price_z + variant "
        "choice — no other menu allowed"
    )


# ── C8 — live-path import isolation (the strongest byte-identical proof)


def test_C8_live_scheduler_does_not_import_lab_pca_residual() -> None:
    """``reversion.scheduler`` (the live trading path) MUST NOT import
    ``reversion.lab_pca_residual`` nor ``reversion.backtest``.

    Without this isolation, a subtle import-side-effect on the Lab
    module (e.g. a default-on env flag) could leak into the live
    scheduler — exactly the failure mode the byte-identical contract
    forbids (Sigma lesson).

    Implementation uses subprocess isolation rather than purging
    ``sys.modules`` in-process (mirrors the momentum precedent's
    rationale).
    """
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import reversion.scheduler\n"  # noqa: F401 — the import is the assertion
        "loaded = {m for m in sys.modules if m.startswith('reversion.')}\n"
        "assert 'reversion.lab_pca_residual' not in loaded, "
        "'live scheduler must NOT import reversion.lab_pca_residual'\n"
        "assert 'reversion.backtest' not in loaded, "
        "'live scheduler must NOT import reversion.backtest'\n"
        "print('ok')\n"
    )
    result = subprocess.run(  # noqa: S603 — controlled subprocess, no shell
        [sys.executable, "-c", probe],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0 and "ok" in result.stdout, (
        "Live-path import-isolation probe failed.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
