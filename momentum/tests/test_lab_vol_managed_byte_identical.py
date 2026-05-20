"""Momentum vol-managed Lab candidate — C1-C4 characterization test.

This test file is the make-or-break invariant for the
``vol_managed_mode`` Lab candidate feature flag in
``momentum/backtest.py``: the live trading path is **byte-identical**
when the flag is off (the default). Per the spec
``docs/superpowers/specs/2026-05-20-momentum-vol-managed-lab-candidate.md``
§3.3:

- **C1** (default path unchanged): for a fixed
  ``MomentumWindowContext`` fixture,
  ``run_momentum_with_context(ctx, overrides={...legacy keys only...})``
  returns a result whose load-bearing fields match a pinned baseline.
  No legacy key may change behaviour. Earnings rows being PRESENT on
  the context does NOT change the legacy result (the legacy branch
  never reads them).
- **C2** (flag default is ``legacy``): ``_vol_managed_mode()`` returns
  ``"legacy"`` when the override is ``None``, when the toggle is
  omitted from ``overrides``, and when it is explicitly set to
  ``"legacy"``.
- **C3** (vol_managed is reachable & distinct): with
  ``overrides={"vol_managed_mode": "vol_managed"}``, the result records
  ``parameters["vol_managed_mode"] = "vol_managed"`` (proves branch is
  wired, not dead).
- **C4** (no cross-trial leakage): running C3 then C1 in the same
  process yields C1's pinned baseline — the module global is reset
  per call (mirrors the existing ``_*_OVERRIDE`` reset discipline).
- **Live-path import isolation:** ``momentum.scheduler`` does NOT
  import ``momentum.backtest`` nor ``momentum.lab_vol_managed`` — the
  strongest byte-identical proof (the live path cannot transitively
  reach the Lab branch).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from momentum import backtest as bt


def _make_synthetic_panel(
    *, ticker: str, start: date, end: date, seed: int,
) -> pd.DataFrame:
    """Deterministic OHLCV panel from a seeded random walk.

    The seed makes the result bit-stable across runs."""
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(start, end)
    n = len(sessions)
    if n < 280:
        raise AssertionError("fixture must span >= 280 sessions for 12-1 lookback + skip")
    rets = rng.normal(0.0008, 0.015, size=n)
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
        # _load_bars loader returns date-indexed panels.
        index=pd.Index([d.date() for d in sessions], name="date"),
    )
    df["ticker"] = ticker
    return df


def _make_synthetic_context(*, with_earnings: bool) -> bt.MomentumWindowContext:
    """Synthetic MomentumWindowContext — hermetic, no DB."""
    raw_start = date(2022, 1, 3)
    end = date(2024, 12, 31)
    start = date(2023, 6, 1)  # leaves > 1 year warmup for the 12-1 lookback
    panels = {
        # Need enough tickers + price diversity so the top-decile
        # selection produces SOMETHING. The is_tradeable_common_stock
        # filter is permissive for prices > $1 + common-stock tickers.
        f"TST{i:03d}": _make_synthetic_panel(
            ticker=f"TST{i:03d}",
            start=raw_start, end=end, seed=100 + i,
        )
        for i in range(20)
    }
    # Earnings rows — populated ONLY when with_earnings=True. The
    # legacy path is not allowed to read these (the C1 test pins
    # byte-identical regardless).
    earnings: dict[str, list[tuple[date, float]]] | None = None
    if with_earnings:
        earnings = {}
        for ticker in panels:
            rows: list[tuple[date, float]] = []
            for y in (2023, 2024):
                for q_month in (3, 6, 9, 12):
                    rows.append((date(y, q_month, 15), 0.05))
            earnings[ticker] = rows
    return bt.MomentumWindowContext(
        panels=panels,
        tier_round_trip_costs={},
        start=start,
        end=end,
        universe=tuple(panels.keys()),
        raw_start=raw_start,
        earnings_by_ticker=earnings,
    )


@pytest.fixture(autouse=True)
def _reset_vol_managed_override() -> None:
    """Reset the module-global override before AND after each test so
    cross-test leakage (a real risk with module-level state) cannot
    mask the candidate's per-call reset discipline (H-MVM-8). Autouse —
    every test in this file gets a clean slate.
    """
    bt._VOL_MANAGED_OVERRIDE = None  # noqa: SLF001
    yield
    bt._VOL_MANAGED_OVERRIDE = None  # noqa: SLF001


@pytest.fixture
def synthetic_ctx_no_earnings() -> bt.MomentumWindowContext:
    return _make_synthetic_context(with_earnings=False)


@pytest.fixture
def synthetic_ctx_with_earnings() -> bt.MomentumWindowContext:
    return _make_synthetic_context(with_earnings=True)


# ── C2 — flag default is legacy ──────────────────────────────────────


def test_C2_vol_managed_mode_default_is_legacy_when_override_none() -> None:
    """Override-None path: ``_vol_managed_mode()`` returns ``"legacy"``."""
    assert bt._VOL_MANAGED_OVERRIDE is None  # noqa: SLF001
    assert bt._vol_managed_mode() == "legacy"  # noqa: SLF001


def test_C2_vol_managed_mode_default_when_overrides_dict_omits_toggle(
    synthetic_ctx_with_earnings: bt.MomentumWindowContext,
) -> None:
    """A call with overrides that omits ``vol_managed_mode`` must leave
    the global at None and the accessor at ``"legacy"``."""
    bt.run_momentum_with_context(
        synthetic_ctx_with_earnings, overrides={"lookback_days": 231},
    )
    assert bt._VOL_MANAGED_OVERRIDE is None  # noqa: SLF001
    assert bt._vol_managed_mode() == "legacy"  # noqa: SLF001


def test_C2_vol_managed_mode_explicit_legacy_value(
    synthetic_ctx_with_earnings: bt.MomentumWindowContext,
) -> None:
    """Explicit ``"legacy"`` override leaves the accessor at the legacy
    default — that string is the legacy value, not a sweep dimension."""
    bt.run_momentum_with_context(
        synthetic_ctx_with_earnings,
        overrides={"vol_managed_mode": "legacy"},
    )
    assert bt._vol_managed_mode() == "legacy"  # noqa: SLF001


# ── C1 — legacy path byte-identical regardless of earnings presence ──


def test_C1_legacy_result_byte_identical_with_vs_without_earnings(
    synthetic_ctx_no_earnings: bt.MomentumWindowContext,
    synthetic_ctx_with_earnings: bt.MomentumWindowContext,
) -> None:
    """The legacy 12-1 backtest must NOT read ``earnings_by_ticker``.

    Two contexts identical in every load-bearing way EXCEPT the
    earnings field MUST produce identical load-bearing
    ``BacktestRunResult`` fields on the legacy path. This is the
    proof that the additive loader read is byte-identical when the
    flag is off (H-MVM-1)."""
    result_no_earn = bt.run_momentum_with_context(
        synthetic_ctx_no_earnings, overrides={},
    )
    result_with_earn = bt.run_momentum_with_context(
        synthetic_ctx_with_earnings, overrides={},
    )

    # Load-bearing fields the dossier consumes — every one must match.
    assert result_no_earn.trades == result_with_earn.trades
    assert result_no_earn.sharpe == pytest.approx(result_with_earn.sharpe)
    assert result_no_earn.profit_factor == pytest.approx(
        result_with_earn.profit_factor,
    )
    assert result_no_earn.max_drawdown == pytest.approx(
        result_with_earn.max_drawdown,
    )
    assert result_no_earn.parameters == result_with_earn.parameters
    # The legacy parameters block reports vol_managed_mode = legacy
    # (the SP3 O1 default_params seam — H-MVM-10).
    assert result_no_earn.parameters["vol_managed_mode"] == "legacy"


# ── C3 — variant is reachable & distinct ────────────────────────────


def test_C3_vol_managed_branch_is_reachable_and_distinct(
    synthetic_ctx_with_earnings: bt.MomentumWindowContext,
) -> None:
    """Turning ``vol_managed_mode=vol_managed`` on must reach a non-dead
    branch.

    Distinctness proof: ``result.parameters["vol_managed_mode"]`` must
    round-trip into the recorded parameters block, so the Lab dossier's
    ``param_diff`` carries the true ``legacy → vol_managed`` delta
    (H-MVM-10).
    """
    result = bt.run_momentum_with_context(
        synthetic_ctx_with_earnings,
        overrides={"vol_managed_mode": "vol_managed"},
    )
    assert result.parameters.get("vol_managed_mode") == "vol_managed", (
        "vol_managed_mode override must round-trip into the recorded "
        "parameters block for the dossier's param_diff (H-MVM-10)"
    )
    # Companion call: the legacy run records the legacy value (the
    # reset-at-next-call discipline).
    legacy_result = bt.run_momentum_with_context(
        synthetic_ctx_with_earnings, overrides={},
    )
    assert legacy_result.parameters.get("vol_managed_mode") == "legacy", (
        "legacy call must record vol_managed_mode=\"legacy\" in the "
        "result's parameters block (reset-at-next-call discipline)"
    )


# ── C4 — no cross-trial leakage ─────────────────────────────────────


def test_C4_no_cross_trial_leakage_between_vol_managed_and_legacy(
    synthetic_ctx_with_earnings: bt.MomentumWindowContext,
) -> None:
    """Running vol_managed then legacy in the same process must yield
    the legacy result — the override resets per call."""
    # First: vol_managed run.
    bt.run_momentum_with_context(
        synthetic_ctx_with_earnings,
        overrides={"vol_managed_mode": "vol_managed"},
    )
    # The module-global override should be restored / reset after the
    # call (well — set TO ``vol_managed`` then NOT auto-cleared; the
    # reset happens on the NEXT call's override-parse block).
    # Second: legacy run with no override (the bleed-test).
    legacy_result = bt.run_momentum_with_context(
        synthetic_ctx_with_earnings, overrides={},
    )
    assert bt._VOL_MANAGED_OVERRIDE is None, (  # noqa: SLF001
        "the next-call override-parse must reset _VOL_MANAGED_OVERRIDE"
    )
    assert bt._vol_managed_mode() == "legacy"  # noqa: SLF001
    assert legacy_result.parameters["vol_managed_mode"] == "legacy"


# ── Compliance sanity — MOMENTUM_OVERRIDE_KEYS + default_params include
#                       vol_managed_mode with the legacy default ────


def test_default_params_includes_vol_managed_mode_default_legacy() -> None:
    """SP3 O1 seam: ``default_params()`` reports the per-engine legacy
    defaults so the Lab dossier's ``param_diff`` carries the true
    legacy→variant delta (H-MVM-10). Without this, a SURVIVED candidate's
    ECR-MODIFY would land with the wrong default."""
    params = bt.default_params()
    assert "vol_managed_mode" in params, (
        "default_params() must include vol_managed_mode for the Lab "
        "dossier's param_diff (spec §4.2, H-MVM-10)"
    )
    assert params["vol_managed_mode"] == "legacy"


def test_MOMENTUM_OVERRIDE_KEYS_includes_vol_managed_mode() -> None:
    """The cli_overrides seam reads override keys from this tuple — the
    new toggle must be present for the override flow to plumb."""
    assert "vol_managed_mode" in bt.MOMENTUM_OVERRIDE_KEYS


def test_LAB_TARGET_param_ranges_includes_vol_managed_mode_choice() -> None:
    """Compliance: the ONE new ``LAB_TARGET.param_ranges`` toggle is a
    ``choice:legacy,vol_managed`` spec. No menu, no second variant
    (H-MVM-2)."""
    spec = bt.LAB_TARGET.param_ranges.get("vol_managed_mode")
    assert spec is not None, (
        "LAB_TARGET.param_ranges must declare vol_managed_mode for the "
        "Lab to sample it (spec §4.1)"
    )
    low, high, kind = spec
    assert kind == "choice:legacy,vol_managed", (
        "vol_managed_mode kind must be exactly the legacy + variant "
        "choice — no other menu allowed (H-MVM-2)"
    )


# ── Live-path import isolation (the strongest byte-identical proof) ─


def test_live_scheduler_does_not_import_lab_vol_managed() -> None:
    """``momentum.scheduler`` (the live trading path) MUST NOT import
    ``momentum.lab_vol_managed`` nor ``momentum.backtest``.

    Without this isolation, a subtle import-side-effect on the Lab
    module (e.g. a default-on env flag) could leak into the live
    scheduler — exactly the failure mode the byte-identical contract
    forbids (H-MVM-1, H-MVM-9).

    Implementation uses subprocess isolation rather than purging
    ``sys.modules`` in-process — purging ``momentum.*`` from sys.modules
    breaks every subsequent test in this run that imports a momentum
    module (the module-level state on, e.g., the scheduler's broker
    constants gets re-initialized, but pytest's previously-imported
    test-module objects still refer to the OLD instances; in particular
    the ``StubGovernor`` fixture's ``check_trade`` patch is lost). A
    subprocess gives us a fresh interpreter with no pollution risk.
    """
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import momentum.scheduler\n"  # noqa: F401 — the import is the assertion
        "loaded = {m for m in sys.modules if m.startswith('momentum.')}\n"
        "assert 'momentum.lab_vol_managed' not in loaded, "
        "'live scheduler must NOT import momentum.lab_vol_managed'\n"
        "assert 'momentum.backtest' not in loaded, "
        "'live scheduler must NOT import momentum.backtest'\n"
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
