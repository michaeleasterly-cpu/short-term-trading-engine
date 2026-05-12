"""Trade-monitor tests — unit + integration.

The unit tests cover the pure helpers (``_decimal``, ``_aware``,
``_resolve_tier2_take_profit``, ``_row_from_record``).

The integration test drives a synthetic ``trade_updates`` stream:

  1. Seed ``platform.open_orders`` with a Tier 1 row (engine submitted).
  2. Emit a fill event for that Tier 1's broker order id.
  3. Assert: row status → 'filled', broker.submit_tier1_only called for
     Tier 2, second open_orders row inserted, audit log row written.
  4. Emit a fill event for the Tier 2 broker order id.
  5. Assert: row status → 'filled', AARWriter received an AfterActionReport
     with combined entry/exit prices, risk_store.record_fill bumped.

Both Sigma (two-tier) and Vector (single-tier, no Tier 2 follow-up) are
exercised so the dispatch logic is covered for both shapes.

No network. No real DB. All async via the existing FakePool pattern.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tpcore.trade_monitor import (
    OpenOrderRow,
    TradeMonitor,
    _aware,
    _decimal,
    _resolve_tier2_take_profit,
    _row_from_record,
)

# ─── Fake asyncpg pool/conn supporting fetchrow + execute + fetch ────────


@dataclass
class _Recorded:
    sql: str
    args: tuple


class _FakeConn:
    """Minimal asyncpg connection stand-in.

    Routes ``fetchrow``/``fetch`` through caller-provided handlers so each
    test can decide what each SELECT returns; records all ``execute`` calls
    so the test can assert the right UPDATE/INSERT was issued.
    """

    def __init__(self, *, fetchrow_handler=None, fetch_handler=None) -> None:
        self._fetchrow = fetchrow_handler or (lambda sql, *args: None)
        self._fetch = fetch_handler or (lambda sql, *args: [])
        self.executed: list[_Recorded] = []

    async def fetchrow(self, sql: str, *args) -> Any:
        return self._fetchrow(sql, *args)

    async def fetch(self, sql: str, *args) -> list[Any]:
        return self._fetch(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        self.executed.append(_Recorded(sql=sql, args=args))
        return "UPDATE 1"


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, *, fetchrow_handler=None, fetch_handler=None) -> None:
        self.conn = _FakeConn(fetchrow_handler=fetchrow_handler, fetch_handler=fetch_handler)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


# ─── Stub services for the monitor's dependencies ────────────────────────


class _StubAARWriter:
    def __init__(self) -> None:
        self.written = []

    async def write_aar(self, aar) -> bool:
        self.written.append(aar)
        return True


class _StubRiskStore:
    def __init__(self) -> None:
        self.fills: list[dict] = []

    async def record_fill(self, *, engine: str, realized_pnl, position_delta: int) -> None:
        self.fills.append(
            {"engine": engine, "realized_pnl": realized_pnl, "position_delta": position_delta}
        )


# ─── Fixtures ────────────────────────────────────────────────────────────


def _decision_data_sigma() -> dict:
    return {
        "decision": {
            "ticker": "AAPL",
            "qty": 4,
            "tier1_qty": 2,
            "tier2_qty": 2,
            "notional_usd": "720.00",
            "risk_amount_usd": "21.60",
            "order_payloads": [
                {
                    "client_order_id": "AAPL_1700000000_tier1",
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "2",
                    "type": "market",
                    "time_in_force": "day",
                    "order_class": "bracket",
                    "take_profit": {"limit_price": "184.00"},
                    "stop_loss": {"stop_price": "174.60"},
                },
                {
                    "client_order_id": "AAPL_1700000000_tier2",
                    "symbol": "AAPL",
                    "side": "sell",
                    "qty": "2",
                    "type": "limit",
                    "limit_price": "190.00",
                    "time_in_force": "gtc",
                },
            ],
            "constructed_at": "2026-05-12T13:30:00+00:00",
        },
        "assessment": {
            "ticker": "AAPL",
            "as_of": "2026-05-12",
            "phase": "ACTIVE",
            "entry_price": "180.00",
            "stop_price": "174.60",
            "take_profit_mid": "184.00",
            "take_profit_far": "190.00",
            "notes": "score=80 prox=0.10 adx=15",
        },
    }


def _decision_data_vector() -> dict:
    return {
        "decision": {
            "ticker": "MSFT",
            "qty": 3,
            "notional_usd": "1050.00",
            "risk_amount_usd": "73.50",
            "vix_size_factor": "1.0",
            "order_payloads": [
                {
                    "client_order_id": "MSFT_1700000000",
                    "symbol": "MSFT",
                    "side": "buy",
                    "qty": "3",
                    "type": "market",
                    "time_in_force": "day",
                    "order_class": "bracket",
                    "take_profit": {"limit_price": "402.50"},
                    "stop_loss": {"stop_price": "325.50"},
                },
            ],
            "constructed_at": "2026-05-12T13:30:00+00:00",
        },
        "assessment": {
            "ticker": "MSFT",
            "as_of": "2026-05-12",
            "phase": "ACTIVE",
            "entry_price": "350.00",
            "stop_price": "325.50",
            "profit_target_price": "402.50",
        },
    }


def _make_event(*, event: str, alpaca_id: str, price: str | None = None) -> SimpleNamespace:
    """Build a TradeUpdate-shaped event for ``on_trade_update``."""
    return SimpleNamespace(
        event=event,
        order=SimpleNamespace(id=alpaca_id),
        price=price,
        timestamp=datetime(2026, 5, 12, 19, 55, tzinfo=UTC),
        execution_id=str(uuid.uuid4()),
    )


# ─── Unit tests — pure helpers ──────────────────────────────────────────


def test_decimal_handles_none_empty_and_numeric() -> None:
    assert _decimal(None) is None
    assert _decimal("") is None
    assert _decimal("12.34") == Decimal("12.34")
    assert _decimal(0) == Decimal("0")


def test_aware_normalizes_naive_to_utc() -> None:
    naive = datetime(2026, 5, 12, 13, 30)
    out = _aware(naive)
    assert out is not None
    assert out.tzinfo == UTC


def test_aware_preserves_aware_datetime() -> None:
    aware = datetime(2026, 5, 12, 13, 30, tzinfo=UTC)
    assert _aware(aware) == aware


def test_aware_handles_none() -> None:
    assert _aware(None) is None


def test_resolve_tier2_take_profit_sigma() -> None:
    assert _resolve_tier2_take_profit(
        engine="sigma", assessment={"take_profit_far": "190.00"}
    ) == Decimal("190.00")


def test_resolve_tier2_take_profit_reversion() -> None:
    assert _resolve_tier2_take_profit(
        engine="reversion", assessment={"target_50ma": "215.00"}
    ) == Decimal("215.00")


def test_resolve_tier2_take_profit_vector_returns_none() -> None:
    # Vector has no Tier 2; the dispatch returns None and the monitor's
    # caller short-circuits on missing levels.
    assert _resolve_tier2_take_profit(engine="vector", assessment={}) is None


def test_row_from_record_parses_decimal_and_jsonb_string() -> None:
    record = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "engine": "sigma",
        "trade_id": "AAPL_1700000000",
        "ticker": "AAPL",
        "order_type": "tier1",
        "alpaca_order_id": "alp-1",
        "status": "pending",
        "fill_price": "180.50",
        "decision_data": json.dumps(_decision_data_sigma()),
    }
    row = _row_from_record(record)
    assert row.engine == "sigma"
    assert row.order_type == "tier1"
    assert row.fill_price == Decimal("180.50")
    assert row.decision_data["decision"]["tier2_qty"] == 2


# ─── Integration tests — full event dispatch ────────────────────────────


def _build_monitor(
    pool: _FakePool,
    broker: AsyncMock,
    *,
    aar_writer: _StubAARWriter,
    risk_store: _StubRiskStore,
) -> TradeMonitor:
    return TradeMonitor(
        pool=pool,
        broker=broker,
        aar_writer=aar_writer,
        risk_store=risk_store,
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000abc"),
    )


@pytest.mark.asyncio
async def test_tier1_fill_for_sigma_triggers_tier2_submission() -> None:
    """Sigma Tier 1 fill → broker.submit_tier1_only called for Tier 2 with the
    decision's tier2_qty and the assessment's take_profit_far; new tier2 row
    inserted in open_orders."""
    sigma_tier1 = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "engine": "sigma",
        "trade_id": "AAPL_1700000000",
        "ticker": "AAPL",
        "order_type": "tier1",
        "alpaca_order_id": "alp-tier1",
        "status": "pending",
        "fill_price": None,
        "decision_data": _decision_data_sigma(),
    }

    def fetchrow_handler(sql: str, *args) -> Any:
        # Route the two SELECT queries the monitor issues:
        #   * lookup by alpaca_order_id
        #   * sibling tier lookup (only after tier2 fill, not exercised here)
        if "alpaca_order_id" in sql and args and args[0] == "alp-tier1":
            return sigma_tier1
        return None

    pool = _FakePool(fetchrow_handler=fetchrow_handler)
    broker = AsyncMock()
    broker.submit_tier1_only.return_value = SimpleNamespace(broker_order_id="alp-tier2")
    aar_writer = _StubAARWriter()
    risk_store = _StubRiskStore()

    monitor = _build_monitor(pool, broker, aar_writer=aar_writer, risk_store=risk_store)
    await monitor.on_trade_update(
        _make_event(event="fill", alpaca_id="alp-tier1", price="180.50")
    )

    # Tier 2 submission: same side (buy), tier2_qty=2, far-target TP, hard stop SL.
    broker.submit_tier1_only.assert_awaited_once()
    submit_kwargs = broker.submit_tier1_only.call_args.kwargs
    assert submit_kwargs["ticker"] == "AAPL"
    assert submit_kwargs["qty"] == 2
    assert submit_kwargs["side"] == "buy"
    assert submit_kwargs["take_profit_price"] == Decimal("190.00")
    assert submit_kwargs["stop_loss_price"] == Decimal("174.60")
    assert submit_kwargs["client_order_id"] == "AAPL_1700000000_tier2"
    assert submit_kwargs["engine_id"] == "sigma"

    # DB writes: one UPDATE for the tier1 row status, one INSERT for the tier2 row,
    # plus DBLogHandler audit log writes (also INSERTs into application_log).
    update_sql = [r for r in pool.conn.executed if "UPDATE platform.open_orders" in r.sql]
    insert_sql = [r for r in pool.conn.executed if "INSERT INTO platform.open_orders" in r.sql]
    assert len(update_sql) == 1
    assert len(insert_sql) == 1
    # The UPDATE marks status=filled, fill_price set.
    assert update_sql[0].args[1] == "filled"
    assert update_sql[0].args[2] == Decimal("180.50")
    # The INSERT carries the tier2 broker_order_id.
    assert insert_sql[0].args[0] == "sigma"
    assert insert_sql[0].args[3] == "alp-tier2"


@pytest.mark.asyncio
async def test_tier1_fill_for_vector_does_not_submit_tier2() -> None:
    """Vector has no Tier 2; the monitor sees tier2_qty=0 and short-circuits."""
    vector_tier1 = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
        "engine": "vector",
        "trade_id": "MSFT_1700000000",
        "ticker": "MSFT",
        "order_type": "tier1",
        "alpaca_order_id": "alp-vec",
        "status": "pending",
        "fill_price": None,
        "decision_data": _decision_data_vector(),
    }

    def fetchrow_handler(sql: str, *args) -> Any:
        if "alpaca_order_id" in sql and args and args[0] == "alp-vec":
            return vector_tier1
        return None

    pool = _FakePool(fetchrow_handler=fetchrow_handler)
    broker = AsyncMock()
    monitor = _build_monitor(pool, broker, aar_writer=_StubAARWriter(), risk_store=_StubRiskStore())

    await monitor.on_trade_update(_make_event(event="fill", alpaca_id="alp-vec", price="350.25"))

    broker.submit_tier1_only.assert_not_awaited()
    # Status update still happened (tier1 marked filled).
    update_sql = [r for r in pool.conn.executed if "UPDATE platform.open_orders" in r.sql]
    assert len(update_sql) == 1


@pytest.mark.asyncio
async def test_tier2_fill_writes_aar_and_bumps_risk_state() -> None:
    """A Tier 2 fill closes the trade: AAR row written with combined entry/exit,
    risk_store.record_fill called with position_delta=-1."""
    tier1_filled = OpenOrderRow(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        engine="sigma",
        trade_id="AAPL_1700000000",
        ticker="AAPL",
        order_type="tier1",
        alpaca_order_id="alp-tier1",
        status="filled",
        fill_price=Decimal("180.00"),
        decision_data=_decision_data_sigma(),
    )
    tier2_pending = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
        "engine": "sigma",
        "trade_id": "AAPL_1700000000",
        "ticker": "AAPL",
        "order_type": "tier2",
        "alpaca_order_id": "alp-tier2",
        "status": "pending",
        "fill_price": None,
        "decision_data": _decision_data_sigma(),
    }

    def fetchrow_handler(sql: str, *args) -> Any:
        if "alpaca_order_id = $1" in sql and args[0] == "alp-tier2":
            return tier2_pending
        if "order_type = $3" in sql and args == ("sigma", "AAPL_1700000000", "tier1"):
            return {
                "id": tier1_filled.id,
                "engine": tier1_filled.engine,
                "trade_id": tier1_filled.trade_id,
                "ticker": tier1_filled.ticker,
                "order_type": tier1_filled.order_type,
                "alpaca_order_id": tier1_filled.alpaca_order_id,
                "status": tier1_filled.status,
                "fill_price": str(tier1_filled.fill_price),
                "decision_data": tier1_filled.decision_data,
            }
        return None

    pool = _FakePool(fetchrow_handler=fetchrow_handler)
    broker = AsyncMock()
    aar_writer = _StubAARWriter()
    risk_store = _StubRiskStore()
    monitor = _build_monitor(pool, broker, aar_writer=aar_writer, risk_store=risk_store)

    await monitor.on_trade_update(_make_event(event="fill", alpaca_id="alp-tier2", price="190.50"))

    assert len(aar_writer.written) == 1
    aar = aar_writer.written[0]
    assert aar.engine == "sigma"
    assert aar.ticker == "AAPL"
    # Weighted avg entry: (180 * 2 + 190.5 * 2) / 4 = 185.25; exit_avg = 190.50.
    assert aar.entry_price == Decimal("185.25")
    assert aar.exit_price == Decimal("190.50")
    assert aar.qty == Decimal("4")
    assert aar.pnl_gross == Decimal("21.00")
    assert len(risk_store.fills) == 1
    assert risk_store.fills[0]["engine"] == "sigma"
    assert risk_store.fills[0]["position_delta"] == -1


@pytest.mark.asyncio
async def test_unmatched_fill_is_ignored_silently() -> None:
    """Fills for orders we don't track (smoke tests, manual orders) are silently
    skipped — no broker calls, no DB writes apart from the audit log."""
    pool = _FakePool(fetchrow_handler=lambda sql, *args: None)
    broker = AsyncMock()
    monitor = _build_monitor(pool, broker, aar_writer=_StubAARWriter(), risk_store=_StubRiskStore())

    await monitor.on_trade_update(
        _make_event(event="fill", alpaca_id="alp-unknown", price="100.00")
    )

    broker.submit_tier1_only.assert_not_awaited()
    update_sql = [r for r in pool.conn.executed if "UPDATE platform.open_orders" in r.sql]
    assert update_sql == []


@pytest.mark.asyncio
async def test_cancelled_event_marks_row_cancelled() -> None:
    """A cancellation event flips status without triggering Tier 2."""
    sigma_tier1 = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "engine": "sigma",
        "trade_id": "AAPL_1700000000",
        "ticker": "AAPL",
        "order_type": "tier1",
        "alpaca_order_id": "alp-tier1",
        "status": "pending",
        "fill_price": None,
        "decision_data": _decision_data_sigma(),
    }

    def fetchrow_handler(sql: str, *args) -> Any:
        if args and args[0] == "alp-tier1":
            return sigma_tier1
        return None

    pool = _FakePool(fetchrow_handler=fetchrow_handler)
    broker = AsyncMock()
    monitor = _build_monitor(pool, broker, aar_writer=_StubAARWriter(), risk_store=_StubRiskStore())

    await monitor.on_trade_update(_make_event(event="canceled", alpaca_id="alp-tier1"))

    broker.submit_tier1_only.assert_not_awaited()
    update_sql = [r for r in pool.conn.executed if "UPDATE platform.open_orders" in r.sql]
    assert len(update_sql) == 1
    assert update_sql[0].args[1] == "cancelled"
