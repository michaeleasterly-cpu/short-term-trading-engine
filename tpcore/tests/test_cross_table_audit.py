"""Unit tests for the structured cross-table audit SoT.

Pure: a fake asyncpg pool whose fetchval returns a scripted count per
check and whose executemany records the persisted rows. No DB.
"""
from __future__ import annotations

from decimal import Decimal

from tpcore.audit.cross_table import (
    CROSS_TABLE_CHECKS,
    CrossTableCheck,
    run_cross_table_audit,
)


class _Conn:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self.persisted: list[tuple] = []

    async def fetchval(self, sql: str):
        for cn, n in self._counts.items():
            if f"/*{cn}*/" in sql:
                return n
        return 0

    async def executemany(self, sql: str, rows) -> None:
        self.persisted.extend(rows)


class _CM:
    def __init__(self, conn: _Conn) -> None:
        self._c = conn

    async def __aenter__(self) -> _Conn:
        return self._c

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, counts: dict[str, int]) -> None:
        self.conn = _Conn(counts)

    def acquire(self) -> _CM:
        return _CM(self.conn)


def test_sot_is_nonempty_and_well_formed() -> None:
    assert CROSS_TABLE_CHECKS, "cross-table SoT must not be empty"
    for c in CROSS_TABLE_CHECKS:
        assert isinstance(c, CrossTableCheck)
        assert c.kind == "violation_count"
        assert c.table and c.check_name and c.sql
        assert f"/*{c.check_name}*/" in c.sql
        assert "." not in c.table and "." not in c.check_name


def test_tradier_orphan_predicate_matches_cross_ref_cleanup() -> None:
    orphan = next(
        c for c in CROSS_TABLE_CHECKS
        if c.table == "tradier_options_chains"
        and c.check_name == "orphan_no_prices"
    )
    assert "prices_daily_tickers" in orphan.sql
    assert "NOT EXISTS" in orphan.sql.upper()
    expired = next(
        c for c in CROSS_TABLE_CHECKS
        if c.table == "tradier_options_chains"
        and c.check_name == "expiration_in_past"
    )
    assert "expiration_date < CURRENT_DATE" in expired.sql


async def test_run_persists_fail_and_ok_rows_with_convention() -> None:
    target = CROSS_TABLE_CHECKS[0]
    pool = _Pool({target.check_name: 3})
    findings = await run_cross_table_audit(pool, persist=True)

    by_key = {(f.table, f.check_name): f for f in findings}
    red = by_key[(target.table, target.check_name)]
    assert red.count == 3 and red.severity == "FAIL"

    persisted = {r[0]: r for r in pool.conn.persisted}
    red_src = f"cross_table_audit.{target.table}.{target.check_name}"
    assert red_src in persisted
    row = persisted[red_src]
    assert row[2] == 0 and row[3] == 0
    assert row[4] is True
    assert row[5] == Decimal("0.000")
    green = next(f for f in findings if f.severity == "OK")
    g_src = f"cross_table_audit.{green.table}.{green.check_name}"
    assert persisted[g_src][4] is False
    assert persisted[g_src][5] == Decimal("1.000")


async def test_persist_false_skips_writes() -> None:
    pool = _Pool({})
    findings = await run_cross_table_audit(pool, persist=False)
    assert findings and pool.conn.persisted == []
