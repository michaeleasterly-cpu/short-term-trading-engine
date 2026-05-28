"""Tests for ``tpcore.ingestion.archive_etl.archive_first_load_bars`` —
the archive-first orchestrator for prices_daily ingestion.

P1 trust-audit (2026-05-25): pins the contract that production writes
to ``platform.prices_daily`` are preceded by:

    1. archive CSV on disk
    2. manifest row INSERT with status=ARCHIVED
    3. ETL that reads back from the on-disk archive (not the
       in-memory archive_rows)
    4. manifest UPDATE to LOADED on success, FAILED on exception.

Hermetic: no live DB; ``_FakePool`` + ``monkeypatch`` for the archive
write + upsert internals.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tpcore.ingestion import archive_etl, csv_archive, manifest


@pytest.fixture
def _archive_root(tmp_path, monkeypatch):
    """Redirect csv_archive to a tmp dir so tests don't touch real data/."""
    monkeypatch.setattr(csv_archive, "repo_data_dir", lambda: tmp_path)
    return tmp_path


class _RecordingConn:
    """asyncpg-shaped fake that records every fetchval / execute call.

    fetchval returns a deterministic UUID per invocation so tests can
    map manifest_id → call.
    """
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self._next_id = uuid4()

    async def fetchval(self, sql: str, *args: object) -> Any:
        self.fetchval_calls.append((sql, args))
        return self._next_id

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _RecordingConn()

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


def _sample_archive_rows() -> list[dict]:
    return [
        {"ticker": "AAPL", "date": "2026-05-20",
         "open": "180.0", "high": "182.0", "low": "179.0",
         "close": "181.0", "volume": "1000000", "vwap": "180.5"},
        {"ticker": "AAPL", "date": "2026-05-21",
         "open": "181.0", "high": "183.0", "low": "180.5",
         "close": "182.5", "volume": "1100000", "vwap": "181.8"},
        {"ticker": "MSFT", "date": "2026-05-20",
         "open": "410.0", "high": "412.0", "low": "409.0",
         "close": "411.0", "volume": "900000", "vwap": "410.5"},
    ]


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_archive_runs_before_any_upsert(
        self, _archive_root, monkeypatch,
    ) -> None:
        """Phase ordering invariant: archive write + manifest INSERT
        must complete BEFORE the first _upsert_bars call.

        This is the prime-directive enforcement: a recorder captures
        the relative ordering of (manifest INSERT, _upsert_bars). The
        manifest INSERT must precede every upsert."""
        pool = _FakePool()
        events: list[str] = []

        async def _spy_upsert_batch(_pool, bars_by_ticker, *, staging_run_id=None, delisted=False, source=None):
            del _pool, delisted, source
            # Per-ticker event log preserved for the ordering assertions.
            for ticker in bars_by_ticker:
                events.append(f"upsert:{ticker}")
            return sum(len(v) for v in bars_by_ticker.values())

        # Patch the batch upsert (2026-05-28: per-ticker variant collapsed
        # to one call/N tickers — 4 RTTs total vs 5×N).
        monkeypatch.setattr(
            "tpcore.data.ingest_alpaca_bars.stage_then_promote_bars_batch",
            _spy_upsert_batch,
        )
        # Wrap pool.acquire().fetchval to record the INSERT timing too.
        orig_fetchval = pool.conn.fetchval

        async def _spy_fetchval(sql: str, *args: object):
            if "INSERT" in sql and "ingest_manifest" in sql:
                events.append("manifest:insert")
            return await orig_fetchval(sql, *args)
        pool.conn.fetchval = _spy_fetchval  # type: ignore[assignment]

        rows_loaded, archive_path = await archive_etl.archive_first_load_bars(
            pool,
            archive_rows=_sample_archive_rows(),
            source="fmp_daily_bars", provider="fmp",
            date_range_start=date(2026, 5, 18),
            date_range_end=date(2026, 5, 25),
        )
        assert rows_loaded > 0
        # The first event must be the manifest INSERT; every upsert
        # follows. Any upsert before the manifest INSERT is a violation.
        assert events[0] == "manifest:insert", events
        assert all(e.startswith("upsert:") for e in events[1:]), events
        # Both tickers got their own upsert (one per group).
        assert "upsert:AAPL" in events
        assert "upsert:MSFT" in events

    @pytest.mark.asyncio
    async def test_writes_archive_with_checksum(
        self, _archive_root, monkeypatch,
    ) -> None:
        pool = _FakePool()

        async def _noop_upsert(*_a, **_kw):
            return 0

        monkeypatch.setattr(
            "tpcore.data.ingest_alpaca_bars.stage_then_promote_bars_batch",
            _noop_upsert,
        )

        await archive_etl.archive_first_load_bars(
            pool,
            archive_rows=_sample_archive_rows(),
            source="fmp_daily_bars", provider="fmp",
            date_range_start=date(2026, 5, 18),
            date_range_end=date(2026, 5, 25),
        )
        # Archive landed on disk under the tmp root.
        files = list((_archive_root / "fmp_daily_bars_archive").glob("*.csv.gz"))
        assert len(files) == 1
        # Manifest INSERT captured the SHA-256 of that exact file.
        sql, args = pool.conn.fetchval_calls[0]
        assert "ingest_manifest" in sql
        archive_path_arg = args[3]
        checksum_arg = args[7]
        assert Path(archive_path_arg) == files[0]
        assert checksum_arg == manifest.compute_sha256(files[0])
        assert checksum_arg != ""

    @pytest.mark.asyncio
    async def test_marks_loaded_after_etl_success(
        self, _archive_root, monkeypatch,
    ) -> None:
        pool = _FakePool()

        async def _upsert(_pool, bars_by_ticker, *, staging_run_id=None, delisted=False, source=None):
            del _pool, delisted, source
            return sum(len(v) for v in bars_by_ticker.values())
        monkeypatch.setattr(
            "tpcore.data.ingest_alpaca_bars.stage_then_promote_bars_batch", _upsert,
        )

        await archive_etl.archive_first_load_bars(
            pool,
            archive_rows=_sample_archive_rows(),
            source="fmp_daily_bars", provider="fmp",
            date_range_start=date(2026, 5, 18),
            date_range_end=date(2026, 5, 25),
        )
        # Exactly one UPDATE call (mark_loaded). Status=LOADED, count=3.
        assert len(pool.conn.execute_calls) == 1
        sql, args = pool.conn.execute_calls[0]
        assert "UPDATE platform.ingest_manifest" in sql
        assert args[1] == "loaded"
        assert args[2] == 3


# ─────────────────────────────────────────────────────────────────────
# Failure path: archive write blocks production
# ─────────────────────────────────────────────────────────────────────


class TestFailedArchive:
    @pytest.mark.asyncio
    async def test_failed_archive_write_blocks_upsert_and_manifest(
        self, _archive_root, monkeypatch,
    ) -> None:
        """If write_archive raises, no _upsert_bars call happens AND
        no manifest row is inserted. The archive-first invariant: a
        failed archive means zero production-side state change."""
        pool = _FakePool()
        upsert_called = []

        async def _spy_upsert(*_a, **_kw):
            upsert_called.append("called")
            return 0

        monkeypatch.setattr(
            "tpcore.data.ingest_alpaca_bars.stage_then_promote_bars", _spy_upsert,
        )
        # Force the archive writer to fail.
        def _boom(*_a, **_kw):
            raise OSError("disk full")
        monkeypatch.setattr(archive_etl, "write_archive", _boom)

        with pytest.raises(OSError, match="disk full"):
            await archive_etl.archive_first_load_bars(
                pool,
                archive_rows=_sample_archive_rows(),
                source="fmp_daily_bars", provider="fmp",
                date_range_start=date(2026, 5, 18),
                date_range_end=date(2026, 5, 25),
            )
        assert upsert_called == []
        assert pool.conn.fetchval_calls == []
        assert pool.conn.execute_calls == []


# ─────────────────────────────────────────────────────────────────────
# Failure path: ETL crashes mid-upsert ⇒ manifest FAILED
# ─────────────────────────────────────────────────────────────────────


class TestFailedEtl:
    @pytest.mark.asyncio
    async def test_failed_etl_marks_manifest_failed(
        self, _archive_root, monkeypatch,
    ) -> None:
        pool = _FakePool()

        async def _raising_upsert(*_a, **_kw):
            raise RuntimeError("simulated upsert failure")
        monkeypatch.setattr(
            "tpcore.data.ingest_alpaca_bars.stage_then_promote_bars_batch",
            _raising_upsert,
        )

        with pytest.raises(RuntimeError, match="simulated upsert failure"):
            await archive_etl.archive_first_load_bars(
                pool,
                archive_rows=_sample_archive_rows(),
                source="fmp_daily_bars", provider="fmp",
                date_range_start=date(2026, 5, 18),
                date_range_end=date(2026, 5, 25),
            )
        # Manifest INSERT happened (status=ARCHIVED), then UPDATE to FAILED.
        assert len(pool.conn.fetchval_calls) == 1
        assert len(pool.conn.execute_calls) == 1
        sql, args = pool.conn.execute_calls[0]
        assert "UPDATE platform.ingest_manifest" in sql
        assert args[1] == "failed"
        # Error summary mentions the original exception type.
        assert "RuntimeError" in args[3]


# ─────────────────────────────────────────────────────────────────────
# ETL reads the archive FILE (not the in-memory list)
# ─────────────────────────────────────────────────────────────────────


class TestEtlReadsArchive:
    @pytest.mark.asyncio
    async def test_etl_sees_archive_file_content_not_input_list(
        self, _archive_root, monkeypatch,
    ) -> None:
        """The orchestrator's ETL phase MUST read rows from the archive
        file, not the in-memory archive_rows the caller handed in. We
        prove this by clobbering the file *after* archive write and
        observing that the upsert sees the CLOBBERED data.

        Mechanism: spy on _upsert_bars to capture the ticker→bars dict
        it receives. After write_archive lands the on-disk CSV, we
        replace it with a single row for a *different* ticker. If the
        ETL reads the file, the spy sees the clobbered ticker; if it
        reads the in-memory list, the spy sees the original tickers."""
        import gzip
        pool = _FakePool()
        upsert_args: list[str] = []

        async def _spy(_pool, bars_by_ticker, *, staging_run_id=None, delisted=False, source=None):
            del _pool, delisted, source
            for ticker in bars_by_ticker:
                upsert_args.append(ticker)
            return sum(len(v) for v in bars_by_ticker.values())

        monkeypatch.setattr(
            "tpcore.data.ingest_alpaca_bars.stage_then_promote_bars_batch", _spy,
        )

        # Replace write_archive with a clobbering version: writes a
        # file containing one row for ticker=CLOBBERED, returns its
        # ArchiveWriteResult.
        original_write = csv_archive.write_archive

        def _clobber_write(*args, **kwargs):
            # First do the real write so a file exists.
            res = original_write(*args, **kwargs)
            # Now clobber it with single-row content for CLOBBERED.
            replacement = (
                b"ticker,date,open,high,low,close,volume,vwap\n"
                b"CLOBBERED,2026-05-25,1.0,1.0,1.0,1.0,100,1.0\n"
            )
            assert isinstance(res.path, Path)
            res.path.write_bytes(gzip.compress(replacement, mtime=0))
            return res

        monkeypatch.setattr(archive_etl, "write_archive", _clobber_write)

        await archive_etl.archive_first_load_bars(
            pool,
            archive_rows=_sample_archive_rows(),  # AAPL, MSFT
            source="fmp_daily_bars", provider="fmp",
            date_range_start=date(2026, 5, 18),
            date_range_end=date(2026, 5, 25),
        )
        # The ETL read the clobbered file: upsert sees only CLOBBERED,
        # NOT the original AAPL/MSFT from the in-memory list. This is
        # the archive-as-substrate invariant.
        assert upsert_args == ["CLOBBERED"]
        assert "AAPL" not in upsert_args
        assert "MSFT" not in upsert_args
