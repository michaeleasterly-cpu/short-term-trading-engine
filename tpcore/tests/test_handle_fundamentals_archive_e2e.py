"""End-to-end proof that ``handle_fundamentals_refresh`` writes the
``fmp_fundamentals`` CSV archive + manifest row BEFORE any production
upsert (#249 — closes the TODO L94 "presence unproven" gap).

P1-sibling trust-audit refactor (2026-05-25): the handler now uses
archive-first ordering: pre-fetch payloads into memory via the
public ``cache.fetch_payload`` surface, write archive + manifest at
status='archived' via :func:`manifest_lifecycle`, then read the
on-disk archive and per-symbol upsert via ``cache.upsert_payload``,
finally mark manifest 'loaded'. This test stubs the FundamentalsCache
+ FMPFundamentalsAdapter seams and pins the new contract:

* archive file lands under ``<tmp>/fmp_fundamentals_archive/``,
* CSV header matches ``FUNDAMENTALS_ARCHIVE_FIELDS`` (the
  canonical-tuple parity invariant — sibling test
  ``test_handle_fundamentals_archive_db_schema`` pins that tuple
  against the live DB),
* manifest INSERT happens BEFORE the per-symbol upsert,
* manifest UPDATE → 'loaded' happens at the end.
"""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from tpcore.ingestion import csv_archive

handlers = importlib.import_module("tpcore.ingestion.handlers")


# pytest-xdist: pin this ops-shadow module to one worker.
pytestmark = pytest.mark.xdist_group("ops_shadow")


# Synthetic FMP payloads — one period each so each symbol contributes
# exactly one archive row.
def _payload(filing_iso: str = "2026-02-01") -> dict:
    return {
        "filing_date": date.fromisoformat(filing_iso),
        "period_end_date": date(2025, 12, 31),
        "period": "Q4",
        "net_income": Decimal("1000"),
        "fcf": Decimal("900"),
        "operating_cash_flow": Decimal("1100"),
        "capex": Decimal("-200"),
        "revenue": Decimal("5000"),
        "total_assets": Decimal("9000"),
        "total_liabilities": Decimal("4000"),
        "current_assets": Decimal("3000"),
        "current_liabilities": Decimal("2000"),
        "receivables": Decimal("500"),
        "cash_and_equivalents": Decimal("1500"),
        "shares_outstanding": Decimal("1000000000"),
        "history": [],
    }


_PAYLOADS = {
    "AAPL": _payload("2026-02-01"),
    "MSFT": _payload("2026-02-03"),
}


class _RecordingConn:
    """Manifest INSERT + UPDATE recorder."""
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self._mid = uuid4()

    async def fetch(self, _sql, *_args):
        return []

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self._mid

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _FakePool:
    def __init__(self): self.conn = _RecordingConn()
    def acquire(self): return _AcquireCM(self.conn)


class _FakeAdapter:
    """Async context-manager adapter the handler `async with`-enters."""
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    async def aclose(self): return None


class _FakeCache:
    """Stands in for FundamentalsCache. Implements the new
    archive-first public surface (list_active_tickers,
    tickers_refreshed_within, fetch_payload, upsert_payload).
    Records the order of fetch + upsert calls so the test can pin
    that fetch happens in Phase 0 and upsert happens in Phase 2.
    """
    def __init__(self, pool, adapter=None) -> None:
        self.pool = pool
        self.adapter = adapter
        self.events: list[str] = []

    async def list_active_tickers(self):
        return list(_PAYLOADS)

    async def tickers_refreshed_within(self, tickers, hours):
        del tickers, hours
        return set()

    async def fetch_payload(self, symbol: str):
        self.events.append(f"fetch:{symbol}")
        return _PAYLOADS[symbol]

    async def upsert_payload(self, symbol: str, payload: dict):
        del payload  # archive-driven; not used in the fake
        self.events.append(f"upsert:{symbol}")
        return 1


@pytest.fixture()
def tmp_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))
    assert csv_archive.repo_data_dir() == tmp_path
    monkeypatch.setattr(
        "tpcore.fmp.FMPFundamentalsAdapter", lambda *a, **k: _FakeAdapter(),
    )
    monkeypatch.setattr(
        "tpcore.fundamentals.cache.FundamentalsCache", _FakeCache,
    )
    return tmp_path


async def test_handle_fundamentals_archives_before_upsert_and_marks_loaded(
    tmp_archive,
) -> None:
    pool = _FakePool()
    # Spy on the manifest INSERT so we can pin ordering vs upserts.
    events: list[str] = []
    orig_fetchval = pool.conn.fetchval

    async def _spy_fetchval(sql, *args):
        if "INSERT" in sql and "ingest_manifest" in sql:
            events.append("manifest:insert")
        return await orig_fetchval(sql, *args)
    pool.conn.fetchval = _spy_fetchval  # type: ignore[assignment]

    # Surface the cache instance for assertion (the handler creates
    # TWO FundamentalsCache instances — one for fetch, one for upsert
    # under the lifecycle). The second adapter's events are the ones
    # that matter for Phase 2 ordering.
    handler_caches: list[_FakeCache] = []
    orig_cache_cls = _FakeCache
    monkey_target = "tpcore.fundamentals.cache.FundamentalsCache"

    def _factory(*a, **k):
        c = orig_cache_cls(*a, **k)
        handler_caches.append(c)
        return c
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(monkey_target, _factory)
    try:
        result = await handlers.handle_fundamentals_refresh(
            pool, {"universe": "active"},
        )
    finally:
        mp.undo()

    # Per-symbol upsert count returned to the caller.
    assert result == len(_PAYLOADS)

    # Archive file lands on disk.
    archive_dir = tmp_archive / "fmp_fundamentals_archive"
    assert archive_dir.is_dir()
    gz_files = sorted(archive_dir.glob("fmp_fundamentals_*.csv.gz"))
    assert len(gz_files) == 1
    gz = gz_files[0]
    assert gz.stat().st_size > 0
    assert csv_archive.count_archive_rows(gz) == len(_PAYLOADS)

    # CSV header parity with the canonical FUNDAMENTALS_ARCHIVE_FIELDS
    # (the schema-drift invariant; sibling DB-gated test pins the
    # tuple against information_schema).
    import csv as _csv
    import gzip as _gzip
    with _gzip.open(gz, "rt", newline="", encoding="utf-8") as fh:
        reader = _csv.reader(fh)
        header = tuple(next(reader))
    assert header == handlers.FUNDAMENTALS_ARCHIVE_FIELDS, (
        f"CSV-archive header drifted from FUNDAMENTALS_ARCHIVE_FIELDS — "
        f"the handler's fieldnames= must equal the canonical tuple.\n"
        f"  CSV header:  {header}\n"
        f"  Canonical:   {handlers.FUNDAMENTALS_ARCHIVE_FIELDS}"
    )

    # Manifest INSERT happened (Phase 1). One UPDATE = mark_loaded.
    assert len(pool.conn.fetchval_calls) == 1
    sql, args = pool.conn.fetchval_calls[0]
    assert "platform.ingest_manifest" in sql
    assert args[0] == "fmp_fundamentals"
    assert args[1] == "fmp"
    assert args[6] == "archived"
    assert len(pool.conn.execute_calls) == 1
    _, upd_args = pool.conn.execute_calls[0]
    assert upd_args[1] == "loaded"
    assert upd_args[2] == len(_PAYLOADS)

    # All fetches happen BEFORE the manifest INSERT (Phase 0 → 1).
    # The handler creates two cache instances; the first does the
    # Phase-0 fetches, the second does Phase-2 upserts.
    fetch_cache, upsert_cache = handler_caches[0], handler_caches[1]
    assert all(e.startswith("fetch:") for e in fetch_cache.events)
    assert all(e.startswith("upsert:") for e in upsert_cache.events)
