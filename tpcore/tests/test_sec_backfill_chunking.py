"""Tests for the #132 bounded/resumable SEC backfill chunking.

Covers the root-cause fix for the pooler "connection was closed" on
the monolithic bootstrap: ticker-chunked download→load→commit, the
``skip_covered`` targeted-resume filter, multi-chunk failure tolerance
(a transient chunk error doesn't abort the run), and that the daily
(single-chunk) path is unchanged + still re-raises.
"""
from __future__ import annotations

import pytest

from tpcore.ingestion import handlers


class _Conn:
    def __init__(self, covered: list[str]):
        self._covered = covered

    async def fetch(self, sql, *a):
        s = sql.lower()
        if "liquidity_tiers" in s:
            return [{"ticker": t} for t in
                    ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]]
        if "sec_insider_transactions" in s or "sec_material_events" in s:
            return [{"ticker": t} for t in self._covered]
        return []

    async def fetchval(self, *a):
        return None  # skip-guard: no prior rows → don't skip


class _Pool:
    def __init__(self, covered=None):
        self._covered = covered or []

    def acquire(self):
        conn = _Conn(self._covered)

        class _CM:
            async def __aenter__(self): return conn
            async def __aexit__(self, *e): return None
        return _CM()


def _patch(monkeypatch, *, fail_chunks=()):
    """Stub the download + load helpers; record which ticker chunks ran."""
    seen: list[list[str]] = []

    async def fake_dl(universe, since, ins_csv, mat_csv):
        seen.append(list(universe))
        if len(seen) in fail_chunks:
            raise RuntimeError("simulated pooler: connection was closed")
        # one insider row per ticker, no material rows
        rows = [(t, since, "X", "BUY", 1, "1.0", "1.0") for t in universe]
        return rows, [], len(rows), 0, len(universe)

    async def fake_load(pool, ins, mat):
        return len(ins), len(mat)

    monkeypatch.setattr(handlers, "_sec_download_to_csv", fake_dl)
    monkeypatch.setattr(handlers, "_sec_load_csvs_to_db", fake_load)
    monkeypatch.setattr(handlers, "_gzip_in_place", lambda *_a: None)
    return seen


async def test_skip_covered_filters_done_tickers(monkeypatch) -> None:
    seen = _patch(monkeypatch)
    n = await handlers.handle_sec_filings(
        _Pool(covered=["AAA", "CCC"]),
        {"skip_covered": True, "ticker_chunk_size": 0, "skip_guard_days": 0},
    )
    flat = [t for c in seen for t in c]
    assert "AAA" not in flat and "CCC" not in flat
    assert set(flat) == {"BBB", "DDD", "EEE", "FFF"}
    assert n == 4  # one row per remaining ticker


async def test_chunking_splits_universe(monkeypatch) -> None:
    seen = _patch(monkeypatch)
    await handlers.handle_sec_filings(
        _Pool(), {"ticker_chunk_size": 2, "skip_guard_days": 0},
    )
    assert [len(c) for c in seen] == [2, 2, 2]  # 6 tickers / 2


async def test_multichunk_failure_is_tolerated(monkeypatch) -> None:
    """A failing chunk doesn't abort the run; the others still load
    (resumable bootstrap). 6 tickers / chunk 2 → 3 chunks, #2 fails."""
    seen = _patch(monkeypatch, fail_chunks=(2,))
    n = await handlers.handle_sec_filings(
        _Pool(), {"ticker_chunk_size": 2, "skip_guard_days": 0},
    )
    assert len(seen) == 3            # all attempted
    assert n == 4                    # chunks 1 & 3 loaded (2 tickers each)


async def test_single_chunk_reraises(monkeypatch) -> None:
    """Daily (single-chunk) path must still surface the error."""
    _patch(monkeypatch, fail_chunks=(1,))
    with pytest.raises(RuntimeError, match="connection was closed"):
        await handlers.handle_sec_filings(
            _Pool(), {"ticker_chunk_size": 0, "skip_guard_days": 0},
        )
