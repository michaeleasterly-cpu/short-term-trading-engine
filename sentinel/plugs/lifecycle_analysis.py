"""Sentinel — Plug 2: Lifecycle Analysis (phase state machine).

Walks the Bear Score time series and produces per-day :class:`SentinelState`
snapshots. The state machine has five phases:

    DORMANT  ──score≥60──→  WATCH
    WATCH    ──3 days≥60 with no SPY>5% rally──→  ACTIVE
    WATCH    ──score<60──→  DORMANT                       (no trade)
    WATCH    ──>10 days without confirming──→  DORMANT     (false signal)
    ACTIVE   ──score<60──→  FADING
    FADING   ──5 trading days──→  EXITED
    FADING   ──score≥60──→  ACTIVE                         (re-entry)
    EXITED   ──score≥60──→  WATCH                          (new cycle)

The ``fade_factor`` rises from 0 → 1 over ``DEACTIVATION_FADE_DAYS``
(default 5 trading days) once in FADING, with an immediate first-step
reduction of ``DEACTIVATION_REDUCE_PCT`` (50%) per the spec. Execution
multiplies the basket weights by ``(1 - fade_factor)`` at order time.

The plug also computes the override flags consumed by ExecutionRisk:

* ``shallow_recession_override`` — True when ACTIVE and Bear Score < 80.
* ``vix_circuit_breaker`` — True when ACTIVE and VIX proxy > 40.
* ``sqqq_eligible`` — True when Bear Score ≥ 80 AND VIX proxy ≥ 30.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date as date_t
from decimal import Decimal

import pandas as pd
import structlog

from sentinel.models import (
    ACTIVATION_CONSECUTIVE_DAYS,
    ACTIVATION_RALLY_VETO_PCT,
    ACTIVATION_SCORE_THRESHOLD,
    DEACTIVATION_FADE_DAYS,
    DEACTIVATION_REDUCE_PCT,
    DEEP_RECESSION_SCORE_THRESHOLD,
    FALSE_SIGNAL_WINDOW_DAYS,
    SQQQ_ELIGIBLE_BEAR_SCORE,
    SQQQ_ELIGIBLE_VIX_THRESHOLD,
    VIX_CIRCUIT_BREAKER_THRESHOLD,
    BearScoreBreakdown,
    SentinelPhase,
    SentinelState,
)
from sentinel.plugs.setup_detection import (
    compute_spy_rally_pct,
    compute_vix_proxy_series,
)

logger = structlog.get_logger(__name__)


class SentinelLifecycleAnalysis:
    """State machine over the daily Bear Score series.

    Stateless — the full state for day ``t`` is derivable from the
    history of scores ``[0..t]`` plus the SPY series, so backtest and
    live can both call :meth:`walk_states` with their respective data
    slices.
    """

    def walk_states(
        self,
        scores: Mapping[date_t, BearScoreBreakdown],
        *,
        spy_close: pd.Series,
    ) -> dict[date_t, SentinelState]:
        """Replay the state machine across every date in ``scores``.

        Returns a dict keyed by date with one :class:`SentinelState` per
        entry. The list is iterated in sorted-date order; the caller
        sees the same ordering. ``spy_close`` must cover the same date
        range (used for the rally veto + VIX proxy).
        """
        sorted_dates = sorted(scores.keys())
        if not sorted_dates:
            return {}

        vix_proxy = compute_vix_proxy_series(spy_close)

        states: dict[date_t, SentinelState] = {}
        phase = SentinelPhase.DORMANT
        consecutive = 0
        days_in_phase = 0
        cycle_id: int | None = None
        next_cycle_id = 1
        fade_step = 0  # number of FADING days elapsed
        sqqq_days_held = 0
        watch_streak_days = 0  # how long we've been in WATCH without confirming

        for d in sorted_dates:
            bs = scores[d].score
            score_ok = bs >= ACTIVATION_SCORE_THRESHOLD
            consecutive = consecutive + 1 if score_ok else 0

            vix_now = self._vix_at(vix_proxy, d)
            shallow = False
            vix_break = False
            sqqq_elig = False
            spy_rally = Decimal("0")
            fade_factor = Decimal("0")

            # Transition logic — current phase determines next phase.
            if phase == SentinelPhase.DORMANT:
                if score_ok:
                    phase = SentinelPhase.WATCH
                    days_in_phase = 0
                    cycle_id = next_cycle_id
                    next_cycle_id += 1
                    watch_streak_days = 1
                else:
                    watch_streak_days = 0
            elif phase == SentinelPhase.WATCH:
                watch_streak_days += 1
                if not score_ok:
                    # False-signal short circuit — drop straight back.
                    phase = SentinelPhase.DORMANT
                    days_in_phase = 0
                    cycle_id = None
                    consecutive = 0
                    watch_streak_days = 0
                elif consecutive >= ACTIVATION_CONSECUTIVE_DAYS:
                    # Rally veto — refuse activation if SPY rallied >5% in window.
                    spy_rally = compute_spy_rally_pct(
                        spy_close, window_end=d, window_days=ACTIVATION_CONSECUTIVE_DAYS,
                    )
                    if spy_rally > ACTIVATION_RALLY_VETO_PCT:
                        # Stay WATCH; activation deferred until rally cools.
                        pass
                    else:
                        phase = SentinelPhase.ACTIVE
                        days_in_phase = 0
                        fade_step = 0
                        sqqq_days_held = 0
                elif watch_streak_days > FALSE_SIGNAL_WINDOW_DAYS:
                    # Crossed but never confirmed — full exit, new cycle on next cross.
                    phase = SentinelPhase.DORMANT
                    days_in_phase = 0
                    cycle_id = None
                    consecutive = 0
                    watch_streak_days = 0
            elif phase == SentinelPhase.ACTIVE:
                shallow = bs < DEEP_RECESSION_SCORE_THRESHOLD
                vix_break = vix_now is not None and vix_now > VIX_CIRCUIT_BREAKER_THRESHOLD
                sqqq_elig = (
                    bs >= SQQQ_ELIGIBLE_BEAR_SCORE
                    and vix_now is not None
                    and vix_now >= SQQQ_ELIGIBLE_VIX_THRESHOLD
                )
                sqqq_days_held = sqqq_days_held + 1 if sqqq_elig else 0
                if not score_ok:
                    phase = SentinelPhase.FADING
                    days_in_phase = 0
                    fade_step = 0
                    fade_factor = DEACTIVATION_REDUCE_PCT  # immediate 50% reduction
                    sqqq_elig = False
                    sqqq_days_held = 0
            elif phase == SentinelPhase.FADING:
                # Re-entry: a new score ≥ 60 during fade lifts back to ACTIVE.
                if score_ok:
                    phase = SentinelPhase.ACTIVE
                    days_in_phase = 0
                    fade_step = 0
                    fade_factor = Decimal("0")
                else:
                    fade_step += 1
                    # Linear scale-out from REDUCE_PCT to 1.0 over FADE_DAYS.
                    remaining = max(0, DEACTIVATION_FADE_DAYS - fade_step)
                    if remaining == 0:
                        fade_factor = Decimal("1")
                        phase = SentinelPhase.EXITED
                        days_in_phase = 0
                    else:
                        fade_factor = DEACTIVATION_REDUCE_PCT + (
                            (Decimal("1") - DEACTIVATION_REDUCE_PCT)
                            * Decimal(fade_step)
                            / Decimal(DEACTIVATION_FADE_DAYS)
                        )
            elif phase == SentinelPhase.EXITED:
                if score_ok:
                    phase = SentinelPhase.WATCH
                    days_in_phase = 0
                    cycle_id = next_cycle_id
                    next_cycle_id += 1
                    watch_streak_days = 1
                else:
                    cycle_id = None

            days_in_phase += 1
            states[d] = SentinelState(
                as_of=d,
                phase=phase,
                bear_score=bs,
                consecutive_days_above_threshold=consecutive,
                days_in_phase=days_in_phase,
                cycle_id=cycle_id,
                shallow_recession_override=shallow,
                vix_circuit_breaker=vix_break,
                sqqq_eligible=sqqq_elig,
                sqqq_days_held=sqqq_days_held,
                spy_rally_pct_in_window=spy_rally,
                fade_factor=fade_factor,
            )

        return states

    @staticmethod
    def _vix_at(vix_proxy: pd.Series, as_of: date_t) -> Decimal | None:
        if len(vix_proxy) == 0:
            return None
        sub = vix_proxy.loc[vix_proxy.index <= pd.Timestamp(as_of)].dropna()
        if len(sub) == 0:
            return None
        return Decimal(str(round(float(sub.iloc[-1]), 6)))


__all__ = ["SentinelLifecycleAnalysis"]
