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
from tpcore.lab.context import assert_not_in_lab
from tpcore.order_ids import ENGINE_PREFIX, is_engine_cid

# Frozen view over the canonical engine-prefix registry. Used by the
# broker-floor attribution helper to detect "some OTHER engine owns this
# symbol" without re-walking the dict on every position. The order_ids
# registry is the single source of truth — this is purely a read-only
# alias for symmetry with the helper's loop body.
_ALL_ENGINE_NAMES: tuple[str, ...] = tuple(ENGINE_PREFIX.keys())

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
    # #251 Part A: opt-in (batch engines only) for the never-fail-open
    # ``effective = max(proxy, broker_floor)`` raise on the
    # concurrent-position check. Default False ⇒ the check is byte-identical
    # to pre-A1 (raw persisted proxy). See spec §2 / §3.
    reconcile_open_floor: bool = False


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

    async def record_close(
        self,
        engine: str,
        trade_id: str | None,
        realized_pnl: Decimal,
    ) -> bool:
        """Idempotently apply ONE position-close to ``engine``'s state.

        The single arbiter for the ``-1``/realized-pnl of a closed
        position. Both close callers (the scheduler rebalance-sell loop
        and the trade-monitor stream) funnel through this so the same
        real close decrements ``open_positions`` AT MOST ONCE — the
        ``platform.risk_close_ledger`` ``(engine, trade_id)`` PK is the
        sole, atomic dedupe key (see #251 spec §2b).

        Contract (every uncertainty branch SKIPS — over-count → tight →
        never fail open):

        * ``trade_id is None`` → structlog WARN, return ``False``, NO
          decrement, NO pnl change (a missing id is never guessed).
        * Insert won (first time this ``(engine, trade_id)`` is seen) →
          ``open_positions = GREATEST(0, open_positions - 1)``,
          ``daily_pnl``/``weekly_pnl += realized_pnl``, return ``True``.
        * Conflict / already-counted / race-loser → return ``False``,
          NO decrement, NO pnl change.

        Returns ``True`` iff this call applied the decrement.
        """
        raise NotImplementedError  # pragma: no cover - interface

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
        # Mirror of platform.risk_close_ledger's (engine, trade_id) PK:
        # a close whose key is already here was already counted → skip.
        self._closed: set[tuple[str, str]] = set()

    async def get(self, engine_id: str) -> RiskState | None:
        return self._states.get(engine_id)

    async def record_close(
        self,
        engine: str,
        trade_id: str | None,
        realized_pnl: Decimal,
    ) -> bool:
        if trade_id is None:
            logger.warning(
                "tpcore.risk.record_close_null_trade_id",
                engine=engine,
                detail="trade_id is None — skipping the decrement (over-count "
                       "is safe; never guess a close id → never fail open)",
            )
            return False
        key = (engine, trade_id)
        if key in self._closed:  # already counted by the other path / a retry
            return False
        state = self._states.get(engine)
        if state is None:
            return False
        self._closed.add(key)
        self._states[engine] = state.model_copy(
            update={
                "open_positions": max(0, state.open_positions - 1),
                "daily_pnl": state.daily_pnl + realized_pnl,
                "weekly_pnl": state.weekly_pnl + realized_pnl,
                "updated_at": datetime.now(UTC),
            }
        )
        return True

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
        assert_not_in_lab()
        self._store = state_store
        self._broker = broker
        self._default_limits = limits or RiskLimits()
        self._engine_limits: dict[str, RiskLimits] = {}
        self._platform_capital = platform_capital
        # asyncpg pool for the optional cost gate (B6). When ``None``
        # ``check_cost`` short-circuits ALLOW so tests without a DB
        # don't need to wire one up.
        self._pool = pool

    @property
    def limits(self) -> RiskLimits:
        return self._default_limits

    async def register_engine(
        self,
        engine_id: str,
        engine_equity: Decimal,
        limits: RiskLimits | None = None,
    ) -> RiskState:
        """Create initial state for an engine. Idempotent — won't clobber existing.

        ``limits`` overrides the governor's default ``RiskLimits`` for this
        engine only. Engines that pass nothing (reversion/vector) keep the
        global default — batch engines (momentum holds ~130 names) pass a
        wider ``max_open_positions`` so the global cap doesn't block them.
        Limits are (re)recorded on every call — even when state already
        exists — so a config/profile change is picked up on the next
        process registration.
        """
        if limits is not None:
            self._engine_limits[engine_id] = limits

        def _warn_if_placeholder(effective_equity: Decimal) -> None:
            # Key the warning off the EFFECTIVE equity the governor will
            # actually gate against — not the raw argument. Schedulers
            # always pass the 10000 default every process run; the allocator
            # writes REAL equity into the store. Warning only when the
            # effective equity is still the placeholder lets it self-silence
            # once tpcore.allocator has run.
            if effective_equity == Decimal("10000"):
                logger.warning(
                    "tpcore.risk.equity_unallocated",
                    engine=engine_id,
                    detail="engine_equity is the 10000 placeholder — allocator "
                           "has not set real capital; caps are evaluated against "
                           "a fiction until tpcore.allocator runs",
                )

        existing = await self._store.get(engine_id)
        if existing is not None:
            _warn_if_placeholder(existing.engine_equity)
            return existing
        now = datetime.now(UTC)
        state = RiskState(
            engine=engine_id,
            engine_equity=engine_equity,
            daily_reset_at=next_open(now),
            weekly_reset_at=next_monday_open(now),
        )
        await self._store.put(state)
        logger.info(
            "tpcore.risk.engine_registered",
            engine=engine_id,
            equity=str(engine_equity),
            limits=limits.model_dump(mode="json") if limits is not None else "default",
        )
        _warn_if_placeholder(engine_equity)
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
            5. Issuer-lifecycle terminated (P2c 2026-05-31) — BLOCK
               new orders when ``platform.ticker_classifications.
               issuer_lifecycle_state`` is ``'deregistered'`` (Form 15
               evidence) or ``'delist_effective'`` (Form 25 evidence).
               Cheap indexed read; placed BEFORE the broker round-trip
               so a known-terminated name short-circuits without any
               broker API cost.
            6. Max open positions.
            7. Platform-wide net long exposure (BUY only).
            8. Round-trip cost vs expected edge (opt-in via kwargs).
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

        limits = self._engine_limits.get(engine_id, self._default_limits)

        daily_floor = -(state.engine_equity * limits.daily_loss_pct)
        if state.daily_pnl <= daily_floor:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=f"daily loss cap hit ({state.daily_pnl} ≤ {daily_floor})",
            )
        weekly_floor = -(state.engine_equity * limits.weekly_loss_pct)
        if state.weekly_pnl <= weekly_floor:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=f"weekly loss cap hit ({state.weekly_pnl} ≤ {weekly_floor})",
            )
        # P2c (2026-05-31) — issuer-lifecycle terminated gate. A ticker
        # with Form 25/Form 15 SEC evidence is terminally ended; no
        # new orders against it can ever close at the assumed price
        # because the security itself is being unwound. Cheap indexed
        # read; fires BEFORE the broker round-trip so a terminated
        # name short-circuits without consuming the broker's API
        # budget. Opt-in via ``ticker`` kwarg (preserves the existing
        # ticker-less call sites that gate non-ticker-aware
        # operations).
        if ticker is not None:
            lifecycle_check = await self.check_lifecycle(ticker)
            if lifecycle_check.decision is RiskDecision.BLOCK:
                return lifecycle_check
        # #251 Part A: the never-fail-open ``max(proxy, broker_floor)``
        # raise. Fetch the broker's open positions ONCE here (the single
        # in-band ``get_positions()`` round-trip — its result is also
        # reused for the BUY net-long check below; NO second round-trip).
        # ``broker_floor`` is the PER-ENGINE open-position COUNT
        # (positions whose ``symbol`` correlates to a recent order
        # carrying ``engine_id``'s ``client_order_id`` prefix). On ANY
        # broker error/timeout/exception/empty/None → ``broker_floor = 0``
        # (a no-op against the ``max``: the conservative proxy stands).
        # The raise is applied to the concurrent-position check ONLY
        # when the per-engine ``reconcile_open_floor`` flag is set;
        # otherwise the check is byte-identical to pre-A1 (raw
        # ``state.open_positions``).
        # ``None`` ⇒ NOT pre-fetched (flag-OFF) → the BUY net-long check
        # below does its own single fetch = byte-identical pre-A1 path.
        # A list (possibly ``[]``) ⇒ pre-fetched here and reused below so
        # ``get_positions()`` is called AT MOST ONCE per ``check_trade``.
        broker_positions: list | None = None
        broker_floor = 0
        broker_errored = False
        if limits.reconcile_open_floor:
            try:
                fetched = await self._broker.get_positions()
            except Exception as exc:  # noqa: BLE001 — never fail open: any
                # broker failure must degrade to broker_floor=0 (proxy
                # stands), never raise out of the gate. BLE001 precedent:
                # the broker is an external dependency; a partial/odd
                # response must not relax the live-money cap.
                logger.warning(
                    "tpcore.risk.broker_floor_unavailable",
                    engine=engine_id,
                    error=str(exc),
                    detail="get_positions failed — broker_floor=0 (proxy "
                           "stands; never fail open)",
                )
                fetched = None
                broker_errored = True
            # error/None/empty → [] (broker_floor stays 0 — the proxy
            # stands; no second round-trip below).
            broker_positions = list(fetched) if fetched else []
            # Per-engine attribution: count only positions whose symbol
            # correlates to a recent engine-tagged order. Unattributed
            # positions still count against ``engine_id`` (over-count →
            # tighter → never-fail-open) plus emit a WARNING. Broker
            # lacking ``list_recent_orders`` degrades to the pre-change
            # cross-engine count.
            broker_floor = await self._count_engine_broker_floor(
                engine_id, broker_positions,
            )

        effective_open = max(state.open_positions, broker_floor)
        if effective_open >= limits.max_open_positions:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=(
                    f"max concurrent positions hit "
                    f"({effective_open} ≥ {limits.max_open_positions})"
                ),
            )

        if direction is OrderSide.BUY:
            if broker_errored:
                # The single in-band ``get_positions()`` failed for a
                # flag-ON engine. Pre-A1 the net-long check would have
                # re-fetched and raised (fail CLOSED). We must NOT silently
                # treat positions as ``[]`` here (that would under-count
                # net-long → fail OPEN). BLOCK is strictly never-fail-open
                # and avoids the forbidden second round-trip.
                return CheckResult(
                    RiskDecision.BLOCK,
                    reason=(
                        "broker positions unavailable — net-long exposure "
                        "cannot be verified (fail closed; never fail open)"
                    ),
                )
            exposure = await self._platform_net_long_after(
                size, positions=broker_positions
            )
            cap = limits.platform_net_long_cap_pct
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

    async def check_lifecycle(self, ticker: str) -> CheckResult:
        """Block when the issuer is terminally delisted / deregistered
        (P2c 2026-05-31).

        Reads ``platform.ticker_classifications.issuer_lifecycle_state``
        — populated by the ``backfill_sec_lifecycle`` stage from SEC
        Form 25 (delist notice) / Form 15 (deregistration) evidence.

        State decisions:
          * ``'deregistered'``  → BLOCK (Form 15 — SEC reporting
                                  obligation terminated; the security
                                  is being unwound)
          * ``'delist_effective'`` → BLOCK (Form 25 — delist effective
                                  on the exchange; may trade on OTC
                                  but the listing is gone)
          * ``'delist_pending'`` → ALLOW (P2c reserved; Form 25
                                  announced but not effective yet —
                                  the security still trades on the
                                  primary listing)
          * ``'active'`` / NULL / any other state → ALLOW

        Returns ALLOW when the governor has no DB pool wired (tests
        without DB stay green) — mirrors :meth:`check_cost`. A NULL
        state (pre-backfill default for most of the universe) is also
        ALLOW — the operator-correct fallback while P2a coverage
        extends.
        """
        if self._pool is None:
            return CheckResult(RiskDecision.ALLOW)
        from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (  # noqa: E501
            TERMINAL_LIFECYCLE_STATES,
        )

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT issuer_lifecycle_state
                FROM platform.ticker_classifications
                WHERE ticker = $1
                """,
                ticker,
            )
        if row is None:
            # Ticker not in classifications — pre-existing universe
            # may not cover every smoke/integration test ticker. ALLOW
            # (the live universe path catches this elsewhere — universe
            # filter, broker tradability — and this gate is additive,
            # not the SoT for "exists at all").
            return CheckResult(RiskDecision.ALLOW)
        state = row["issuer_lifecycle_state"]
        if state in TERMINAL_LIFECYCLE_STATES:
            return CheckResult(
                RiskDecision.BLOCK,
                reason=(
                    f"issuer lifecycle terminated for {ticker} "
                    f"(state={state}) — SEC Form 25/15 evidence "
                    f"(see platform.ticker_lifecycle_events)"
                ),
            )
        return CheckResult(RiskDecision.ALLOW)

    async def _count_engine_broker_floor(
        self,
        engine_id: str,
        broker_positions: list,
    ) -> int:
        """Count ``broker_positions`` attributable to ``engine_id``.

        Attribution joins on the position's ``symbol`` against the
        ``client_order_id`` prefix of recent broker orders (mirrors the
        canonical precedent at ``momentum/scheduler.py``
        ``_filter_to_engine_holdings`` — Position carries ``symbol`` but
        not ``client_order_id``, so the recent-orders list is the
        attribution substrate).

        Behaviour:

        * Broker exposes ``list_recent_orders`` → fetch with
          ``limit=500`` (matches the momentum/canary callers); build
          ``engine_symbols = {o.symbol for o in recent if
          is_engine_cid(o.client_order_id, engine_id)}``; count
          positions whose symbol is in ``engine_symbols``.
        * Unattributed position (symbol not in ``engine_symbols``) →
          COUNTS against ``engine_id`` (over-count → tighter →
          never-fail-open) AND emits
          ``tpcore.risk.unattributed_broker_position`` WARNING so the
          operator can clean up. NOT silently ignored.
        * Broker lacks ``list_recent_orders`` (non-Alpaca / smoke
          fixtures) → emit ``tpcore.risk.broker_attribution_unavailable``
          WARNING and return the pre-change cross-engine count
          (``len(broker_positions)``) — degraded but still tighter than
          proxy-only; never-fail-open.
        * ``list_recent_orders`` call errors → same degraded fallback as
          missing primitive (logged separately so the operator can
          distinguish transient broker hiccups from non-Alpaca adapters).

        Never raises; the broker-floor invariant is "tighter is safe,
        looser is forbidden" — a buggy helper returning 0 on real
        positions is still bounded by the ``max(proxy, broker_floor)``
        raise in ``check_trade`` (proxy still wins).
        """
        if not broker_positions:
            return 0
        list_fn = getattr(self._broker, "list_recent_orders", None)
        if list_fn is None:
            logger.warning(
                "tpcore.risk.broker_attribution_unavailable",
                engine=engine_id,
                n_positions=len(broker_positions),
                detail="broker has no list_recent_orders — degrading to "
                       "cross-engine count (tighter than proxy-only; "
                       "never fail open)",
            )
            return len(broker_positions)
        try:
            recent = await list_fn(limit=500)
        except Exception as exc:  # noqa: BLE001 — broker call: never raise out
            logger.warning(
                "tpcore.risk.broker_attribution_unavailable",
                engine=engine_id,
                n_positions=len(broker_positions),
                error=str(exc),
                detail="list_recent_orders failed — degrading to "
                       "cross-engine count (tighter than proxy-only; "
                       "never fail open)",
            )
            return len(broker_positions)
        if not recent:
            # No recent orders → every current position is unattributed
            # by definition. Count all of them against engine_id (the
            # over-count fail-safe) and log once per gate, not per
            # position, so a busy account doesn't spam.
            logger.warning(
                "tpcore.risk.unattributed_broker_position",
                engine=engine_id,
                n_positions=len(broker_positions),
                symbols=[p.symbol for p in broker_positions],
                detail="no recent orders on file — every open position "
                       "is unattributable; counting all against "
                       f"{engine_id} (over-count fail-safe; never fail open)",
            )
            return len(broker_positions)
        # Build per-symbol attribution in ONE pass over recent orders:
        # ``symbol_to_engines[sym]`` = set of engines whose CID prefixes
        # match an order on that symbol. Empty set ⇒ no engine claims it
        # (legacy / manual / corporate-action — unattributed).
        symbol_to_engines: dict[str, set[str]] = {}
        for o in recent:
            cid = getattr(o, "client_order_id", None)
            sym = getattr(o, "symbol", None)
            if not sym:
                continue
            owners = symbol_to_engines.setdefault(sym, set())
            for eng in _ALL_ENGINE_NAMES:
                if is_engine_cid(cid, eng):
                    owners.add(eng)
                    break
        # Track unattributed for one WARNING per gate (not per position).
        attributed = 0
        unattributed_symbols: list[str] = []
        for pos in broker_positions:
            owners = symbol_to_engines.get(pos.symbol, set())
            if engine_id in owners:
                attributed += 1
                continue
            if owners:
                # Genuine cross-engine isolation — some other engine
                # owns this symbol; do NOT count it against engine_id.
                continue
            # No engine claims this symbol → over-count fail-safe.
            unattributed_symbols.append(pos.symbol)
        if unattributed_symbols:
            logger.warning(
                "tpcore.risk.unattributed_broker_position",
                engine=engine_id,
                symbols=unattributed_symbols,
                n_unattributed=len(unattributed_symbols),
                detail="open broker positions with no recent "
                       "engine-tagged order — counting against "
                       f"{engine_id} (over-count fail-safe; operator "
                       "should investigate)",
            )
        return attributed + len(unattributed_symbols)

    async def _platform_net_long_after(
        self,
        additional_long: Decimal,
        *,
        positions: list | None = None,
    ) -> Decimal:
        """Sum of current long position market values + ``additional_long``.

        ``positions`` may be the list already fetched by :meth:`check_trade`'s
        #251 Part A broker-floor step (reused so ``get_positions`` is called
        AT MOST once per gate — no second round-trip). ``None`` ⇒ not
        pre-fetched (flag-OFF path) → fetch here, byte-identical to pre-A1.
        """
        if positions is None:
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

    async def record_close(
        self,
        engine_id: str,
        trade_id: str | None,
        realized_pnl: Decimal = Decimal("0"),
    ) -> bool:
        """Route a position-close through the idempotent ledger arbiter.

        The ONLY sanctioned ``-1`` close path (#251 B1) — both the
        scheduler rebalance-sell loop and the trade-monitor stream funnel
        here so the same real close decrements ``open_positions`` at most
        once (``risk_close_ledger`` ``(engine, trade_id)`` PK arbitrates).
        The ``+1`` open path / ``record_fill`` non-close behaviour is
        unchanged. Returns ``True`` iff this call applied the decrement.
        """
        applied = await self._store.record_close(engine_id, trade_id, realized_pnl)
        logger.info(
            "tpcore.risk.close_routed",
            engine=engine_id,
            trade_id=trade_id,
            realized_pnl=str(realized_pnl),
            applied=applied,
        )
        return applied

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
