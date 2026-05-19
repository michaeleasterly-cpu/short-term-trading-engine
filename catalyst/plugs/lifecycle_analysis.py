"""Catalyst — Plug 2: Lifecycle Analysis.

Builds the :class:`PhaseAssessment` for a fresh setup, and the
post-fill / per-bar phase transitions for an open trade. The mechanics
mirror Vector's flat-bracket pattern: ENTRY → HOLDING → EXIT, with an
EARLY_CUT if the close drops below the 10-SMA in days 1-3.

Pure (no DB, no broker). The scheduler/order-manager feeds in bar data
and caches the assessment; this plug just maps inputs → next phase.
"""
from __future__ import annotations

from datetime import date as date_t
from decimal import Decimal

import structlog

from catalyst.models import (
    HARD_STOP_PCT,
    PROFIT_TARGET_PCT,
    TRAILING_STOP_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)

_EARLY_CUT_MAX_DAYS = 3


class CatalystLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 — flat-bracket lifecycle (entry/hold/early-cut/exit)."""

    engine_name = "catalyst"

    def validate_dependencies(self) -> bool:
        return (
            HARD_STOP_PCT > Decimal("0")
            and PROFIT_TARGET_PCT > Decimal("0")
            and TRAILING_STOP_PCT > Decimal("0")
        )

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {
                "hard_stop_pct": str(HARD_STOP_PCT),
                "profit_target_pct": str(PROFIT_TARGET_PCT),
                "trailing_stop_pct": str(TRAILING_STOP_PCT),
            },
        }

    def assess_entry(self, candidate: SetupCandidate) -> PhaseAssessment:
        """Build the initial ENTRY-phase assessment from a fresh candidate.

        Entry price = the candidate's last close. Stop = entry × (1−HSP).
        Target = entry × (1+PTP). Trailing stop arms later (in
        :meth:`update_phase`) once the close has reached the trigger.
        """
        entry = candidate.last_close
        return PhaseAssessment(
            ticker=candidate.ticker,
            as_of=candidate.as_of,
            phase=Phase.ENTRY,
            entry_price=entry,
            stop_price=(entry * (Decimal("1") - HARD_STOP_PCT)).quantize(
                Decimal("0.0001")),
            profit_target_price=(
                entry * (Decimal("1") + PROFIT_TARGET_PCT)
            ).quantize(Decimal("0.0001")),
            days_held=0,
            trailing_armed=False,
            trail_high_water=entry,
        )

    def update_phase(
        self,
        prior: PhaseAssessment,
        *,
        as_of: date_t,
        close: Decimal,
        sma_10: Decimal | None = None,
    ) -> PhaseAssessment:
        """Map (prior, today's close, 10-SMA) → next PhaseAssessment.

        Pure. The scheduler feeds in today's bar; this returns the new
        assessment the order manager persists for the next bar.
        """
        days_held = prior.days_held + 1
        entry = prior.entry_price

        # Exit on profit-target or hard stop hit on close (intra-bar
        # fills are handled by the broker bracket; this is the
        # close-of-bar reconciliation).
        if close >= prior.profit_target_price:
            return prior.model_copy(update={"phase": Phase.EXIT,
                                            "as_of": as_of,
                                            "days_held": days_held})
        if close <= prior.stop_price:
            return prior.model_copy(update={"phase": Phase.EXIT,
                                            "as_of": as_of,
                                            "days_held": days_held})

        # Trailing-stop logic: arm when close ≥ entry × (1+trigger);
        # once armed, track the high-water close and trip the trail if
        # close drops > TRAILING_STOP_PCT below it.
        trigger = entry * (Decimal("1") + TRAILING_STOP_TRIGGER_PCT)
        trail_high = prior.trail_high_water or entry
        if close > trail_high:
            trail_high = close
        trailing_armed = prior.trailing_armed or (close >= trigger)
        if trailing_armed:
            trail_floor = trail_high * (Decimal("1") - TRAILING_STOP_PCT)
            if close <= trail_floor:
                return prior.model_copy(update={
                    "phase": Phase.EXIT,
                    "as_of": as_of,
                    "days_held": days_held,
                    "trailing_armed": True,
                    "trail_high_water": trail_high,
                })

        # Early-cut: in the first 3 days, a close below the 10-SMA
        # forces a reduce-50% cut (flag-only here; sizing is applied by
        # the execution-risk plug downstream).
        early_cut = prior.early_cut_applied
        if (
            not early_cut
            and days_held <= _EARLY_CUT_MAX_DAYS
            and sma_10 is not None
            and close < sma_10
        ):
            early_cut = True
            return prior.model_copy(update={
                "phase": Phase.EARLY_CUT,
                "as_of": as_of,
                "days_held": days_held,
                "trailing_armed": trailing_armed,
                "trail_high_water": trail_high,
                "early_cut_applied": True,
            })

        return prior.model_copy(update={
            "phase": Phase.HOLDING,
            "as_of": as_of,
            "days_held": days_held,
            "trailing_armed": trailing_armed,
            "trail_high_water": trail_high,
            "early_cut_applied": early_cut,
        })


__all__ = ["CatalystLifecycleAnalysis"]
