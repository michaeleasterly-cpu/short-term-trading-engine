"""Tests for Wave-4 E10: ``tpcore.order_management.execution_risk_skip``.

Contract under test:

* On plug success (sync or async), the result is returned unchanged.
* On plug exception, ``execute_with_risk_skip`` returns ``None`` —
  the trade is SKIPPED, not re-raised; the engine cycle continues.
* The ``EXECUTION_RISK_ESCALATED`` event lands in
  ``platform.application_log`` with the engine + ticker + error class.
* When ``cancel_in_flight`` is provided, it's called before the
  escalation emit; a raising cancel hook is logged but does NOT crash
  the helper (self-heal must never raise).
"""
from __future__ import annotations

import json

from tpcore.order_management.execution_risk_skip import (
    EXECUTION_RISK_ESCALATED_EVENT,
    execute_with_risk_skip,
)


class _FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return None


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


async def test_sync_plug_success_returns_result() -> None:
    pool = _FakePool()
    result = await execute_with_risk_skip(
        lambda: {"ok": True},
        pool=pool,
        engine="reversion",
        ticker="AAPL",
    )
    assert result == {"ok": True}
    # No escalation emit on success.
    assert pool.conn.executes == []


async def test_async_plug_success_returns_awaited_result() -> None:
    pool = _FakePool()

    async def _decide():
        return {"shape": "tier1_bracket"}

    result = await execute_with_risk_skip(
        _decide,
        pool=pool,
        engine="vector",
        ticker="MSFT",
    )
    assert result == {"shape": "tier1_bracket"}
    assert pool.conn.executes == []


async def test_plug_exception_returns_none_and_emits_escalation() -> None:
    pool = _FakePool()

    def _decide():
        raise ValueError("non-positive entry price")

    result = await execute_with_risk_skip(
        _decide,
        pool=pool,
        engine="reversion",
        ticker="AAPL",
        telemetry={"sizing_input": "10000"},
    )
    assert result is None
    # One INSERT INTO platform.application_log with EXECUTION_RISK_ESCALATED.
    assert len(pool.conn.executes) == 1
    sql, args = pool.conn.executes[0]
    assert "INSERT INTO platform.application_log" in sql
    assert args[0] == "reversion"  # engine
    assert args[2] == EXECUTION_RISK_ESCALATED_EVENT
    assert args[3] == "ERROR"
    payload = json.loads(args[5])
    assert payload["ticker"] == "AAPL"
    assert payload["error_type"] == "ValueError"
    assert "non-positive entry price" in payload["error_message"]
    # Telemetry was merged into the payload.
    assert payload["sizing_input"] == "10000"


async def test_cancel_hook_called_before_emit() -> None:
    pool = _FakePool()
    cancel_calls = []

    async def _cancel():
        cancel_calls.append(True)

    def _decide():
        raise RuntimeError("plug broken")

    result = await execute_with_risk_skip(
        _decide,
        pool=pool,
        engine="vector",
        ticker="MSFT",
        cancel_in_flight=_cancel,
    )
    assert result is None
    # Cancel ran exactly once.
    assert cancel_calls == [True]
    # Escalation still emitted.
    assert len(pool.conn.executes) == 1


async def test_cancel_hook_raise_is_swallowed() -> None:
    """A raising cancel hook must NOT crash the self-heal path."""
    pool = _FakePool()

    async def _cancel():
        raise RuntimeError("broker offline")

    def _decide():
        raise ValueError("orig")

    # No raise — the helper swallows the cancel exception AND still
    # emits the escalation.
    result = await execute_with_risk_skip(
        _decide,
        pool=pool,
        engine="reversion",
        ticker="AAPL",
        cancel_in_flight=_cancel,
    )
    assert result is None
    assert len(pool.conn.executes) == 1


async def test_no_pool_skips_emit_but_still_returns_none() -> None:
    """``pool=None`` is the test/no-DB shape — escalation emit is
    skipped but the trade-skip behavior is unchanged."""

    def _decide():
        raise ValueError("orig")

    result = await execute_with_risk_skip(
        _decide,
        pool=None,
        engine="reversion",
        ticker="AAPL",
    )
    assert result is None
