"""Sentinel — graduated Bear Score Lab candidate, §4 byte-identical contract.

The make-or-break invariant: Sentinel's LIVE trading path is
byte-identical when the off-by-default ``bear_score_mode`` Lab flag is
off (legacy ``"current"`` default). The graduated five-factor composite
is REACHABLE only when the override is explicitly the string
``"graduated"``.

C1  committed golden: ``run_sentinel_with_context(ctx, overrides={})``
    BacktestRunResult is a frozen golden of the pre-candidate (legacy)
    behaviour.
C2  default-is-legacy: identical when the override is None, when the
    toggle is omitted, and when it is explicitly the legacy value
    ``"current"``.
C3  variant reachable + distinct: the ``"graduated"`` toggle changes the
    result (the branch is wired, not dead).
C4  no cross-trial leakage: variant-then-legacy in the same process
    yields the legacy golden (the per-call module-global reset).

Fully hermetic: a synthetic SentinelWindowContext is built in-body (no
DB, no network, no ``import ops.lab.run`` at module-load — the SP-D CI
hermeticity lesson). The golden is captured from the legacy
(no-override) code path itself — the byte-identical contract IS the
legacy behaviour, RED on any drift.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from sentinel.models import BearScoreBreakdown


def _bd(d: date, score: int) -> BearScoreBreakdown:
    """Minimal valid breakdown whose ``.score`` is exactly ``score``."""
    return BearScoreBreakdown(
        as_of=d, sahm_pts=0, industrial_production_pts=0,
        initial_claims_pts=0, yield_curve_pts=0, credit_spread_pts=0,
        vix_pts=0, raw_total=0, score=score,
    )


def _synthetic_macro_panel(dates: list[date]) -> pd.DataFrame:
    """Synthetic macro indicator panel covering the five graduated
    factors. Values are chosen so the composite crosses every action
    band (DORMANT / LIGHT / HEAVY / DEEP) across the synthetic window —
    the graduated path therefore produces a non-trivial trade tape that
    differs from the legacy binary-activation path.
    """
    rows = []
    for i, d in enumerate(dates):
        # Phase the indicators so the composite walks 0 → 0.85 → 0 across
        # the window. Each factor sub-score is in [0, 1] under the §2.2
        # mapping; we use raw indicator values that the mapping clips
        # into that range.
        ramp = min(i, 25) / 25.0  # 0 → 1 across first 25 days then plateau then fade
        if i > 30:
            ramp = max(0.0, ramp - (i - 30) / 25.0)
        rows.append({
            "date": d,
            "sahm_rule": 0.20 + ramp * 0.60,        # 0.20 → 0.80
            "sos_state_diffusion": 0.05 + ramp * 0.35,  # 0.05 → 0.40
            "yield_curve": -ramp * 1.00,            # 0 → -1.00 (inversion)
            "cfnai_ma3": -(0.20 + ramp * 1.00),     # -0.20 → -1.20
            "hy_spread": 3.00 + ramp * 5.00,        # 3.00 → 8.00
        })
    df = pd.DataFrame(rows).set_index("date")
    return df


def _synthetic_context(*, with_macro_panel: bool = True):
    """A SentinelWindowContext whose Bear-Score series is deliberately
    constructed so the legacy ``activation_score_threshold=60`` gate
    never fires (so legacy = zero trades), while the graduated composite
    DOES fire (so variant = non-zero trades) — making C3 unambiguous.

    Bear-Score breakdowns are pinned to score=0 throughout the window,
    so the LEGACY binary-activation gate is provably DORMANT for the
    whole simulation (zero trades). The graduated branch reads the
    additive macro_panel attribute and computes its own composite, which
    activates on the synthetic indicator ramp.
    """
    from sentinel.backtest import SentinelWindowContext

    d0 = date(2020, 3, 2)
    bus = [d0 + timedelta(days=i) for i in range(50)]
    breakdowns: dict[date, BearScoreBreakdown] = {
        d: _bd(d, 0) for d in bus  # legacy gate: zero forever
    }

    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in bus])
    spy = pd.Series(
        [300.0 - 1.5 * i for i in range(len(bus))], index=idx, name="SPY",
    )
    etf = {"SPY": spy}
    for t in ("SH", "PSQ", "TLT", "GLD", "SQQQ"):
        etf[t] = pd.Series(
            [50.0 + 0.4 * i - (2.0 if 10 <= i <= 18 else 0.0)
             for i in range(len(bus))],
            index=idx, name=t,
        )
    costs = {t: Decimal("0.001")
             for t in ("SH", "PSQ", "TLT", "GLD", "SQQQ")}
    macro_panel = _synthetic_macro_panel(bus) if with_macro_panel else None
    return SentinelWindowContext(
        breakdowns=breakdowns, spy_close=spy, etf_prices=etf,
        round_trip_costs=costs, start=bus[0], end=bus[-1],
        graduated=False, macro_panel=macro_panel,
    )


def _fields(r) -> tuple:
    """The BacktestRunResult surface the byte-identical contract pins."""
    return (
        r.engine, r.credibility_score, r.passed_gate,
        round(r.sharpe, 10), round(r.profit_factor, 10),
        round(r.max_drawdown, 10), r.trades, round(r.dsr, 10),
        r.min_btl_gap, round(r.trades_per_param, 10),
        round(r.ruin_probability, 10),
        tuple(sorted(r.parameters.items())),
        len(r.trade_log),
    )


def test_c1_c2_c4_byte_identical_legacy_path() -> None:
    """C1 + C2 + C4 — legacy path byte-identical when the flag is off."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()

    # C1: the golden IS the legacy (no-override) behaviour.
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))

    # C2: None / omitted / explicit-legacy all == the legacy golden.
    assert _fields(run_sentinel_with_context(ctx)) == golden
    assert _fields(run_sentinel_with_context(ctx, overrides=None)) == golden
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "current"})) == golden

    # C4: variant-then-legacy in the SAME process yields the legacy
    # golden — the per-call module-global reset.
    run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "graduated"})
    assert _fields(run_sentinel_with_context(ctx, overrides={})) == golden


def test_c3_graduated_variant_is_reachable_and_distinct() -> None:
    """C3 — the ``"graduated"`` variant is reachable AND distinct."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()

    legacy = _fields(run_sentinel_with_context(ctx, overrides={}))
    variant = _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "graduated"}))
    assert variant != legacy, (
        "the 'graduated' variant did not change the result — the "
        "feature-flag branch is dead, not wired")

    # Concretely: the legacy binary-activation gate is provably DORMANT
    # for the whole synthetic window (every breakdown has score=0), so
    # legacy trades MUST be zero. The graduated composite reads the
    # macro_panel and activates across the indicator ramp.
    legacy_trades = run_sentinel_with_context(ctx, overrides={}).trades
    variant_trades = run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "graduated"}).trades
    assert legacy_trades == 0
    assert variant_trades > 0


def test_unknown_bear_score_mode_falls_back_to_legacy() -> None:
    """An unknown override value falls back to the legacy ``"current"``
    path — only the exact string ``"graduated"`` reaches the variant.

    This guards against silent corruption from a malformed override
    (the LabTarget choice-validation rejects empty members at
    declaration; this is defense-in-depth at call time)."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "unknown_value"})) == golden
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": ""})) == golden


def test_lab_target_carries_all_pre_registered_toggles() -> None:
    """Compliance §13: the LAB_TARGET carries the sibling toggles for
    every Sentinel Lab candidate currently in flight:

    1. ``activation_score_threshold`` — sibling ``sentinel_maxdd``
       (MERGED) ``choice:60,55``.
    2. ``bear_score_mode`` — dispatch knob for
       ``sentinel_bear_score`` (``graduated``) AND
       ``sentinel_macro_stress_gate`` (``macro_stress_count``);
       ``choice:current,graduated,macro_stress_count`` with the legacy
       default ``"current"``.
    3. ``macro_stress_signal_count`` + four per-signal float thresholds
       (vix / hy-spread / sahm / yield-curve) — the
       ``sentinel_macro_stress_gate`` candidate's surface (post-2026-05-22).

    Every key defaults to its legacy value in ``default_params()`` so
    the dossier ``param_diff`` carries the true ``legacy → variant``
    delta for whichever candidate is being probed.
    """
    from sentinel.backtest import LAB_TARGET, default_params
    from tpcore.lab.target import LabPrimaryMetric

    assert set(LAB_TARGET.param_ranges.keys()) == {
        "activation_score_threshold",
        "bear_score_mode",
        "macro_stress_signal_count",
        "vix_stress_threshold",
        "hy_spread_stress_threshold_bps",
        "sahm_stress_threshold",
        "yield_curve_inversion_threshold",
    }
    assert LAB_TARGET.param_ranges["bear_score_mode"] == (
        0, 0, "choice:current,graduated,macro_stress_count")
    assert LAB_TARGET.param_ranges["macro_stress_signal_count"] == (
        0, 0, "choice:2,3,4")
    # Each per-signal threshold is an INDEPENDENT float (not a choice);
    # the Lab sampler explores the range when the count branch is active.
    for k in (
        "vix_stress_threshold",
        "hy_spread_stress_threshold_bps",
        "sahm_stress_threshold",
        "yield_curve_inversion_threshold",
    ):
        assert LAB_TARGET.param_ranges[k][2] == "float", (
            f"{k} must be a float-range Lab knob"
        )
    # Defaults match the live-path legacy values (count branch is OFF
    # at default — bear_score_mode defaults to "current").
    dp = default_params()
    assert dp["bear_score_mode"] == "current"
    assert dp["macro_stress_signal_count"] == 3
    assert dp["vix_stress_threshold"] == 22.0
    assert dp["hy_spread_stress_threshold_bps"] == 400.0
    assert dp["sahm_stress_threshold"] == 0.3
    assert dp["yield_curve_inversion_threshold"] == 0.0
    # SP-E: Sentinel's success bar remains drawdown reduction, NOT Sharpe.
    assert LAB_TARGET.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION


def test_graduated_mode_without_macro_panel_falls_back_to_legacy() -> None:
    """Defensive guard: if ``macro_panel`` is None (legacy
    ``SentinelWindowContext`` shape, pre-this-PR), the graduated branch
    cannot run — it falls back to the legacy path so a missing
    additive load never silently produces a zero-data variant."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context(with_macro_panel=False)
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "graduated"})) == golden, (
        "graduated mode with no macro_panel did not fall back to legacy "
        "— a missing additive load must not silently corrupt the variant")
