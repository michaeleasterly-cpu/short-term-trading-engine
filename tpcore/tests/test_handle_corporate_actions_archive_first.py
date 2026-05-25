"""Tests for the archive-first refactor of ``handle_corporate_actions``.

P1-sibling trust-audit (2026-05-25 PR-3): the corp_actions handler
used to call ``upsert_corporate_actions`` INSIDE the per-chunk loop,
then write the archive AFTER. Now: all actions accumulate in memory,
``manifest_lifecycle`` writes archive + manifest BEFORE any production
write, ETL reads the archive file back, calls upsert from that.
Hermetic — no live DB, no live Alpaca.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

import tpcore.ingestion.csv_archive as csv_archive
from tpcore.ingestion.handlers import (
    _action_to_archive_row,
    _archive_row_to_action,
)


def _action(
    *, ticker: str = "AAPL", date_iso: str = "2024-01-15",
    type_: str = "split", ratio: str = "2.0", raw_data: dict | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "action_date": date.fromisoformat(date_iso),
        "action_type": type_,
        "ratio": Decimal(ratio),
        "raw_data": raw_data or {"symbol": ticker, "type": type_, "extra": "x" * 1000},
    }


class TestActionArchiveSerde:
    def test_roundtrip_preserves_action_fields(self) -> None:
        a = _action()
        row = _action_to_archive_row(a)
        back = _archive_row_to_action(row)
        assert back["ticker"] == a["ticker"]
        assert back["action_date"] == a["action_date"]
        assert back["action_type"] == a["action_type"]
        assert back["ratio"] == a["ratio"]
        assert back["raw_data"] == a["raw_data"]

    def test_raw_data_not_truncated(self) -> None:
        """The legacy handler clipped raw_data to 500 chars — that broke
        archive-as-substrate round-trip for actions with longer payloads.
        Pin that the new serde preserves the FULL raw_data dict."""
        huge_raw = {"k": "v" * 2000, "nested": {"deep": "x" * 1500}}
        a = _action(raw_data=huge_raw)
        row = _action_to_archive_row(a)
        # The serialized JSON exceeds the legacy 500-char cap by far.
        assert len(row["raw_data"]) > 3000
        back = _archive_row_to_action(row)
        assert back["raw_data"] == huge_raw

    def test_csv_safe_under_complex_raw_data(self, tmp_path: Path, monkeypatch) -> None:
        """Quote / comma / newline-containing raw_data survives a real
        CSV write+read via :mod:`tpcore.ingestion.csv_archive`."""
        monkeypatch.setattr(csv_archive, "repo_data_dir", lambda: tmp_path)
        nasty_raw = {
            "title": 'has "quotes" and, commas',
            "body": "and\nembedded\nnewlines",
            "extra": "x" * 5000,
        }
        rows = [_action_to_archive_row(_action(raw_data=nasty_raw))]
        res = csv_archive.write_archive(
            "alpaca_corporate_actions", rows,
            fieldnames=["ticker", "action_date", "action_type", "ratio", "raw_data"],
        )
        assert res.rows_written == 1
        from tpcore.ingestion.archive_etl import read_archive_csv
        back_rows = read_archive_csv(res.path)
        assert len(back_rows) == 1
        back = _archive_row_to_action(back_rows[0])
        assert back["raw_data"] == nasty_raw


# ─────────────────────────────────────────────────────────────────────
# End-to-end: handler runs archive-first ordering
# ─────────────────────────────────────────────────────────────────────


class _RecordingConn:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self._next_id = uuid4()

    async def fetch(self, _sql, *_args):
        return []  # no universe rows; we'll supply via config

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self._next_id

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn
    async def __aexit__(self, *exc): return None


class _FakePool:
    def __init__(self): self.conn = _RecordingConn()
    def acquire(self): return _AcquireCM(self.conn)


@pytest.fixture
def _archive_root(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_archive, "repo_data_dir", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_handler_archives_before_upsert_and_marks_loaded(
    _archive_root, monkeypatch,
) -> None:
    """Phase ordering invariant: archive + manifest INSERT precede the
    upsert. The upsert is called once after Phase 2 reads the archive."""
    from tpcore.ingestion import handlers
    pool = _FakePool()
    events: list[str] = []

    # Fake the corp-actions fetch to return a fixed set of normalized
    # actions for the chunk's symbols.
    sample_actions = [
        _action(ticker="AAPL", date_iso="2020-08-31", type_="split", ratio="4"),
        _action(ticker="MSFT", date_iso="2024-03-15", type_="dividend", ratio="0.75"),
    ]

    async def _fake_fetch(client, *, symbols, start, end, types):
        del client, symbols, start, end, types
        events.append("fetch")
        return list(sample_actions)

    async def _fake_upsert(pool_arg, actions: list[dict]) -> int:
        del pool_arg
        events.append(f"upsert:{len(actions)}")
        # Verify what reaches the upsert is the parsed-from-archive
        # action shape, not the in-memory list (the orchestrator
        # re-parses through the CSV file).
        for a in actions:
            assert isinstance(a["action_date"], date)
            assert isinstance(a["ratio"], Decimal)
        return len(actions)

    monkeypatch.setattr(
        "tpcore.data.ingest_corporate_actions.fetch_corporate_actions",
        _fake_fetch,
    )
    monkeypatch.setattr(
        "tpcore.data.ingest_corporate_actions.upsert_corporate_actions",
        _fake_upsert,
    )
    monkeypatch.setattr(
        "tpcore.data.ingest_alpaca_bars._alpaca_headers",
        lambda: {"APCA-API-KEY-ID": "fake", "APCA-API-SECRET-KEY": "fake"},
    )
    # apply_all_splits doesn't matter for this invariant
    async def _noop_splits(_pool, only_tickers=None):
        del _pool, only_tickers
        return {"applied": [], "skipped": []}
    monkeypatch.setattr(
        "tpcore.data.apply_splits.apply_all_splits", _noop_splits,
    )
    # Skip shrinkage detection (no archive history in tmp).
    monkeypatch.setattr(
        "tpcore.ingestion.csv_archive.detect_shrinkage",
        lambda *a, **k: None,
    )
    # Skip d2 metrics (no DB).
    async def _noop_metrics(*a, **k):
        return None

    class _V:
        shrunk = False
        median_rows = 0
        samples_used = 0
        shrinkage_pct = 0.0

    async def _noop_v2(*a, **k):
        return _V()
    monkeypatch.setattr(
        "tpcore.ingestion.d2_metrics.record_ingestion_metrics", _noop_metrics,
    )
    monkeypatch.setattr(
        "tpcore.ingestion.d2_metrics.check_shrinkage_vs_rolling_median", _noop_v2,
    )
    # Record manifest INSERT timing
    orig_fetchval = pool.conn.fetchval

    async def _spy_fetchval(sql, *args):
        if "INSERT" in sql and "ingest_manifest" in sql:
            events.append("manifest:insert")
        return await orig_fetchval(sql, *args)
    pool.conn.fetchval = _spy_fetchval  # type: ignore[assignment]

    result = await handlers.handle_corporate_actions(
        pool, {"universe": ["AAPL", "MSFT"], "ingest_start": "2018-01-01"},
    )
    assert result == 2
    # Required ordering: fetch → manifest:insert → upsert
    assert events[0] == "fetch"
    insert_idx = events.index("manifest:insert")
    upsert_idx = next(i for i, e in enumerate(events) if e.startswith("upsert:"))
    assert insert_idx < upsert_idx, events

    # Manifest INSERT INSERT'd with status='archived' and source/provider.
    sql, args = pool.conn.fetchval_calls[0]
    assert "platform.ingest_manifest" in sql
    assert args[0] == "alpaca_corporate_actions"
    assert args[1] == "alpaca"
    assert args[6] == "archived"
    # Exactly one UPDATE — mark_loaded.
    assert len(pool.conn.execute_calls) == 1
    _, upd_args = pool.conn.execute_calls[0]
    assert upd_args[1] == "loaded"
    assert upd_args[2] == 2


@pytest.mark.asyncio
async def test_handler_failed_upsert_marks_manifest_failed(
    _archive_root, monkeypatch,
) -> None:
    from tpcore.ingestion import handlers
    pool = _FakePool()

    async def _fake_fetch(client, *, symbols, start, end, types):
        del client, symbols, start, end, types
        return [_action()]

    async def _raising_upsert(pool_arg, actions):
        del pool_arg, actions
        raise RuntimeError("simulated upsert outage")

    async def _noop_splits(_pool, only_tickers=None):
        del _pool, only_tickers
        return {"applied": [], "skipped": []}

    monkeypatch.setattr(
        "tpcore.data.ingest_corporate_actions.fetch_corporate_actions",
        _fake_fetch,
    )
    monkeypatch.setattr(
        "tpcore.data.ingest_corporate_actions.upsert_corporate_actions",
        _raising_upsert,
    )
    monkeypatch.setattr(
        "tpcore.data.apply_splits.apply_all_splits", _noop_splits,
    )
    monkeypatch.setattr(
        "tpcore.data.ingest_alpaca_bars._alpaca_headers",
        lambda: {"APCA-API-KEY-ID": "fake", "APCA-API-SECRET-KEY": "fake"},
    )

    with pytest.raises(RuntimeError, match="simulated upsert outage"):
        await handlers.handle_corporate_actions(
            pool, {"universe": ["AAPL"], "ingest_start": "2024-01-01"},
        )
    # Manifest archived (INSERT) + failed (UPDATE).
    assert len(pool.conn.fetchval_calls) == 1
    assert len(pool.conn.execute_calls) == 1
    _, upd_args = pool.conn.execute_calls[0]
    assert upd_args[1] == "failed"
    assert "RuntimeError" in upd_args[3]
