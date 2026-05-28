"""Tests for the multi-symbol rewire of ``_handle_daily_bars_explicit``.

2026-05-15: the handler's per-symbol fetch loop (a ~45-min rate-limit
floor on the ~7,669-ticker universe) was replaced with Alpaca's
``/v2/stocks/bars?symbols=…`` multi endpoint in 100-symbol chunks.

2026-05-22: with FMP as the default daily-bars feed, every test here
that exercises the Alpaca multi-symbol code path explicitly pins
``feed="iex"`` so the legacy chunking semantics still get covered.
The FMP path has its own dedicated suite in ``test_ingest_fmp_bars*``.

2026-05-25 (P1 trust-audit): ``_fetch_via_alpaca`` no longer touches
the production DB — it returns ``(archive_rows, failures)``. The
``archive_first_load_bars`` orchestrator writes the archive +
manifest BEFORE the upsert and reads bars BACK FROM the on-disk
archive. These tests pin chunking + per-symbol upsert (driven from
the archive read), per-chunk failure recording, and ``_parse_params``.
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


class _RecordingConn:
    def __init__(self) -> None:
        self.fetchval_calls: list = []
        self.execute_calls: list = []

    async def fetchval(self, _sql: str, *args):
        from uuid import uuid4
        self.fetchval_calls.append(args)
        return uuid4()

    async def execute(self, _sql: str, *args):
        self.execute_calls.append(args)
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *_exc) -> None:
        return None


class _FakePool:
    """Pool that routes acquire()→RecordingConn for manifest writes."""
    def __init__(self) -> None:
        self.conn = _RecordingConn()

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


def _bar(t: str) -> dict:
    return {"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000, "vw": 1.4}


@pytest.fixture
def _archive_root(tmp_path, monkeypatch):
    """Redirect csv_archive to tmp so tests don't touch real data/."""
    monkeypatch.setattr(csv_archive, "repo_data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _fast_and_stubbed(monkeypatch):
    """Mock the transport (rate limit + headers + per-ticker upsert).

    The archive-first orchestrator is real — it writes the CSV +
    manifest + reads back. Only the network transport + final per-
    ticker upsert are stubbed.
    """
    monkeypatch.setattr(ab, "_RATE_LIMIT_SLEEP_SEC", 0.0)
    monkeypatch.setattr(ab, "_alpaca_headers", lambda: {})

    upserts: list[tuple[str, int]] = []

    async def _fake_upsert_batch(_pool, bars_by_ticker, *, staging_run_id=None, delisted=False, source=None):
        del _pool, delisted, source
        total = 0
        for symbol, bars in bars_by_ticker.items():
            upserts.append((symbol, len(bars)))
            total += len(bars)
        return total

    # 2026-05-28: archive_etl now calls the batch variant (one call per
    # chunk instead of one call per ticker).
    monkeypatch.setattr(ab, "stage_then_promote_bars_batch", _fake_upsert_batch)
    return {"upserts": upserts}


class TestMultiSymbolChunking:
    @pytest.mark.asyncio
    async def test_chunks_at_100_and_upserts_per_symbol(
        self, monkeypatch, _archive_root, _fast_and_stubbed,
    ):
        calls: list[list[str]] = []

        async def _fake_multi(client, symbols, start, end, **kw):
            calls.append(list(symbols))
            return {s: [_bar("2026-05-14T05:00:00Z")] for s in symbols}

        monkeypatch.setattr(ab, "fetch_daily_bars_multi", _fake_multi)

        universe = [f"T{i:04d}" for i in range(250)]  # 250 → chunks 100/100/50
        rows = await handle_daily_bars(
            _FakePool(), {"universe": universe, "feed": "iex"},
        )

        assert [len(c) for c in calls] == [100, 100, 50]
        assert rows == 250  # 1 bar/symbol upserted via ETL
        assert len(_fast_and_stubbed["upserts"]) == 250
        # Archive landed on disk under tmp.
        files = list((_archive_root / "alpaca_daily_bars_archive").glob("*.csv.gz"))
        assert len(files) == 1, files

    @pytest.mark.asyncio
    async def test_chunk_failure_recorded_and_others_continue(
        self, monkeypatch, _archive_root,
    ):
        async def _fake_multi(client, symbols, start, end, **kw):
            if "T0150" in symbols:  # the 2nd chunk (indices 100-199)
                resp = httpx.Response(403, request=httpx.Request("GET", "http://x"))
                raise httpx.HTTPStatusError("403", request=resp.request, response=resp)
            return {s: [_bar("2026-05-14T05:00:00Z")] for s in symbols}

        monkeypatch.setattr(ab, "fetch_daily_bars_multi", _fake_multi)

        universe = [f"T{i:04d}" for i in range(250)]
        with pytest.raises(RuntimeError, match="fetch failure"):
            await handle_daily_bars(
                _FakePool(), {"universe": universe, "feed": "iex"},
            )

    @pytest.mark.asyncio
    async def test_symbols_with_no_bars_skipped(
        self, monkeypatch, _archive_root, _fast_and_stubbed,
    ):
        async def _fake_multi(client, symbols, start, end, **kw):
            # Half the symbols return [] (no trades in window).
            return {s: ([_bar("2026-05-14T05:00:00Z")] if i % 2 == 0 else [])
                    for i, s in enumerate(symbols)}

        monkeypatch.setattr(ab, "fetch_daily_bars_multi", _fake_multi)
        rows = await handle_daily_bars(
            _FakePool(),
            {"universe": [f"S{i}" for i in range(40)], "feed": "iex"},
        )
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
