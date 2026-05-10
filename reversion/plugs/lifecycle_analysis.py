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
    * Earnings-quality screen: caller passes a ``fundamentals`` dict
      from ``tpcore.fmp.FMPFundamentalsAdapter``; we run
      ``check_earnings_quality`` against it. Only ``HIGH`` passes —
      ``MEDIUM`` and ``LOW`` are both suppressed (``earnings_quality_blocked
      =True``). **No fundamentals → no trade.** The HIGH-only policy
      was set after the 2018–2025 backtest showed only HIGH-grade
      trades were profitable. ``DataProviderOutage`` from the adapter
      propagates so the scheduler can decide whether the whole scan
      should bail.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog

from reversion.models import (
    HARD_STOP_PCT,
    MAX_ADX_FOR_REVERSION,
    TIME_STOP_DAYS,
    Direction,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)
from tpcore.fundamentals.earnings_quality import (
    EarningsQualityGrade,
    EarningsQualityResult,
    check_earnings_quality,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


def _round_cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _evaluate_earnings_quality(
    fundamentals: dict[str, Any] | None,
    *,
    symbol: str,
) -> tuple[bool, EarningsQualityResult | None]:
    """Run the earnings-quality screen and return ``(blocked, result)``.

    No fundamentals dict → blocked (no data, no trade).
    HIGH grade → not blocked. MEDIUM and LOW → blocked.

    The HIGH-only policy was set after the 2018–2025 backtest showed
    HIGH-grade trades produced PF 1.39 / +0.87% avg while MEDIUM and
    LOW combined averaged −1.65%. See ``backtests/reversion_diagnosis.txt``
    and master plan §4.2.
    """
    if fundamentals is None:
        logger.warning(
            "reversion.lifecycle.no_fundamentals",
            symbol=symbol,
            note="trade suppressed — no fundamentals data available for the gate",
        )
        return True, None
    result = check_earnings_quality(fundamentals)
    blocked = result.grade is not EarningsQualityGrade.HIGH
    logger.info(
        "reversion.lifecycle.earnings_quality",
        symbol=symbol,
        grade=result.grade.value,
        fcf_to_ni=str(result.fcf_to_ni_ratio) if result.fcf_to_ni_ratio is not None else None,
        accruals=str(result.accruals_ratio) if result.accruals_ratio is not None else None,
        notes=result.notes,
        blocked=blocked,
    )
    return blocked, result


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

    def assess(
        self,
        candidate: SetupCandidate,
        *,
        fundamentals: dict[str, Any] | None = None,
    ) -> PhaseAssessment:
        """Build a ``PhaseAssessment`` for ``candidate``.

        ``fundamentals`` is the dict from
        ``tpcore.fmp.FMPFundamentalsAdapter.get_quarterly_fundamentals``.
        When ``None`` (e.g. live adapter unreachable, or test fixture
        without one), the earnings-quality gate fires and the trade is
        suppressed — "no data, no trade" per the plan policy.
        """
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

        blocked, eq_result = _evaluate_earnings_quality(fundamentals, symbol=candidate.ticker)
        if blocked:
            phase = Phase.EXHAUSTED

        eq_grade = eq_result.grade.value if eq_result is not None else "no_data"
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
                f"score={candidate.fade_score} z={candidate.z_score:+.2f} "
                f"adx={candidate.adx_14} eq={eq_grade}"
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
