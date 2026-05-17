"""Plug 5 — tiny fixed cap; canary NEVER graduates (no rubric row).

`assert_can_graduate` always raises CredibilityScoreInsufficientError
because canary's backtest deliberately never writes a credibility
rubric (spec §4b). This keeps canary a permanent paper canary by
construction — NOT by a flag."""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from canary.models import CANARY_MAX_NOTIONAL_USD
from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.quality.validation.capital_gate import assert_passed

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class CanaryCapitalGate(BaseEnginePlug):
    engine_name = "canary"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": self.engine_name, "plug": "capital_gate",
                "ok": True,
                "details": {"max_notional_usd": str(CANARY_MAX_NOTIONAL_USD)}}

    def check_trade(self, *, size: Decimal, engine_pnl: Decimal,
                    open_positions: int = 0) -> bool:
        if size <= 0 or size > CANARY_MAX_NOTIONAL_USD:
            return False
        if open_positions >= 1:   # canary holds at most 1 position
            return False
        return True

    @classmethod
    async def assert_can_graduate(cls, stats, pool: asyncpg.Pool) -> bool:
        """Canary is non-graduating BY CONSTRUCTION: no credibility
        rubric row is ever written, so graduation_ready is always
        False → always raises. Documented deviation, spec §4b."""
        await assert_passed(pool)
        if not await graduation_ready(pool, engine_name="canary"):
            raise CredibilityScoreInsufficientError(
                "canary is a permanent paper canary — never graduates "
                "(no credibility rubric by design, spec §4b)")
        return True  # pragma: no cover — unreachable by construction
