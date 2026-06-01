"""F0 (2026-06-01) — cutover_agent parity-freshness gate tests.

Hermetic. Mocks the asyncpg pool to return synthetic
``platform.data_quality_log`` rows and asserts the cutover_agent's
``_parity_verdict_fresh`` behavior on each verdict shape:

  * no row → BLOCK
  * latest verdict NOT_EVALUABLE (confidence IS NULL) → BLOCK
  * latest verdict FAIL (confidence = 0.0) → BLOCK even when an older
    PASS exists (we always read the latest)
  * latest verdict PASS but older than _MAX_PARITY_AGE_DAYS → BLOCK
  * recent PASS → ALLOW

These cover the operator hard rule "no fail-open path — any
uncertainty blocks the cutover."
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from ops.cutover_agent import (
    _MAX_PARITY_AGE_DAYS,
    _parity_verdict_fresh,
)


def _mock_pool_returning(row: dict | None) -> MagicMock:
    """asyncpg.Pool stub whose ``acquire().fetchrow(...)`` returns
    the requested row payload (or None when there's no verdict on
    file)."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.conn_for_assertions = conn
    return pool


# ─── TEST-F0-J — missing verdict → BLOCK


@pytest.mark.asyncio
async def test_cutover_agent_blocks_on_missing_parity() -> None:
    """The most operator-facing failure mode: cutover triggered before
    the operator ever ran ``evaluate_provider_parity``. Surface a
    clear message naming the exact ops command to run."""
    pool = _mock_pool_returning(None)
    result = await _parity_verdict_fresh(
        pool, feed="daily_bars", candidate="challenger_test",
    )
    assert result.fresh is False
    assert "no parity verdict on file" in result.reason
    assert "evaluate_provider_parity" in result.reason
    assert "daily_bars" in result.reason
    assert "challenger_test" in result.reason


# ─── TEST-F0-K — stale PASS → BLOCK


@pytest.mark.asyncio
async def test_cutover_agent_blocks_on_stale_parity() -> None:
    """A PASS verdict older than _MAX_PARITY_AGE_DAYS is stale —
    block cutover and tell the operator to re-evaluate."""
    stale_ts = datetime.now(UTC) - timedelta(days=_MAX_PARITY_AGE_DAYS + 5)
    pool = _mock_pool_returning({
        "timestamp": stale_ts,
        "confidence": 1.0,
        "notes": json.dumps({"verdict": "pass"}),
    })
    result = await _parity_verdict_fresh(
        pool, feed="daily_bars", candidate="challenger_test",
    )
    assert result.fresh is False
    assert result.verdict == "pass"
    assert result.verdict_age_days is not None
    assert result.verdict_age_days >= _MAX_PARITY_AGE_DAYS
    assert "stale" in result.reason
    assert "re-evaluate" in result.reason or "force=true" in result.reason


# ─── TEST-F0-L — latest FAIL → BLOCK even with older PASS


@pytest.mark.asyncio
async def test_cutover_agent_blocks_on_latest_fail_verdict() -> None:
    """The freshness check reads the SINGLE latest row by timestamp.
    A FAIL is the latest → BLOCK regardless of older history (which
    the query LIMIT 1 by DESC timestamp guarantees we never see)."""
    recent_fail_ts = datetime.now(UTC) - timedelta(days=2)
    pool = _mock_pool_returning({
        "timestamp": recent_fail_ts,
        "confidence": 0.0,  # FAIL
        "notes": json.dumps({"verdict": "fail",
                             "evidence": "FAIL on: coverage"}),
    })
    result = await _parity_verdict_fresh(
        pool, feed="daily_bars", candidate="challenger_test",
    )
    assert result.fresh is False
    assert result.verdict == "fail"
    assert "FAIL" in result.reason
    assert "coverage" in result.reason or "accuracy" in result.reason or "dimension" in result.reason


# ─── TEST-F0-M — NOT_EVALUABLE → BLOCK


@pytest.mark.asyncio
async def test_cutover_agent_blocks_on_not_evaluable_verdict() -> None:
    """An honest non-pass — DERIVED feed or empty incumbent. Still
    blocks promotion."""
    pool = _mock_pool_returning({
        "timestamp": datetime.now(UTC) - timedelta(days=1),
        "confidence": None,  # NOT_EVALUABLE
        "notes": json.dumps({"verdict": "not_evaluable",
                             "evidence": "DERIVED feed"}),
    })
    result = await _parity_verdict_fresh(
        pool, feed="daily_bars", candidate="challenger_test",
    )
    assert result.fresh is False
    assert result.verdict == "not_evaluable"


# ─── TEST-F0-N — recent PASS → ALLOW


@pytest.mark.asyncio
async def test_cutover_agent_proceeds_on_recent_pass() -> None:
    """Happy path — recent PASS within the freshness window allows
    cutover to proceed to the normal ``plan_cutover`` call."""
    recent_ts = datetime.now(UTC) - timedelta(days=3)
    pool = _mock_pool_returning({
        "timestamp": recent_ts,
        "confidence": 1.0,
        "notes": json.dumps({"verdict": "pass"}),
    })
    result = await _parity_verdict_fresh(
        pool, feed="daily_bars", candidate="challenger_test",
    )
    assert result.fresh is True
    assert result.verdict == "pass"
    assert result.verdict_age_days == 3


# ─── TEST-F0-O — boundary: exactly at limit


@pytest.mark.asyncio
async def test_cutover_agent_pass_at_age_boundary() -> None:
    """The freshness gate uses strict ``age > timedelta(days=N)``.
    A PASS aged ``_MAX_PARITY_AGE_DAYS - small`` hours is INSIDE
    (fresh); a PASS aged ``_MAX_PARITY_AGE_DAYS + small`` hours is
    OUTSIDE (stale). Pin both ends so the next operator who tweaks
    the constant sees the boundary semantics fire."""
    # Inside window: 29 days 23 hours → strict-less-than 30 days → fresh.
    inside_ts = datetime.now(UTC) - timedelta(
        days=_MAX_PARITY_AGE_DAYS - 1, hours=23,
    )
    pool_inside = _mock_pool_returning({
        "timestamp": inside_ts,
        "confidence": 1.0,
        "notes": json.dumps({"verdict": "pass"}),
    })
    result_inside = await _parity_verdict_fresh(
        pool_inside, feed="daily_bars", candidate="challenger_test",
    )
    assert result_inside.fresh is True, (
        f"PASS at {_MAX_PARITY_AGE_DAYS - 1}d 23h should be FRESH "
        f"(strict-greater-than comparison); got reason={result_inside.reason}"
    )

    # Outside window: 30 days 1 hour → strict-greater-than 30 days → stale.
    outside_ts = datetime.now(UTC) - timedelta(
        days=_MAX_PARITY_AGE_DAYS, hours=1,
    )
    pool_outside = _mock_pool_returning({
        "timestamp": outside_ts,
        "confidence": 1.0,
        "notes": json.dumps({"verdict": "pass"}),
    })
    result_outside = await _parity_verdict_fresh(
        pool_outside, feed="daily_bars", candidate="challenger_test",
    )
    assert result_outside.fresh is False, (
        f"PASS at {_MAX_PARITY_AGE_DAYS}d 1h should be STALE; "
        f"got reason={result_outside.reason}"
    )


# ─── TEST-F0-P — _MAX_PARITY_AGE_DAYS constant pinned


def test_max_parity_age_days_constant() -> None:
    """Operator decision pinned at F0 implementation (2026-06-01) =
    30 days. Tightening or loosening this constant is a deliberate
    operator decision — this test fires to force a discussion."""
    assert _MAX_PARITY_AGE_DAYS == 30
