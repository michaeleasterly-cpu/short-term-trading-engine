"""Tests for ``tpcore.risk.PostgresRiskStateStore`` and ``tpcore.db`` helpers.

The store is exercised against a fake asyncpg pool so no live database is
needed. ``normalize_database_url`` has its own focused doctest-style tests.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tpcore.db import normalize_database_url
from tpcore.risk.governor import RiskState
from tpcore.risk.persistent_store import PostgresRiskStateStore

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — minimum surface the store touches
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Records SQL/args and returns canned responses. Mirrors asyncpg.Connection."""

    def __init__(self) -> None:
        self.fetchrow_result: dict | None = None
        self.fetch_result: list[dict] = []
        self.calls: list[tuple[str, str, tuple]] = []  # (op, sql, args)

    async def fetchrow(self, sql: str, *args) -> dict | None:
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_result

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.calls.append(("fetch", sql, args))
        return self.fetch_result

    async def execute(self, sql: str, *args) -> str:
        self.calls.append(("execute", sql, args))
        return "OK"


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


def _state(**overrides: Any) -> RiskState:
    base = dict(
        engine="sigma",
        engine_equity=Decimal("10000"),
        daily_pnl=Decimal("0"),
        weekly_pnl=Decimal("0"),
        open_positions=0,
        daily_reset_at=datetime(2026, 5, 11, 13, 30, tzinfo=UTC),
        weekly_reset_at=datetime(2026, 5, 11, 13, 30, tzinfo=UTC),
        kill_switch_active=False,
        kill_switch_reason=None,
        updated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return RiskState(**base)


# ────────────────────────────────────────────────────────────────────────────
# normalize_database_url
# ────────────────────────────────────────────────────────────────────────────


def test_normalize_database_url_strips_asyncpg_suffix_and_translates_ssl() -> None:
    src = "postgresql+asyncpg://u:p@h:5432/d?ssl=require"
    assert normalize_database_url(src) == "postgresql://u:p@h:5432/d?sslmode=require"


def test_normalize_database_url_passthrough_when_already_clean() -> None:
    src = "postgres://u:p@h/d?sslmode=disable&application_name=ste"
    assert normalize_database_url(src) == src


def test_normalize_database_url_preserves_other_query_params() -> None:
    src = "postgresql+asyncpg://u:p@h/d?ssl=require&pool_max_conns=4"
    out = normalize_database_url(src)
    assert "sslmode=require" in out
    assert "pool_max_conns=4" in out
    assert "+asyncpg" not in out
    assert "ssl=require" not in out  # only as part of "sslmode=require"


# ────────────────────────────────────────────────────────────────────────────
# get / put / list_all
# ────────────────────────────────────────────────────────────────────────────


async def test_get_returns_none_when_engine_missing() -> None:
    pool = _FakePool()
    pool.conn.fetchrow_result = None
    store = PostgresRiskStateStore(pool)
    assert await store.get("ghost") is None
    op, sql, args = pool.conn.calls[-1]
    assert op == "fetchrow"
    assert "WHERE engine = $1" in sql
    assert args == ("ghost",)


async def test_get_materializes_row_into_RiskState() -> None:
    now = datetime.now(UTC)
    pool = _FakePool()
    pool.conn.fetchrow_result = {
        "engine": "sigma",
        "engine_equity": Decimal("10000.0000"),
        "daily_pnl": Decimal("-50.0000"),
        "weekly_pnl": Decimal("-50.0000"),
        "open_positions": 2,
        "daily_reset_at": now + timedelta(days=1),
        "weekly_reset_at": now + timedelta(days=3),
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "updated_at": now,
    }
    store = PostgresRiskStateStore(pool)
    state = await store.get("sigma")
    assert state is not None
    assert state.engine == "sigma"
    assert state.daily_pnl == Decimal("-50.0000")
    assert state.open_positions == 2


async def test_put_uses_upsert_with_all_columns() -> None:
    pool = _FakePool()
    store = PostgresRiskStateStore(pool)
    state = _state(daily_pnl=Decimal("-25"), open_positions=1)
    await store.put(state)
    op, sql, args = pool.conn.calls[-1]
    assert op == "execute"
    assert "INSERT INTO platform.risk_state" in sql
    assert "ON CONFLICT (engine) DO UPDATE" in sql
    # Args are positional and match the column order in the SQL.
    assert args[0] == "sigma"
    assert args[1] == Decimal("10000")
    assert args[2] == Decimal("-25")
    assert args[4] == 1  # open_positions


async def test_list_all_orders_by_engine() -> None:
    pool = _FakePool()
    pool.conn.fetch_result = [
        {
            "engine": "reversion",
            "engine_equity": Decimal("5000"),
            "daily_pnl": Decimal("0"),
            "weekly_pnl": Decimal("0"),
            "open_positions": 0,
            "daily_reset_at": datetime.now(UTC),
            "weekly_reset_at": datetime.now(UTC),
            "kill_switch_active": False,
            "kill_switch_reason": None,
            "updated_at": datetime.now(UTC),
        },
        {
            "engine": "sigma",
            "engine_equity": Decimal("10000"),
            "daily_pnl": Decimal("0"),
            "weekly_pnl": Decimal("0"),
            "open_positions": 1,
            "daily_reset_at": datetime.now(UTC),
            "weekly_reset_at": datetime.now(UTC),
            "kill_switch_active": False,
            "kill_switch_reason": None,
            "updated_at": datetime.now(UTC),
        },
    ]
    store = PostgresRiskStateStore(pool)
    states = await store.list_all()
    assert [s.engine for s in states] == ["reversion", "sigma"]


async def test_set_kill_switch_all_flips_every_engine() -> None:
    pool = _FakePool()
    store = PostgresRiskStateStore(pool)
    await store.set_kill_switch_all(active=True, reason="incident #99")
    op, sql, args = pool.conn.calls[-1]
    assert op == "execute"
    assert "UPDATE platform.risk_state" in sql
    assert args == (True, "incident #99")

    # Deactivating should clear the reason.
    await store.set_kill_switch_all(active=False, reason="resolved")
    _op, _sql, args = pool.conn.calls[-1]
    assert args == (False, None)


# ────────────────────────────────────────────────────────────────────────────
# Optional integration test against a real DB
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not __import__("os").environ.get("RUN_DB_INTEGRATION_TESTS"),
    reason="RUN_DB_INTEGRATION_TESTS not set",
)
async def test_postgres_store_integration_roundtrip() -> None:
    import os

    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        store = PostgresRiskStateStore(pool)
        engine_id = "sigma-integration-test"
        # Clean any leftover row from a prior run.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM platform.risk_state WHERE engine=$1", engine_id)

        state = _state(engine=engine_id, daily_pnl=Decimal("-10.5"), open_positions=2)
        await store.put(state)
        loaded = await store.get(engine_id)
        assert loaded is not None
        assert loaded.daily_pnl == Decimal("-10.5000")
        assert loaded.open_positions == 2

        # Cleanup.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM platform.risk_state WHERE engine=$1", engine_id)
    finally:
        await pool.close()
