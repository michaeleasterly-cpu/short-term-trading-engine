"""Tests for the zero-tolerance sec_insider_monotone invariant.

The check compares per-ticker live ``COUNT(*)`` on
``platform.insider_transactions`` against the prior baseline in
``platform.sec_insider_row_counts_snapshot``, then UPSERTs the new
baseline on PASS — all in a single transaction. Tests pin behavior with
a fake asyncpg pool that records the SQL it sees + the UPSERT writes,
mirroring the test_check_corporate_actions_completeness fake-pool
pattern but driving the test through ``_evaluate``'s real transactional
shape (no module-level patching).
"""
from __future__ import annotations

from typing import Any

from tpcore.quality.validation.checks.sec_insider_monotone import (
    CHECK_NAME,
    check_sec_insider_monotone,
    compute_sec_monotone_repair_targets,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetch(
        self, sql: str, *args: object
    ) -> list[dict[str, Any]]:
        # Two distinct SELECTs are issued — route by SQL substring.
        # Live per-ticker counts.
        if "FROM platform.insider_transactions" in sql:
            return [
                {"ticker": t, "rowcount": c}
                for t, c in self._owner.live_counts.items()
            ]
        # Prior baseline (FOR UPDATE locked).
        if "platform.sec_insider_row_counts_snapshot" in sql:
            return [
                {"ticker": t, "rowcount": c}
                for t, c in self._owner.snapshot.items()
            ]
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def execute(self, sql: str, *args: object) -> str:
        # Only the UPSERT path uses execute().
        if "INSERT INTO platform.sec_insider_row_counts_snapshot" in sql:
            ticker = args[0]
            count = args[1]
            assert isinstance(ticker, str)
            assert isinstance(count, int)
            self._owner.snapshot[ticker] = count
            self._owner.upserts.append((ticker, count))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    def transaction(self) -> _TxCM:
        return _TxCM()


class _TxCM:
    async def __aenter__(self) -> _TxCM:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(
        self,
        live_counts: dict[str, int],
        snapshot: dict[str, int] | None = None,
    ) -> None:
        self.live_counts = dict(live_counts)
        self.snapshot: dict[str, int] = dict(snapshot or {})
        self.upserts: list[tuple[str, int]] = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


# ── C1 — all per-ticker counts ≥ snapshot → PASS ─────────────────────


async def test_C1_all_counts_at_or_above_snapshot_passes() -> None:
    """Live ≥ prior across every ticker → pass + snapshot updated."""
    pool = _Pool(
        live_counts={"AAPL": 1200, "MSFT": 900, "GOOG": 450},
        snapshot={"AAPL": 1200, "MSFT": 850, "GOOG": 450},
    )
    result = await check_sec_insider_monotone(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.name == CHECK_NAME
    # Snapshot was upserted with the new (live) values.
    assert pool.snapshot == {"AAPL": 1200, "MSFT": 900, "GOOG": 450}


# ── C2 — single ticker decreased → FAIL ──────────────────────────────


async def test_C2_one_ticker_decreased_fails() -> None:
    """Any negative delta on ANY ticker fails — zero tolerance."""
    pool = _Pool(
        live_counts={"AAPL": 1100, "MSFT": 900},  # AAPL lost 100
        snapshot={"AAPL": 1200, "MSFT": 900},
    )
    result = await check_sec_insider_monotone(pool)
    assert result.passed is False
    assert result.failed == 1
    fail = result.failures[0]
    assert fail.ticker == "AAPL"
    assert fail.reason == "rowcount_decreased"
    # Snapshot must NOT be downgraded on FAIL — keeps the original floor.
    assert pool.snapshot == {"AAPL": 1200, "MSFT": 900}
    assert pool.upserts == []


# ── C3 — first run with empty snapshot → PASS + seed ─────────────────


async def test_C3_first_run_empty_snapshot_passes_and_seeds() -> None:
    """No prior snapshot rows → pass (seed) + snapshot now populated."""
    pool = _Pool(
        live_counts={"AAPL": 1200, "MSFT": 900},
        snapshot={},  # never run before
    )
    result = await check_sec_insider_monotone(pool)
    assert result.passed is True
    assert result.failed == 0
    # Seeded — next run will gate against these values.
    assert pool.snapshot == {"AAPL": 1200, "MSFT": 900}
    # Two UPSERTs (one per ticker).
    assert sorted(pool.upserts) == [("AAPL", 1200), ("MSFT", 900)]


# ── C4 — clean pass updates snapshot (explicit table-write check) ────


async def test_C4_pass_updates_snapshot_in_place() -> None:
    """On PASS the snapshot table must reflect the NEW live counts —
    not the prior frozen ones."""
    pool = _Pool(
        live_counts={"AAPL": 1500, "MSFT": 1000},  # both grew
        snapshot={"AAPL": 1200, "MSFT": 900},
    )
    result = await check_sec_insider_monotone(pool)
    assert result.passed is True
    assert pool.snapshot == {"AAPL": 1500, "MSFT": 1000}
    assert sorted(pool.upserts) == [("AAPL", 1500), ("MSFT", 1000)]


# ── C5 — multi-ticker decrease — sample is capped, count is full ────


async def test_C5_many_decreases_capped_failure_list_but_full_count() -> None:
    """If many tickers decreased, the FailureDetail list is sampled but
    CheckResult.failed reflects the TRUE count."""
    live: dict[str, int] = {f"T{i:02d}": 100 for i in range(10)}
    snap: dict[str, int] = {f"T{i:02d}": 200 for i in range(10)}
    pool = _Pool(live_counts=live, snapshot=snap)
    result = await check_sec_insider_monotone(pool)
    assert result.passed is False
    assert result.failed == 10  # true count
    assert len(result.failures) <= 5  # sample cap


# ── C6 — healer symmetry: empty when no decrease ─────────────────────


async def test_C6_repair_targets_empty_on_clean() -> None:
    pool = _Pool(
        live_counts={"AAPL": 1200, "MSFT": 900},
        snapshot={"AAPL": 1200, "MSFT": 900},
    )
    targets = await compute_sec_monotone_repair_targets(pool)
    assert targets == []


async def test_C6b_repair_targets_lists_decreased() -> None:
    pool = _Pool(
        live_counts={"AAPL": 1000, "MSFT": 900, "GOOG": 400},
        snapshot={"AAPL": 1200, "MSFT": 900, "GOOG": 450},
    )
    targets = await compute_sec_monotone_repair_targets(pool)
    assert set(targets) == {"AAPL", "GOOG"}
