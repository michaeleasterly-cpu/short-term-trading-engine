"""catalyst.backtest._fetch_prices — edge adapter contract.

Verifies the abstraction-layer migration preserves the ticker-keyed
contract while internally using IdentityDispatcher + PricesRepo. The
function is the edge: ticker in, ticker out; cid is engine-internal.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from catalyst.backtest import _fetch_prices


class _FakePool:
    """Pool stub that hands the same connection on every acquire.

    Tracks SQL calls + supports both fetchval (dispatcher) and fetch
    (PricesRepo) on the same connection.
    """

    def __init__(
        self,
        ticker_to_cid: dict[str, str | None],
        bars_by_cid: dict[str, list[dict]],
    ) -> None:
        self._ticker_to_cid = ticker_to_cid
        self._bars_by_cid = bars_by_cid
        self.fetchval_calls: list[tuple] = []
        self.fetch_calls: list[tuple] = []

        async def _fetchval(sql, *args):
            self.fetchval_calls.append((sql, args))
            ticker = args[0]
            return self._ticker_to_cid.get(ticker)

        async def _fetch(sql, *args):
            self.fetch_calls.append((sql, args))
            cids = args[0]
            rows: list[dict] = []
            for cid in cids:
                for b in self._bars_by_cid.get(cid, []):
                    rows.append({"classification_id": cid, **b})
            return rows

        self._fetchval = _fetchval
        self._fetch = _fetch

    def acquire(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=self._fetchval)
        conn.fetch = AsyncMock(side_effect=self._fetch)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm


def _bar(d: date, close: str, volume: int) -> dict:
    return {
        "date": d,
        "open": Decimal(close),
        "high": Decimal(close),
        "low": Decimal(close),
        "close": Decimal(close),
        "volume": volume,
    }


@pytest.mark.asyncio
async def test_returns_ticker_keyed_dataframe_per_ticker():
    """ticker → cid → bars → ticker-keyed pandas DataFrame round-trip."""
    pool = _FakePool(
        ticker_to_cid={"AAPL": "USOZ80NAAPL456", "MSFT": "USOZ80NMSFT789"},
        bars_by_cid={
            "USOZ80NAAPL456": [_bar(date(2026, 1, 5), "150", 1000)],
            "USOZ80NMSFT789": [_bar(date(2026, 1, 5), "400", 2000)],
        },
    )
    out = await _fetch_prices(
        pool,
        universe=("AAPL", "MSFT"),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
    )
    assert set(out.keys()) == {"AAPL", "MSFT"}
    assert isinstance(out["AAPL"], pd.DataFrame)
    assert list(out["AAPL"].columns) == ["close", "volume"]
    assert out["AAPL"]["close"].iloc[0] == 150.0
    assert out["MSFT"]["volume"].iloc[0] == 2000


@pytest.mark.asyncio
async def test_unknown_ticker_silently_dropped():
    """Ticker with no ticker_history row → absent from output (preserved behavior)."""
    pool = _FakePool(
        ticker_to_cid={"AAPL": "USOZ80NAAPL456", "UNKNOWN": None},
        bars_by_cid={"USOZ80NAAPL456": [_bar(date(2026, 1, 5), "150", 1000)]},
    )
    out = await _fetch_prices(
        pool,
        universe=("AAPL", "UNKNOWN"),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
    )
    assert "AAPL" in out
    assert "UNKNOWN" not in out


@pytest.mark.asyncio
async def test_known_ticker_with_no_bars_absent_from_output():
    """cid resolves but has no bars in window → not in output dict."""
    pool = _FakePool(
        ticker_to_cid={"AAPL": "USOZ80NAAPL456", "NOBARS": "USOZ_NOBARS"},
        bars_by_cid={"USOZ80NAAPL456": [_bar(date(2026, 1, 5), "150", 1000)]},
    )
    out = await _fetch_prices(
        pool,
        universe=("AAPL", "NOBARS"),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
    )
    assert "AAPL" in out
    assert "NOBARS" not in out


@pytest.mark.asyncio
async def test_empty_universe_returns_empty_dict():
    pool = _FakePool(ticker_to_cid={}, bars_by_cid={})
    out = await _fetch_prices(
        pool,
        universe=(),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_uses_classification_id_join_not_ticker():
    """Verify the batch fetch SQL queries by classification_id, NOT ticker."""
    pool = _FakePool(
        ticker_to_cid={"AAPL": "USOZ80NAAPL456"},
        bars_by_cid={"USOZ80NAAPL456": [_bar(date(2026, 1, 5), "150", 1000)]},
    )
    await _fetch_prices(
        pool,
        universe=("AAPL",),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
    )
    assert pool.fetch_calls, "PricesRepo should have called fetch"
    sql_used = pool.fetch_calls[0][0]
    assert "classification_id = ANY" in sql_used
    assert "ticker = ANY" not in sql_used
    # And the args carry cids, not tickers
    assert pool.fetch_calls[0][1][0] == ["USOZ80NAAPL456"]


@pytest.mark.asyncio
async def test_bars_sorted_by_date_in_output():
    """Out-of-order bars from DB are sorted ascending by date in the DataFrame."""
    pool = _FakePool(
        ticker_to_cid={"AAPL": "USOZ80NAAPL456"},
        bars_by_cid={
            "USOZ80NAAPL456": [
                _bar(date(2026, 1, 7), "152", 1200),
                _bar(date(2026, 1, 5), "150", 1000),
                _bar(date(2026, 1, 6), "151", 1100),
            ]
        },
    )
    out = await _fetch_prices(
        pool,
        universe=("AAPL",),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
    )
    closes = out["AAPL"]["close"].tolist()
    assert closes == [150.0, 151.0, 152.0]
