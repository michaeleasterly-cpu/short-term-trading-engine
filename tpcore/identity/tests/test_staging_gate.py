"""Tests for the P1-P5 staging-spine completeness gate (probe wiring).

The probes are parameterized COUNT SQL run against a staging schema + the live
prices table; these tests verify the gate's wiring + verdict logic with a fake
connection (the SQL itself is integration-validated by the build orchestrator
against the live DB). Spec §0 / §5.
"""
from __future__ import annotations

import pytest

from tpcore.identity.staging_gate import evaluate_staging_gate


class _FakeConn:
    """Minimal fake: maps probe-key substrings to canned COUNT returns."""

    def __init__(self, counts: dict[str, int], p3_rows: list | None = None):
        self._counts = counts
        self._p3_rows = p3_rows or []

    async def fetchval(self, sql: str) -> int:
        # Match on the distinctive table/clause of each probe.
        if "current_ticker IS NOT NULL" in sql and "NOT EXISTS" in sql:
            return self._counts.get("P1", 0)
        if "th1.classification_id <> th2.classification_id" in sql:
            return self._counts.get("P2", 0)
        if "WITH span AS" in sql and "NOT EXISTS" in sql and "count(*)" in sql:
            return self._counts.get("P3", 0)
        if "EXTRACT(month FROM tc.lifetime_start) = 1" in sql:
            return self._counts.get("P4", 0)
        if "GROUP BY id HAVING count(*) > 1" in sql:
            return self._counts.get("P5_dup", 0)
        if "th.classification_id)" in sql and "ticker_history th" in sql:
            return self._counts.get("P5_orphan", 0)
        if "current_ticker IS NULL" in sql:
            return self._counts.get("P5_null", 0)
        if "lifetime_end <= lifetime_start" in sql:
            return self._counts.get("P5_order", 0)
        if "'1900-01-01'" in sql:
            return self._counts.get("P5_sentinel", 0)
        return 0

    async def fetch(self, sql: str) -> list:
        return self._p3_rows


class _Row(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


@pytest.mark.asyncio
async def test_gate_green_when_all_zero() -> None:
    res = await evaluate_staging_gate(_FakeConn({}), raise_on_fail=True)
    assert res.passed
    assert res.violations == {}


@pytest.mark.asyncio
async def test_gate_reports_p3_violations_and_sample() -> None:
    from datetime import date

    rows = [
        _Row(ticker="ECCW", min_date=date(2021, 3, 29),
             max_date=date(2026, 5, 7), n_bars=1284),
    ]
    res = await evaluate_staging_gate(
        _FakeConn({"P3": 1}, p3_rows=rows), raise_on_fail=False
    )
    assert not res.passed
    assert res.violations == {"P3_priced_uncovered": 1}
    assert res.p3_violator_sample[0]["ticker"] == "ECCW"


@pytest.mark.asyncio
async def test_gate_raises_on_fail_when_requested() -> None:
    with pytest.raises(RuntimeError, match="staging gate FAILED"):
        await evaluate_staging_gate(_FakeConn({"P1": 3}), raise_on_fail=True)


@pytest.mark.asyncio
async def test_gate_p5_internal_consistency_probes() -> None:
    res = await evaluate_staging_gate(
        _FakeConn({"P5_null": 5, "P5_sentinel": 2}), raise_on_fail=False
    )
    assert not res.passed
    assert res.violations["P5_null_current_ticker"] == 5
    assert res.violations["P5_sentinel_lifetime_start"] == 2
