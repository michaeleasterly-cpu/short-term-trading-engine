"""Sigma — Plug 2: Lifecycle Analysis.

Given a ``SetupCandidate``, decide which phase the trade is in and produce
concrete entry / stop / take-profit prices per plan §4.1::

    Hard stop      = entry × (1 − 0.03)
    Take-profit 1  = mid-band   (50% scale-out)
    Take-profit 2  = upper band (final exit)

Phase rules (Phase 1, deterministic):
    sigma_score ≥ 70 and band_proximity ≤ 0.20  → ACTIVE        (enter now)
    sigma_score ≥ 70 and band_proximity ≤ 0.40  → APPROACHING   (close to entry)
    sigma_score ≥ 50                            → SETUP         (watch list)
    band_proximity ≥ 0.95                       → EXHAUSTION    (skip)
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import structlog

from sigma.models import (
    HARD_STOP_PCT,
    SCORE_STRONG,
    SCORE_WEAK,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


def _round_cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SigmaLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 of Sigma."""

    engine_name = "sigma"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {"hard_stop_pct": str(HARD_STOP_PCT)},
        }

    def assess(self, candidate: SetupCandidate) -> PhaseAssessment:
        phase = self._classify(candidate)
        entry = candidate.suggested_entry_price
        stop = _round_cents(entry * (Decimal("1") - HARD_STOP_PCT))
        return PhaseAssessment(
            ticker=candidate.ticker,
            as_of=candidate.as_of,
            phase=phase,
            entry_price=entry,
            stop_price=stop,
            take_profit_mid=_round_cents(candidate.bb_mid),
            take_profit_far=_round_cents(candidate.bb_upper),
            notes=f"score={candidate.sigma_score} prox={candidate.band_proximity:.3f} adx={candidate.adx}",
        )

    def handle_tier1_fill(
        self,
        assessment: PhaseAssessment,
        position_remaining: int,
    ) -> PhaseAssessment:
        """Record that the Tier 1 (mid-band) leg has filled.

        Returns a copy of ``assessment`` with ``tier1_filled=True`` and
        ``remaining_shares=position_remaining``. The phase stays ACTIVE while
        the Tier 2 leg is still open; it transitions to EXHAUSTION only once
        ``position_remaining`` is zero (Tier 2 also closed).
        """
        if position_remaining < 0:
            raise ValueError(f"position_remaining must be ≥ 0, got {position_remaining}")
        next_phase = Phase.EXHAUSTION if position_remaining == 0 else Phase.ACTIVE
        return assessment.model_copy(
            update={
                "phase": next_phase,
                "tier1_filled": True,
                "remaining_shares": position_remaining,
            }
        )

    @staticmethod
    def _classify(c: SetupCandidate) -> Phase:
        if c.band_proximity >= 0.95:
            return Phase.EXHAUSTION
        if c.sigma_score >= SCORE_STRONG and c.band_proximity <= 0.20:
            return Phase.ACTIVE
        if c.sigma_score >= SCORE_STRONG and c.band_proximity <= 0.40:
            return Phase.APPROACHING
        if c.sigma_score >= SCORE_WEAK:
            return Phase.SETUP
        return Phase.EXHAUSTION
