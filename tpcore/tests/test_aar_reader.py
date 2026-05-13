"""Unit tests for :mod:`tpcore.aar.reader` — the shared AAR read-side."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tpcore.aar.reader import AARReader, _parse_ts, _row_to_aar


def _record(
    *,
    engine: str = "sigma",
    trade_id: str = "AAPL_1",
    ticker: str = "AAPL",
    aar_data: dict | str = None,  # type: ignore[assignment]
    recorded_at: datetime | None = None,
) -> dict:
    return {
        "engine": engine,
        "trade_id": trade_id,
        "ticker": ticker,
        "aar_data": aar_data if aar_data is not None else {},
        "recorded_at": recorded_at or datetime(2026, 5, 12, tzinfo=UTC),
    }


def test_parse_ts_handles_z_suffix() -> None:
    ts = _parse_ts("2026-05-12T13:32:02.458834Z")
    assert ts == datetime(2026, 5, 12, 13, 32, 2, 458834, tzinfo=UTC)


def test_parse_ts_attaches_utc_to_naive_string() -> None:
    ts = _parse_ts("2026-05-12T13:32:02")
    assert ts is not None
    assert ts.tzinfo == UTC


def test_parse_ts_returns_none_on_garbage() -> None:
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(None) is None
    assert _parse_ts(12345) is None


def test_parse_ts_passes_through_aware_datetime() -> None:
    dt = datetime(2026, 5, 12, tzinfo=UTC)
    assert _parse_ts(dt) == dt


def test_row_to_aar_happy_path_with_dict_jsonb() -> None:
    record = _record(
        aar_data={
            "pnl_net": "-6.72",
            "exit_ts": "2026-05-13T16:32:40.386700Z",
            "entry_ts": "2026-05-12T13:32:02Z",
            "exit_reason": "time_stop",
        }
    )
    aar = _row_to_aar(record)
    assert aar is not None
    assert aar.engine == "sigma"
    assert aar.trade_id == "AAPL_1"
    assert aar.pnl_net == Decimal("-6.72")
    assert aar.exit_reason == "time_stop"
    assert aar.entry_ts == datetime(2026, 5, 12, 13, 32, 2, tzinfo=UTC)


def test_row_to_aar_handles_jsonb_as_string() -> None:
    payload = {"pnl_net": "1.50", "exit_ts": "2026-05-12T13:32:02Z"}
    record = _record(aar_data=json.dumps(payload))
    aar = _row_to_aar(record)
    assert aar is not None
    assert aar.pnl_net == Decimal("1.50")


def test_row_to_aar_skips_when_pnl_missing() -> None:
    record = _record(aar_data={"exit_ts": "2026-05-12T13:32:02Z"})
    assert _row_to_aar(record) is None


def test_row_to_aar_skips_when_exit_ts_missing() -> None:
    record = _record(aar_data={"pnl_net": "1"})
    assert _row_to_aar(record) is None


def test_row_to_aar_skips_when_pnl_not_decimal() -> None:
    record = _record(aar_data={"pnl_net": "not-a-number", "exit_ts": "2026-05-12T13:32:02Z"})
    assert _row_to_aar(record) is None


def test_row_to_aar_skips_when_jsonb_string_malformed() -> None:
    record = _record(aar_data="{not valid json")
    assert _row_to_aar(record) is None


# ── AARReader integration with fake pool ────────────────────────────────


class _FakeConn:
    def __init__(self, rows_by_engine: dict[str, list[dict]]) -> None:
        self.rows_by_engine = rows_by_engine
        self.all_rows = [r for rows in rows_by_engine.values() for r in rows]

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if "WHERE engine = $1" in sql:
            return self.rows_by_engine.get(args[0], [])
        return self.all_rows


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_reader_fetch_by_engine_filters_and_parses() -> None:
    sigma_rows = [
        _record(
            engine="sigma",
            trade_id=f"T{i}",
            aar_data={"pnl_net": str(p), "exit_ts": f"2026-05-{10+i:02d}T00:00:00Z"},
        )
        for i, p in enumerate([10, -5, 3])
    ]
    pool = _FakePool(_FakeConn({"sigma": sigma_rows, "momentum": []}))
    reader = AARReader(pool)  # type: ignore[arg-type]

    sigma = await reader.fetch_by_engine("sigma")
    assert [a.trade_id for a in sigma] == ["T0", "T1", "T2"]
    assert [a.pnl_net for a in sigma] == [Decimal("10"), Decimal("-5"), Decimal("3")]


@pytest.mark.asyncio
async def test_reader_fetch_all_grouped_sorts_by_exit_ts() -> None:
    out_of_order = [
        _record(
            engine="sigma",
            trade_id="late",
            aar_data={"pnl_net": "1", "exit_ts": "2026-05-13T00:00:00Z"},
        ),
        _record(
            engine="sigma",
            trade_id="early",
            aar_data={"pnl_net": "2", "exit_ts": "2026-05-11T00:00:00Z"},
        ),
        _record(
            engine="momentum",
            trade_id="mom1",
            aar_data={"pnl_net": "3", "exit_ts": "2026-05-12T00:00:00Z"},
        ),
    ]
    pool = _FakePool(_FakeConn({"all": out_of_order}))
    reader = AARReader(pool)  # type: ignore[arg-type]

    grouped = await reader.fetch_all_grouped()
    assert set(grouped.keys()) == {"sigma", "momentum"}
    assert [a.trade_id for a in grouped["sigma"]] == ["early", "late"]


@pytest.mark.asyncio
async def test_reader_skips_bad_rows_without_failing() -> None:
    rows = [
        _record(
            engine="sigma",
            trade_id="ok",
            aar_data={"pnl_net": "1", "exit_ts": "2026-05-12T00:00:00Z"},
        ),
        _record(engine="sigma", trade_id="bad", aar_data={"pnl_net": None}),
    ]
    pool = _FakePool(_FakeConn({"sigma": rows}))
    reader = AARReader(pool)  # type: ignore[arg-type]
    out = await reader.fetch_by_engine("sigma")
    assert len(out) == 1
    assert out[0].trade_id == "ok"
