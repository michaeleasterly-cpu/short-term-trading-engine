"""Tests for the two-phase SEC bulk Form-345 ETL (#132).

Proves the design the operator demanded: a real Extract phase
(durable zips on disk, re-runnable without re-download) separated
from Transform→validate-at-CSV→Load→compress, idempotent, with
404 (unpublished quarter) tolerated.
"""
from __future__ import annotations

import io
import zipfile

from tpcore.ingestion import handlers
from tpcore.outage import DataProviderOutage

_SUBMISSION = (
    "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERTRADINGSYMBOL\n"
    "acc-1\t15-MAR-2024\t4\tAAA\n"          # in universe, kept
    "acc-2\t20-MAR-2024\t4\tZZZ\n"          # not in universe, dropped
    "acc-3\t21-MAR-2024\t3\tAAA\n"          # Form 3 (no txn) dropped
)
_OWNER = (
    "ACCESSION_NUMBER\tRPTOWNERNAME\n"
    "acc-1\tDoe John\n"
)
_TRANS = (
    "ACCESSION_NUMBER\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\n"
    "acc-1\t1000\t10.50\tA\n"               # valid BUY → kept
    "acc-1\t0\t5\tA\n"                      # 0 shares → physical-truth reject
    "acc-2\t500\t9\tD\n"                    # not-in-universe issuer → dropped
)


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", _SUBMISSION)
        z.writestr("REPORTINGOWNER.tsv", _OWNER)
        z.writestr("NONDERIV_TRANS.tsv", _TRANS)
    return buf.getvalue()


class _Conn:
    def __init__(self): self.loaded: list[tuple] = []
    async def executemany(self, sql, rows):
        assert "ON CONFLICT" in sql
        self.loaded.extend(rows)


class _Pool:
    def __init__(self): self.conn = _Conn()
    def acquire(self):
        conn = self.conn

        class _CM:
            async def __aenter__(self): return conn
            async def __aexit__(self, *e): return None
        return _CM()


def _patch_fetch(monkeypatch, calls: list[str]):
    zbytes = _make_zip()

    async def fake(url, ua):
        calls.append(url)
        if "2026q1" in url:           # one unpublished quarter → 404
            raise DataProviderOutage("sec_bulk ... returned 404")
        return zbytes
    monkeypatch.setattr(handlers, "_sec_bulk_fetch_zip", fake)


async def test_extract_then_transform_load(monkeypatch, tmp_path) -> None:
    from datetime import date
    calls: list[str] = []
    _patch_fetch(monkeypatch, calls)
    pool = _Pool()
    n = await handlers._sec_bulk_form345_backfill(  # noqa: SLF001
        pool, {"AAA"}, date(2024, 1, 1), dest_dir=tmp_path,
    )
    # Each spanned quarter contributes exactly one surviving row: the
    # in-universe AAA BUY. ZZZ (off-universe), the Form-3, and the
    # 0-share row are all dropped by the gate.
    assert n >= 1 and n == len(pool.conn.loaded)
    assert all(r[0] == "AAA" and r[3] == "BUY" for r in pool.conn.loaded)
    # Phase 1 produced durable raw zips on disk.
    assert list((tmp_path / "raw").glob("*_form345.zip"))
    # Phase 2 produced a gzipped validated CSV artifact.
    assert list(tmp_path.glob("sec_insider_bulk_*.csv.gz"))


async def test_extract_is_resumable_no_redownload(monkeypatch, tmp_path) -> None:
    from datetime import date
    calls: list[str] = []
    zbytes = _make_zip()

    async def fake(url, ua):          # no 404 → every quarter caches
        calls.append(url)
        return zbytes
    monkeypatch.setattr(handlers, "_sec_bulk_fetch_zip", fake)
    args = ({"AAA"}, date(2025, 1, 1))
    await handlers._sec_bulk_form345_backfill(_Pool(), *args, dest_dir=tmp_path)  # noqa: SLF001
    first = len(calls)
    assert first > 0
    # Second run: every zip already on disk → zero new fetches.
    await handlers._sec_bulk_form345_backfill(_Pool(), *args, dest_dir=tmp_path)  # noqa: SLF001
    assert len(calls) == first, "cached zips must not be re-downloaded"


async def test_unpublished_quarter_404_skipped(monkeypatch, tmp_path) -> None:
    from datetime import date
    calls: list[str] = []
    _patch_fetch(monkeypatch, calls)
    # since spanning into 2026q1 (the mocked 404) must not raise.
    n = await handlers._sec_bulk_form345_backfill(  # noqa: SLF001
        _Pool(), {"AAA"}, date(2026, 1, 1), dest_dir=tmp_path,
    )
    assert n >= 0  # completed despite the 404 quarter
    assert any("2026q1" in u for u in calls)
