"""Vector — Plug 2: Lifecycle Analysis.

Translate a ``SetupCandidate`` into a ``PhaseAssessment`` with concrete
entry / stop / target levels per plan §4.3:

    Hard stop          = entry × (1 − 0.07)
    Profit target      = entry × (1 + 0.15)
    Trailing stop      = arm at +10% from entry, exit at −5% from peak

Phase rules:
    days_held in [0, 3) AND close < 10-MA  → EARLY_CUT (size reduced 50% by execution layer)
    days_held in [0, 3)                    → ENTRY  (validating)
    days_held >= 3                         → HOLDING
    target/trail/stop hit                  → EXIT
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

from vector.models import (
    HARD_STOP_PCT,
    PROFIT_TARGET_PCT,
    TRAILING_STOP_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)

logger = structlog.get_logger(__name__)

EARLY_CUT_WINDOW_DAYS = 3


def _round_cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class VectorLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 of Vector."""

    engine_name = "vector"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {
                "hard_stop_pct": str(HARD_STOP_PCT),
                "profit_target_pct": str(PROFIT_TARGET_PCT),
            },
        }

    def assess(self, candidate: SetupCandidate) -> PhaseAssessment:
        """Initial assessment — phase=ENTRY, levels off the candidate's last close."""
        entry = candidate.last_close
        return PhaseAssessment(
            ticker=candidate.ticker,
            as_of=candidate.as_of,
            phase=Phase.ENTRY,
            entry_price=entry,
            stop_price=_round_cents(entry * (Decimal("1") - HARD_STOP_PCT)),
            profit_target_price=_round_cents(entry * (Decimal("1") + PROFIT_TARGET_PCT)),
            days_held=0,
            notes=(
                f"score={candidate.vector_score:.0f} "
                f"trigger={candidate.pullback_or_breakout} "
                f"vix={candidate.vix_at_entry}"
            ),
        )

    def step(
        self,
        prev: PhaseAssessment,
        *,
        today_close: Decimal,
        today_sma_10: Decimal,
    ) -> PhaseAssessment:
        """Advance one trading day; recompute phase and trail state.

        Caller passes today's close + 10-MA so we can detect the early-cut
        condition without hard-coupling to a price-feed import.
        """
        days_held = prev.days_held + 1

        # Update high-water mark for the trailing stop.
        new_high = (
            today_close if (prev.trail_high_water is None or today_close > prev.trail_high_water)
            else prev.trail_high_water
        )
        # Arm trailing stop the first time close >= entry × (1 + trigger).
        trailing_armed = prev.trailing_armed or (
            today_close >= prev.entry_price * (Decimal("1") + TRAILING_STOP_TRIGGER_PCT)
        )

        # Phase resolution. Order matters: a hard stop or target *always*
        # forces an EXIT, even within the early-cut window — early-cut is
        # a position-reduction signal, not an exit override.
        early_cut_applied = prev.early_cut_applied
        if self._exit_condition(prev, today_close, new_high, trailing_armed):
            phase = Phase.EXIT
        elif (
            not early_cut_applied
            and days_held <= EARLY_CUT_WINDOW_DAYS
            and today_close < today_sma_10
        ):
            phase = Phase.EARLY_CUT
            early_cut_applied = True
        elif days_held < EARLY_CUT_WINDOW_DAYS:
            phase = Phase.ENTRY
        else:
            phase = Phase.HOLDING

        return prev.model_copy(
            update={
                "phase": phase,
                "days_held": days_held,
                "trail_high_water": new_high,
                "trailing_armed": trailing_armed,
                "early_cut_applied": early_cut_applied,
            }
        )

    @staticmethod
    def _exit_condition(
        prev: PhaseAssessment,
        today_close: Decimal,
        high_water: Decimal,
        trailing_armed: bool,
    ) -> bool:
        """True iff target/trail/stop is hit."""
        # Target.
        if today_close >= prev.profit_target_price:
            return True
        # Hard stop.
        if today_close <= prev.stop_price:
            return True
        # Trailing stop, only after armed.
        if trailing_armed:
            trail_floor = high_water * (Decimal("1") - TRAILING_STOP_PCT)
            if today_close <= trail_floor:
                return True
        return False


__all__ = ["VectorLifecycleAnalysis", "EARLY_CUT_WINDOW_DAYS"]
