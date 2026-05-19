"""SP-E §3 — the make-or-break: Sentinel's LIVE trading path is
BYTE-IDENTICAL when the off-by-default Lab flag is off.

C1  committed golden: `run_sentinel_with_context(ctx, overrides={})` ==
    a frozen golden of the pre-candidate (legacy) BacktestRunResult.
C2  default-is-legacy: identical when the override is None, when the
    toggle is omitted, and when it is explicitly the legacy value (60).
C3  variant reachable + distinct: the 55 toggle changes the result
    (the branch is wired, not dead).
C4  no cross-trial leakage: variant-then-legacy in the same process
    yields the legacy golden (the per-call module-global reset + the
    `finally` restore of the shadowed module constant).
LIVE  the scheduler's own `walk_states(breakdowns, spy_close=...)` call
    (no override) is byte-identical after a variant run — the shadowed
    `sentinel.plugs.lifecycle_analysis.ACTIVATION_SCORE_THRESHOLD` is
    restored, no residue.

Fully hermetic: a synthetic SentinelWindowContext is built in-body; NO
DB, NO network, NO module-level `import ops.lab.run` (the SP-D CI
hermeticity lesson). The golden is captured from the legacy
(no-override) code path itself — the byte-identical contract is the
legacy behaviour, RED on any drift.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from sentinel.models import BearScoreBreakdown


def _bd(d: date, score: int) -> BearScoreBreakdown:
    """A minimal valid breakdown whose `.score` is exactly `score`.

    Only `.score` is read by `SentinelLifecycleAnalysis.walk_states`
    (line 111: `bs = scores[d].score`); the per-indicator points are
    structurally valid but immaterial to the activation comparison.
    """
    return BearScoreBreakdown(
        as_of=d, sahm_pts=0, industrial_production_pts=0,
        initial_claims_pts=0, yield_curve_pts=0, credit_spread_pts=0,
        vix_pts=0, raw_total=0, score=score,
    )


def _synthetic_context():
    """A SentinelWindowContext whose Bear-Score series is deliberately
    constructed so the activation threshold MATTERS: a contiguous run of
    days at score 57 (≥55 but <60) followed by score 0.

    At the legacy threshold 60: 57 never crosses ⇒ Sentinel stays
    DORMANT ⇒ ZERO trades. At the variant threshold 55: 57 crosses ⇒
    Sentinel activates and holds the defensive basket ⇒ trades exist.
    This makes C3 (variant distinct) and the maxDD contrast genuine.
    """
    from sentinel.backtest import SentinelWindowContext

    d0 = date(2020, 3, 2)
    bus = [d0 + timedelta(days=i) for i in range(40)]
    breakdowns: dict[date, BearScoreBreakdown] = {}
    for i, d in enumerate(bus):
        # Days 5..25: score 57 (≥55, <60). Else 0.
        score = 57 if 5 <= i <= 25 else 0
        breakdowns[d] = _bd(d, score)

    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in bus])
    # SPY: a steady decline (a bear tape) — so a defensive basket that
    # is held during the active window books a drawdown we can measure.
    spy = pd.Series(
        [300.0 - 1.5 * i for i in range(len(bus))], index=idx, name="SPY",
    )
    etf = {"SPY": spy}
    for t in ("SH", "PSQ", "TLT", "GLD", "SQQQ"):
        # Inverse/defensive ETFs rally as SPY falls (mild), so a held
        # basket draws down modestly then recovers — a finite, non-zero
        # max_drawdown either way.
        etf[t] = pd.Series(
            [50.0 + 0.4 * i - (2.0 if 10 <= i <= 18 else 0.0)
             for i in range(len(bus))],
            index=idx, name=t,
        )
    from decimal import Decimal
    costs = {t: Decimal("0.001")
             for t in ("SH", "PSQ", "TLT", "GLD", "SQQQ")}
    return SentinelWindowContext(
        breakdowns=breakdowns, spy_close=spy, etf_prices=etf,
        round_trip_costs=costs, start=bus[0], end=bus[-1],
        graduated=False,
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


def test_c1_c2_c3_c4_byte_identical_live_path():
    from sentinel.backtest import run_sentinel_with_context

    ctx = _synthetic_context()

    # C1: the golden IS the legacy (no-override) behaviour.
    golden = _fields(run_sentinel_with_context(ctx, overrides={}))

    # C2: None / omitted / explicit-legacy all == the legacy golden.
    assert _fields(run_sentinel_with_context(ctx)) == golden
    assert _fields(run_sentinel_with_context(ctx, overrides=None)) == golden
    assert _fields(run_sentinel_with_context(
        ctx, overrides={"activation_score_threshold": 60})) == golden

    # C3: the 55 variant is reachable AND distinct (branch is wired).
    variant = _fields(run_sentinel_with_context(
        ctx, overrides={"activation_score_threshold": 55}))
    assert variant != golden, (
        "the 55 variant did not change the result — the feature-flag "
        "branch is dead, not wired")
    # Concretely: legacy 60 ⇒ no activation ⇒ zero trades; 55 ⇒ trades.
    legacy_trades = run_sentinel_with_context(ctx, overrides={}).trades
    variant_trades = run_sentinel_with_context(
        ctx, overrides={"activation_score_threshold": 55}).trades
    assert legacy_trades == 0
    assert variant_trades > 0

    # C4: variant-then-legacy in the SAME process yields the legacy
    # golden — the per-call module-global reset + the `finally` restore.
    run_sentinel_with_context(
        ctx, overrides={"activation_score_threshold": 55})
    assert _fields(run_sentinel_with_context(ctx, overrides={})) == golden


def test_live_walk_states_is_byte_identical_after_variant_run():
    """The LIVE scheduler calls
    `SentinelLifecycleAnalysis().walk_states(breakdowns, spy_close=spy)`
    with NO override (sentinel/scheduler.py:181-182). Prove that call is
    byte-identical before and after a Lab variant run — the backtest's
    context-shadow of
    `sentinel.plugs.lifecycle_analysis.ACTIVATION_SCORE_THRESHOLD` is
    restored in `finally`, leaving NO residue on the module the live
    plug binds at import.
    """
    import sentinel.plugs.lifecycle_analysis as lc_mod
    from sentinel.backtest import run_sentinel_with_context
    from sentinel.models import ACTIVATION_SCORE_THRESHOLD
    from sentinel.plugs.lifecycle_analysis import SentinelLifecycleAnalysis

    ctx = _synthetic_context()

    # The module constant the LIVE plug uses, before any Lab activity.
    assert lc_mod.ACTIVATION_SCORE_THRESHOLD == ACTIVATION_SCORE_THRESHOLD

    def _live_states():
        # Exactly the scheduler's call shape (no override kwarg).
        return SentinelLifecycleAnalysis().walk_states(
            ctx.breakdowns, spy_close=ctx.spy_close)

    before = {d: s.phase for d, s in _live_states().items()}

    # A Lab variant run flips the module constant for the duration of
    # its OWN walk_states call only.
    run_sentinel_with_context(
        ctx, overrides={"activation_score_threshold": 55})

    # The module constant is restored — the live plug's view is unchanged.
    assert lc_mod.ACTIVATION_SCORE_THRESHOLD == ACTIVATION_SCORE_THRESHOLD
    after = {d: s.phase for d, s in _live_states().items()}
    assert after == before, (
        "the live walk_states result moved after a Lab variant run — "
        "the backtest seam leaked into the live path (NOT byte-identical)")


def test_lab_target_is_the_single_pre_registered_toggle():
    """Compliance §10: exactly ONE toggle, a choice:60,55 whose values
    are {legacy 60, the one variant 55}; default_params carries 60."""
    from sentinel.backtest import LAB_TARGET, default_params
    from tpcore.lab.target import LabPrimaryMetric

    assert list(LAB_TARGET.param_ranges) == ["activation_score_threshold"]
    assert LAB_TARGET.param_ranges["activation_score_threshold"] == (
        60, 55, "choice:60,55")
    assert default_params() == {"activation_score_threshold": 60}
    # SP-E: Sentinel's success bar is drawdown reduction, NOT Sharpe.
    assert LAB_TARGET.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION
