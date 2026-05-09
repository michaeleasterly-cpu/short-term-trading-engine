"""Reversion — Plug 2: Lifecycle Analysis.

Translates a ``SetupCandidate`` into a ``PhaseAssessment`` with concrete
entry/stop/target levels per plan §4.2:

    Hard stop      = entry × (1 − 0.08)   for LONG
                   = entry × (1 + 0.08)   for SHORT
    Tier 1 target  = 20-day MA  (75% scale-out)
    Tier 2 target  = 50-day MA  (25% remainder)

Plus the engine-specific gates:
    * Time stop after 5 trading days without touching the 20-day MA →
      EXHAUSTED. Tracked via ``bars_held``.
    * ADX > 25 → engine disabled (already filtered in setup_detection,
      but re-checked here so the lifecycle is robust to candidates
      that drift trending while in the queue).
    * Earnings-quality screen: ``tpcore.fundamentals.earnings_quality``
      must grade the candidate. ``LOW`` → trade suppressed
      (``earnings_quality_blocked=True``). The current implementation is
      a stub that raises ``NotImplementedError`` and depends on
      fundamentals data the live data adapter doesn't fetch yet — we
      catch the exception and pass through (allow), logging a warning
      so the gate becomes effective the moment the underlying ships.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

from reversion.models import (
    HARD_STOP_PCT,
    MAX_ADX_FOR_REVERSION,
    TIME_STOP_DAYS,
    Direction,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)

logger = structlog.get_logger(__name__)


def _round_cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _earnings_quality_blocked(symbol: str) -> bool:
    """Run the earnings-quality screen if the underlying is available.

    The shipped ``tpcore.fundamentals.earnings_quality.check_earnings_quality``
    raises ``NotImplementedError`` and depends on fundamentals data that
    the live data adapter doesn't fetch today (no FMP/EDGAR adapter
    wired yet). We deliberately call it anyway so the integration point
    exists — when the underlying is implemented, this gate becomes
    effective with no further engine change. Until then, log and allow.
    """
    try:
        from tpcore.fundamentals.earnings_quality import (
            EarningsQualityGrade,
            check_earnings_quality,
        )

        # We have no fundamentals provider plumbed yet — this call will
        # NotImplementedError, which is fine. Once the real adapter is
        # wired, replace these zeros with a fundamentals fetch.
        result = check_earnings_quality(
            net_income=Decimal("0"),
            fcf=Decimal("0"),
            total_assets=Decimal("0"),
            revenue=Decimal("0"),
            receivables=Decimal("0"),
            capex=Decimal("0"),
            fcf_history=[],
        )
        # NOTE: The plan brief calls this "overall_quality"; the actual
        # field on EarningsQualityResult is ``grade``.
        return result.grade is EarningsQualityGrade.LOW
    except NotImplementedError:
        logger.debug(
            "reversion.lifecycle.earnings_quality_unimplemented",
            symbol=symbol,
            note="allowing the trade — gate will activate when tpcore.fundamentals ships",
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "reversion.lifecycle.earnings_quality_failed",
            symbol=symbol,
            error=str(exc),
        )
        return False


class ReversionLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 of Reversion."""

    engine_name = "reversion"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {
                "hard_stop_pct": str(HARD_STOP_PCT),
                "time_stop_days": TIME_STOP_DAYS,
                "max_adx": MAX_ADX_FOR_REVERSION,
            },
        }

    def assess(self, candidate: SetupCandidate) -> PhaseAssessment:
        entry = candidate.suggested_entry_price
        if candidate.direction is Direction.LONG:
            stop = _round_cents(entry * (Decimal("1") - HARD_STOP_PCT))
        else:
            stop = _round_cents(entry * (Decimal("1") + HARD_STOP_PCT))

        # ADX gate is enforced upstream in setup_detection; re-check here.
        if candidate.adx_14 > MAX_ADX_FOR_REVERSION:
            phase = Phase.EXHAUSTED
        else:
            phase = Phase.ACTIVE

        blocked = _earnings_quality_blocked(candidate.ticker)
        if blocked:
            phase = Phase.EXHAUSTED

        return PhaseAssessment(
            ticker=candidate.ticker,
            as_of=candidate.as_of,
            direction=candidate.direction,
            phase=phase,
            entry_price=entry,
            stop_price=stop,
            target_20ma=_round_cents(candidate.target_20ma),
            target_50ma=_round_cents(candidate.target_50ma),
            earnings_quality_blocked=blocked,
            notes=(
                f"score={candidate.reversion_score} z={candidate.z_score:+.2f} "
                f"adx={candidate.adx_14}"
            ),
        )

    def handle_tier1_fill(
        self,
        assessment: PhaseAssessment,
        position_remaining: int,
    ) -> PhaseAssessment:
        """Record that the Tier 1 (20-day MA) leg has filled.

        Phase becomes REVERTING while Tier 2 runs; transitions to
        EXHAUSTED only when ``position_remaining`` is zero.
        """
        if position_remaining < 0:
            raise ValueError(f"position_remaining must be ≥ 0, got {position_remaining}")
        next_phase = Phase.EXHAUSTED if position_remaining == 0 else Phase.REVERTING
        return assessment.model_copy(
            update={
                "phase": next_phase,
                "tier1_filled": True,
                "remaining_shares": position_remaining,
            }
        )

    def advance_bar(self, assessment: PhaseAssessment, *, touched_20ma: bool) -> PhaseAssessment:
        """Bump ``bars_held`` and apply the 5-day time stop.

        Caller passes ``touched_20ma=True`` if today's bar reached the
        20-day MA (which resets the time-stop counter — the trade is
        progressing). If the bar count exceeds ``TIME_STOP_DAYS`` without
        ever touching the MA, the trade is force-exited (EXHAUSTED).
        """
        new_bars = 0 if touched_20ma else assessment.bars_held + 1
        next_phase = assessment.phase
        if new_bars >= TIME_STOP_DAYS and not assessment.tier1_filled:
            next_phase = Phase.EXHAUSTED
        return assessment.model_copy(update={"bars_held": new_bars, "phase": next_phase})
