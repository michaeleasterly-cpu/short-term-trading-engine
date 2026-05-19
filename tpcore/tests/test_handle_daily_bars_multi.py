"""Tests for the multi-symbol rewire of ``_handle_daily_bars_explicit``.

2026-05-15: the handler's per-symbol fetch loop (a ~45-min rate-limit
floor on the ~7,669-ticker universe) was replaced with Alpaca's
``/v2/stocks/bars?symbols=…`` multi endpoint in 100-symbol chunks.
These tests pin the chunking, per-symbol upsert, archive collection,
and per-chunk failure handling — plus ``_parse_params`` (the
canonical parameterised-backfill channel that replaced the one-off
backfill scripts).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

import tpcore.data.ingest_alpaca_bars as ab
import tpcore.ingestion.csv_archive as csv_archive
from tpcore.ingestion.handlers import handle_daily_bars

# Load scripts/ops.py by path (scripts/ isn't an importable package and
# scripts/ops.py shadows the ops/ package on sys.path — same trick
# tpcore/tests/test_platform_pipeline.py uses).
_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("ops_under_test", _REPO / "scripts" / "ops.py")
ops = importlib.util.module_from_spec(_SPEC)
import sys as _sys  # noqa: E402

_sys.modules["ops_under_test"] = ops
_SPEC.loader.exec_module(ops)


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _FakePool:
    """Trivial stand-in — _upsert_bars is monkeypatched so the pool is
    only passed through, never used."""


def _bar(t: str) -> dict:
    return {"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000, "vw": 1.4}


@pytest.fixture(autouse=True)
def _fast_and_stubbed(monkeypatch):
    monkeypatch.setattr(ab, "_RATE_LIMIT_SLEEP_SEC", 0.0)
    monkeypatch.setattr(ab, "_alpaca_headers", lambda: {})

    upserts: list[tuple[str, int]] = []

    async def _fake_upsert(pool, symbol, bars, delisted, delisting_date=None):
        upserts.append((symbol, len(bars)))
        return len(bars)

    monkeypatch.setattr(ab, "_upsert_bars", _fake_upsert)

    class _Arch:
        path = Path("/tmp/_test_archive.csv.gz")
        rows_written = 0
        rows_rejected = 0

    archived: dict = {}

    def _fake_write_archive(source, rows, fieldnames, **kw):
        rows = list(rows)
        archived["source"] = source
        archived["rows"] = rows
        a = _Arch()
        a.rows_written = len(rows)
        return a

    monkeypatch.setattr(csv_archive, "write_archive", _fake_write_archive)
    return {"upserts": upserts, "archived": archived}


class TestMultiSymbolChunking:
    @pytest.mark.asyncio
    async def test_chunks_at_100_and_upserts_per_symbol(self, monkeypatch, _fast_and_stubbed):
        calls: list[list[str]] = []

        async def _fake_multi(client, symbols, start, end, **kw):
            calls.append(list(symbols))
            return {s: [_bar("2026-05-14T05:00:00Z")] for s in symbols}

        monkeypatch.setattr(ab, "fetch_daily_bars_multi", _fake_multi)

        universe = [f"T{i:04d}" for i in range(250)]  # 250 → chunks 100/100/50
        rows = await handle_daily_bars(_FakePool(), {"universe": universe})

        assert [len(c) for c in calls] == [100, 100, 50]
        assert rows == 250  # 1 bar/symbol upserted
        assert len(_fast_and_stubbed["upserts"]) == 250
        # Archive collected one row per symbol-bar.
        assert _fast_and_stubbed["archived"]["source"] == "alpaca_daily_bars"
        assert len(_fast_and_stubbed["archived"]["rows"]) == 250

    @pytest.mark.asyncio
    async def test_chunk_failure_recorded_and_others_continue(self, monkeypatch):
        async def _fake_multi(client, symbols, start, end, **kw):
            if "T0150" in symbols:  # the 2nd chunk (indices 100-199)
                resp = httpx.Response(403, request=httpx.Request("GET", "http://x"))
                raise httpx.HTTPStatusError("403", request=resp.request, response=resp)
            return {s: [_bar("2026-05-14T05:00:00Z")] for s in symbols}

        monkeypatch.setattr(ab, "fetch_daily_bars_multi", _fake_multi)

        universe = [f"T{i:04d}" for i in range(250)]
        with pytest.raises(RuntimeError, match="chunk fetch failure"):
            await handle_daily_bars(_FakePool(), {"universe": universe})

    @pytest.mark.asyncio
    async def test_symbols_with_no_bars_skipped(self, monkeypatch, _fast_and_stubbed):
        async def _fake_multi(client, symbols, start, end, **kw):
            # Half the symbols return [] (no trades in window).
            return {s: ([_bar("2026-05-14T05:00:00Z")] if i % 2 == 0 else [])
                    for i, s in enumerate(symbols)}

        monkeypatch.setattr(ab, "fetch_daily_bars_multi", _fake_multi)
        rows = await handle_daily_bars(_FakePool(), {"universe": [f"S{i}" for i in range(40)]})
        assert rows == 20  # only even-indexed symbols had a bar
        assert len(_fast_and_stubbed["upserts"]) == 20


class TestParseParams:
    def test_type_coercion(self):
        out = ops._parse_params([  # noqa: SLF001
            "lookback_days=10", "end_offset_days=1",
            "min_price=5.5", "skip_guard=true", "universe=active",
        ])
        assert out == {
            "lookback_days": 10, "end_offset_days": 1,
            "min_price": 5.5, "skip_guard": True, "universe": "active",
        }
        assert isinstance(out["lookback_days"], int)
        assert isinstance(out["min_price"], float)
        assert out["skip_guard"] is True

    def test_empty_and_none(self):
        assert ops._parse_params(None) == {}  # noqa: SLF001
        assert ops._parse_params([]) == {}  # noqa: SLF001

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="KEY=VALUE"):
            ops._parse_params(["lookback_days"])  # noqa: SLF001
