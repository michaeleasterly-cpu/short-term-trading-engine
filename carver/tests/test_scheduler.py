"""carver/scheduler.py — run_once orchestration tests."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

# ── Stub infrastructure ─────────────────────────────────────────────────


class _StubAccount:
    def __init__(self, equity: Decimal = Decimal("100000")) -> None:
        self.equity = equity


class _StubPosition:
    def __init__(self, symbol: str, qty: int) -> None:
        self.symbol = symbol
        self.qty = qty


class _StubOrderRecord:
    def __init__(self, symbol: str, client_order_id: str) -> None:
        self.symbol = symbol
        self.client_order_id = client_order_id


class _StubPlacedOrder:
    def __init__(self, broker_order_id: str) -> None:
        self.broker_order_id = broker_order_id


class _StubBroker:
    def __init__(self) -> None:
        self._account = _StubAccount()
        self._positions: list[_StubPosition] = []
        self._recent_orders: list[_StubOrderRecord] = []
        self.placed: list[Any] = []

    async def get_account(self) -> _StubAccount:
        return self._account

    async def get_positions(self) -> list[_StubPosition]:
        return list(self._positions)

    async def list_recent_orders(self, limit: int = 500) -> list[_StubOrderRecord]:
        del limit
        return list(self._recent_orders)

    async def place_order(self, order: Any) -> _StubPlacedOrder:
        self.placed.append(order)
        return _StubPlacedOrder(broker_order_id=f"broker_{len(self.placed)}")

    async def cancel_order(self, order_id: str) -> bool:
        del order_id
        return True


class _StubRiskState:
    def __init__(
        self,
        *,
        kill_switch_active: bool = False,
        kill_switch_reason: str | None = None,
    ) -> None:
        self.kill_switch_active = kill_switch_active
        self.kill_switch_reason = kill_switch_reason


class _StubGovernor:
    def __init__(self, *, kill: bool = False) -> None:
        self._kill = kill
        self.registered: list[str] = []
        self.closed: list[str] = []

    async def register_engine(self, name: str, equity: Decimal, *, limits: Any = None) -> None:
        del equity, limits
        self.registered.append(name)

    async def state_for(self, name: str) -> _StubRiskState | None:
        del name
        return _StubRiskState(kill_switch_active=self._kill)

    async def record_close(self, **kwargs: Any) -> None:
        self.closed.append(kwargs.get("trade_id", "?"))


class _StubDBLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.startup_called = False
        self.shutdown_called = False
        self.shutdown_kwargs: dict = {}

    async def startup(self, *, commit_sha: str | None = None) -> None:
        del commit_sha
        self.startup_called = True
        self.events.append(("startup", {}))

    async def shutdown(self, *, duration_ms: int, exit_code: int) -> None:
        self.shutdown_called = True
        self.shutdown_kwargs = {"duration_ms": duration_ms, "exit_code": exit_code}
        self.events.append(("shutdown", self.shutdown_kwargs))

    async def log(self, event_type: str, message: str, **kwargs: Any) -> None:
        self.events.append((event_type, {"message": message, **kwargs}))

    async def signal(self, ticker: str, **kwargs: Any) -> None:
        self.events.append(("signal", {"ticker": ticker, **kwargs}))

    async def order_submitted(self, ticker: str, **kwargs: Any) -> None:
        self.events.append(("order_submitted", {"ticker": ticker, **kwargs}))

    async def error(self, exc: Exception, *, context: str = "") -> None:
        self.events.append(("error", {"exc": str(exc), "context": context}))


class _StubAcquireCtx:
    async def __aenter__(self) -> Any:
        return _StubConn()

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StubConn:
    async def fetch(self, *args: Any, **kwargs: Any) -> list:
        del args, kwargs
        return []

    async def fetchrow(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return None


class _StubPool:
    def acquire(self) -> _StubAcquireCtx:
        return _StubAcquireCtx()

    async def close(self) -> None:
        return None


# ── Helpers ─────────────────────────────────────────────────────────────


def _install_stubs(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    """Patch every external seam of carver.scheduler with a stub.

    Returns the stubs the tests want to assert against."""
    import carver.scheduler as sched

    pool = _StubPool()
    broker = overrides.get("broker") or _StubBroker()
    governor = overrides.get("governor") or _StubGovernor()
    db_log = overrides.get("db_log") or _StubDBLog()
    cancel_calls: list[dict] = []

    async def _build_pool(_db_url: str) -> _StubPool:
        return pool

    async def _cancel(broker_, *, order_prefix: str, log_namespace: str) -> int:
        cancel_calls.append({"prefix": order_prefix, "namespace": log_namespace})
        del broker_
        return 0

    async def _gate(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr(sched, "build_asyncpg_pool", _build_pool)
    monkeypatch.setattr(sched, "AlpacaPaperBrokerAdapter", lambda: broker)
    monkeypatch.setattr(sched, "DBLogHandler", lambda **kwargs: db_log)
    monkeypatch.setattr(
        sched, "PostgresRiskStateStore", lambda **kwargs: object(),
    )
    monkeypatch.setattr(sched, "RiskGovernor", lambda **kwargs: governor)
    monkeypatch.setattr(sched, "cancel_stale_orders", _cancel)
    monkeypatch.setattr(sched, "gate_batch_order", _gate)
    # Provide a DATABASE_URL so the scheduler doesn't bail early.
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")

    return {
        "broker": broker,
        "governor": governor,
        "db_log": db_log,
        "pool": pool,
        "cancel_calls": cancel_calls,
    }


# ── Tests ───────────────────────────────────────────────────────────────


def test_non_trading_day_early_returns_with_action_non_trading_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from carver.scheduler import CarverScheduler

    _install_stubs(monkeypatch)
    sched_obj = CarverScheduler()
    # 2026-01-04 is a Sunday -> not a trading day.
    summary = asyncio.run(sched_obj.run_once(as_of=date(2026, 1, 4)))
    assert summary.is_rebalance_day is False


def test_startup_shutdown_bookend_events_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from carver.scheduler import CarverScheduler

    stubs = _install_stubs(monkeypatch)
    sched_obj = CarverScheduler(submit_orders=False)
    asyncio.run(sched_obj.run_once(as_of=date(2026, 1, 5)))  # Mon trading day
    db_log: _StubDBLog = stubs["db_log"]
    assert db_log.startup_called is True
    assert db_log.shutdown_called is True
    assert db_log.shutdown_kwargs.get("exit_code") == 0


def test_kill_switch_pre_flight_blocks_rebalance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from carver.scheduler import CarverScheduler

    stubs = _install_stubs(
        monkeypatch, governor=_StubGovernor(kill=True),
    )
    sched_obj = CarverScheduler()
    summary = asyncio.run(sched_obj.run_once(as_of=date(2026, 1, 5)))
    broker: _StubBroker = stubs["broker"]
    assert broker.placed == []
    assert summary.is_rebalance_day is False


def test_filter_to_engine_holdings_only_returns_cv_prefixed_positions() -> None:
    from carver.scheduler import _filter_to_engine_holdings

    positions = [
        _StubPosition("AAPL", 10),
        _StubPosition("MSFT", 5),
        _StubPosition("GOOG", 3),
    ]
    recent_orders = [
        _StubOrderRecord("AAPL", "cv_AAPL_123"),
        _StubOrderRecord("MSFT", "mo_MSFT_456"),  # momentum's
        _StubOrderRecord("GOOG", "sg_GOOG_789"),  # sigma legacy
    ]
    filtered = _filter_to_engine_holdings(
        positions=positions, recent_orders=recent_orders, prefix="cv_",
    )
    assert filtered == {"AAPL": 10}
    assert "MSFT" not in filtered
    assert "GOOG" not in filtered


def test_stale_order_cancel_delegate_uses_cv_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When submit_orders=True the scheduler must delegate stale cancel
    with order_prefix='cv_' and the carver log namespace."""
    from carver.scheduler import CarverScheduler

    stubs = _install_stubs(monkeypatch)
    sched_obj = CarverScheduler(submit_orders=True)
    asyncio.run(sched_obj.run_once(as_of=date(2026, 1, 5)))
    cancel_calls: list[dict] = stubs["cancel_calls"]
    assert any(
        c["prefix"] == "cv_" and c["namespace"] == "carver.scheduler"
        for c in cancel_calls
    )


def test_run_summary_repr_does_not_raise() -> None:
    from carver.scheduler import RunSummary

    s = RunSummary(
        as_of=date(2026, 1, 5),
        is_rebalance_day=False,
        decision=None,
        submitted_order_ids=[],
        dry_run=True,
    )
    assert "RunSummary" in repr(s)


def test_engine_order_prefix_constant_is_cv() -> None:
    from carver.scheduler import ENGINE_ORDER_PREFIX

    assert ENGINE_ORDER_PREFIX == "cv_"


def test_module_constant_datetime_utc_smoke() -> None:
    # Sentinel: any UTC arithmetic in the scheduler imports cleanly.
    import carver.scheduler  # noqa: F401

    assert datetime.now(UTC).tzinfo is not None
