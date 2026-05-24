"""MacroRepo — series_id-keyed observations + latest-as-of PIT lookup."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from tpcore.data.repositories.macro import MacroObservation, MacroRepo


def _obs_row(
    *,
    d: date = date(2026, 1, 5),
    num: str | None = "18.5",
    text: str | None = None,
    source: str = "fred",
) -> dict:
    return {
        "observed_date": d,
        "value_num": Decimal(num) if num is not None else None,
        "value_text": text,
        "source": source,
    }


def _mock_pool(fetch_returns=None, fetchrow_returns=None) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_returns or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_returns)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.conn_for_assertions = conn
    return pool


# ─────────────────────────────────────────────────────────────────
# get_window
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_window_returns_observations_in_order():
    rows = [
        _obs_row(d=date(2026, 1, 5), num="18.5"),
        _obs_row(d=date(2026, 1, 6), num="19.0"),
    ]
    pool = _mock_pool(fetch_returns=rows)
    repo = MacroRepo(pool)
    out = await repo.get_window("fred:VIXCLS", date(2026, 1, 1), date(2026, 1, 7))
    assert len(out) == 2
    assert isinstance(out[0], MacroObservation)
    assert out[0].value_num == Decimal("18.5")
    sql_used = pool.conn_for_assertions.fetch.await_args.args[0]
    assert "series_id = $1" in sql_used
    assert "observed_date BETWEEN $2 AND $3" in sql_used


@pytest.mark.asyncio
async def test_get_window_empty_returns_empty_list():
    pool = _mock_pool(fetch_returns=[])
    repo = MacroRepo(pool)
    out = await repo.get_window("fred:UNKNOWN", date(2026, 1, 1), date(2026, 1, 7))
    assert out == []


# ─────────────────────────────────────────────────────────────────
# get_latest_as_of (PIT)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_latest_as_of_returns_most_recent():
    """SQL is ORDER BY observed_date DESC LIMIT 1 — repo trusts that."""
    pool = _mock_pool(fetchrow_returns=_obs_row(d=date(2025, 12, 31), num="22.0"))
    repo = MacroRepo(pool)
    out = await repo.get_latest_as_of("fred:VIXCLS", date(2026, 1, 1))
    assert out is not None
    assert out.observed_date == date(2025, 12, 31)
    assert out.value_num == Decimal("22.0")
    sql_used = pool.conn_for_assertions.fetchrow.await_args.args[0]
    assert "observed_date <= $2" in sql_used
    assert "ORDER BY observed_date DESC" in sql_used
    assert "LIMIT 1" in sql_used


@pytest.mark.asyncio
async def test_get_latest_as_of_returns_none_when_no_observations():
    pool = _mock_pool(fetchrow_returns=None)
    repo = MacroRepo(pool)
    out = await repo.get_latest_as_of("fred:UNKNOWN", date(2026, 1, 1))
    assert out is None


# ─────────────────────────────────────────────────────────────────
# Model invariants
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_valued_observation_supported():
    """value_text is for string-valued series; value_num is None there."""
    pool = _mock_pool(fetch_returns=[_obs_row(num=None, text="bullish")])
    repo = MacroRepo(pool)
    out = await repo.get_window("aaii:regime_label", date(2026, 1, 1), date(2026, 1, 7))
    assert out[0].value_num is None
    assert out[0].value_text == "bullish"


@pytest.mark.asyncio
async def test_observation_is_frozen():
    obs = MacroObservation(
        observed_date=date(2026, 1, 5),
        value_num=Decimal("18.5"),
        value_text=None,
        source="fred",
    )
    with pytest.raises(ValidationError):
        obs.value_num = Decimal("999")  # type: ignore[misc]
