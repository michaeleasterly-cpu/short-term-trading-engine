"""Tests for ``scripts/ops.py::_stage_seed_monotone_snapshots`` — the
operator one-shot bulk-seed stage that lands the per-ticker monotone
baselines for ``sec_insider_monotone`` + ``earnings_events_monotone``
in a single set-based ``INSERT ... SELECT``.

The Python-in-check seed loop times out against the Supavisor pooler
on a fresh DB (read in test_data_validation_9_failures audit
2026-05-21). This stage replaces that loop with one SQL round-trip per
table, so the seed lands in seconds regardless of universe size.

These are unit tests against a fake asyncpg pool — verify the SQL the
stage emits + the structure of its return dict. The DB-side semantics
are pinned by the corresponding alembic migrations + the live
sec_insider_monotone / earnings_events_monotone tests in
tpcore/quality/validation/tests/.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load scripts/ops.py by path — canonical ops-shadow pattern.
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_seed_monotone", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_seed_monotone"] = ops
_spec.loader.exec_module(ops)


# pytest-xdist: ops-shadow tests pin to a single worker.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _FakeConn:
    """Stand-in for the asyncpg connection acquired from the pool.

    Records every ``execute`` / ``fetchval`` call so the test can assert
    the SQL the stage emits + return values it sees.
    """

    def __init__(self, *, sec_count: int, earnings_count: int) -> None:
        self.execute_calls: list[str] = []
        self._fetchval_returns = iter([sec_count, earnings_count])

    async def execute(self, sql: str) -> str:
        self.execute_calls.append(sql)
        # Match what asyncpg returns for an INSERT statement.
        return "INSERT 0 1"

    async def fetchval(self, _sql: str) -> int:
        return next(self._fetchval_returns)


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_seed_monotone_snapshots_runs_both_upserts_and_returns_counts() -> None:
    """Stage must emit one bulk INSERT per snapshot table (sec_insider +
    earnings_events) and return the post-write per-table row counts the
    operator audit log relies on."""
    conn = _FakeConn(sec_count=1306, earnings_count=1104)
    pool = _FakePool(conn)

    result = await ops._stage_seed_monotone_snapshots(pool, None)  # noqa: SLF001

    # Both bulk INSERTs ran, in order (sec_insider first, then earnings).
    assert len(conn.execute_calls) == 2, conn.execute_calls
    first, second = conn.execute_calls
    assert "platform.sec_insider_row_counts_snapshot" in first
    assert "platform.sec_insider_transactions" in first
    assert "ON CONFLICT (ticker)" in first
    assert "platform.earnings_events_count_snapshot" in second
    assert "platform.earnings_events" in second
    assert "EARNINGS_BEAT" in second and "EARNINGS_NO_BEAT" in second
    assert "ON CONFLICT (ticker)" in second

    # Return shape matches the audit-doc contract.
    assert result == {
        "sec_insider_row_counts_snapshot_rows": 1306,
        "earnings_events_count_snapshot_rows": 1104,
    }


@pytest.mark.asyncio
async def test_seed_monotone_snapshots_accepts_none_cfg() -> None:
    """Stage discards cfg — operator may pass None or {} interchangeably."""
    conn = _FakeConn(sec_count=0, earnings_count=0)
    pool = _FakePool(conn)
    result_none = await ops._stage_seed_monotone_snapshots(pool, None)  # noqa: SLF001
    assert result_none["sec_insider_row_counts_snapshot_rows"] == 0

    conn = _FakeConn(sec_count=42, earnings_count=7)
    pool = _FakePool(conn)
    result_empty = await ops._stage_seed_monotone_snapshots(pool, {})  # noqa: SLF001
    assert result_empty["sec_insider_row_counts_snapshot_rows"] == 42
    assert result_empty["earnings_events_count_snapshot_rows"] == 7


def test_seed_monotone_snapshots_in_known_stages() -> None:
    """Stage must be registered in ``KNOWN_STAGES`` so ``--stage
    seed_monotone_snapshots`` resolves at the CLI level."""
    assert "seed_monotone_snapshots" in ops.KNOWN_STAGES


def test_seed_monotone_snapshots_not_in_update_pipeline() -> None:
    """Stage is operator-on-demand only — NOT in ``OPS_UPDATE_STAGES``,
    so the daily ``--update`` cadence never invokes it. (Reseeding the
    baseline is a deliberate operator action, not a recurring cron
    step.)"""
    if hasattr(ops, "OPS_UPDATE_STAGES"):
        assert "seed_monotone_snapshots" not in ops.OPS_UPDATE_STAGES
