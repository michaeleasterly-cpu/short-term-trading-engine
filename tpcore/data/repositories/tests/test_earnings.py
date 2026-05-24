"""EarningsRepo — classification_id-keyed events + positive-beat filter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.data.repositories.earnings import EarningsEvent, EarningsRepo


def _event(
    *,
    cid: str | None = None,
    d: date = date(2026, 1, 30),
    event_type: str = "EARNINGS_BEAT",
    magnitude: str | None = "5.2",
    source: str = "fmp",
) -> dict:
    out = {
        "event_date": d,
        "event_type": event_type,
        "magnitude_pct": Decimal(magnitude) if magnitude is not None else None,
        "source": source,
    }
    if cid is not None:
        out["classification_id"] = cid
    return out


def _mock_pool(fetch_returns: list | None = None) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_returns or [])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    pool.conn_for_assertions = conn
    return pool


# ─── get_window ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_window_returns_events_in_order():
    rows = [_event(d=date(2026, 1, 30)), _event(d=date(2026, 4, 30))]
    pool = _mock_pool(fetch_returns=rows)
    repo = EarningsRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2026, 1, 1), date(2026, 6, 30))
    assert len(out) == 2
    assert isinstance(out[0], EarningsEvent)
    sql = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "classification_id = $1" in sql
    assert "event_date BETWEEN $2 AND $3" in sql


# ─── get_window_batch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_groups_by_classification_id():
    rows = [
        _event(cid="CID_A", d=date(2026, 1, 30)),
        _event(cid="CID_B", d=date(2026, 2, 15)),
    ]
    pool = _mock_pool(fetch_returns=rows)
    repo = EarningsRepo(pool)
    out = await repo.get_window_batch(
        ["CID_A", "CID_B"],
        date(2026, 1, 1),
        date(2026, 6, 30),
    )
    assert set(out.keys()) == {"CID_A", "CID_B"}


@pytest.mark.asyncio
async def test_batch_empty_input_short_circuits():
    pool = _mock_pool(fetch_returns=[])
    repo = EarningsRepo(pool)
    out = await repo.get_window_batch([], date(2026, 1, 1), date(2026, 6, 30))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 0


# ─── get_beats ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_beats_applies_filter_in_sql():
    rows = [_event(cid="CID_A", event_type="EARNINGS_BEAT", magnitude="3.5")]
    pool = _mock_pool(fetch_returns=rows)
    repo = EarningsRepo(pool)
    out = await repo.get_beats(["CID_A"], date(2026, 1, 1), date(2026, 6, 30))
    assert "CID_A" in out
    sql = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "event_type = 'EARNINGS_BEAT'" in sql
    assert "magnitude_pct > 0" in sql


@pytest.mark.asyncio
async def test_get_beats_empty_input_short_circuits():
    pool = _mock_pool(fetch_returns=[])
    repo = EarningsRepo(pool)
    out = await repo.get_beats([], date(2026, 1, 1), date(2026, 6, 30))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 0


# ─── Model invariants ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_handles_null_magnitude():
    """NO_BEAT events have NULL magnitude_pct; model accepts it."""
    rows = [_event(event_type="EARNINGS_NO_BEAT", magnitude=None)]
    pool = _mock_pool(fetch_returns=rows)
    repo = EarningsRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2026, 1, 1), date(2026, 6, 30))
    assert out[0].magnitude_pct is None
    assert out[0].event_type == "EARNINGS_NO_BEAT"


@pytest.mark.asyncio
async def test_event_is_frozen():
    from pydantic import ValidationError

    e = EarningsEvent(
        event_date=date(2026, 1, 30),
        event_type="EARNINGS_BEAT",
        magnitude_pct=Decimal("5.0"),
        source="fmp",
    )
    with pytest.raises(ValidationError):
        e.event_type = "OTHER"  # type: ignore[misc]
