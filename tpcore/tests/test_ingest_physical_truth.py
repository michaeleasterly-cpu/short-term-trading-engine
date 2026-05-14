"""Write-time physical-truth tests — every ingest path must REJECT bad rows.

Until 2026-05-14 our tests only verified that the validation suite
*detected* bad rows after the fact. The ingest writers themselves were
unchecked — which is how a fresh corporate_actions refresh re-introduced
MCHB rows with ratio=1168 even after we'd deleted them. These tests
close that gap: each ingest helper is exercised with a known-bad payload
and the assertion is that the bad rows DO NOT reach the database.

Pattern: instantiate a fake pool that records every ``executemany`` and
``execute`` call, hand the writer a payload mixing good and bad rows,
assert the recorded INSERT batch contains ONLY the good rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

# ─── Fake asyncpg pool that records executemany payloads ────────────────


class _FakeConn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list]] = []
        self.execute_calls: list[str] = []
    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))
    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append(sql)
        return "DELETE 0"
    async def fetch(self, sql: str, *args: Any) -> list:
        return []


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()
    def acquire(self) -> _FakePool:
        return self
    async def __aenter__(self) -> _FakeConn:
        return self.conn
    async def __aexit__(self, *_: Any) -> None:
        return None


# ─── corporate_actions ingest ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_corp_actions_ingest_rejects_ratio_above_1000():
    """The 2026-05-14 MCHB incident: Alpaca returned dividends with
    ratio=1168 (likely a special distribution mis-encoded). Cleanup
    deleted them but ingest re-inserted them on next refresh. Now
    physical-truth-rejected at write time."""
    from tpcore.data.ingest_corporate_actions import upsert_corporate_actions
    pool = _FakePool()
    good = {
        "ticker": "AAPL", "action_date": date(2024, 1, 1),
        "action_type": "dividend", "ratio": Decimal("0.50"),
        "raw_data": {"src": "test"},
    }
    bad = {
        "ticker": "MCHB", "action_date": date(2022, 11, 16),
        "action_type": "dividend", "ratio": Decimal("1168"),
        "raw_data": {"src": "test"},
    }
    written = await upsert_corporate_actions(pool, [good, bad])  # type: ignore[arg-type]
    assert written == 1, "bad row leaked through ingest"
    assert len(pool.conn.executemany_calls) == 1
    inserted_rows = pool.conn.executemany_calls[0][1]
    assert len(inserted_rows) == 1
    assert inserted_rows[0][0] == "AAPL"


@pytest.mark.asyncio
async def test_corp_actions_ingest_rejects_zero_or_negative_ratio():
    from tpcore.data.ingest_corporate_actions import upsert_corporate_actions
    pool = _FakePool()
    good = {
        "ticker": "AAPL", "action_date": date(2024, 1, 1),
        "action_type": "split", "ratio": Decimal("4"),
        "raw_data": {},
    }
    zero = {**good, "ticker": "Z", "ratio": Decimal("0")}
    neg = {**good, "ticker": "N", "ratio": Decimal("-1")}
    written = await upsert_corporate_actions(pool, [good, zero, neg])  # type: ignore[arg-type]
    assert written == 1


@pytest.mark.asyncio
async def test_corp_actions_ingest_rejects_far_future_date():
    from tpcore.data.ingest_corporate_actions import upsert_corporate_actions
    pool = _FakePool()
    too_far = date.today() + timedelta(days=365 * 10)
    good = {
        "ticker": "AAPL", "action_date": date(2024, 1, 1),
        "action_type": "split", "ratio": Decimal("4"), "raw_data": {},
    }
    bad = {**good, "ticker": "BAD", "action_date": too_far}
    written = await upsert_corporate_actions(pool, [good, bad])  # type: ignore[arg-type]
    assert written == 1


# ─── prices_daily ingest ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prices_daily_ingest_rejects_negative_close():
    """The 94k-bad-row Tradier incident: close < 0 or scale-corrupt
    rows (up to $99T for DCTH). Cleanup deleted them; this test pins
    that the writer can't accept them in the first place."""
    from tpcore.data.ingest_alpaca_bars import _upsert_bars
    pool = _FakePool()
    today_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    bars = [
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1000},
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": -5, "v": 1000},  # neg close
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": 1e10, "v": 1000},  # scale corruption
    ]
    written = await _upsert_bars(pool, "AAPL", bars, delisted=False)  # type: ignore[arg-type]
    assert written == 1


@pytest.mark.asyncio
async def test_prices_daily_ingest_rejects_ohlc_inconsistent():
    from tpcore.data.ingest_alpaca_bars import _upsert_bars
    pool = _FakePool()
    today_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    bars = [
        # high < low — impossible
        {"t": today_iso, "o": 10, "h": 5, "l": 11, "c": 10, "v": 1000},
        # high < close — impossible
        {"t": today_iso, "o": 10, "h": 9, "l": 8, "c": 15, "v": 1000},
        # one good row to anchor
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1000},
    ]
    written = await _upsert_bars(pool, "AAPL", bars, delisted=False)  # type: ignore[arg-type]
    assert written == 1


@pytest.mark.asyncio
async def test_prices_daily_ingest_rejects_null_fields():
    from tpcore.data.ingest_alpaca_bars import _upsert_bars
    pool = _FakePool()
    today_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    bars = [
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": None, "v": 1000},  # null close
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": 10, "v": None},  # null volume
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1000},  # good
    ]
    written = await _upsert_bars(pool, "AAPL", bars, delisted=False)  # type: ignore[arg-type]
    assert written == 1


@pytest.mark.asyncio
async def test_prices_daily_ingest_rejects_future_date():
    from tpcore.data.ingest_alpaca_bars import _upsert_bars
    pool = _FakePool()
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    today_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    bars = [
        {"t": future, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1000},
        {"t": today_iso, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1000},
    ]
    written = await _upsert_bars(pool, "AAPL", bars, delisted=False)  # type: ignore[arg-type]
    assert written == 1


# ─── fundamentals_quarterly ingest ──────────────────────────────────────


@pytest.mark.asyncio
async def test_fundamentals_ingest_rejects_zero_shares():
    """USAR / SLDB came in with shares_outstanding=0 on 2026-05-14.
    Validation flagged it; ingest must now reject it at write time."""
    from tpcore.fundamentals.cache import FundamentalsCache

    pool = _FakePool()
    cache = FundamentalsCache(pool)  # type: ignore[arg-type]
    payload = {
        "filing_date": date(2026, 3, 31),
        "period_end_date": date(2026, 3, 31),
        "period": "2026Q1",
        "shares_outstanding": Decimal("0"),
        "history": [
            {
                "filing_date": date(2025, 12, 31),
                "period_end_date": date(2025, 12, 31),
                "period": "2025Q4",
                "shares_outstanding": Decimal("100000"),
            }
        ],
    }
    written = await cache._upsert_payload("USAR", payload)
    # Only the good history row should write; the zero-shares latest is dropped.
    assert written == 1


@pytest.mark.asyncio
async def test_fundamentals_ingest_rejects_period_after_filing():
    from tpcore.fundamentals.cache import FundamentalsCache

    pool = _FakePool()
    cache = FundamentalsCache(pool)  # type: ignore[arg-type]
    payload = {
        "filing_date": date(2025, 8, 4),
        "period_end_date": date(2025, 9, 30),  # AFTER filing — impossible
        "period": "2025Q3",
        "shares_outstanding": Decimal("1000000"),
        "history": [
            {
                "filing_date": date(2025, 5, 1),
                "period_end_date": date(2025, 3, 31),
                "period": "2025Q1",
                "shares_outstanding": Decimal("1000000"),
            }
        ],
    }
    written = await cache._upsert_payload("VNOM", payload)
    assert written == 1


@pytest.mark.asyncio
async def test_fundamentals_ingest_rejects_future_filing_date():
    from tpcore.fundamentals.cache import FundamentalsCache

    pool = _FakePool()
    cache = FundamentalsCache(pool)  # type: ignore[arg-type]
    payload = {
        "filing_date": date.today() + timedelta(days=10),  # future
        "period_end_date": date.today(),
        "period": "fakeQ",
        "shares_outstanding": Decimal("1000000"),
    }
    written = await cache._upsert_payload("FAKE", payload)
    assert written == 0
