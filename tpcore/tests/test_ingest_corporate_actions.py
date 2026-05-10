"""Tests for `tpcore.data.ingest_corporate_actions`.

Mocks Alpaca via `httpx.MockTransport` and the DB via the same fake-pool
pattern used elsewhere (test_aar_writer.py, test_data_quality_writer.py).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx

from tpcore.data.ingest_corporate_actions import (
    fetch_corporate_actions,
    upsert_corporate_actions,
)

# ────────────────────────────────────────────────────────────────────────────
# Mocked Alpaca responses
# ────────────────────────────────────────────────────────────────────────────


_AAPL_SPLIT = {
    "cusip": "037833100",
    "due_bill_redemption_date": "2020-09-01",
    "ex_date": "2020-08-31",
    "id": "d209b6c2-9231-474c-a80e-1a79dacadbb2",
    "new_rate": 4,
    "old_rate": 1,
    "payable_date": "2020-08-28",
    "process_date": "2020-08-31",
    "record_date": "2020-08-24",
    "symbol": "AAPL",
}

_AAPL_DIVIDEND = {
    "cusip": "037833100",
    "ex_date": "2020-08-07",
    "foreign": False,
    "id": "d09386ae-0c2b-4280-8aa0-295e545354b1",
    "payable_date": "2020-08-13",
    "process_date": "2020-08-13",
    "rate": 0.82,
    "record_date": "2020-08-10",
    "special": False,
    "symbol": "AAPL",
}


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://data.alpaca.markets",
    )


# ────────────────────────────────────────────────────────────────────────────
# fetch_corporate_actions
# ────────────────────────────────────────────────────────────────────────────


async def test_fetch_returns_normalized_split() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = {
            "corporate_actions": {"forward_splits": [_AAPL_SPLIT]},
            "next_page_token": None,
        }
        return httpx.Response(200, json=body)

    async with _make_client(handler) as client:
        actions = await fetch_corporate_actions(
            client,
            symbols=["AAPL"],
            start=date(2020, 1, 1),
            end=date(2021, 1, 1),
        )

    assert len(actions) == 1
    a = actions[0]
    assert a["ticker"] == "AAPL"
    assert a["action_date"] == date(2020, 8, 31)
    assert a["action_type"] == "split"
    assert a["ratio"] == Decimal("4")
    assert a["raw_data"]["id"] == _AAPL_SPLIT["id"]


async def test_fetch_returns_normalized_dividend() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = {
            "corporate_actions": {"cash_dividends": [_AAPL_DIVIDEND]},
            "next_page_token": None,
        }
        return httpx.Response(200, json=body)

    async with _make_client(handler) as client:
        actions = await fetch_corporate_actions(
            client,
            symbols=["AAPL"],
            start=date(2020, 1, 1),
            end=date(2021, 1, 1),
        )

    assert len(actions) == 1
    a = actions[0]
    assert a["action_type"] == "dividend"
    assert a["action_date"] == date(2020, 8, 7)
    assert a["ratio"] == Decimal("0.82")


async def test_fetch_handles_pagination() -> None:
    """Two pages stitched into one list."""
    page_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        page_calls.append(req.url.params.get("page_token"))
        if not req.url.params.get("page_token"):
            return httpx.Response(
                200,
                json={
                    "corporate_actions": {"forward_splits": [_AAPL_SPLIT]},
                    "next_page_token": "abc123",
                },
            )
        # second page
        return httpx.Response(
            200,
            json={
                "corporate_actions": {"cash_dividends": [_AAPL_DIVIDEND]},
                "next_page_token": None,
            },
        )

    async with _make_client(handler) as client:
        actions = await fetch_corporate_actions(
            client,
            symbols=["AAPL"],
            start=date(2020, 1, 1),
            end=date(2021, 1, 1),
        )

    assert len(actions) == 2
    assert page_calls == [None, "abc123"]


async def test_fetch_passes_types_param() -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.url.params.get("types") or "")
        return httpx.Response(200, json={"corporate_actions": {}, "next_page_token": None})

    async with _make_client(handler) as client:
        await fetch_corporate_actions(
            client,
            symbols=["AAPL"],
            start=date(2020, 1, 1),
            end=date(2021, 1, 1),
            types=["forward_split"],
        )
    assert captured == ["forward_split"]


# ────────────────────────────────────────────────────────────────────────────
# upsert_corporate_actions — fake pool
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, rows))


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _action(action_type: str = "split", ratio: str = "4") -> dict:
    return {
        "ticker": "AAPL",
        "action_date": date(2020, 8, 31),
        "action_type": action_type,
        "ratio": Decimal(ratio),
        "raw_data": {"id": "abc", "symbol": "AAPL"},
    }


async def test_upsert_writes_one_row_per_action() -> None:
    pool = _FakePool()
    n = await upsert_corporate_actions(pool, [_action(), _action(action_type="dividend", ratio="0.82")])
    assert n == 2
    sql, rows = pool.conn.executemany_calls[0]
    assert "INSERT INTO platform.corporate_actions" in sql
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    assert len(rows) == 2


async def test_upsert_serializes_raw_data_to_json() -> None:
    pool = _FakePool()
    await upsert_corporate_actions(pool, [_action()])
    _, rows = pool.conn.executemany_calls[0]
    # raw_data is the 5th arg per the table column order: ticker, action_date, action_type, ratio, raw_data
    raw_data_arg = rows[0][4]
    # It should be a JSON string; round-trip parse
    assert json.loads(raw_data_arg)["id"] == "abc"


async def test_upsert_returns_zero_for_empty_input() -> None:
    pool = _FakePool()
    assert await upsert_corporate_actions(pool, []) == 0
    assert pool.conn.executemany_calls == []
