"""FundamentalsRepo — classification_id-keyed quarterly + PIT + batch."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.data.repositories.fundamentals import (
    FundamentalsRepo,
    QuarterlyFundamentals,
)


def _row(
    *,
    cid: str | None = None,
    filing: date = date(2026, 1, 30),
    period_end: date = date(2025, 12, 31),
    label: str = "Q4",
    revenue: str | None = "1000000000",
    net_income: str | None = "200000000",
) -> dict:
    d = {
        "filing_date": filing,
        "period_end_date": period_end,
        "period_label": label,
        "net_income": Decimal(net_income) if net_income is not None else None,
        "fcf": Decimal("180000000"),
        "operating_cash_flow": Decimal("250000000"),
        "capex": Decimal("-70000000"),
        "revenue": Decimal(revenue) if revenue is not None else None,
        "total_assets": Decimal("5000000000"),
        "total_liabilities": Decimal("2000000000"),
        "current_assets": Decimal("1500000000"),
        "current_liabilities": Decimal("800000000"),
        "receivables": Decimal("400000000"),
        "cash_and_equivalents": Decimal("600000000"),
        "shares_outstanding": Decimal("100000000"),
        "pb": Decimal("3.5"),
        "de": Decimal("0.66"),
    }
    if cid is not None:
        d["classification_id"] = cid
    return d


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
async def test_get_window_filters_by_classification_id_and_date():
    pool = _mock_pool(fetch_returns=[_row()])
    repo = FundamentalsRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2025, 1, 1), date(2026, 6, 30))
    assert len(out) == 1
    assert isinstance(out[0], QuarterlyFundamentals)
    sql = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "classification_id = $1" in sql
    assert "filing_date BETWEEN $2 AND $3" in sql


@pytest.mark.asyncio
async def test_get_window_empty_returns_empty_list():
    pool = _mock_pool(fetch_returns=[])
    repo = FundamentalsRepo(pool)
    out = await repo.get_window("UNKNOWN", date(2025, 1, 1), date(2026, 6, 30))
    assert out == []


# ─── get_quarterly_pit ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pit_no_as_of_returns_overall_latest():
    rows = [
        _row(filing=date(2026, 1, 30), label="Q4"),
        _row(filing=date(2025, 10, 30), label="Q3"),
        _row(filing=date(2025, 7, 30), label="Q2"),
    ]
    pool = _mock_pool(fetch_returns=rows)
    repo = FundamentalsRepo(pool)
    latest, history = await repo.get_quarterly_pit("USOZ80NAAPL456")
    assert latest is not None
    assert latest.period_label == "Q4"
    assert len(history) == 2
    assert history[0].period_label == "Q3"
    sql = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "filing_date <=" not in sql  # no PIT cutoff binding
    assert "ORDER BY filing_date DESC" in sql


@pytest.mark.asyncio
async def test_pit_with_as_of_applies_filing_cutoff():
    rows = [
        _row(filing=date(2025, 10, 30), label="Q3"),
        _row(filing=date(2025, 7, 30), label="Q2"),
    ]
    pool = _mock_pool(fetch_returns=rows)
    repo = FundamentalsRepo(pool)
    latest, history = await repo.get_quarterly_pit(
        "USOZ80NAAPL456",
        as_of=date(2025, 12, 31),
    )
    assert latest is not None and latest.period_label == "Q3"
    assert len(history) == 1
    sql = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "filing_date <= $2" in sql
    assert pool.conn_for_assertions.fetch.await_args.args[1:] == (
        "USOZ80NAAPL456",
        date(2025, 12, 31),
    )


@pytest.mark.asyncio
async def test_pit_returns_none_history_empty_when_no_rows():
    pool = _mock_pool(fetch_returns=[])
    repo = FundamentalsRepo(pool)
    latest, history = await repo.get_quarterly_pit("UNKNOWN_CID")
    assert latest is None
    assert history == []


# ─── get_window_batch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_groups_by_classification_id():
    rows = [
        _row(cid="CID_A", filing=date(2026, 1, 30)),
        _row(cid="CID_A", filing=date(2025, 10, 30)),
        _row(cid="CID_B", filing=date(2026, 1, 30)),
    ]
    pool = _mock_pool(fetch_returns=rows)
    repo = FundamentalsRepo(pool)
    out = await repo.get_window_batch(
        ["CID_A", "CID_B"],
        date(2025, 1, 1),
        date(2026, 6, 30),
    )
    assert set(out.keys()) == {"CID_A", "CID_B"}
    assert len(out["CID_A"]) == 2
    assert len(out["CID_B"]) == 1
    assert isinstance(out["CID_A"][0], QuarterlyFundamentals)


@pytest.mark.asyncio
async def test_batch_empty_input_short_circuits():
    pool = _mock_pool(fetch_returns=[])
    repo = FundamentalsRepo(pool)
    out = await repo.get_window_batch([], date(2025, 1, 1), date(2026, 6, 30))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 0


@pytest.mark.asyncio
async def test_batch_chunks_at_500_cids():
    chunk_returns = [[], [], []]
    pool = _mock_pool(fetch_returns=chunk_returns)
    repo = FundamentalsRepo(pool)
    cids = [f"CID_{i:04d}" for i in range(1200)]
    out = await repo.get_window_batch(cids, date(2025, 1, 1), date(2026, 6, 30))
    assert out == {}
    assert pool.conn_for_assertions.fetch.await_count == 3


@pytest.mark.asyncio
async def test_batch_omits_cids_with_no_rows():
    rows = [_row(cid="CID_A")]  # CID_B has no rows
    pool = _mock_pool(fetch_returns=rows)
    repo = FundamentalsRepo(pool)
    out = await repo.get_window_batch(
        ["CID_A", "CID_B"],
        date(2025, 1, 1),
        date(2026, 6, 30),
    )
    assert "CID_A" in out
    assert "CID_B" not in out


# ─── Model invariants ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fundamentals_preserves_decimal_precision():
    pool = _mock_pool(fetch_returns=[_row(revenue="1234567890.123456")])
    repo = FundamentalsRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2025, 1, 1), date(2026, 6, 30))
    assert out[0].revenue == Decimal("1234567890.123456")


@pytest.mark.asyncio
async def test_fundamentals_handles_null_numeric_fields():
    pool = _mock_pool(fetch_returns=[_row(revenue=None, net_income=None)])
    repo = FundamentalsRepo(pool)
    out = await repo.get_window("USOZ80NAAPL456", date(2025, 1, 1), date(2026, 6, 30))
    assert out[0].revenue is None
    assert out[0].net_income is None
