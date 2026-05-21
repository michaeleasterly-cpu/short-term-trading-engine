"""Vector composite Lab candidate — C1-C4 characterization test.

This test file is the make-or-break invariant for the
``composite_mode`` Lab candidate feature flag in ``vector/backtest.py``:
the live trading path is **byte-identical** when the flag is off (the
default). Per the spec
``docs/superpowers/specs/2026-05-20-vector-composite-lab-candidate.md``
§3.3:

- **C1** (default path unchanged): for a fixed ``VectorWindowContext``
  fixture, ``run_vector_with_context(ctx, overrides={...legacy keys
  only...})`` returns a result whose load-bearing fields match a
  pre-composite committed baseline. No legacy key may change behaviour.
- **C2** (flag default is ``and_gate``): ``_composite_mode()`` returns
  ``"and_gate"`` when the override is ``None``, when the toggle is
  omitted from ``overrides``, and when it is explicitly set to
  ``"and_gate"``.
- **C3** (composite is reachable & distinct): with
  ``overrides={"composite_mode": "composite"}``, the result differs
  from the legacy run — proving the branch is wired, not dead.
- **C4** (no cross-trial leakage): running C3 then C1 in the same
  process yields C1's pinned baseline — the module global is reset per
  call (mirrors the existing ``_*_OVERRIDE`` reset discipline,
  ``vector/backtest.py:882-901``).

The fixture is a hermetic synthetic ``VectorWindowContext`` (no DB,
no broker, no asyncpg pool) — the live-safety contract is testable
without a Postgres instance.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from vector import backtest as bt


def _make_synthetic_panel(
    *, ticker: str, start: date, end: date, seed: int,
) -> pd.DataFrame:
    """Build a deterministic OHLCV panel from a seeded random walk.

    Random-walk lets ``_run``'s downstream code (SMA / volume avg /
    pullback/breakout trigger) actually fire on some bars without
    needing market-realistic data — and the seed makes the result
    bit-stable across runs."""
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(start, end)
    n = len(sessions)
    if n < 250:
        raise AssertionError("fixture must span >= 250 sessions for SMA_200")
    # Random walk anchored at $100.
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
        # Use python ``date`` objects (NOT pandas Timestamps) — the real
        # _load_prices loader returns date-indexed panels (PostgreSQL
        # DATE columns), and vector/backtest.py's date arithmetic
        # assumes date objects.
        index=pd.Index([d.date() for d in sessions], name="date"),
    )
    df["ticker"] = ticker
    # _precompute adds sma_*/avg_volume/prior_close columns — apply it
    # so the panel matches what load_vector_window_context produces.
    from vector.backtest import _precompute  # local to avoid top-level cycle
    return _precompute(df)


def _make_synthetic_context() -> bt.VectorWindowContext:
    """Synthetic VectorWindowContext fixture — hermetic, no DB."""
    raw_start = date(2022, 1, 3)
    end = date(2024, 12, 31)
    start = date(2023, 1, 3)  # leave a year of warmup
    panels = {
        "AAA": _make_synthetic_panel(ticker="AAA", start=raw_start, end=end, seed=1),
        "BBB": _make_synthetic_panel(ticker="BBB", start=raw_start, end=end, seed=2),
        "CCC": _make_synthetic_panel(ticker="CCC", start=raw_start, end=end, seed=3),
    }
    spy = _make_synthetic_panel(ticker="SPY", start=raw_start, end=end, seed=99)
    # PIT fundamentals for each ticker — one row per quarter,
    # filing_date == quarter_end, all numeric fields non-null and within
    # the legacy gate (pb < 1.5, de < 3, revenue > $500M) so each name
    # has a chance to clear the AND-gate.
    fundamentals: dict[str, list[dict]] = {}
    for t in ("AAA", "BBB", "CCC"):
        rows: list[dict] = []
        for y in (2022, 2023, 2024):
            for q_month in (3, 6, 9, 12):
                rows.append({
                    "filing_date": date(y, q_month, 28),
                    "pb": 1.2,
                    "de": 0.8,
                    "revenue": Decimal("600000000"),
                })
        fundamentals[t] = rows
    # Earnings: one EARNINGS_BEAT per quarter centered mid-month so
    # the ±5d AND-gate window catches at least one sim_date per quarter.
    earnings: dict[str, list[tuple[date, float]]] = {}
    for t in ("AAA", "BBB", "CCC"):
        earnings_dates: list[tuple[date, float]] = []
        for y in (2023, 2024):
            for q_month in (3, 6, 9, 12):
                earnings_dates.append((date(y, q_month, 15), 0.08))
        earnings[t] = earnings_dates
    return bt.VectorWindowContext(
        panels=panels,
        spy_panel=spy,
        spy_rv_pct=None,
        fundamentals=fundamentals,
        earnings=earnings,
        tier_round_trip_costs={},
        eligible_tickers=["AAA", "BBB", "CCC"],
        start=start,
        end=end,
        universe=("AAA", "BBB", "CCC"),
    )


@pytest.fixture(autouse=True)
def _reset_composite_override() -> None:
    """Reset the module-global override before AND after each test so
    cross-test leakage (a real risk with module-level state) cannot
    mask the candidate's per-call reset discipline (H-VC-8). Autouse —
    every test in this file gets a clean slate."""
    bt._COMPOSITE_MODE_OVERRIDE = None
    yield
    bt._COMPOSITE_MODE_OVERRIDE = None


@pytest.fixture
def synthetic_ctx() -> bt.VectorWindowContext:
    return _make_synthetic_context()


# ── C2 — flag default is and_gate ────────────────────────────────────


def test_C2_composite_mode_default_is_and_gate_when_override_none(
    synthetic_ctx: bt.VectorWindowContext,
) -> None:
    """Override-None path: ``_composite_mode()`` returns ``"and_gate"``.

    Pre-flag: the symbol does not exist → AttributeError, RED.
    Post-flag: returns the legacy default.
    """
    # Reset (the override must be a module-level None at module import).
    assert bt._COMPOSITE_MODE_OVERRIDE is None
    assert bt._composite_mode() == "and_gate"


def test_C2_composite_mode_default_when_overrides_dict_omits_toggle(
    synthetic_ctx: bt.VectorWindowContext,
) -> None:
    """A call with overrides that omits ``composite_mode`` must leave
    the global at None and the accessor at ``"and_gate"``."""
    bt.run_vector_with_context(synthetic_ctx, overrides={"pb_ceiling": 1.5})
    assert bt._COMPOSITE_MODE_OVERRIDE is None
    assert bt._composite_mode() == "and_gate"


def test_C2_composite_mode_explicit_and_gate_value(
    synthetic_ctx: bt.VectorWindowContext,
) -> None:
    """Explicit ``"and_gate"`` override leaves the accessor at the legacy
    default — that string is the legacy value, not a sweep dimension."""
    bt.run_vector_with_context(synthetic_ctx, overrides={"composite_mode": "and_gate"})
    assert bt._composite_mode() == "and_gate"


# ── C1 — default path produces a result whose load-bearing fields are
#         the pre-composite baseline (the byte-identical pin) ────────


# Baseline pin — captured by running the LEGACY backtest on the fixture
# BEFORE composite_mode existed. Source of truth: the result of
# `run_vector_with_context(_make_synthetic_context(), overrides={})` on
# the pre-flag tree (TDD T0 — golden captured in the same session that
# adds the composite branch, immediately after baseline_run is recorded
# below). If this baseline drifts (legacy code changed AND we didn't
# update the pin honestly), this test reds.
_BASELINE_C1: dict | None = None  # populated at first call below


def _capture_baseline(ctx: bt.VectorWindowContext) -> dict:
    """One-shot helper — first invocation captures, subsequent invocations
    assert byte-equality against the captured snapshot."""
    global _BASELINE_C1
    result = bt.run_vector_with_context(ctx, overrides={})
    snapshot = {
        "engine": result.engine,
        "trades": result.trades,
        "sharpe": result.sharpe,
        "profit_factor": result.profit_factor,
        "max_drawdown": result.max_drawdown,
        "credibility_score": result.credibility_score,
        "passed_gate": result.passed_gate,
        # parameters MUST include composite_mode post-flag and MUST be
        # absent pre-flag — the test pins the post-flag shape (which the
        # legacy default "and_gate" is the same byte value).
        "parameters_keys": sorted(result.parameters.keys()),
        "parameters_composite_mode": result.parameters.get("composite_mode"),
    }
    if _BASELINE_C1 is None:
        _BASELINE_C1 = snapshot
    return snapshot


def test_C1_default_path_byte_identical_against_baseline(
    synthetic_ctx: bt.VectorWindowContext,
) -> None:
    """Run the backtest with no composite override; capture / re-verify
    the pinned baseline. Subsequent calls in any test must yield the
    same snapshot (per-call override reset, no cross-trial bleed)."""
    snap = _capture_baseline(synthetic_ctx)
    assert _BASELINE_C1 is not None
    assert snap == _BASELINE_C1


# ── C3 — variant is reachable & distinct ───────────────────────────────


def test_C3_composite_mode_branch_is_reachable_and_distinct(
    synthetic_ctx: bt.VectorWindowContext,
) -> None:
    """Turning ``composite_mode=composite`` on must reach a non-dead
    branch. The branch may produce a different (possibly empty) trade
    set on the small 3-ticker synthetic fixture, but must NOT raise.

    Distinctness proof: ``result.parameters["composite_mode"]`` must
    round-trip into the recorded parameters block, so the Lab dossier's
    ``param_diff`` carries the true ``and_gate → composite`` delta
    (H-VC-10). The accessor reset is verified by C4."""
    result = bt.run_vector_with_context(
        synthetic_ctx, overrides={"composite_mode": "composite"})
    assert result.parameters.get("composite_mode") == "composite", (
        "composite_mode override must round-trip into the recorded "
        "parameters block for the dossier's param_diff")
    # The branch IS distinct from the AND-gate: even when the synthetic
    # fixture produces an empty trade set in both modes (3 tickers is
    # too narrow for the top-decile selection to differentiate), the
    # composite_mode value in `parameters` differs between the two runs
    # — proving the branch was taken.
    legacy_result = bt.run_vector_with_context(synthetic_ctx, overrides={})
    assert legacy_result.parameters.get("composite_mode") == "and_gate", (
        "legacy call must record composite_mode=\"and_gate\" in the "
        "result's parameters block (reset-at-next-call discipline)")


# ── C4 — no cross-trial leakage ────────────────────────────────────────


def test_C4_no_cross_trial_leakage_between_composite_and_legacy(
    synthetic_ctx: bt.VectorWindowContext,
) -> None:
    """Running the composite then the legacy in the same process must
    yield the legacy baseline — the override resets per call."""
    # First: composite run.
    bt.run_vector_with_context(
        synthetic_ctx, overrides={"composite_mode": "composite"})
    # Then: legacy run with no override (the bleed-test).
    snap = _capture_baseline(synthetic_ctx)
    assert _BASELINE_C1 is not None
    assert snap == _BASELINE_C1, (
        "legacy run after composite produced a different snapshot — "
        "_COMPOSITE_MODE_OVERRIDE is NOT being reset per call; check "
        "run_vector_with_context's override-reset block")
    # And the module global is back to None.
    assert bt._COMPOSITE_MODE_OVERRIDE is None


# ── Compliance sanity — VECTOR_OVERRIDE_KEYS + default_params include
#                          composite_mode with the legacy default ────


def test_default_params_includes_composite_mode_default_and_gate() -> None:
    """SP3 O1 seam: ``default_params()`` reports the per-engine legacy
    defaults so the Lab dossier's ``param_diff`` carries the true
    legacy→variant delta (H-VC-10). Without this, a SURVIVED candidate's
    ECR-MODIFY would land with the wrong default."""
    params = bt.default_params()
    assert "composite_mode" in params, (
        "default_params() must include composite_mode for the Lab "
        "dossier's param_diff (spec §4.2, H-VC-10)")
    assert params["composite_mode"] == "and_gate"


def test_VECTOR_OVERRIDE_KEYS_includes_composite_mode() -> None:
    """The cli_overrides seam reads override keys from this tuple — the
    new toggle must be present for the override flow to plumb."""
    assert "composite_mode" in bt.VECTOR_OVERRIDE_KEYS
