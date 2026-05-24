"""UniverseRepo — classification_id-keyed universe enumeration."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from tpcore.data.repositories.universe import UniverseRepo, UniverseRow


def _row(
    *,
    cid: str = "USOZ22OFB123XX",
    ticker: str = "META",
    current: str | None = "META",
    asset_class: str | None = "stock",
    country: str | None = "US",
    status: str | None = "active",
    tier: int | None = 1,
    valid_from: date = date(2022, 6, 9),
    valid_to: date | None = None,
) -> dict:
    return {
        "classification_id": cid,
        "ticker_at_date": ticker,
        "current_ticker": current,
        "asset_class": asset_class,
        "country": country,
        "status": status,
        "liquidity_tier": tier,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def _mock_pool(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.conn_for_assertions = conn
    return pool


# ─────────────────────────────────────────────────────────────────
# default (as_of=None) — currently-active rows
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enumerate_default_filters_to_open_rows():
    """as_of=None ⇒ WHERE valid_to IS NULL (currently-active only)."""
    pool = _mock_pool([_row()])
    repo = UniverseRepo(pool)
    out = await repo.enumerate()
    assert len(out) == 1
    assert isinstance(out[0], UniverseRow)
    sql_used = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "valid_to IS NULL" in sql_used
    assert "valid_from <=" not in sql_used


@pytest.mark.asyncio
async def test_enumerate_as_of_applies_scd2_clause():
    """as_of=<date> ⇒ WHERE valid_from <= $1 AND (valid_to IS NULL OR valid_to >= $1)."""
    pool = _mock_pool([_row(ticker="FB", valid_to=date(2022, 6, 8))])
    repo = UniverseRepo(pool)
    out = await repo.enumerate(as_of=date(2015, 6, 1))
    assert out[0].ticker_at_date == "FB"
    sql_used = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "valid_from <= $1" in sql_used
    assert "valid_to IS NULL OR valid_to >= $1" in sql_used
    assert pool.conn_for_assertions.fetch.await_args.args[1] == date(2015, 6, 1)


# ─────────────────────────────────────────────────────────────────
# liquidity tier filter
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_liquidity_tier_filters_strict_by_default():
    """include_untracked_liquidity=False (default) ⇒ NULL tier excluded.

    as_of=None hardcodes `valid_to IS NULL` (binds no param), so the tier
    placeholder is $1 — params only count when bound.
    """
    pool = _mock_pool([_row()])
    repo = UniverseRepo(pool)
    await repo.enumerate(max_liquidity_tier=2)
    sql_used = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "liquidity_tier <= $1" in sql_used
    assert "IS NULL OR liquidity_tier" not in sql_used


@pytest.mark.asyncio
async def test_max_liquidity_tier_includes_untracked_when_flag_set():
    pool = _mock_pool([_row()])
    repo = UniverseRepo(pool)
    await repo.enumerate(max_liquidity_tier=2, include_untracked_liquidity=True)
    sql_used = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "liquidity_tier IS NULL OR liquidity_tier <= $1" in sql_used


# ─────────────────────────────────────────────────────────────────
# asset_class + country filters
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_asset_class_and_country_filters_bind_in_order():
    pool = _mock_pool([_row()])
    repo = UniverseRepo(pool)
    await repo.enumerate(asset_class="stock", country="US")
    args = pool.conn_for_assertions.fetch.await_args.args
    sql_used = args[0]
    assert "asset_class = $1" in sql_used
    assert "country = $2" in sql_used
    assert args[1] == "stock"
    assert args[2] == "US"


@pytest.mark.asyncio
async def test_all_filters_combined():
    """All four filters together — param order matches placeholder order in SQL."""
    pool = _mock_pool([_row()])
    repo = UniverseRepo(pool)
    await repo.enumerate(
        as_of=date(2020, 1, 1),
        max_liquidity_tier=2,
        asset_class="stock",
        country="US",
    )
    args = pool.conn_for_assertions.fetch.await_args.args
    sql_used = args[0]
    # binding order: as_of=$1, tier=$2, asset_class=$3, country=$4
    assert args[1:] == (date(2020, 1, 1), 2, "stock", "US")
    assert "valid_from <= $1" in sql_used
    assert "liquidity_tier <= $2" in sql_used
    assert "asset_class = $3" in sql_used
    assert "country = $4" in sql_used


# ─────────────────────────────────────────────────────────────────
# Row mapping
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_row_mapping_preserves_all_columns():
    pool = _mock_pool(
        [
            _row(
                cid="USOZ22OFB123XX",
                ticker="META",
                current="META",
                asset_class="stock",
                country="US",
                status="active",
                tier=1,
                valid_from=date(2022, 6, 9),
                valid_to=None,
            )
        ]
    )
    repo = UniverseRepo(pool)
    out = await repo.enumerate()
    r = out[0]
    assert r.classification_id == "USOZ22OFB123XX"
    assert r.ticker_at_date == "META"
    assert r.current_ticker == "META"
    assert r.asset_class == "stock"
    assert r.country == "US"
    assert r.status == "active"
    assert r.liquidity_tier == 1
    assert r.valid_from == date(2022, 6, 9)
    assert r.valid_to is None


@pytest.mark.asyncio
async def test_row_mapping_handles_null_fields():
    """liquidity_tier IS NULL is valid (untracked); country can be NULL."""
    pool = _mock_pool([_row(tier=None, country=None, current=None)])
    repo = UniverseRepo(pool)
    out = await repo.enumerate()
    assert out[0].liquidity_tier is None
    assert out[0].country is None
    assert out[0].current_ticker is None


@pytest.mark.asyncio
async def test_universe_row_is_frozen():
    """Pydantic model is immutable — engines can't mutate it accidentally."""
    row = UniverseRow.model_validate(_row())
    with pytest.raises(ValidationError):
        row.classification_id = "DIFFERENT"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_empty_result_returns_empty_list():
    pool = _mock_pool([])
    repo = UniverseRepo(pool)
    out = await repo.enumerate(max_liquidity_tier=2)
    assert out == []
