"""Cross-engine risk governor.

Enforces:
  * Per-engine daily loss limit  — default **5%** of engine equity.
  * Per-engine weekly loss limit — default **10%** of engine equity.
  * Per-engine max open positions — default **8**.
  * Platform-wide net long exposure cap — default **60%** of total platform capital.
  * Global kill switch — set by ``emergency_kill()``; blocks all new trades,
    cancels open orders, and flattens positions.

State is persisted to ``platform.risk_state`` so the governor survives
process restarts. Daily counters reset at the next XNYS session open;
weekly counters reset at the next Monday session open.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from tpcore.calendar import next_monday_open, next_open
from tpcore.interfaces.broker import BrokerExecutionInterface, OrderSide


class RiskDecision(str, Enum):
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
    daily_reset_at: datetime  # next XNYS open in UTC
    weekly_reset_at: datetime  # next Monday open in UTC
    kill_switch_active: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CheckResult:
    decision: RiskDecision
    reason: str | None = None


class RiskGovernor:
    """Coordinates risk checks across engines.

    Concrete persistence is delegated to a state store passed at construction
    time — this class is pure logic and is unit-testable without a database.
    """

    def __init__(
        self,
        state_store: "RiskStateStore",
        broker: BrokerExecutionInterface,
        limits: RiskLimits | None = None,
        platform_capital: Decimal = Decimal("0"),
    ) -> None:
        self._store = state_store
        self._broker = broker
        self._limits = limits or RiskLimits()
        self._platform_capital = platform_capital

    async def check_trade(
        self,
        engine_id: str,
        size: Decimal,
        direction: OrderSide,
    ) -> CheckResult:
        """Return ALLOW/BLOCK for a proposed trade.

        ``size`` is the notional dollar value. ``direction`` is the order side.
        TODO: implement full logic — kill switch, per-engine PnL caps, position
        count cap, and platform-wide net long exposure check.
        """
        await self._maybe_reset_counters(engine_id)
        state = await self._store.get(engine_id)
        if state is None:
            return CheckResult(RiskDecision.BLOCK, reason=f"no risk state for engine {engine_id!r}")
        if state.kill_switch_active:
            return CheckResult(RiskDecision.BLOCK, reason="kill switch active")
        # TODO: daily/weekly PnL caps, max position count, platform exposure check.
        return CheckResult(RiskDecision.ALLOW)

    async def record_fill(
        self,
        engine_id: str,
        realized_pnl: Decimal,
        position_delta: int,
    ) -> None:
        """Update counters after a fill. Persists via the state store."""
        # TODO: load → mutate → save under a transaction.
        raise NotImplementedError

    async def emergency_kill(self, reason: str) -> None:
        """Activate the global kill switch.

        1. Mark all engines' risk state as ``kill_switch_active=True``.
        2. Cancel all open broker orders.
        3. Flatten all positions (TODO: market-on-close vs. immediate market — policy decision).
        """
        await self._store.set_kill_switch_all(active=True, reason=reason)
        await self._broker.emergency_cancel_all()
        # TODO: flatten positions per policy.

    async def _maybe_reset_counters(self, engine_id: str) -> None:
        """Reset daily/weekly counters if the relevant XNYS session has opened."""
        state = await self._store.get(engine_id)
        if state is None:
            return
        now = datetime.now(UTC)
        if now >= state.daily_reset_at:
            state.daily_pnl = Decimal("0")
            state.daily_reset_at = next_open(now)
        if now >= state.weekly_reset_at:
            state.weekly_pnl = Decimal("0")
            state.weekly_reset_at = next_monday_open(now)
        await self._store.put(state)


class RiskStateStore:
    """Abstract persistence layer for ``RiskState``.

    Concrete implementation in ``platform`` (asyncpg/SQLAlchemy). Defined here
    only to keep ``RiskGovernor`` decoupled from the database driver.
    """

    async def get(self, engine_id: str) -> RiskState | None:  # pragma: no cover - interface
        raise NotImplementedError

    async def put(self, state: RiskState) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def set_kill_switch_all(
        self, *, active: bool, reason: str
    ) -> None:  # pragma: no cover - interface
        raise NotImplementedError
