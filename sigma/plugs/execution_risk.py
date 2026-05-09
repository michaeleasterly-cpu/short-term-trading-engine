"""Sigma — Plug 3: Execution & Risk Scaling.

Given a PhaseAssessment + account capital:
    * size the position (pre-grad cap $1,500, ≤ 4 concurrent)
    * build TWO Alpaca **paper** order payloads implementing the plan §4.1
      50/50 scale-out — Alpaca brackets only allow a single take-profit, so
      the second exit must ride on a separate order.

Order layout (long side, the only side Sigma trades):
    Tier 1 — bracket: 50% of qty (odd-share remainder goes to Tier 1).
        TP = mid-band (``take_profit_mid``), SL = hard stop (entry × −3%).
    Tier 2 — limit (GTC): remaining 50% at the opposite (upper) band.

Stop-cancellation protocol
--------------------------
Alpaca auto-cancels the bracket's stop-loss leg the moment Tier 1 fills, so
Tier 2 is left running by the broker as intended. The inverse case is the
operator's responsibility: **if the hard stop fires before Tier 1 fills, the
order manager MUST cancel the open Tier 2 GTC limit** — Alpaca will not link
the bracket and the standalone limit on its end.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

from sigma.models import (
    HARD_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    ExecutionDecision,
    Phase,
    PhaseAssessment,
)

logger = structlog.get_logger(__name__)

DEFAULT_ACCOUNT_CAPITAL = Decimal("10000")
PER_TRADE_EQUITY_FRACTION = Decimal("0.15")


class SizingError(Exception):
    """Raised when no valid position size can be computed (e.g. price ≤ 0)."""


class SigmaExecutionRisk(BaseEnginePlug):
    """Plug 3 of Sigma."""

    engine_name = "sigma"

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
            },
        }

    def decide(
        self,
        assessment: PhaseAssessment,
        account_capital: Decimal = DEFAULT_ACCOUNT_CAPITAL,
        open_positions: int = 0,
    ) -> ExecutionDecision | None:
        """Build two order payloads for ``assessment``. Returns ``None`` when the
        phase is not ACTIVE, the position-count cap is hit, or the sized total
        is below two shares (need ≥ 1 per tier).
        """
        if assessment.phase is not Phase.ACTIVE:
            logger.debug("sigma.exec.skip_non_active", ticker=assessment.ticker, phase=assessment.phase)
            return None
        if open_positions >= MAX_CONCURRENT_POSITIONS:
            logger.info(
                "sigma.exec.position_cap_hit",
                ticker=assessment.ticker,
                open_positions=open_positions,
                cap=MAX_CONCURRENT_POSITIONS,
            )
            return None

        if assessment.entry_price <= 0:
            raise SizingError(f"non-positive entry price for {assessment.ticker}")

        notional_cap = min(
            PRE_GRAD_POSITION_CAP_USD,
            (account_capital * PER_TRADE_EQUITY_FRACTION).quantize(Decimal("0.01")),
        )
        qty = int((notional_cap / assessment.entry_price).to_integral_value(rounding=ROUND_DOWN))
        if qty < 2:
            logger.info(
                "sigma.exec.qty_below_two_shares",
                ticker=assessment.ticker,
                qty=qty,
                cap=str(notional_cap),
            )
            return None

        tier2_qty = qty // 2
        tier1_qty = qty - tier2_qty  # odd share goes to Tier 1
        notional = (assessment.entry_price * Decimal(qty)).quantize(Decimal("0.01"))
        risk_amount = (notional * HARD_STOP_PCT).quantize(Decimal("0.01"))

        client_id_prefix = self._client_id_prefix(assessment.ticker)
        tier1_payload = self._tier1_bracket_payload(
            ticker=assessment.ticker,
            qty=tier1_qty,
            take_profit=assessment.take_profit_mid,
            stop_price=assessment.stop_price,
            client_order_id=f"{client_id_prefix}_tier1",
        )
        tier2_payload = self._tier2_limit_payload(
            ticker=assessment.ticker,
            qty=tier2_qty,
            limit_price=assessment.take_profit_far,
            client_order_id=f"{client_id_prefix}_tier2",
        )

        return ExecutionDecision(
            ticker=assessment.ticker,
            qty=qty,
            tier1_qty=tier1_qty,
            tier2_qty=tier2_qty,
            notional_usd=notional,
            risk_amount_usd=risk_amount,
            order_payloads=[tier1_payload, tier2_payload],
            constructed_at=datetime.now(UTC),
        )

    @staticmethod
    def _client_id_prefix(ticker: str) -> str:
        return f"{ticker}_{int(datetime.now(UTC).timestamp())}"

    @staticmethod
    def _tier1_bracket_payload(
        *,
        ticker: str,
        qty: int,
        take_profit: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict:
        """Alpaca v2 ``POST /v2/orders`` body for the Tier 1 long bracket.

        The bracket attaches a take-profit at the mid-band and a stop-loss at
        the hard stop. Alpaca auto-cancels the SL leg when the TP leg fills.
        """
        return {
            "symbol": ticker,
            "qty": str(qty),
            "side": "buy",
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
        limit_price: Decimal,
        client_order_id: str,
    ) -> dict:
        """Standalone GTC sell-limit at the upper band for the remaining 50%.

        Sized in shares already held after Tier 1 fills — the order manager
        submits this on the same entry as the Tier 1 bracket so both sit in the
        book together. Operator is responsible for cancelling this leg if the
        Tier 1 hard stop trips before Tier 1 fills (see module docstring).
        """
        return {
            "symbol": ticker,
            "qty": str(qty),
            "side": "sell",
            "type": "limit",
            "limit_price": f"{limit_price:.2f}",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        }
