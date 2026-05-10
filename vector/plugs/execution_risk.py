"""Vector — Plug 3: Execution & Risk Sizing.

Per plan §4.3:

    Pre-grad cap:        $2,000 per position
    Max concurrent:      5
    Hard stop:           −7%
    Profit target:       +15% (market exit handled by lifecycle)
    Sizing scale (VIX):  VIX > 25 → 50% size; VIX > 30 → 25% size

Builds a single Alpaca *bracket* order (entry + take-profit limit + hard stop).
The trailing-stop logic stays client-side because Alpaca brackets don't
support an "arm at +X%" delay — we re-evaluate every session in
``LifecycleAnalysis``.

Returns ``None`` rather than raising when the candidate doesn't qualify
(below score floor, account too constrained, etc.); the order manager
acts on None as "no trade".
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug
from vector.models import (
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    SCORE_WEAK,
    VIX_SCALE_DOWN_25,
    VIX_SCALE_DOWN_50,
    ExecutionDecision,
    PhaseAssessment,
    SetupCandidate,
)

logger = structlog.get_logger(__name__)


class VectorExecutionRisk(BaseEnginePlug):
    """Plug 3 of Vector."""

    engine_name = "vector"

    def __init__(
        self,
        *,
        max_position_usd: Decimal = PRE_GRAD_POSITION_CAP_USD,
        max_positions: int = MAX_CONCURRENT_POSITIONS,
        score_floor: int = SCORE_WEAK,
    ) -> None:
        self._max_position_usd = max_position_usd
        self._max_positions = max_positions
        self._score_floor = score_floor

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "execution_risk",
            "ok": True,
            "details": {
                "max_position_usd": str(self._max_position_usd),
                "max_positions": self._max_positions,
            },
        }

    def decide(
        self,
        candidate: SetupCandidate,
        assessment: PhaseAssessment,
        *,
        account_equity: Decimal,
        open_positions: int,
    ) -> ExecutionDecision | None:
        """Score-gate, sizing-gate, and concurrency-gate the candidate; return a sized decision or None."""
        if candidate.swing_score < self._score_floor:
            logger.info(
                "vector.exec.reject_below_floor",
                ticker=candidate.ticker,
                score=candidate.swing_score,
            )
            return None
        if open_positions >= self._max_positions:
            logger.info(
                "vector.exec.reject_position_count",
                ticker=candidate.ticker,
                open_positions=open_positions,
            )
            return None

        size_factor = self._vix_size_factor(candidate.vix_at_entry)
        if assessment.early_cut_applied:
            # Position-reduction signal flowed through from lifecycle — halve again.
            size_factor *= Decimal("0.5")

        notional = (self._max_position_usd * size_factor).quantize(Decimal("0.01"))
        if notional <= 0:
            return None
        if notional > account_equity:
            notional = account_equity.quantize(Decimal("0.01"))
        qty = int((notional / candidate.last_close).to_integral_value(rounding=ROUND_DOWN))
        if qty <= 0:
            logger.info(
                "vector.exec.reject_zero_qty",
                ticker=candidate.ticker,
                notional=str(notional),
                last_close=str(candidate.last_close),
            )
            return None

        actual_notional = (Decimal(qty) * candidate.last_close).quantize(Decimal("0.01"))
        risk_amount = (Decimal(qty) * (assessment.entry_price - assessment.stop_price)).quantize(
            Decimal("0.01")
        )

        client_order_id = self._client_order_id(candidate.ticker)
        order_payload = self._bracket_order(
            ticker=candidate.ticker,
            qty=qty,
            stop_price=assessment.stop_price,
            target_price=assessment.profit_target_price,
            client_order_id=client_order_id,
        )
        return ExecutionDecision(
            ticker=candidate.ticker,
            qty=qty,
            notional_usd=actual_notional,
            risk_amount_usd=risk_amount,
            vix_size_factor=size_factor,
            order_payloads=[order_payload],
            constructed_at=datetime.now(UTC),
        )

    @staticmethod
    def _vix_size_factor(vix: float | None) -> Decimal:
        """Plan §4.3 size scaling: low VIX 1.0; >25 0.5; >30 0.25."""
        if vix is None:
            return Decimal("1.0")
        v = Decimal(str(vix))
        if v > VIX_SCALE_DOWN_25:
            return Decimal("0.25")
        if v > VIX_SCALE_DOWN_50:
            return Decimal("0.50")
        return Decimal("1.0")

    @staticmethod
    def _client_order_id(ticker: str) -> str:
        """Stable prefix the order manager keys assessments + AARs by.

        Format: ``vector_{TICKER}_{epoch}``. The epoch ensures uniqueness
        across same-day re-entries (rare but possible on volatile names).
        """
        return f"vector_{ticker}_{int(datetime.now(UTC).timestamp())}"

    @staticmethod
    def _bracket_order(
        *,
        ticker: str,
        qty: int,
        stop_price: Decimal,
        target_price: Decimal,
        client_order_id: str,
    ) -> dict:
        """Alpaca bracket-order payload — entry + TP + SL in one shot."""
        return {
            "client_order_id": client_order_id,
            "symbol": ticker,
            "qty": qty,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{target_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }


__all__ = ["VectorExecutionRisk"]
