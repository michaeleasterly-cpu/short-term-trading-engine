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
        # Persisted rows are now captured per-row via the canonical
        # write_row path (kind='validation' + jsonb notes, uuid PK, no
        # ON CONFLICT) — fetchrow returns RETURNING 1 so write_row reports
        # a successful write.
        self.persisted: list[tuple] = []

    async def fetchval(self, sql: str):
        for cn, n in self._counts.items():
            if f"/*{cn}*/" in sql:
                return n
        return 0

    async def fetchrow(self, sql: str, *args):
        # write_row INSERT … RETURNING 1; record (kind, source, ts,
        # latency_ms, missing_bars, stale, confidence, notes).
        self.persisted.append(args)
        return {"?column?": 1}


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
        assert f"/*{c.key}*/" in c.sql
        assert "." not in c.table and "." not in c.check_name
    assert len({c.key for c in CROSS_TABLE_CHECKS}) == len(CROSS_TABLE_CHECKS), "duplicate (table, check_name) key"


def test_tradier_checks_removed_with_dropped_table() -> None:
    # Plan 2 / migration 0300 drops platform.tradier_options_chains (Tradier
    # closed). No cross-table check may read the dropped table.
    assert not any(c.table == "tradier_options_chains" for c in CROSS_TABLE_CHECKS)


async def test_run_persists_fail_and_ok_rows_with_convention() -> None:
    target = CROSS_TABLE_CHECKS[0]
    pool = _Pool({target.key: 3})
    findings = await run_cross_table_audit(pool, persist=True)

    by_key = {(f.table, f.check_name): f for f in findings}
    red = by_key[(target.table, target.check_name)]
    assert red.count == 3 and red.severity == "FAIL"

    # write_row args: (kind, source, timestamp, latency_ms, missing_bars,
    #                  stale, confidence, notes_jsonb_text)
    persisted = {r[1]: r for r in pool.conn.persisted}
    red_src = f"cross_table_audit.{target.table}.{target.check_name}"
    assert red_src in persisted
    row = persisted[red_src]
    assert row[0] == "validation"  # kind discriminator
    assert row[3] == 0 and row[4] == 0  # latency_ms, missing_bars
    assert row[5] is True  # stale
    assert row[6] == Decimal("0.000")  # confidence
    green = next(f for f in findings if f.severity == "OK")
    g_src = f"cross_table_audit.{green.table}.{green.check_name}"
    assert persisted[g_src][5] is False
    assert persisted[g_src][6] == Decimal("1.000")


async def test_persist_false_skips_writes() -> None:
    pool = _Pool({})
    findings = await run_cross_table_audit(pool, persist=False)
    assert findings and pool.conn.persisted == []
