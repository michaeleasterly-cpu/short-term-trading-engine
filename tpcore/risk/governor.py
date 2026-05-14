"""Cross-engine risk governor.

Enforces:
  * Per-engine daily loss limit  — default **5%** of engine equity.
  * Per-engine weekly loss limit — default **10%** of engine equity.
  * Per-engine max open positions — default **8**.
  * Platform-wide net long exposure cap — default **60%** of total platform capital.
  * Global kill switch — set by ``emergency_kill()``; blocks all new trades,
    cancels open orders, and flattens positions.

State is persisted via the ``RiskStateStore`` abstraction. Concrete
in-memory implementation lives in this module; a Postgres-backed store
(``platform.risk_state`` table) lives in ``tpcore.risk.postgres_store``.
Daily counters reset at the next XNYS session open; weekly counters reset
at the next Monday session open. Reset happens lazily on the next
``check_trade``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from tpcore.calendar import next_monday_open, next_open
from tpcore.interfaces.broker import BrokerExecutionInterface, OrderSide

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class RiskDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class RiskLimits(BaseModel):
    """Per-engine + platform-wide thresholds."""

    model_config = ConfigDict(extra="forbid")

    daily_loss_pct: Decimal = Decimal("0.05")
    weekly_loss_pct: Decimal = Decimal("0.10")
    max_open_positions: int = 8
    platform_net_long_cap_pct: Decimal = Decimal("0.60")


class RiskState(BaseModel):
    """Mutable risk state for a single engine. Mirror of ``platform.risk_state``."""

    model_config = ConfigDict(extra="forbid")

    engine: str
    engine_equity: Decimal
    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")
    open_positions: int = 0
    daily_reset_at: datetime
    weekly_reset_at: datetime
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CheckResult:
    decision: RiskDecision
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is RiskDecision.ALLOW


class RiskStateStore:
    """Abstract persistence layer for ``RiskState``."""

    async def get(self, engine_id: str) -> RiskState | None:  # pragma: no cover - interface
        raise NotImplementedError

    async def put(self, state: RiskState) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def list_all(self) -> list[RiskState]:  # pragma: no cover - interface
        raise NotImplementedError

    async def set_kill_switch_all(
        self, *, active: bool, reason: str | None = None
    ) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def record_fill(
        self,
        *,
        engine: str,
        realized_pnl: Decimal,
        position_delta: int,
    ) -> RiskState | None:
        """Apply one fill's effects to ``engine``'s state.

        Default implementation reads-modifies-writes via ``get`` + ``put``
        so concrete stores only need to override when they want a faster
        path (e.g. a single UPDATE). Returns the new state, or None if
        the engine has no row yet.

        Called by ``tpcore.trade_monitor`` after every closed-position
        AAR write. ``realized_pnl`` is signed (gain positive);
        ``position_delta`` is -1 when a position closes.
        """
        state = await self.get(engine)
        if state is None:
            return None
        new_open = max(0, state.open_positions + position_delta)
        updated = state.model_copy(
            update={
                "daily_pnl": state.daily_pnl + realized_pnl,
                "weekly_pnl": state.weekly_pnl + realized_pnl,
                "open_positions": new_open,
                "updated_at": datetime.now(UTC),
            }
        )
        await self.put(updated)
        return updated


class InMemoryRiskStateStore(RiskStateStore):
    """Process-local store. Use for tests and single-process dev runs."""

    def __init__(self) -> None:
        self._states: dict[str, RiskState] = {}

    async def get(self, engine_id: str) -> RiskState | None:
        return self._states.get(engine_id)

    async def put(self, state: RiskState) -> None:
        self._states[state.engine] = state.model_copy(update={"updated_at": datetime.now(UTC)})

    async def list_all(self) -> list[RiskState]:
        return list(self._states.values())

    async def set_kill_switch_all(self, *, active: bool, reason: str | None = None) -> None:
        for engine_id, state in list(self._states.items()):
            self._states[engine_id] = state.model_copy(
                update={
                    "kill_switch_active": active,
                    "kill_switch_reason": reason if active else None,
                    "updated_at": datetime.now(UTC),
                }
            )


class RiskGovernor:
    """Coordinates risk checks across engines.

    Pure logic; persistence is delegated to a state store passed at
    construction. Use ``InMemoryRiskStateStore`` in tests.
    """

    def __init__(
        self,
        state_store: RiskStateStore,
        broker: BrokerExecutionInterface,
        limits: RiskLimits | None = None,
        platform_capital: Decimal = Decimal("0"),
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._store = state_store
        self._broker = broker
        self._limits = limits or RiskLimits()
        self._platform_capital = platform_capital
        # asyncpg pool for the optional cost gate (B6). When ``None``
        # ``check_cost`` short-circuits ALLOW so tests without a DB
        # don't need to wire one up.
        self._pool = pool

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    async def register_engine(
        self,
        engine_id: str,
        engine_equity: Decimal,
    ) -> RiskState:
        """Create initial state for an engine. Idempotent — won't clobber existing."""
        existing = await self._store.get(engine_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        state = RiskState(
            engine=engine_id,
            engine_equity=engine_equity,
            daily_reset_at=next_open(now),
            weekly_reset_at=next_monday_open(now),
        )
        await self._store.put(state)
        logger.info("tpcore.risk.engine_registered", engine=engine_id, equity=str(engine_equity))
        return state

    async def state_for(self, engine_id: str) -> RiskState | None:
        """Read-only peek at the current ``RiskState`` for one engine.

        Returns ``None`` if the engine isn't registered. Use this for
        cheap pre-flight checks (``kill_switch_active``, ``daily_pnl``,
        ``open_positions``) BEFORE the heavier :meth:`check_trade` call.
        The returned object is a snapshot — mutations must go through
        :meth:`record_fill`, :meth:`register_engine`, or
        :meth:`emergency_kill`. Added 2026-05-14 to eliminate the
        private-attribute leak pattern (``governor._store.get(...)``)
        across the engines' order managers.
        """
        return await self._store.get(engine_id)

    async def check_trade(
        self,
        engine_id: str,
        size: Decimal,
        direction: OrderSide,
        *,
        ticker: str | None = None,
        expected_edge_pct: Decimal | None = None,
    ) -> CheckResult:
        """Return ALLOW/BLOCK for a proposed trade.

        ``size`` is the positive notional dollar value of the proposed entry.
        ``direction`` is ``BUY`` or ``SELL``.

        When ``ticker`` and ``expected_edge_pct`` are both provided AND
        the governor was constructed with a DB pool, the cost gate
        (B6) fires: if the ticker's round-trip cost in
        ``platform.liquidity_tiers`` exceeds the trade's expected edge,
        the trade is BLOCKED. Engines compute the edge from the
        assessment's entry + take-profit prices.

        Order of checks (most fundamental first; first failure wins):
            1. Engine registered.
            2. Kill switch active.
            3. Daily loss cap.
            4. Weekly loss cap.
            5. Max open positions.
            6. Platform-wide net long exposure (BUY only).
            7. Round-trip cost vs expected edge (opt-in via kwargs).
        """
        if size <= 0:
            return CheckResult(RiskDecision.BLOCK, reason="size must be positive")

        await self._maybe_reset_counters(engine_id)
        state = await self._store.get(engine_id)
        if state is None:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=f"no risk state for engine {engine_id!r} — call register_engine first",
            )
        if state.kill_switch_active:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=f"kill switch active: {state.kill_switch_reason or 'unspecified'}",
            )

        daily_floor = -(state.engine_equity * self._limits.daily_loss_pct)
        if state.daily_pnl <= daily_floor:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=f"daily loss cap hit ({state.daily_pnl} ≤ {daily_floor})",
            )
        weekly_floor = -(state.engine_equity * self._limits.weekly_loss_pct)
        if state.weekly_pnl <= weekly_floor:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=f"weekly loss cap hit ({state.weekly_pnl} ≤ {weekly_floor})",
            )
        if state.open_positions >= self._limits.max_open_positions:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=(
                    f"max concurrent positions hit "
                    f"({state.open_positions} ≥ {self._limits.max_open_positions})"
                ),
            )

        if direction is OrderSide.BUY:
            exposure = await self._platform_net_long_after(size)
            cap = self._limits.platform_net_long_cap_pct
            if self._platform_capital > 0 and exposure / self._platform_capital > cap:
                return CheckResult(
                    RiskDecision.BLOCK,
                    reason=(
                        f"platform net-long exposure {exposure}/{self._platform_capital} "
                        f"would exceed {cap:%} cap"
                    ),
                )

        if ticker is not None and expected_edge_pct is not None:
            cost_check = await self.check_cost(ticker, expected_edge_pct)
            if cost_check.decision is RiskDecision.BLOCK:
                return cost_check

        return CheckResult(RiskDecision.ALLOW)

    async def check_cost(
        self,
        ticker: str,
        expected_edge_pct: Decimal,
    ) -> CheckResult:
        """Block when the ticker's round-trip cost exceeds the trade's edge.

        Reads the median spread from ``platform.liquidity_tiers`` via
        ``tpcore.backtest.cost_model.get_round_trip_cost``. Returns
        ALLOW when the governor has no DB pool wired — tests that
        don't need the gate stay green without extra fixtures.
        """
        if self._pool is None:
            return CheckResult(RiskDecision.ALLOW)
        from tpcore.backtest.cost_model import get_round_trip_cost

        cost = await get_round_trip_cost(self._pool, ticker)
        if cost > expected_edge_pct:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=(
                    f"round-trip cost {cost} > expected edge {expected_edge_pct} "
                    f"for {ticker}"
                ),
            )
        return CheckResult(RiskDecision.ALLOW)

    async def _platform_net_long_after(self, additional_long: Decimal) -> Decimal:
        """Sum of current long position market values + ``additional_long``."""
        positions = await self._broker.get_positions()
        existing_long = sum(
            (p.market_value or p.qty * p.avg_entry_price for p in positions if p.qty > 0),
            start=Decimal("0"),
        )
        return existing_long + additional_long

    async def record_fill(
        self,
        engine_id: str,
        realized_pnl: Decimal,
        position_delta: int,
    ) -> RiskState:
        """Update counters after a fill.

        ``realized_pnl`` is signed (gain positive). ``position_delta`` is +1
        when a new position opens, -1 when one closes.
        """
        state = await self._store.get(engine_id)
        if state is None:
            raise ValueError(f"engine {engine_id!r} has no risk state")
        new_open = max(0, state.open_positions + position_delta)
        updated = state.model_copy(
            update={
                "daily_pnl": state.daily_pnl + realized_pnl,
                "weekly_pnl": state.weekly_pnl + realized_pnl,
                "open_positions": new_open,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.put(updated)
        logger.info(
            "tpcore.risk.fill_recorded",
            engine=engine_id,
            realized_pnl=str(realized_pnl),
            position_delta=position_delta,
            daily_pnl=str(updated.daily_pnl),
            weekly_pnl=str(updated.weekly_pnl),
            open_positions=updated.open_positions,
        )
        return updated

    async def emergency_kill(self, reason: str) -> int:
        """Activate the global kill switch and cancel every open broker order.

        Returns the number of orders cancelled. Position flattening is a
        policy decision (market-on-close vs. immediate market) made by the
        operator's runbook, not done here.
        """
        await self._store.set_kill_switch_all(active=True, reason=reason)
        cancelled = await self._broker.emergency_cancel_all()
        logger.error("tpcore.risk.kill_switch", reason=reason, cancelled=cancelled)
        return cancelled

    async def _maybe_reset_counters(self, engine_id: str) -> None:
        state = await self._store.get(engine_id)
        if state is None:
            return
        now = datetime.now(UTC)
        update: dict = {}
        if now >= state.daily_reset_at:
            update["daily_pnl"] = Decimal("0")
            update["daily_reset_at"] = next_open(now)
        if now >= state.weekly_reset_at:
            update["weekly_pnl"] = Decimal("0")
            update["weekly_reset_at"] = next_monday_open(now)
        if update:
            await self._store.put(state.model_copy(update=update))
            logger.info(
                "tpcore.risk.counters_reset",
                engine=engine_id,
                **{k: str(v) for k, v in update.items()},
            )


__all__ = [
    "CheckResult",
    "InMemoryRiskStateStore",
    "RiskDecision",
    "RiskGovernor",
    "RiskLimits",
    "RiskState",
    "RiskStateStore",
]
