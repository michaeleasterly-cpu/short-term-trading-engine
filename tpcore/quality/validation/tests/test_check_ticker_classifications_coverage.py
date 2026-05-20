"""Tests for the Path D zero-tolerance ticker_classifications drift
invariant.

The check reads two things:

* the LATEST snapshot row from
  ``platform.ticker_classifications_source_count`` (one row per refresh
  — the classifier writes ``source_count = N`` where N is what Alpaca
  returned for that refresh)
* the live ``COUNT(*)`` on ``platform.ticker_classifications``

PASS iff the two are equal AND the snapshot is within
``MAX_AGE_DAYS``. First-run case (no snapshot yet) PASSes with a notice
— the next classify_tickers run seeds the baseline.

Fake-pool pattern mirrors ``test_check_sec_insider_monotone`` /
``test_check_liquidity_tiers_completeness`` — narrow SQL substring
routing, no module-level patching.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.ticker_classifications_freshness import (
    CHECK_NAME,
    MAX_AGE_DAYS,
    check_ticker_classifications_coverage,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetchrow(
        self, sql: str, *args: object
    ) -> dict[str, Any] | None:
        sql_lower = sql.lower()
        # Latest snapshot row probe.
        if (
            "platform.ticker_classifications_source_count" in sql_lower
            and "order by snapshot_at desc" in sql_lower
        ):
            if self._owner.snapshot is None:
                return None
            return {
                "snapshot_at": self._owner.snapshot[0],
                "source_count": self._owner.snapshot[1],
            }
        # Live COUNT(*) probe — exclude the snapshot-table SQL above by
        # asserting "source_count" is NOT in the SQL.
        if (
            "count(*)" in sql_lower
            and "from platform.ticker_classifications" in sql_lower
            and "source_count" not in sql_lower
        ):
            return {"n": self._owner.live_count}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")


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
        *,
        live_count: int,
        snapshot: tuple[datetime, int] | None,
    ) -> None:
        self.live_count = live_count
        # snapshot is either (snapshot_at, source_count) or None
        # (first-run: snapshot table is empty).
        self.snapshot: tuple[datetime, int] | None = snapshot

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


# ── C1 — source_count == live count → PASS ────────────────────────────


async def test_C1_source_count_matches_live_count_passes() -> None:
    """Live ``COUNT(*)`` equals the latest snapshot's ``source_count``,
    snapshot is fresh → PASS."""
    pool = _Pool(
        live_count=13000,
        snapshot=(datetime.now(UTC) - timedelta(days=10), 13000),
    )
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.failures == []
    assert result.name == CHECK_NAME


# ── C2 — source_count drift → FAIL ────────────────────────────────────


async def test_C2_source_count_drift_fails() -> None:
    """Live ``COUNT(*)`` ≠ snapshot's ``source_count`` → FAIL with both
    counts surfaced in the observed text."""
    pool = _Pool(
        live_count=13050,  # 50 more rows than the snapshot recorded
        snapshot=(datetime.now(UTC) - timedelta(days=5), 13000),
    )
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is False
    assert result.failed == 1
    fail = result.failures[0]
    assert fail.reason == "source_count_drift"
    assert "live=13050" in fail.observed
    assert "snapshot=13000" in fail.observed
    assert "delta=50" in fail.observed


# ── C3 — first run (empty snapshot table) → PASS + notice ─────────────


async def test_C3_first_run_no_snapshot_passes() -> None:
    """No snapshot row yet → PASS (the next classify_tickers run will
    seed the baseline). Same bootstrap shape as sec_insider_monotone /
    earnings_events_monotone first-run."""
    pool = _Pool(live_count=0, snapshot=None)
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is True
    assert result.failed == 0
    assert result.failures == []


async def test_C3b_first_run_with_populated_table_still_passes() -> None:
    """Edge case: the table has rows but the snapshot table is empty
    (e.g. immediately after the alembic migration but before the next
    classify_tickers run). First-run pattern means PASS — the next run
    seeds the baseline, the run after that enforces it."""
    pool = _Pool(live_count=13000, snapshot=None)
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is True
    assert result.failed == 0


# ── C4 — freshness floor exceeded → FAIL with stale_snapshot ──────────


async def test_C4_stale_snapshot_fails_even_when_counts_match() -> None:
    """Counts match (no drift) but the snapshot is older than
    ``MAX_AGE_DAYS`` → FAIL with the freshness reason."""
    pool = _Pool(
        live_count=13000,
        snapshot=(
            datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS + 5),
            13000,
        ),
    )
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is False
    assert result.failed == 1
    fail = result.failures[0]
    assert fail.reason == "stale_snapshot"
    assert f"{MAX_AGE_DAYS}d" in fail.expected


# ── C5 — drift AND stale → both failures recorded ─────────────────────


async def test_C5_drift_and_stale_records_both_failures() -> None:
    """Both invariants fail simultaneously — both FailureDetail rows
    appear (no short-circuit). Confidence in CheckResult.failed reflects
    the TRUE per-failure count."""
    pool = _Pool(
        live_count=12500,  # drifted
        snapshot=(
            datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS + 30),
            13000,
        ),
    )
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is False
    assert result.failed == 2
    reasons = {f.reason for f in result.failures}
    assert reasons == {"source_count_drift", "stale_snapshot"}


# ── C6 — empty table + populated snapshot → drift FAIL ────────────────
# (preserves the legacy "empty table is a fail" intent: if a refresh
# wiped the rows but the snapshot still says N, that's drift.)


async def test_C6_empty_table_against_populated_snapshot_fails() -> None:
    """Live table emptied (e.g. accidental truncate) while the latest
    snapshot still records source_count > 0 → DRIFT FAIL. Preserves the
    legacy ``empty_table`` invariant's intent under the new shape."""
    pool = _Pool(
        live_count=0,
        snapshot=(datetime.now(UTC) - timedelta(days=1), 13000),
    )
    result = await check_ticker_classifications_coverage(pool)
    assert result.passed is False
    assert result.failed == 1
    fail = result.failures[0]
    assert fail.reason == "source_count_drift"
    assert "live=0" in fail.observed
    assert "snapshot=13000" in fail.observed
