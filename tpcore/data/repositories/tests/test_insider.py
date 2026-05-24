"""InsiderRepo — classification_id-keyed Form-3/4/5 transactions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.data.repositories.insider import InsiderRepo, InsiderTransaction


def _txn(
    *,
    cid: str | None = None,
    d: date = date(2026, 1, 30),
    insider: str = "Smith John",
    txn_type: str = "BUY",
    shares: int = 1000,
    price: str = "150.00",
    source: str = "sec",
) -> dict:
    out = {
        "filing_date": d,
        "insider_name": insider,
        "transaction_type": txn_type,
        "shares": shares,
        "price": Decimal(price),
        "value": Decimal(price) * shares,
        "source": source,
    }
    if cid is not None:
        out["classification_id"] = cid
    return out


def _mock_pool(fetch_returns: list | None = None) -> MagicMock:
    conn = MagicMock()
    if isinstance(fetch_returns, list) and fetch_returns and not isinstance(fetch_returns[0], dict):
        conn.fetch = AsyncMock(side_effect=fetch_returns)
    else:
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
async def test_get_window_returns_transactions():
    rows = [_txn(d=date(2026, 1, 30))]
    pool = _mock_pool(fetch_returns=rows)
    repo = InsiderRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2026, 1, 1), date(2026, 6, 30))
    assert len(out) == 1
    assert isinstance(out[0], InsiderTransaction)
    sql = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "platform.insider_transactions" in sql
    assert "classification_id = $1" in sql
    assert "filing_date BETWEEN $2 AND $3" in sql


# ─── get_window_batch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_groups_by_classification_id():
    rows = [_txn(cid="CID_A"), _txn(cid="CID_B", insider="Doe Jane")]
    pool = _mock_pool(fetch_returns=rows)
    repo = InsiderRepo(pool)
    out = await repo.get_window_batch(
        ["CID_A", "CID_B"],
        date(2026, 1, 1),
        date(2026, 6, 30),
    )
    assert set(out.keys()) == {"CID_A", "CID_B"}
    assert out["CID_A"][0].insider_name == "Smith John"


@pytest.mark.asyncio
async def test_batch_empty_input_short_circuits():
    pool = _mock_pool(fetch_returns=[])
    repo = InsiderRepo(pool)
    out = await repo.get_window_batch([], date(2026, 1, 1), date(2026, 6, 30))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 0


@pytest.mark.asyncio
async def test_batch_chunks_at_500_cids():
    chunk_returns = [[], [], []]
    pool = _mock_pool(fetch_returns=chunk_returns)
    repo = InsiderRepo(pool)
    cids = [f"CID_{i:04d}" for i in range(1200)]
    out = await repo.get_window_batch(cids, date(2026, 1, 1), date(2026, 6, 30))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 3


# ─── Model invariants ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transaction_is_frozen():
    from pydantic import ValidationError

    t = InsiderTransaction(
        filing_date=date(2026, 1, 30),
        insider_name="Smith John",
        transaction_type="BUY",
        shares=1000,
        price=Decimal("150"),
        value=Decimal("150000"),
        source="sec",
    )
    with pytest.raises(ValidationError):
        t.transaction_type = "SELL"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_transaction_preserves_decimal_precision():
    rows = [_txn(price="123.456789")]
    pool = _mock_pool(fetch_returns=rows)
    repo = InsiderRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2026, 1, 1), date(2026, 6, 30))
    assert out[0].price == Decimal("123.456789")
