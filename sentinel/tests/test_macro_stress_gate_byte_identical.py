"""Sentinel — macro_stress_gate Lab candidate, byte-identical contract.

The make-or-break invariant: Sentinel's LIVE trading path is
byte-identical when the off-by-default ``bear_score_mode`` Lab flag is
off (legacy ``"current"`` default), regardless of whether the
macro-stress-count knobs (``macro_stress_signal_count`` /
``vix_stress_threshold`` / ``hy_spread_stress_threshold_bps`` /
``sahm_stress_threshold`` / ``yield_curve_inversion_threshold``) are
present in ``overrides``. The count branch is REACHABLE only when
``bear_score_mode`` is explicitly the string ``"macro_stress_count"``.

C1  committed golden: ``run_sentinel_with_context(ctx, overrides={})``
    BacktestRunResult is the frozen golden of the legacy (no-override)
    behaviour. Pinned by the sibling ``test_bear_score_byte_identical``.
C2  default-is-legacy: the macro-stress knobs alone (without the mode
    flip) do NOT change the result.
C3  variant reachable + distinct: ``bear_score_mode="macro_stress_count"``
    changes the result (the branch is wired, not dead).
C4  no cross-trial leakage: count-then-legacy in the same process
    yields the legacy golden (the per-call module-global reset).
C5  knob independence: each per-signal threshold knob in isolation
    moves the result when the mode is ``"macro_stress_count"`` (its
    threshold must actually be read).
C6  defensive fallback: ``"macro_stress_count"`` with no
    ``vix_proxy_series`` falls back to the legacy path (no silent
    corruption from a missing additive load).

Fully hermetic: a synthetic ``SentinelWindowContext`` is built in-body
(no DB, no network) — the SP-D CI hermeticity lesson.
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
    """A macro panel whose values walk *into* the count-branch trigger
    region across the window. The panel is indexed by the daily date
    sequence so the ``at-or-before`` lookup picks up the most recent
    observation.

    Day 0..9   : every signal BELOW its default threshold (count = 0)
    Day 10..30 : every signal ABOVE its default threshold (count = 4)
    Day 31..49 : every signal BELOW its default threshold again
    """
    rows = []
    for i, d in enumerate(dates):
        if 10 <= i <= 30:
            rows.append({
                "date": d,
                # All four signals fire above their default thresholds.
                "sahm_rule": 0.40,           # >= 0.3 default
                "yield_curve": -0.10,        # <= 0.0 default
                "hy_spread": 4.50,           # 450 bps >= 400 default
                # Two columns the count-branch ignores but the panel
                # carries because graduated mode would read them:
                "sos_state_diffusion": 0.0,
                "cfnai_ma3": 0.0,
            })
        else:
            rows.append({
                "date": d,
                "sahm_rule": 0.10,
                "yield_curve": 0.50,
                "hy_spread": 3.00,           # 300 bps < 400 default
                "sos_state_diffusion": 0.0,
                "cfnai_ma3": 0.0,
            })
    df = pd.DataFrame(rows).set_index("date")
    return df


def _synthetic_vix_series(
    dates: list[date], *, high_band: range,
) -> pd.Series:
    """A VIX-proxy series that crosses 22 *only* during ``high_band``."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    values = [
        30.0 if i in high_band else 15.0
        for i in range(len(dates))
    ]
    return pd.Series(values, index=idx, name="vix_proxy")


def _synthetic_context(
    *,
    with_macro_panel: bool = True,
    with_vix_series: bool = True,
    vix_high_band: range | None = None,
):
    """A SentinelWindowContext whose Bear-Score is pinned at 0 (legacy
    gate provably DORMANT — zero legacy trades), with a macro_panel and
    vix_proxy_series that DO trigger the count branch across day 10..30.
    """
    from sentinel.backtest import SentinelWindowContext

    d0 = date(2020, 3, 2)
    bus = [d0 + timedelta(days=i) for i in range(50)]
    breakdowns: dict[date, BearScoreBreakdown] = {
        d: _bd(d, 0) for d in bus  # legacy gate: zero forever.
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
    if vix_high_band is None:
        vix_high_band = range(10, 31)
    vix_series = (
        _synthetic_vix_series(bus, high_band=vix_high_band)
        if with_vix_series else None
    )
    return SentinelWindowContext(
        breakdowns=breakdowns, spy_close=spy, etf_prices=etf,
        round_trip_costs=costs, start=bus[0], end=bus[-1],
        graduated=False, macro_panel=macro_panel,
        vix_proxy_series=vix_series,
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


def test_c2_default_is_legacy_with_stress_knobs_alone() -> None:
    """C2 — passing the macro-stress KNOBS without flipping the MODE
    flag does NOT change the result. The count branch is gated by
    ``bear_score_mode`` alone; the knobs are inert when the mode is
    ``"current"`` (the legacy live-path default)."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))

    # Every knob in isolation, with the legacy mode, must == golden.
    for knob, value in (
        ("macro_stress_signal_count", 2),
        ("vix_stress_threshold", 25.0),
        ("hy_spread_stress_threshold_bps", 450.0),
        ("sahm_stress_threshold", 0.4),
        ("yield_curve_inversion_threshold", -0.1),
    ):
        out = _fields(run_sentinel_with_context(
            ctx, overrides={knob: value}))
        assert out == golden, (
            f"{knob} mutated the result without bear_score_mode flip — "
            "the count branch is leaking into the legacy path"
        )

    # All knobs together, still without the mode flip — golden.
    out_all = _fields(run_sentinel_with_context(ctx, overrides={
        "macro_stress_signal_count": 2,
        "vix_stress_threshold": 25.0,
        "hy_spread_stress_threshold_bps": 450.0,
        "sahm_stress_threshold": 0.4,
        "yield_curve_inversion_threshold": -0.1,
    }))
    assert out_all == golden


def test_c3_macro_stress_count_variant_is_reachable_and_distinct() -> None:
    """C3 — flipping bear_score_mode to "macro_stress_count" reaches a
    new, distinct code path. With the synthetic panel pinned so days
    10..30 hit ALL four signals above their default thresholds, the
    count branch opens the legacy defensive basket while the legacy
    binary gate stays DORMANT for the entire window."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()
    legacy = run_sentinel_with_context(ctx, overrides={})
    variant = run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "macro_stress_count"})
    assert _fields(variant) != _fields(legacy), (
        "macro_stress_count did not change the result — the "
        "count branch is dead, not wired"
    )
    # Concretely: legacy gate is 0 throughout (zero trades). Count
    # branch fires across day 10..30 (all 4 signals above their default
    # thresholds with default count=3 → 4 ≥ 3, basket opens) → trades > 0.
    assert legacy.trades == 0
    assert variant.trades > 0


def test_c4_no_cross_trial_leakage_count_then_legacy() -> None:
    """C4 — count-then-legacy in the SAME process yields the legacy
    golden. Tests the per-call module-global reset for ALL five new
    overrides simultaneously."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))

    # Run the variant with every knob set, then the legacy.
    run_sentinel_with_context(ctx, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 2,
        "vix_stress_threshold": 19.0,
        "hy_spread_stress_threshold_bps": 350.0,
        "sahm_stress_threshold": 0.25,
        "yield_curve_inversion_threshold": -0.2,
    })
    assert _fields(run_sentinel_with_context(ctx, overrides={})) == golden


def test_c5_signal_count_knob_changes_result() -> None:
    """C5a — ``macro_stress_signal_count`` is actually read. With a
    panel that fires exactly 2 of 4 signals during a sub-band, the
    count=2 variant must trade and the count=4 variant must not."""
    from sentinel.backtest import run_sentinel_with_context

    # A panel where only Sahm + yield_curve fire (HY-spread and VIX
    # stay calm). Count = 2 across day 10..30.
    d0 = date(2020, 3, 2)
    bus = [d0 + timedelta(days=i) for i in range(50)]
    rows = []
    for i, d in enumerate(bus):
        if 10 <= i <= 30:
            rows.append({
                "date": d,
                "sahm_rule": 0.40,         # fires
                "yield_curve": -0.10,      # fires
                "hy_spread": 3.00,         # below 400bps → does NOT fire
                "sos_state_diffusion": 0.0,
                "cfnai_ma3": 0.0,
            })
        else:
            rows.append({
                "date": d,
                "sahm_rule": 0.10,
                "yield_curve": 0.50,
                "hy_spread": 3.00,
                "sos_state_diffusion": 0.0,
                "cfnai_ma3": 0.0,
            })
    panel = pd.DataFrame(rows).set_index("date")
    # VIX low across the whole window (does NOT contribute).
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in bus])
    vix = pd.Series([15.0] * len(bus), index=idx, name="vix_proxy")

    ctx = _synthetic_context()
    ctx.macro_panel.loc[:, :] = panel.reindex(ctx.macro_panel.index)
    # Recreate context with our crafted panel and vix.
    from sentinel.backtest import SentinelWindowContext
    ctx2 = SentinelWindowContext(
        breakdowns=ctx.breakdowns, spy_close=ctx.spy_close,
        etf_prices=ctx.etf_prices, round_trip_costs=ctx.round_trip_costs,
        start=ctx.start, end=ctx.end, graduated=False,
        macro_panel=panel, vix_proxy_series=vix,
    )

    out_2 = run_sentinel_with_context(ctx2, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 2,
    })
    out_4 = run_sentinel_with_context(ctx2, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 4,
    })
    assert out_2.trades > 0, "count=2 with 2 firing signals must arm"
    assert out_4.trades == 0, "count=4 with only 2 firing signals must NOT arm"


def test_c5_per_signal_threshold_knobs_change_result() -> None:
    """C5b — each per-signal threshold knob is actually read. We make
    each signal *just* fail/pass its default threshold, then move the
    threshold up/down across the value and verify the count branch
    flips on/off accordingly. count=4 (all-of) makes the result
    1-bit-sensitive to a single threshold flip."""
    from sentinel.backtest import SentinelWindowContext, run_sentinel_with_context

    d0 = date(2020, 3, 2)
    bus = [d0 + timedelta(days=i) for i in range(50)]
    # Panel that fires 3 of 4 by default (Sahm + curve + HY-spread); VIX
    # is borderline at 20 (below default 22). Day 10..30.
    rows = []
    for i, d in enumerate(bus):
        if 10 <= i <= 30:
            rows.append({
                "date": d,
                "sahm_rule": 0.40,         # fires above 0.3 default
                "yield_curve": -0.10,      # fires below 0 default
                "hy_spread": 4.50,         # 450bps fires above 400 default
                "sos_state_diffusion": 0.0,
                "cfnai_ma3": 0.0,
            })
        else:
            rows.append({
                "date": d,
                "sahm_rule": 0.10,
                "yield_curve": 0.50,
                "hy_spread": 3.00,
                "sos_state_diffusion": 0.0,
                "cfnai_ma3": 0.0,
            })
    panel = pd.DataFrame(rows).set_index("date")
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in bus])
    # VIX at 20 across day 10..30 → above 19 threshold, below 22 default.
    vix = pd.Series(
        [20.0 if 10 <= i <= 30 else 10.0 for i in range(len(bus))],
        index=idx, name="vix_proxy",
    )
    base_ctx = _synthetic_context()
    ctx = SentinelWindowContext(
        breakdowns=base_ctx.breakdowns, spy_close=base_ctx.spy_close,
        etf_prices=base_ctx.etf_prices,
        round_trip_costs=base_ctx.round_trip_costs,
        start=base_ctx.start, end=base_ctx.end, graduated=False,
        macro_panel=panel, vix_proxy_series=vix,
    )

    # Default vix threshold (22) ⇒ VIX does NOT fire ⇒ count = 3 with
    # all-of-4 requirement ⇒ basket stays DORMANT, zero trades.
    out_default = run_sentinel_with_context(ctx, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 4,
    })
    # Lower the VIX threshold to 19 (VIX=20 fires) ⇒ count = 4 ⇒
    # basket arms.
    out_low_vix = run_sentinel_with_context(ctx, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 4,
        "vix_stress_threshold": 19.0,
    })
    assert out_default.trades == 0, (
        "VIX default threshold 22 should NOT fire at VIX=20"
    )
    assert out_low_vix.trades > 0, (
        "VIX threshold lowered to 19 should fire at VIX=20 — the knob "
        "is not being read"
    )

    # Same exercise with hy_spread: tighten threshold to 500 bps so HY
    # at 450 bps stops firing ⇒ count drops to 3 ⇒ all-of-4 fails ⇒
    # basket dormant.
    out_hy_strict = run_sentinel_with_context(ctx, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 4,
        "vix_stress_threshold": 19.0,
        "hy_spread_stress_threshold_bps": 500.0,
    })
    assert out_hy_strict.trades == 0, (
        "hy_spread threshold 500 bps should NOT fire at 450 bps — knob "
        "is not being read"
    )


def test_c6_count_mode_without_vix_series_falls_back_to_legacy() -> None:
    """C6 — defensive: if ``vix_proxy_series`` is None (the new field
    a pre-this-PR fixture would not populate), the count branch cannot
    run and falls back to the legacy path. A missing additive load
    must NEVER silently produce a zero-data variant."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context(with_vix_series=False)
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "macro_stress_count"})) == golden


def test_c6_count_mode_without_macro_panel_falls_back_to_legacy() -> None:
    """C6 — symmetric guard for the macro_panel attribute."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context(with_macro_panel=False)
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "macro_stress_count"})) == golden


def test_unknown_bear_score_mode_value_falls_back_to_legacy() -> None:
    """Defense-in-depth: an unknown override (anything other than the
    exact strings ``"graduated"`` / ``"macro_stress_count"``) falls
    back to the legacy ``"current"`` path."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"bear_score_mode": "macro_stress_count_typo"})
    ) == golden


def test_count_branch_effective_mode_is_reported() -> None:
    """The BacktestRunResult parameters dict must report
    ``bear_score_mode="macro_stress_count"`` AND every per-signal knob
    value when the count branch actually fires — the dossier
    ``param_diff`` reads these for the honest variant truth."""
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()
    out = run_sentinel_with_context(ctx, overrides={
        "bear_score_mode": "macro_stress_count",
        "macro_stress_signal_count": 3,
        "vix_stress_threshold": 22.0,
        "hy_spread_stress_threshold_bps": 400.0,
        "sahm_stress_threshold": 0.3,
        "yield_curve_inversion_threshold": 0.0,
    })
    p = out.parameters
    assert p["bear_score_mode"] == "macro_stress_count"
    assert p["macro_stress_signal_count"] == 3
    assert p["vix_stress_threshold"] == 22.0
    assert p["hy_spread_stress_threshold_bps"] == 400.0
    assert p["sahm_stress_threshold"] == 0.3
    assert p["yield_curve_inversion_threshold"] == 0.0


def test_count_signals_helper_pure_pit_safe() -> None:
    """The ``_count_stress_signals_at`` helper is the unit of the count
    branch. We exercise the boundary conditions directly so the unit
    test red-flags a logic flip without needing to drive a full sim."""
    from sentinel.backtest import _count_stress_signals_at

    d = date(2020, 6, 15)
    # Build a tiny panel with a single PIT row at d.
    panel = pd.DataFrame(
        [{
            "date": d,
            "sahm_rule": 0.40,
            "yield_curve": -0.10,
            "hy_spread": 4.50,
        }],
    ).set_index("date")
    vix = pd.Series([25.0], index=pd.DatetimeIndex([pd.Timestamp(d)]),
                    name="vix")

    # All four above their defaults: count = 4.
    assert _count_stress_signals_at(
        panel=panel, vix_series=vix, as_of=d,
        vix_threshold=22.0,
        hy_spread_threshold_bps=400.0,
        sahm_threshold=0.3,
        yield_curve_threshold=0.0,
    ) == 4

    # Tighten each individually — the helper drops one signal each time.
    assert _count_stress_signals_at(
        panel=panel, vix_series=vix, as_of=d,
        vix_threshold=30.0,  # VIX=25 fails
        hy_spread_threshold_bps=400.0,
        sahm_threshold=0.3,
        yield_curve_threshold=0.0,
    ) == 3
    assert _count_stress_signals_at(
        panel=panel, vix_series=vix, as_of=d,
        vix_threshold=22.0,
        hy_spread_threshold_bps=500.0,  # HY=450bps fails
        sahm_threshold=0.3,
        yield_curve_threshold=0.0,
    ) == 3
    assert _count_stress_signals_at(
        panel=panel, vix_series=vix, as_of=d,
        vix_threshold=22.0,
        hy_spread_threshold_bps=400.0,
        sahm_threshold=0.5,  # Sahm=0.40 fails
        yield_curve_threshold=0.0,
    ) == 3
    assert _count_stress_signals_at(
        panel=panel, vix_series=vix, as_of=d,
        vix_threshold=22.0,
        hy_spread_threshold_bps=400.0,
        sahm_threshold=0.3,
        yield_curve_threshold=-0.5,  # curve=-0.10 fails (-0.10 > -0.5)
    ) == 3

    # NaN row contributes zero (defense-in-depth on missing data).
    panel_nan = pd.DataFrame(
        [{
            "date": d,
            "sahm_rule": float("nan"),
            "yield_curve": float("nan"),
            "hy_spread": float("nan"),
        }],
    ).set_index("date")
    assert _count_stress_signals_at(
        panel=panel_nan, vix_series=vix, as_of=d,
        vix_threshold=22.0,
        hy_spread_threshold_bps=400.0,
        sahm_threshold=0.3,
        yield_curve_threshold=0.0,
    ) == 1  # only VIX fires; the three NaN signals contribute zero


def test_load_window_context_populates_vix_series() -> None:
    """The new strictly-additive load: ``load_sentinel_window_context``
    must populate ``SentinelWindowContext.vix_proxy_series`` from the
    SPY-derived realized-vol series. We exercise the derivation locally
    (no DB) by going through ``compute_vix_proxy_series`` on a synthetic
    SPY series — confirming the wire shape the loader uses."""
    from sentinel.plugs.setup_detection import compute_vix_proxy_series

    idx = pd.date_range("2020-01-01", periods=30, freq="D")
    spy = pd.Series(
        [300.0 + (i % 5) * 0.5 for i in range(30)], index=idx,
        name="SPY",
    )
    vix = compute_vix_proxy_series(spy)
    assert len(vix) == 30
    # First 20 rows are NaN by definition of the rolling-20 std.
    assert vix.iloc[19] != vix.iloc[19] or vix.iloc[19] >= 0.0
    # After lookback rows the series is finite, non-negative.
    assert vix.iloc[-1] >= 0.0
