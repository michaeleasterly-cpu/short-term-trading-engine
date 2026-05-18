"""End-to-end proof that ``handle_fundamentals_refresh`` writes the
``fmp_fundamentals`` CSV archive (#249 — closes the TODO L94
"presence unproven" gap).

The handler constructs ``FMPFundamentalsAdapter`` and
``FundamentalsCache`` internally, so the seams are monkeypatched at
their source modules. A fake adapter (async context manager) + a fake
cache returning a known ``backfill_all`` result + a fake pool whose
``conn.fetch`` serves the rows the archive SELECT pulls let the handler
run end-to-end with NO network and NO real DB. ``TP_DATA_DIR`` is
pointed at ``tmp_path`` so the archive is written under tmp and the
real repo ``data/`` dir is never touched.

NOTE (STOP/report, see PR description): against the *current* handler
this test fails — ``handlers.py:64`` unpacks ``backfill_all()`` as a
3-tuple while ``FundamentalsCache.backfill_all`` returns a 4-tuple
``(rows, no_data, failures, skipped)`` (the 2026-05-13 resumable-refresh
change updated ``scripts/ops.py:667`` but NOT this handler). The
``ValueError`` raises *before* ``write_archive`` is reached, so the
archive code path is currently dead. This test pins the correct
end-to-end behaviour and will pass once the handler unpack is fixed to
match the cache contract.
"""
from __future__ import annotations

import importlib

import pytest

from tpcore.ingestion import csv_archive

handlers = importlib.import_module("tpcore.ingestion.handlers")


# Two known fundamentals records the archive SELECT will "pull" from
# the DB. Values are strings (the handler stringifies via str(v)).
_KNOWN_ROWS = [
    {
        "ticker": "AAPL", "filing_date": "2026-02-01",
        "period_end_date": "2025-12-31", "period_label": "Q4",
        "net_income": "1000", "fcf": "900", "operating_cash_flow": "1100",
        "capex": "-200", "revenue": "5000", "total_assets": "9000",
        "total_liabilities": "4000", "current_assets": "3000",
        "current_liabilities": "2000", "receivables": "500",
        "cash_and_equivalents": "1500", "shares_outstanding": "1000000000",
        "pb": "12.3", "de": "0.4", "recorded_at": "2026-02-02T00:00:00Z",
    },
    {
        "ticker": "MSFT", "filing_date": "2026-02-03",
        "period_end_date": "2025-12-31", "period_label": "Q4",
        "net_income": "2000", "fcf": "1800", "operating_cash_flow": "2200",
        "capex": "-400", "revenue": "8000", "total_assets": "12000",
        "total_liabilities": "5000", "current_assets": "4000",
        "current_liabilities": "2500", "receivables": "700",
        "cash_and_equivalents": "2500", "shares_outstanding": "7000000000",
        "pb": "9.8", "de": "0.3", "recorded_at": "2026-02-04T00:00:00Z",
    },
]


class _FakeConn:
    async def fetch(self, sql: str, *args):
        # The handler's only conn.fetch is the fundamentals_quarterly
        # archive SELECT — serve the known rows.
        assert "platform.fundamentals_quarterly" in sql
        return list(_KNOWN_ROWS)


class _AcquireCM:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _FakePool:
    def __init__(self): self.conn = _FakeConn()
    def acquire(self): return _AcquireCM(self.conn)


class _FakeAdapter:
    """Async context-manager adapter the handler `async with`-enters."""

    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    async def aclose(self): return None


class _FakeCache:
    """Stands in for FundamentalsCache. ``backfill_all`` returns the
    real 4-tuple contract ``(rows, no_data, failures, skipped)``."""

    def __init__(self, pool, adapter=None) -> None:
        self.pool = pool
        self.adapter = adapter

    async def backfill_all(self, *a, **k):
        # rows ingested, no_data list, failures list, skipped count.
        return (len(_KNOWN_ROWS), [], [], 0)


@pytest.fixture()
def tmp_archive(tmp_path, monkeypatch):
    # TP_DATA_DIR seam → archive root is tmp_path; real data/ untouched.
    monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))
    assert csv_archive.repo_data_dir() == tmp_path
    # Patch the seams at their source modules (handler imports them
    # lazily from these modules inside the function body).
    monkeypatch.setattr("tpcore.fmp.FMPFundamentalsAdapter",
                         lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr("tpcore.fundamentals.cache.FundamentalsCache",
                         _FakeCache)
    return tmp_path


@pytest.mark.xfail(
    reason=(
        "DEFECT (surfaced by #249, NOT fixed under a test task): "
        "tpcore/ingestion/handlers.py:64 unpacks `await cache.backfill_all()` "
        "as a 3-tuple, but FundamentalsCache.backfill_all returns a 4-tuple "
        "(rows, no_data, failures, skipped) since the 2026-05-13 "
        "resumable-refresh change (scripts/ops.py:667 was updated, this "
        "handler was not). The ValueError raises BEFORE write_archive is "
        "reached, so the fmp_fundamentals archive code path is dead in "
        "production. xfail(strict) pins the gap and auto-fails (alerts) the "
        "moment the handler unpack is corrected to match the cache contract "
        "— at which point this becomes the real end-to-end archive proof."
    ),
    strict=True,
    raises=ValueError,
)
async def test_handle_fundamentals_writes_csv_archive_end_to_end(tmp_archive):
    pool = _FakePool()

    result = await handlers.handle_fundamentals_refresh(pool, {"universe": "active"})

    # Handler returns rows_ingested.
    assert result == len(_KNOWN_ROWS)

    # The archive file must exist under <tmp>/fmp_fundamentals_archive/.
    archive_dir = tmp_archive / "fmp_fundamentals_archive"
    assert archive_dir.is_dir(), "archive dir not created"
    gz_files = sorted(archive_dir.glob("fmp_fundamentals_*.csv.gz"))
    assert len(gz_files) == 1, f"expected one archive, got {gz_files}"

    gz = gz_files[0]
    assert gz.stat().st_size > 0, "archive file is empty"

    # Row count must match the payload (validator passes: every known
    # row has ticker + period_end_date).
    assert csv_archive.count_archive_rows(gz) == len(_KNOWN_ROWS)
