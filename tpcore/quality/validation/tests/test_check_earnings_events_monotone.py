"""Tests for the zero-tolerance earnings_events_monotone invariant.

The check compares per-ticker live ``COUNT(*) WHERE
event_type IN ('EARNINGS_BEAT','EARNINGS_NO_BEAT')`` on
``platform.earnings_events`` against the prior baseline in
``platform.earnings_events_count_snapshot``, then UPSERTs the new
baseline on PASS — all in a single transaction. Tests pin behavior
with a fake asyncpg pool that records the SQL it sees + the UPSERT
writes, mirroring the test_check_sec_insider_monotone fake-pool
pattern but driving the test through ``_evaluate``'s real
transactional shape (no module-level patching).

The fake pool's ``fetch`` routes the live-count SELECT on the
``EARNINGS_BEAT`` substring (the SQL emits the BEAT + NO_BEAT IN
list) — fixture callers feed in the live total (BEAT + NO_BEAT
combined) under the ``live_counts`` mapping. The snapshot table
column is ``beat_count`` for column-name history reasons but the
semantics today are the full reported-earnings count.
"""
from __future__ import annotations

from typing import Any

from tpcore.quality.validation.checks.earnings_events_monotone import (
    CHECK_NAME,
    check_earnings_events_monotone,
    compute_earnings_events_repair_targets,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetch(
        self, sql: str, *args: object
    ) -> list[dict[str, Any]]:
        # Two distinct SELECTs are issued — route by SQL substring.
        # Live per-ticker reported-earnings counts (BEAT + NO_BEAT
        # union). The check's SQL filter is
        # ``event_type IN ('EARNINGS_BEAT', 'EARNINGS_NO_BEAT')``; we
        # assert both literals appear so a regression to BEAT-only
        # reds the test rather than silently working.
        if (
            "FROM platform.earnings_events" in sql
            and "EARNINGS_BEAT" in sql
        ):
            assert "EARNINGS_NO_BEAT" in sql, (
                "live-counts SQL must filter on the BEAT + NO_BEAT "
                "union; BEAT-only is the prior KNOWN GAP and would "
                "miss FMP-outage'd quarters."
            )
            return [
                {"ticker": t, "beat_count": c}
                for t, c in self._owner.live_counts.items()
            ]
        # Prior baseline (FOR UPDATE locked).
        if "platform.earnings_events_count_snapshot" in sql:
            return [
                {"ticker": t, "beat_count": c}
                for t, c in self._owner.snapshot.items()
            ]
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def execute(self, sql: str, *args: object) -> str:
        # Only the UPSERT path uses execute().
        if "INSERT INTO platform.earnings_events_count_snapshot" in sql:
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
        live_counts={"AAPL": 12, "MSFT": 9, "GOOG": 4},
        snapshot={"AAPL": 12, "MSFT": 8, "GOOG": 4},
    )
    result = await check_earnings_events_monotone(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.name == CHECK_NAME
    # Snapshot was upserted with the new (live) values.
    assert pool.snapshot == {"AAPL": 12, "MSFT": 9, "GOOG": 4}


# ── C2 — single ticker decreased → FAIL ──────────────────────────────


async def test_C2_one_ticker_decreased_fails() -> None:
    """Any negative delta on ANY ticker fails — zero tolerance."""
    pool = _Pool(
        live_counts={"AAPL": 11, "MSFT": 9},  # AAPL lost an earnings row
        snapshot={"AAPL": 12, "MSFT": 9},
    )
    result = await check_earnings_events_monotone(pool)
    assert result.passed is False
    assert result.failed == 1
    fail = result.failures[0]
    assert fail.ticker == "AAPL"
    assert fail.reason == "earnings_count_decreased"
    # Snapshot must NOT be downgraded on FAIL — keeps the original floor.
    assert pool.snapshot == {"AAPL": 12, "MSFT": 9}
    assert pool.upserts == []


# ── C3 — first run with empty snapshot → PASS + seed ─────────────────


async def test_C3_first_run_empty_snapshot_passes_and_seeds() -> None:
    """No prior snapshot rows → pass (seed) + snapshot now populated."""
    pool = _Pool(
        live_counts={"AAPL": 12, "MSFT": 9},
        snapshot={},  # never run before
    )
    result = await check_earnings_events_monotone(pool)
    assert result.passed is True
    assert result.failed == 0
    # Seeded — next run will gate against these values.
    assert pool.snapshot == {"AAPL": 12, "MSFT": 9}
    # Two UPSERTs (one per ticker).
    assert sorted(pool.upserts) == [("AAPL", 12), ("MSFT", 9)]


# ── C4 — clean pass updates snapshot (explicit table-write check) ────


async def test_C4_pass_updates_snapshot_in_place() -> None:
    """On PASS the snapshot table must reflect the NEW live counts —
    not the prior frozen ones."""
    pool = _Pool(
        live_counts={"AAPL": 15, "MSFT": 10},  # both grew
        snapshot={"AAPL": 12, "MSFT": 9},
    )
    result = await check_earnings_events_monotone(pool)
    assert result.passed is True
    assert pool.snapshot == {"AAPL": 15, "MSFT": 10}
    assert sorted(pool.upserts) == [("AAPL", 15), ("MSFT", 10)]


# ── C5 — multi-ticker decrease — sample is capped, count is full ────


async def test_C5_many_decreases_capped_failure_list_but_full_count() -> None:
    """If many tickers decreased, the FailureDetail list is sampled but
    CheckResult.failed reflects the TRUE count."""
    live: dict[str, int] = {f"T{i:02d}": 1 for i in range(10)}
    snap: dict[str, int] = {f"T{i:02d}": 5 for i in range(10)}
    pool = _Pool(live_counts=live, snapshot=snap)
    result = await check_earnings_events_monotone(pool)
    assert result.passed is False
    assert result.failed == 10  # true count
    assert len(result.failures) <= 5  # sample cap


# ── C6 — healer symmetry: empty when no decrease ─────────────────────


async def test_C6_repair_targets_empty_on_clean() -> None:
    pool = _Pool(
        live_counts={"AAPL": 12, "MSFT": 9},
        snapshot={"AAPL": 12, "MSFT": 9},
    )
    targets = await compute_earnings_events_repair_targets(pool)
    assert targets == []


async def test_C6b_repair_targets_lists_decreased() -> None:
    pool = _Pool(
        live_counts={"AAPL": 10, "MSFT": 9, "GOOG": 3},
        snapshot={"AAPL": 12, "MSFT": 9, "GOOG": 4},
    )
    targets = await compute_earnings_events_repair_targets(pool)
    assert set(targets) == {"AAPL", "GOOG"}


# ── C7 — NO_BEAT rows count toward the monotone gate ─────────────────


async def test_C7_no_beat_rows_increment_count_alongside_beats() -> None:
    """A delisting / quarter-of-misses scenario: the most recent
    reporting event was a NO_BEAT, not a BEAT. The new live count
    (BEAT + NO_BEAT union) is the prior + 1 — that's a legitimate
    monotone increment, NOT a regression.

    Pre-NO_BEAT-sentinel, this same scenario would have left the
    invariant blind (no row written for the miss). Post-sentinel, the
    NO_BEAT row lands in ``platform.earnings_events`` and the live
    union count reflects it. The fake pool routes the live-counts
    SELECT only when the SQL filter includes both ``EARNINGS_BEAT``
    AND ``EARNINGS_NO_BEAT`` (asserted in ``_Conn.fetch``), so this
    test simultaneously pins the SQL shape and the count semantics.
    """
    # AAPL had 12 BEATs at last snapshot; this quarter reported a
    # miss → NO_BEAT row inserted. Live union = 12 BEAT + 1 NO_BEAT
    # = 13. MSFT unchanged. Both monotone-non-decrease.
    pool = _Pool(
        live_counts={"AAPL": 13, "MSFT": 9},
        snapshot={"AAPL": 12, "MSFT": 9},
    )
    result = await check_earnings_events_monotone(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    # New baseline absorbs the NO_BEAT — next run gates against 13.
    assert pool.snapshot == {"AAPL": 13, "MSFT": 9}
    assert sorted(pool.upserts) == [("AAPL", 13), ("MSFT", 9)]
