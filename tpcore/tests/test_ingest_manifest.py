"""Tests for ``tpcore.ingestion.manifest`` — ``platform.ingest_manifest`` writer.

P1 trust-audit (2026-05-25): the manifest table existed schema-only.
These tests pin the writer's contract — create_archived_row,
mark_loaded, mark_failed, compute_sha256 — without needing a live DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tpcore.ingestion import manifest


class _Conn:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args: object) -> Any:
        self.fetchval_calls.append((sql, args))
        return uuid4()

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.conn = _Conn()

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


# ─────────────────────────────────────────────────────────────────────
# compute_sha256
# ─────────────────────────────────────────────────────────────────────


class TestComputeSha256:
    def test_known_digest(self, tmp_path: Path) -> None:
        p = tmp_path / "x.bin"
        p.write_bytes(b"hello world\n")
        # `printf 'hello world\n' | sha256sum`
        assert manifest.compute_sha256(p) == (
            "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"
        )

    def test_handles_large_file_via_chunks(self, tmp_path: Path) -> None:
        # 256 KiB — exceeds the 64 KiB chunk size; same digest regardless.
        p = tmp_path / "big.bin"
        p.write_bytes(b"a" * (256 * 1024))
        h1 = manifest.compute_sha256(p)
        # Sanity: same content = same digest (idempotence over chunking).
        p.write_bytes(b"a" * (256 * 1024))
        assert manifest.compute_sha256(p) == h1

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            manifest.compute_sha256(tmp_path / "nope.csv")


# ─────────────────────────────────────────────────────────────────────
# create_archived_row
# ─────────────────────────────────────────────────────────────────────


class TestCreateArchivedRow:
    @pytest.mark.asyncio
    async def test_inserts_with_status_archived(self) -> None:
        pool = _Pool()
        mid = await manifest.create_archived_row(
            pool,
            source="fmp_daily_bars",
            provider="fmp",
            archive_path="/tmp/fmp_daily_bars_20260525T000000Z.csv.gz",
            archived_row_count=12345,
            checksum="deadbeef" * 8,
            date_range_start=datetime(2026, 5, 18, tzinfo=UTC),
            date_range_end=datetime(2026, 5, 25, tzinfo=UTC),
        )
        assert mid is not None
        assert len(pool.conn.fetchval_calls) == 1
        sql, args = pool.conn.fetchval_calls[0]
        assert "platform.ingest_manifest" in sql
        assert "INSERT INTO" in sql
        # Field order matches _INSERT_SQL parameter binding.
        assert args[0] == "fmp_daily_bars"            # source
        assert args[1] == "fmp"                       # provider
        assert args[3] == "/tmp/fmp_daily_bars_20260525T000000Z.csv.gz"
        assert args[5] == 12345                       # actual_rows
        assert args[6] == "archived"                  # status
        assert args[7] == "deadbeef" * 8              # checksum

    @pytest.mark.asyncio
    async def test_date_range_normalised_to_date(self) -> None:
        """date_range_start/end accepted as datetime, persisted as date."""
        pool = _Pool()
        await manifest.create_archived_row(
            pool, source="x", provider="y", archive_path="/tmp/x",
            archived_row_count=1, checksum="abc",
            date_range_start=datetime(2024, 1, 5, 12, 0, tzinfo=UTC),
            date_range_end=datetime(2024, 1, 7, 13, 0, tzinfo=UTC),
        )
        _, args = pool.conn.fetchval_calls[0]
        from datetime import date as _date
        assert args[8] == _date(2024, 1, 5)
        assert args[9] == _date(2024, 1, 7)


# ─────────────────────────────────────────────────────────────────────
# mark_loaded / mark_failed
# ─────────────────────────────────────────────────────────────────────


class TestMarkLoaded:
    @pytest.mark.asyncio
    async def test_updates_to_loaded(self) -> None:
        pool = _Pool()
        mid = uuid4()
        await manifest.mark_loaded(pool, mid, actual_rows=98765)
        assert len(pool.conn.execute_calls) == 1
        sql, args = pool.conn.execute_calls[0]
        assert "UPDATE platform.ingest_manifest" in sql
        assert args[0] == mid
        assert args[1] == "loaded"
        assert args[2] == 98765


class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_updates_to_failed(self) -> None:
        pool = _Pool()
        mid = uuid4()
        await manifest.mark_failed(
            pool, mid, error_summary="ConnectionRefused: 5/5 hosts",
            actual_rows=42,
        )
        sql, args = pool.conn.execute_calls[0]
        assert "UPDATE platform.ingest_manifest" in sql
        assert args[0] == mid
        assert args[1] == "failed"
        assert args[2] == 42
        assert "ConnectionRefused" in args[3]

    @pytest.mark.asyncio
    async def test_error_summary_truncated(self) -> None:
        pool = _Pool()
        long_err = "x" * 5000
        await manifest.mark_failed(pool, uuid4(), error_summary=long_err)
        _, args = pool.conn.execute_calls[0]
        assert len(args[3]) <= 2000


# ─────────────────────────────────────────────────────────────────────
# Status enum coverage
# ─────────────────────────────────────────────────────────────────────


def test_known_statuses_exhaustive() -> None:
    """KNOWN_STATUSES is the SoT for what producers can write — if a
    new status is added but not in this set, the audit query
    ``WHERE status NOT IN (KNOWN_STATUSES)`` would catch the drift."""
    assert manifest.KNOWN_STATUSES == frozenset({"archived", "loaded", "failed"})
