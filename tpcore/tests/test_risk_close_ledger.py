"""``record_close`` — the idempotent close-decrement arbiter (#251 B1.2/B1.3).

LIVE-MONEY RiskGovernor. The sacred invariant: a single real close
decrements ``open_positions`` AT MOST ONCE — never twice (fail open),
and every uncertainty branch SKIPS (over-count → tight → safe).

Tests use a fake asyncpg pool that models ``platform.risk_close_ledger``
(PK ``(engine, trade_id)`` → ``ON CONFLICT DO NOTHING``) and
``platform.risk_state`` with the real single-transaction semantics. No
real DB / broker / repo / ``data/`` is touched.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import structlog

from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskState,
)
from tpcore.risk.persistent_store import PostgresRiskStateStore

# ─── Fake asyncpg pool modelling the two tables + ON CONFLICT ────────────


class _FakeConn:
    def __init__(self, store: _FakePool) -> None:
        self._store = store
        self._in_txn = False

    def transaction(self):
        conn = self

        class _Txn:
            async def __aenter__(self):
                conn._in_txn = True
                conn._snapshot = (
                    dict(conn._store.ledger),
                    {k: dict(v) for k, v in conn._store.risk_state.items()},
                )
                return conn

            async def __aexit__(self, exc_type, *_):
                if exc_type is not None:
                    led, rs = conn._snapshot  # rollback
                    conn._store.ledger = led
                    conn._store.risk_state = rs
                conn._in_txn = False
                return False

        return _Txn()

    async def execute(self, sql: str, *args) -> str:
        s = " ".join(sql.split())
        if "INSERT INTO platform.risk_close_ledger" in s:
            if self._store.raise_on_ledger_insert:
                raise RuntimeError("simulated ledger INSERT failure")
            engine, trade_id = args[0], args[1]
            key = (engine, trade_id)
            if key in self._store.ledger:
                return "INSERT 0 0"  # ON CONFLICT DO NOTHING
            self._store.ledger[key] = datetime.now(UTC)
            return "INSERT 0 1"
        if "UPDATE platform.risk_state SET open_positions = GREATEST(0" in s:
            engine = args[0]
            pnl = args[1]
            row = self._store.risk_state.get(engine)
            if row is not None:
                row["open_positions"] = max(0, row["open_positions"] - 1)
                row["daily_pnl"] += pnl
                row["weekly_pnl"] += pnl
            return "UPDATE 1"
        if "DELETE FROM platform.risk_close_ledger" in s:
            cutoff = datetime.now(UTC).timestamp() - 14 * 86400
            before = len(self._store.ledger)
            self._store.ledger = {
                k: v for k, v in self._store.ledger.items() if v.timestamp() >= cutoff
            }
            return f"DELETE {before - len(self._store.ledger)}"
        raise AssertionError(f"unexpected SQL: {s}")


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.ledger: dict[tuple[str, str], datetime] = {}
        self.risk_state: dict[str, dict] = {}
        self.raise_on_ledger_insert = False

    def seed_engine(self, engine: str, *, open_positions: int = 3) -> None:
        self.risk_state[engine] = {
            "open_positions": open_positions,
            "daily_pnl": Decimal("0"),
            "weekly_pnl": Decimal("0"),
        }

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_FakeConn(self))


def _pg_store() -> tuple[PostgresRiskStateStore, _FakePool]:
    pool = _FakePool()
    pool.seed_engine("momentum", open_positions=3)
    return PostgresRiskStateStore(pool=pool), pool  # type: ignore[arg-type]


# ─── Postgres store: core contract ───────────────────────────────────────


async def test_first_close_decrements_once_and_applies_pnl() -> None:
    store, pool = _pg_store()
    applied = await store.record_close("momentum", "t1", Decimal("25"))
    assert applied is True
    assert pool.risk_state["momentum"]["open_positions"] == 2
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("25")
    assert pool.risk_state["momentum"]["weekly_pnl"] == Decimal("25")


async def test_duplicate_trade_id_is_idempotent_no_double_decrement() -> None:
    store, pool = _pg_store()
    assert await store.record_close("momentum", "t1", Decimal("25")) is True
    # Same (engine, trade_id) again — the other path / a retry.
    assert await store.record_close("momentum", "t1", Decimal("25")) is False
    assert pool.risk_state["momentum"]["open_positions"] == 2  # NOT 1
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("25")  # NOT 50


async def test_distinct_trade_ids_each_decrement() -> None:
    store, pool = _pg_store()
    assert await store.record_close("momentum", "t1", Decimal("10")) is True
    assert await store.record_close("momentum", "t2", Decimal("10")) is True
    assert pool.risk_state["momentum"]["open_positions"] == 1
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("20")


async def test_null_trade_id_skips_and_warns() -> None:
    store, pool = _pg_store()
    structlog.testing.LogCapture()
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        applied = await store.record_close("momentum", None, Decimal("99"))
    finally:
        structlog.reset_defaults()
    assert applied is False
    assert pool.risk_state["momentum"]["open_positions"] == 3  # untouched (over-count = safe)
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("0")
    assert any(e.get("log_level") == "warning" for e in cap.entries)


async def test_greatest_zero_floor_holds() -> None:
    store, pool = _pg_store()
    pool.risk_state["momentum"]["open_positions"] = 0
    assert await store.record_close("momentum", "t1", Decimal("0")) is True
    assert pool.risk_state["momentum"]["open_positions"] == 0  # GREATEST(0, …)


async def test_ledger_insert_error_contained_no_decrement() -> None:
    store, pool = _pg_store()
    pool.raise_on_ledger_insert = True
    with pytest.raises(RuntimeError):
        await store.record_close("momentum", "t1", Decimal("25"))
    # Transaction rolled back: NO decrement, NO pnl (never fail open).
    assert pool.risk_state["momentum"]["open_positions"] == 3
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("0")


async def test_concurrent_same_key_nets_exactly_one_decrement() -> None:
    store, pool = _pg_store()
    results = await asyncio.gather(
        store.record_close("momentum", "t1", Decimal("5")),
        store.record_close("momentum", "t1", Decimal("5")),
    )
    assert sorted(results) == [False, True]  # exactly one winner
    assert pool.risk_state["momentum"]["open_positions"] == 2  # net -1, never -2
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("5")


@pytest.mark.parametrize("n", [2, 5, 13])
async def test_idempotency_property_n_applications_one_net_decrement(n: int) -> None:
    store, pool = _pg_store()
    for _ in range(n):
        await store.record_close("momentum", "t1", Decimal("3"))
    assert pool.risk_state["momentum"]["open_positions"] == 2  # exactly one net -1
    assert pool.risk_state["momentum"]["daily_pnl"] == Decimal("3")  # pnl once


# ─── In-memory store parity (identical skip semantics) ───────────────────


async def _seed_inmem() -> InMemoryRiskStateStore:
    store = InMemoryRiskStateStore()
    await store.put(
        RiskState(
            engine="momentum",
            engine_equity=Decimal("10000"),
            open_positions=3,
            daily_reset_at=datetime.now(UTC),
            weekly_reset_at=datetime.now(UTC),
        )
    )
    return store


async def test_inmem_first_close_decrements_once() -> None:
    store = await _seed_inmem()
    assert await store.record_close("momentum", "t1", Decimal("25")) is True
    st = await store.get("momentum")
    assert st.open_positions == 2
    assert st.daily_pnl == Decimal("25")
    assert st.weekly_pnl == Decimal("25")


async def test_inmem_duplicate_is_idempotent() -> None:
    store = await _seed_inmem()
    assert await store.record_close("momentum", "t1", Decimal("25")) is True
    assert await store.record_close("momentum", "t1", Decimal("25")) is False
    st = await store.get("momentum")
    assert st.open_positions == 2  # NOT 1
    assert st.daily_pnl == Decimal("25")  # NOT 50


async def test_inmem_null_trade_id_skips() -> None:
    store = await _seed_inmem()
    assert await store.record_close("momentum", None, Decimal("99")) is False
    st = await store.get("momentum")
    assert st.open_positions == 3
    assert st.daily_pnl == Decimal("0")


async def test_inmem_greatest_zero_floor() -> None:
    store = InMemoryRiskStateStore()
    await store.put(
        RiskState(
            engine="m",
            engine_equity=Decimal("1"),
            open_positions=0,
            daily_reset_at=datetime.now(UTC),
            weekly_reset_at=datetime.now(UTC),
        )
    )
    assert await store.record_close("m", "t1", Decimal("0")) is True
    st = await store.get("m")
    assert st.open_positions == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
