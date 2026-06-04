"""Unit tests for ``tpcore.ingestion.d2_metrics`` — the durable Postgres
rolling-median shrinkage detector (D2 substrate, LOCKED 2026-05-18
archive-substrate migration).

Coverage:
  (a) ``record_ingestion_metrics`` writes one row with the expected
      column values; a write failure is swallowed (observability — not
      a producer hard-stop).
  (b) ``check_shrinkage_vs_rolling_median`` cold-start returns
      ``cold_start=True, shrunk=False`` (no baseline → never flag).
  (c) Median absorbs a single outlier: 9 healthy rows + 1 outlier →
      median ≈ healthy; current run vs that median behaves correctly.
  (d) ``shrunk=True`` only when the deficit exceeds the threshold.
  (e) ``shrunk=False`` when growth (negative shrinkage_pct).
  (f) Default rolling window is 10 (operator-spec value).
  (g) ``detectors_disagree`` returns True iff the two reach different
      conclusions AND the v2 verdict is not cold-start.
"""
from __future__ import annotations

import pytest

from tpcore.ingestion.d2_metrics import (
    DEFAULT_ROLLING_WINDOW,
    DEFAULT_SHRINKAGE_THRESHOLD_PCT,
    ShrinkageVerdict,
    check_shrinkage_vs_rolling_median,
    detectors_disagree,
    record_ingestion_metrics,
)

# ────────────────────────────────────────────────────────────────────────
# Fakes — asyncpg-shaped no-DB stand-ins.
# ────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, history: list[int]) -> None:
        self.history = history
        self.executed: list[tuple] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        # Plan 2: _RECENT_SQL takes (source, d2_tag, limit) against
        # platform.ingest_manifest. We ignore source + tag in the fake (one
        # fake per source) and respect the limit so the test exercises the
        # LIMIT clause.
        limit = args[2] if len(args) >= 3 else len(self.history)
        return [{"row_count": rc} for rc in self.history[:limit]]

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(args)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        return self._conn


class _ExplodingPool:
    """A pool whose acquire blows up — exercises the swallow path."""

    def acquire(self):
        raise RuntimeError("simulated DB outage")


# ────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────


def test_default_rolling_window_is_ten() -> None:
    """Behaviour (f): operator-spec rolling window = 10."""
    assert DEFAULT_ROLLING_WINDOW == 10


def test_default_threshold_matches_v1_detector() -> None:
    """Cross-detector parity: same 20% threshold as
    ``csv_archive.detect_shrinkage`` so the two are directly
    comparable during the soak period."""
    assert DEFAULT_SHRINKAGE_THRESHOLD_PCT == 0.20


@pytest.mark.asyncio
async def test_record_metrics_inserts_row() -> None:
    """Behaviour (a): one INSERT per call carrying the row count
    and the optional fields verbatim (Plan 2: D2-tagged ingest_manifest row)."""
    conn = _FakeConn(history=[])
    pool = _FakePool(conn)
    await record_ingestion_metrics(
        pool, "fred_macro", 12345,
    )
    assert len(conn.executed) == 1
    args = conn.executed[0]
    # (source, d2_tag, actual_rows, min_date, max_date) — provider+source_locator
    # both bind $2 (the d2 tag) in the SQL.
    assert args[0] == "fred_macro"
    assert args[1] == "d2_metrics"
    assert args[2] == 12345
    assert args[3] is None
    assert args[4] is None


@pytest.mark.asyncio
async def test_record_metrics_swallows_errors() -> None:
    """Behaviour (a): a DB outage MUST NOT propagate to the producer
    (this is observability, not a hard-stop)."""
    pool = _ExplodingPool()
    # Must not raise.
    await record_ingestion_metrics(pool, "fred_macro", 100)


@pytest.mark.asyncio
async def test_cold_start_never_flags() -> None:
    """Behaviour (b): no history → cold_start=True, shrunk=False.
    A fresh source can't be vendor-truncated by definition."""
    conn = _FakeConn(history=[])
    pool = _FakePool(conn)
    v = await check_shrinkage_vs_rolling_median(pool, "fred_macro", 0)
    assert v.cold_start is True
    assert v.shrunk is False
    assert v.samples_used == 0


@pytest.mark.asyncio
async def test_median_absorbs_single_outlier() -> None:
    """Behaviour (c): a 1-of-10 outlier (say, a partial vendor pull
    that landed at 50% of normal) does NOT poison the baseline —
    median = healthy population, so the NEXT healthy run is judged
    against the healthy median rather than the outlier."""
    # 9 healthy runs (1000) + 1 outlier (500). Median = 1000.
    conn = _FakeConn(history=[1000] * 9 + [500])
    pool = _FakePool(conn)
    # A current run at 990 is well within the 20% threshold of the
    # healthy median (1000) — NOT shrunk.
    v = await check_shrinkage_vs_rolling_median(pool, "fred_macro", 990)
    assert v.cold_start is False
    assert v.median_rows == 1000
    assert v.shrunk is False
    # A current run at 700 is 30% below the healthy median — shrunk.
    v2 = await check_shrinkage_vs_rolling_median(pool, "fred_macro", 700)
    assert v2.shrunk is True
    assert abs(v2.shrinkage_pct - 0.30) < 1e-9


@pytest.mark.asyncio
async def test_threshold_is_exclusive() -> None:
    """Behaviour (d): exactly-at-threshold is NOT shrunk; just-above
    IS shrunk. Mirrors the v1 detector's strict > comparison."""
    conn = _FakeConn(history=[1000])
    pool = _FakePool(conn)
    # 20% deficit exactly → shrinkage_pct == 0.20; > comparison
    # makes this NOT-shrunk.
    v_exact = await check_shrinkage_vs_rolling_median(
        pool, "fred_macro", 800,
    )
    assert v_exact.shrunk is False
    # 21% deficit → shrunk.
    v_over = await check_shrinkage_vs_rolling_median(
        pool, "fred_macro", 790,
    )
    assert v_over.shrunk is True


@pytest.mark.asyncio
async def test_growth_is_not_shrinkage() -> None:
    """Behaviour (e): a current run LARGER than the median has
    negative shrinkage_pct and is never shrunk."""
    conn = _FakeConn(history=[1000])
    pool = _FakePool(conn)
    v = await check_shrinkage_vs_rolling_median(pool, "fred_macro", 1500)
    assert v.shrunk is False
    assert v.shrinkage_pct < 0


@pytest.mark.asyncio
async def test_zero_median_treated_as_cold_start() -> None:
    """Defensive: all-zero history is a degenerate state — we cannot
    compute a meaningful shrinkage fraction (div-by-zero). Treat as
    cold-start so we never flag a vendor that legitimately returned
    nothing this run."""
    conn = _FakeConn(history=[0, 0, 0])
    pool = _FakePool(conn)
    v = await check_shrinkage_vs_rolling_median(pool, "fred_macro", 100)
    assert v.cold_start is True
    assert v.shrunk is False


@pytest.mark.asyncio
async def test_rejects_zero_window() -> None:
    """Defensive: a 0-window query is nonsense; reject loudly."""
    conn = _FakeConn(history=[1000])
    pool = _FakePool(conn)
    with pytest.raises(ValueError, match="rolling_window"):
        await check_shrinkage_vs_rolling_median(
            pool, "fred_macro", 800, rolling_window=0,
        )


def test_detectors_disagree_when_v1_flags_v2_does_not() -> None:
    """Behaviour (g): the v1 single-prior detector flagged but the v2
    rolling-median did not (typical case: v1 baseline IS the outlier
    that the v2 median ignored)."""
    v2 = ShrinkageVerdict(
        source="x", current_rows=900, median_rows=1000.0,
        samples_used=10, shrinkage_pct=0.10, shrunk=False,
        cold_start=False,
    )
    assert detectors_disagree(True, v2) is True


def test_detectors_disagree_when_v2_flags_v1_does_not() -> None:
    """Behaviour (g): the v2 detector caught a slow drift the v1
    detector missed (multi-run gradual shrinkage)."""
    v2 = ShrinkageVerdict(
        source="x", current_rows=700, median_rows=1000.0,
        samples_used=10, shrinkage_pct=0.30, shrunk=True,
        cold_start=False,
    )
    assert detectors_disagree(False, v2) is True


def test_detectors_dont_disagree_when_both_flag() -> None:
    """Behaviour (g): consensus → no disagree event."""
    v2 = ShrinkageVerdict(
        source="x", current_rows=500, median_rows=1000.0,
        samples_used=10, shrinkage_pct=0.50, shrunk=True,
        cold_start=False,
    )
    assert detectors_disagree(True, v2) is False


def test_detectors_dont_disagree_on_cold_start() -> None:
    """Behaviour (g): cold-start v2 cannot disagree — neither detector
    has a meaningful comparison so a "disagree" event would be noise.
    """
    v2 = ShrinkageVerdict(
        source="x", current_rows=100, median_rows=0.0,
        samples_used=0, shrinkage_pct=0.0, shrunk=False,
        cold_start=True,
    )
    assert detectors_disagree(True, v2) is False
    assert detectors_disagree(False, v2) is False
