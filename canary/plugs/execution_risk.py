"""Plug 3 — size exactly 1 share SPY, day-market."""
from __future__ import annotations

from decimal import Decimal

import structlog

from canary.models import CANARY_QTY, CANARY_TICKER, CanaryDecision
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class CanaryExecutionRisk(BaseEnginePlug):
    engine_name = "canary"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": self.engine_name, "plug": "execution_risk",
                "ok": True, "details": {}}

    def decide(self, *, price: Decimal) -> CanaryDecision:
        return CanaryDecision(ticker=CANARY_TICKER, qty=CANARY_QTY,
                              notional_usd=(price * CANARY_QTY))
