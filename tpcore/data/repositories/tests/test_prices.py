"""PricesRepo — classification_id-keyed bars + batch fetch with chunking."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.data.repositories.prices import Bar, PricesRepo


def _bar_row(
    *,
    cid: str = "USOZ22OFB123XX",
    d: date = date(2026, 1, 5),
    open_: str = "100.00",
    high: str = "101.00",
    low: str = "99.00",
    close: str = "100.50",
    vol: int = 1_000_000,
) -> dict:
    return {
        "classification_id": cid,
        "date": d,
        "open": Decimal(open_),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
        "volume": vol,
    }


def _mock_pool(fetch_returns: list | object) -> MagicMock:
    conn = MagicMock()
    if isinstance(fetch_returns, list) and fetch_returns and not isinstance(fetch_returns[0], dict):
        conn.fetch = AsyncMock(side_effect=fetch_returns)
    else:
        conn.fetch = AsyncMock(return_value=fetch_returns)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.conn_for_assertions = conn
    return pool


# ─────────────────────────────────────────────────────────────────
# get_window (single instrument)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_window_returns_bars_in_order():
    rows = [
        {
            "date": date(2026, 1, 5),
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100.5"),
            "volume": 1000,
        },
        {
            "date": date(2026, 1, 6),
            "open": Decimal("100.5"),
            "high": Decimal("102"),
            "low": Decimal("100"),
            "close": Decimal("101"),
            "volume": 1100,
        },
    ]
    pool = _mock_pool(rows)
    repo = PricesRepo(pool)
    out = await repo.get_window("USOZ22OFB123XX", date(2026, 1, 1), date(2026, 1, 7))
    assert len(out) == 2
    assert isinstance(out[0], Bar)
    assert out[0].date == date(2026, 1, 5)
    assert out[0].close == Decimal("100.5")
    sql_used = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "WHERE classification_id = $1" in sql_used
    assert "date BETWEEN $2 AND $3" in sql_used


@pytest.mark.asyncio
async def test_get_window_returns_empty_when_no_bars():
    pool = _mock_pool([])
    repo = PricesRepo(pool)
    out = await repo.get_window("UNKNOWN_CID", date(2026, 1, 1), date(2026, 1, 7))
    assert out == []


# ─────────────────────────────────────────────────────────────────
# get_window_batch
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_window_batch_groups_by_cid():
    rows = [
        _bar_row(cid="CID_A", d=date(2026, 1, 5)),
        _bar_row(cid="CID_A", d=date(2026, 1, 6)),
        _bar_row(cid="CID_B", d=date(2026, 1, 5)),
    ]
    pool = _mock_pool(rows)
    repo = PricesRepo(pool)
    out = await repo.get_window_batch(["CID_A", "CID_B"], date(2026, 1, 1), date(2026, 1, 7))
    assert set(out.keys()) == {"CID_A", "CID_B"}
    assert len(out["CID_A"]) == 2
    assert len(out["CID_B"]) == 1
    assert isinstance(out["CID_A"][0], Bar)


@pytest.mark.asyncio
async def test_get_window_batch_empty_input_short_circuits():
    pool = _mock_pool([])
    repo = PricesRepo(pool)
    out = await repo.get_window_batch([], date(2026, 1, 1), date(2026, 1, 7))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 0


@pytest.mark.asyncio
async def test_get_window_batch_omits_cids_with_no_bars():
    """Behavior matches existing fetch_bars_batch: missing cids absent from dict."""
    rows = [_bar_row(cid="CID_A")]  # CID_B requested but no rows
    pool = _mock_pool(rows)
    repo = PricesRepo(pool)
    out = await repo.get_window_batch(["CID_A", "CID_B"], date(2026, 1, 1), date(2026, 1, 7))
    assert "CID_A" in out
    assert "CID_B" not in out


@pytest.mark.asyncio
async def test_get_window_batch_chunks_at_500():
    """Universe of 1200 cids → 3 chunks (500+500+200), 3 fetch calls."""
    # Each chunk returns no rows — just count the calls.
    chunk_returns = [[], [], []]
    pool = _mock_pool(chunk_returns)
    repo = PricesRepo(pool)
    cids = [f"CID_{i:04d}" for i in range(1200)]
    out = await repo.get_window_batch(cids, date(2026, 1, 1), date(2026, 1, 7))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 3


# ─────────────────────────────────────────────────────────────────
# Bar model invariants
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bar_preserves_decimal_precision():
    """Decimal in → Decimal out, no float coercion (would lose precision)."""
    rows = [
        {
            "date": date(2026, 1, 5),
            "open": Decimal("100.123456789"),
            "high": Decimal("101.987654321"),
            "low": Decimal("99.5"),
            "close": Decimal("100.5"),
            "volume": 1000,
        }
    ]
    pool = _mock_pool(rows)
    repo = PricesRepo(pool)
    out = await repo.get_window("USOZ22OFB123XX", date(2026, 1, 1), date(2026, 1, 7))
    assert out[0].open == Decimal("100.123456789")
    assert isinstance(out[0].open, Decimal)


@pytest.mark.asyncio
async def test_bar_is_frozen():
    bar = Bar(
        date=date(2026, 1, 5),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1000,
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        bar.close = Decimal("200")  # type: ignore[misc]
