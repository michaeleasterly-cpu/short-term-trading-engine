"""Reversion — Plug 3: Execution & Risk Scaling.

Per plan §4.2, the engine fades both directions:
    LONG (oversold fade)   → BUY entry, SELL exits at the 20-day MA / 50-day MA.
    SHORT (overbought fade) → SELL_SHORT entry, BUY_TO_COVER exits at the MAs.

Two-tier scale-out:
    Tier 1 (75%) — bracket order: TP at the 20-day MA, SL at the hard stop.
    Tier 2 (25%) — GTC limit at the 50-day MA.

Both legs share a ``{ticker}_{ts}`` ``client_order_id`` prefix with
``_tier1`` / ``_tier2`` suffixes so the order manager can group them.
Stop-cancellation protocol same as Sigma — Alpaca auto-cancels the
bracket SL when Tier 1 fills; the operator/order manager must cancel the
standalone Tier 2 limit if the hard stop trips before Tier 1 fills.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import structlog

from reversion.models import (
    HARD_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    Direction,
    ExecutionDecision,
    Phase,
    PhaseAssessment,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)

DEFAULT_ACCOUNT_CAPITAL = Decimal("10000")
PER_TRADE_EQUITY_FRACTION = Decimal("0.20")  # cap = min($2000, 20% of equity).
TIER1_FRACTION = Decimal("0.75")


class SizingError(Exception):
    """Raised when no valid position size can be computed (e.g. price ≤ 0)."""


class ReversionExecutionRisk(BaseEnginePlug):
    """Plug 3 of Reversion."""

    engine_name = "reversion"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "execution_risk",
            "ok": True,
            "details": {
                "pre_grad_cap_usd": str(PRE_GRAD_POSITION_CAP_USD),
                "max_positions": MAX_CONCURRENT_POSITIONS,
                "tier1_fraction": str(TIER1_FRACTION),
            },
        }

    def decide(
        self,
        assessment: PhaseAssessment,
        account_capital: Decimal = DEFAULT_ACCOUNT_CAPITAL,
        open_positions: int = 0,
        *,
        allow_shorts: bool = False,
    ) -> ExecutionDecision | None:
        """Build the two payloads. Returns ``None`` if the trade is gated out.

        ``allow_shorts`` lets the caller (the scheduler) clamp the engine to
        long-only on Alpaca paper, where short-borrow availability is
        per-symbol and unstable.
        """
        if assessment.phase is not Phase.ACTIVE:
            logger.debug(
                "reversion.exec.skip_non_active",
                ticker=assessment.ticker,
                phase=assessment.phase,
            )
            return None
        if assessment.earnings_quality_blocked:
            logger.info(
                "reversion.exec.earnings_quality_blocked", ticker=assessment.ticker
            )
            return None
        if open_positions >= MAX_CONCURRENT_POSITIONS:
            logger.info(
                "reversion.exec.position_cap_hit",
                ticker=assessment.ticker,
                open_positions=open_positions,
                cap=MAX_CONCURRENT_POSITIONS,
            )
            return None
        if assessment.direction is Direction.SHORT and not allow_shorts:
            logger.info(
                "reversion.exec.short_disabled", ticker=assessment.ticker
            )
            return None

        if assessment.entry_price <= 0:
            raise SizingError(f"non-positive entry price for {assessment.ticker}")

        notional_cap = min(
            PRE_GRAD_POSITION_CAP_USD,
            (account_capital * PER_TRADE_EQUITY_FRACTION).quantize(Decimal("0.01")),
        )
        qty = int((notional_cap / assessment.entry_price).to_integral_value(rounding=ROUND_DOWN))
        if qty < 4:
            # Need at least 4 shares so a 75/25 split gives ≥ 1 share each tier.
            logger.info(
                "reversion.exec.qty_below_four_shares",
                ticker=assessment.ticker,
                qty=qty,
                cap=str(notional_cap),
            )
            return None

        tier1_qty = int((Decimal(qty) * TIER1_FRACTION).to_integral_value(rounding=ROUND_DOWN))
        tier2_qty = qty - tier1_qty
        if tier1_qty < 1 or tier2_qty < 1:
            return None

        notional = (assessment.entry_price * Decimal(qty)).quantize(Decimal("0.01"))
        risk_amount = (notional * HARD_STOP_PCT).quantize(Decimal("0.01"))

        client_id_prefix = f"{assessment.ticker}_{int(datetime.now(UTC).timestamp())}"
        tier1_payload = self._tier1_bracket_payload(
            ticker=assessment.ticker,
            qty=tier1_qty,
            direction=assessment.direction,
            take_profit=assessment.target_20ma,
            stop_price=assessment.stop_price,
            client_order_id=f"{client_id_prefix}_tier1",
        )
        tier2_payload = self._tier2_limit_payload(
            ticker=assessment.ticker,
            qty=tier2_qty,
            direction=assessment.direction,
            limit_price=assessment.target_50ma,
            client_order_id=f"{client_id_prefix}_tier2",
        )

        return ExecutionDecision(
            ticker=assessment.ticker,
            direction=assessment.direction,
            qty=qty,
            tier1_qty=tier1_qty,
            tier2_qty=tier2_qty,
            notional_usd=notional,
            risk_amount_usd=risk_amount,
            order_payloads=[tier1_payload, tier2_payload],
            constructed_at=datetime.now(UTC),
        )

    @staticmethod
    def _tier1_bracket_payload(
        *,
        ticker: str,
        qty: int,
        direction: Direction,
        take_profit: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict:
        return {
            "symbol": ticker,
            "qty": str(qty),
            "side": "buy" if direction is Direction.LONG else "sell",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "client_order_id": client_order_id,
        }

    @staticmethod
    def _tier2_limit_payload(
        *,
        ticker: str,
        qty: int,
        direction: Direction,
        limit_price: Decimal,
        client_order_id: str,
    ) -> dict:
        # Tier 2 closes the *remaining* position from the same direction.
        # LONG entry → tier2 sells; SHORT entry → tier2 buys to cover.
        return {
            "symbol": ticker,
            "qty": str(qty),
            "side": "sell" if direction is Direction.LONG else "buy",
            "type": "limit",
            "limit_price": f"{limit_price:.2f}",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        }
