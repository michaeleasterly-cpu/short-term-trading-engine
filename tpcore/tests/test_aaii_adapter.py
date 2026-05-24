"""Tests for the AAII Sentiment Survey adapter + handler.

Cases: successful download+parse (real .xls fixture), corrupt-row +
footer skipping, historical value validation, 404 missing file,
malformed/empty workbook, 403 permanent anti-bot block (no retry),
429 retry, and the handler's skip-guard + idempotent upsert.
httpx.MockTransport — no network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from tpcore.aaii import AAIIAdapter, parse_sentiment_workbook
from tpcore.outage import DataProviderOutage

_FIXTURE = Path(__file__).parent / "fixtures" / "aaii_sample.xls"
_XLS_BYTES = _FIXTURE.read_bytes()


def _adapter(handler) -> AAIIAdapter:
    return AAIIAdapter(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.aaii.com", follow_redirects=True))


async def test_happy_download_and_parse() -> None:
    a = _adapter(lambda r: httpx.Response(200, content=_XLS_BYTES))
    recs = await a.get_sentiment_history()
    # 3 good rows; the corrupt (sum≈378) row and the "Count '24"
    # footer row are skipped, not persisted.
    assert len(recs) == 3
    assert [r.date.isoformat() for r in recs] == [
        "1987-07-24", "2026-05-07", "2026-05-14"]
    await a.aclose()


async def test_historical_value_validation() -> None:
    a = _adapter(lambda r: httpx.Response(200, content=_XLS_BYTES))
    recs = await a.get_sentiment_history()
    by_date = {r.date.isoformat(): r for r in recs}
    first = by_date["1987-07-24"]
    assert first.bullish_pct == Decimal("36.00")
    assert first.neutral_pct == Decimal("50.00")
    assert first.bearish_pct == Decimal("14.00")
    last = by_date["2026-05-14"]
    assert last.bullish_pct == Decimal("39.32")
    assert last.neutral_pct == Decimal("24.07")
    assert last.bearish_pct == Decimal("36.61")
    # Every retained row sums to ~100 (the corrupt one was dropped).
    for r in recs:
        assert abs(r.bullish_pct + r.neutral_pct + r.bearish_pct
                   - Decimal("100")) <= Decimal("1.5")
    await a.aclose()


async def test_corrupt_row_skipped_not_persisted() -> None:
    recs = parse_sentiment_workbook(_XLS_BYTES)
    assert "2026-04-30" not in {r.date.isoformat() for r in recs}


async def test_missing_file_404_is_outage() -> None:
    a = _adapter(lambda r: httpx.Response(404, text="not found"))
    with pytest.raises(DataProviderOutage, match="404"):
        await a.get_sentiment_history()
    await a.aclose()


async def test_malformed_workbook_is_outage() -> None:
    a = _adapter(lambda r: httpx.Response(200, content=b"not-a-real-xls"))
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_sentiment_history()
    await a.aclose()


async def test_empty_body_is_outage() -> None:
    a = _adapter(lambda r: httpx.Response(200, content=b""))
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_sentiment_history()
    await a.aclose()


async def test_403_block_is_permanent_outage_no_retry() -> None:
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(403, text="blocked")
    a = _adapter(h)
    with pytest.raises(DataProviderOutage):
        await a.get_sentiment_history()
    assert n["c"] == 1  # 403 is permanent — not retried
    await a.aclose()


async def test_429_retries_then_succeeds() -> None:
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(200, content=_XLS_BYTES)
    a = _adapter(h)
    recs = await a.get_sentiment_history()
    assert len(recs) == 3 and n["c"] == 2
    await a.aclose()


# ── Handler: skip-guard + idempotent upsert ────────────────────────────

class _Adapter:
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    async def get_sentiment_history(self):
        return parse_sentiment_workbook(_XLS_BYTES)


class _Conn:
    def __init__(self, newest):
        self._newest = newest
        self.upserts: list[list] = []
        self.sql = ""

    async def fetchval(self, *a):
        return self._newest

    async def executemany(self, sql, rows):
        self.sql = sql
        self.upserts.append(list(rows))

    # Task #18 P3: handle_aaii_sentiment now wraps the legacy upsert
    # + macro_data double-write in conn.transaction(). The double-write
    # itself is monkeypatched to a no-op in this test (see below) — but
    # the transaction context manager must still exist on the mock.
    def transaction(self):
        class _Txn:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *exc): return None
        return _Txn()


class _Pool:
    def __init__(self, conn): self._conn = conn
    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self): return conn
            async def __aexit__(self, *e): return None
        return _CM()


async def test_handler_skip_guard_fresh(monkeypatch) -> None:
    """Recent recorded_at within skip_guard_days → no-op (returns 0)."""
    from tpcore.ingestion import handlers
    conn = _Conn(newest=datetime.now(UTC) - timedelta(days=1))
    monkeypatch.setattr("tpcore.aaii.AAIIAdapter", _Adapter)
    n = await handlers.handle_aaii_sentiment(_Pool(conn), {"skip_guard_days": 5})
    assert n == 0 and conn.upserts == []


async def test_handler_idempotent_upsert(monkeypatch) -> None:
    """Two forced runs produce the same rows + a bitemporal SCD-2 emit per
    call (idempotent / self-correcting full-history workbook).

    Task #18 P5: legacy platform.aaii_sentiment renamed to *_legacy and
    the canonical name is now a view. Handler writes ONLY to macro_data
    via upsert_macro_data_bitemporal. This test stubs the helper as a
    call-recorder and verifies the (channel, date, value, None) row
    shape it gets from the wide → 3-tall fan-out.
    """
    from tpcore.ingestion import handlers
    conn = _Conn(newest=None)
    monkeypatch.setattr("tpcore.aaii.AAIIAdapter", _Adapter)
    monkeypatch.setattr("tpcore.ingestion.csv_archive.write_archive",
                        lambda *a, **k: type("A", (), {"path": "/tmp/x"})())
    calls: list[dict] = []
    async def _record(conn_, *, source, rows):
        calls.append({"source": source, "row_count": len(rows), "rows": list(rows)})
        return {"inserted": len(rows), "revised": 0, "no_change": 0}
    monkeypatch.setattr(
        "tpcore.ingestion.macro_data_emit.upsert_macro_data_bitemporal", _record,
    )
    n1 = await handlers.handle_aaii_sentiment(_Pool(conn), {"skip_guard_days": 0})
    n2 = await handlers.handle_aaii_sentiment(_Pool(conn), {"skip_guard_days": 0})
    assert n1 == n2 == 3
    assert len(calls) == 2 and calls[0]["source"] == "aaii"
    # Wide → 3 tall channels per date; 3 dates in the fixture → 9 rows per call.
    assert calls[0]["row_count"] == calls[1]["row_count"] == 9
    assert calls[0]["rows"] == calls[1]["rows"]  # identical → idempotent
    # Channel shape sanity: every row is (str, date, numeric, None) — value_num
    # channel, value_text NULL (XOR CHECK).
    for r in calls[0]["rows"]:
        assert isinstance(r[0], str) and r[0] in ("bullish_pct","bearish_pct","neutral_pct")
        assert r[3] is None
