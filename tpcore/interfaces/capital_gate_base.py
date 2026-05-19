"""Lean P5.5 — shared per-trade capital-gate base (clusters #3/#4/#7).

``PerTradeCapitalGateBase`` consolidates the byte-identical engine-local
guardrail previously copy-pasted across ``reversion`` and ``vector``
(and the shared ``assert_can_graduate`` shape, also in ``momentum``):

* a per-position USD cap,
* a max-concurrent-positions limit,
* a daily-loss kill that mirrors :class:`tpcore.risk.RiskGovernor`
  (freeze on a ``_daily_loss_freeze_pct`` engine-equity drawdown),
* a paper→live graduation gate composed of per-engine stats thresholds
  AND a fresh Data Validation Suite pass AND a credibility-rubric score.

**This is the live-money risk gate.** Behavior — every reject branch,
the exact ``drawdown <= -pct`` boundary, the ``engine_equity == 0`` skip,
the raise-vs-return matrix of ``assert_can_graduate`` — is identical to
the pre-consolidation per-engine code. The emitted **structlog event
name is observable behavior** (forensics/dashboards key on it); it is
derived from ``self.engine_name`` and MUST equal the historical strings
(``"<engine>.gate.reject_*"``) — asserted by the P5.5a characterization
test.

This is a **dedicated** per-trade base (spec §7 D2): it is NOT a generic
``BaseEnginePlug`` extension every plug inherits — batch engines
(momentum/sentinel) must NOT inherit per-trade ``check_trade``. It
mirrors the per-trade-only ``tpcore.order_management.BaseOrderManager``
precedent. A subclass supplies:

* class attr ``engine_name`` (from :class:`BaseEnginePlug`),
* class attr ``_daily_loss_freeze_pct`` (``Decimal``; ``0.05`` today),
* the **abstract** :meth:`is_graduated` (thresholds stay per-engine).
"""
from __future__ import annotations

import os
from abc import abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.backtest.cost_model import capital_gate_healthcheck
from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.quality.validation.capital_gate import assert_passed_for_engine

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class PerTradeCapitalGateBase(BaseEnginePlug):
    """Shared per-trade engine-local capital gate (Plug 5).

    Concrete :meth:`check_trade`, :meth:`healthcheck`, and
    :meth:`assert_can_graduate`; abstract :meth:`is_graduated`. The
    subclass provides ``engine_name`` and ``_daily_loss_freeze_pct``.
    """

    #: Engine-equity drawdown that triggers the daily-loss freeze.
    #: Subclass MUST override (reversion/vector both ``Decimal("0.05")``).
    _daily_loss_freeze_pct: Decimal = Decimal("0.05")

    def __init__(
        self,
        engine_equity: Decimal,
        max_position_usd: Decimal,
        max_positions: int,
    ) -> None:
        self._engine_equity = engine_equity
        self._max_position_usd = max_position_usd
        self._max_positions = max_positions

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return capital_gate_healthcheck(
            self.engine_name,
            self._engine_equity,
            self._max_position_usd,
            self._max_positions,
        )

    def check_trade(
        self,
        size: Decimal,
        engine_pnl: Decimal,
        open_positions: int = 0,
    ) -> bool:
        """Return True iff the proposed trade obeys engine-local limits.

        Branch order (observable — earlier rejects pre-empt later ones):
        nonpositive size → oversize → position count → daily-loss
        drawdown. The ``engine_equity == 0`` case skips the drawdown
        block entirely (no divide). The boundary is ``<= -pct`` — a
        drawdown EXACTLY at the negative threshold rejects.
        """
        engine = self.engine_name
        if size <= 0:
            logger.info(f"{engine}.gate.reject_nonpositive", size=str(size))
            return False
        if size > self._max_position_usd:
            logger.info(
                f"{engine}.gate.reject_oversize",
                size=str(size),
                cap=str(self._max_position_usd),
            )
            return False
        if open_positions >= self._max_positions:
            logger.info(
                f"{engine}.gate.reject_position_count",
                open_positions=open_positions,
                cap=self._max_positions,
            )
            return False
        if self._engine_equity > 0:
            drawdown_pct = engine_pnl / self._engine_equity
            if drawdown_pct <= -self._daily_loss_freeze_pct:
                logger.warning(
                    f"{engine}.gate.reject_daily_loss",
                    drawdown_pct=float(drawdown_pct),
                    threshold=float(-self._daily_loss_freeze_pct),
                )
                return False
        return True

    @staticmethod
    @abstractmethod
    def is_graduated(stats: object) -> bool:
        """Per-engine paper→live stats-threshold check (abstract).

        Thresholds differ per engine (e.g. reversion's profit-factor
        floor) so this stays engine-owned — the consolidation does NOT
        flatten it.
        """
        raise NotImplementedError

    @classmethod
    async def assert_can_graduate(
        cls, stats: object, pool: asyncpg.Pool
    ) -> bool:
        """Combined gate: stats thresholds AND Data Validation Suite AND
        credibility ≥ 60.

        Returns ``False`` (without raising) if the per-engine stats
        thresholds aren't met (the normal pre-grad case). Otherwise
        requires a fresh successful validation run *and* a credibility
        rubric score; raises ``ValidationStaleError`` /
        ``ValidationFailedError`` if the data gate isn't satisfied, and
        ``CredibilityScoreInsufficientError`` if the latest backtest
        credibility row is below threshold or absent.
        """
        if not cls.is_graduated(stats):
            return False
        engine = cls.engine_name
        await assert_passed_for_engine(
            pool,
            engine,
            require_all_green=os.getenv(
                "CAPITAL_GATE_REQUIRE_ALL_GREEN", ""
            ).strip().lower()
            in ("1", "true", "yes", "on"),
        )
        if not await graduation_ready(pool, engine_name=engine):
            raise CredibilityScoreInsufficientError(
                f"{engine.capitalize()} backtest credibility score < 60 "
                "(or no rubric run on record)"
            )
        return True


__all__ = ["PerTradeCapitalGateBase"]
