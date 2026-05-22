"""Unit tests for the insider-filings backfill stages in ``scripts/ops.py``.

Two stages cover daily-granularity insider-filings ingestion:

* ``historical_insider_sentiment_daily`` — one-shot operator backfill via FMP.
* ``daily_insider_sentiment_delta`` — IN OPS_UPDATE_STAGES (nightly cadence).

These tests verify:

1. Both stages are registered in ``KNOWN_STAGES`` (CLI resolves them).
2. The historical stage is in ``_OFF_CYCLE_STAGES`` (operator-on-demand).
3. The daily-delta stage is NOT in ``_OFF_CYCLE_STAGES`` (rides --update).
4. The daily-delta stage is in ``FEED_STAGE`` AND has a FeedProfile.
5. Per-symbol upsert SQL writes to ``platform.insider_filings`` with the
   full PK dedupe shape.
6. Resume probe against ``application_log`` skips already-completed symbols.
7. The ``_physical_truth_rows`` gate drops obvious bad rows.

Live FMP/DB integration is the operator's post-merge one-shot run; this
test stays hermetic via fakes.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tpcore.data import insider_backfill as ib

_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_insider_filings", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_insider_filings"] = ops
_spec.loader.exec_module(ops)

# pytest-xdist: ops-shadow tests pin to a single worker.
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ──────────────────────────────────────────────────────────────────────
# Fake asyncpg machinery — same pattern as
# test_stage_historical_delisted_universe.py
# ──────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(
        self,
        *,
        fetch_responses: list | None = None,
        fetchval_responses: list | None = None,
        executemany_calls: list | None = None,
    ) -> None:
        self.fetch_calls: list[tuple] = []
        self.fetchval_calls: list[tuple] = []
        self.execute_calls: list[str] = []
        self.executemany_calls = (
            executemany_calls if executemany_calls is not None else []
        )
        self._fetch_q = iter(fetch_responses or [])
        self._fetchval_q = iter(fetchval_responses or [])

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        try:
            return next(self._fetch_q)
        except StopIteration:
            return []

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        try:
            return next(self._fetchval_q)
        except StopIteration:
            return None

    async def execute(self, sql: str, *args) -> str:
        self.execute_calls.append(sql)
        return "UPDATE 1"

    async def executemany(self, sql: str, rows) -> None:
        self.executemany_calls.append((sql, list(rows)))


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _FakeDBLog:
    def __init__(self) -> None:
        self.logged: list[dict] = []

    async def log(self, event_type, message, severity="INFO", data=None):
        self.logged.append({
            "event_type": event_type,
            "message": message,
            "severity": severity,
            "data": data or {},
        })


# ──────────────────────────────────────────────────────────────────────
# Stage-registration invariants — the CLI must resolve both stages.
# ──────────────────────────────────────────────────────────────────────


def test_historical_insider_sentiment_daily_in_known_stages() -> None:
    assert "historical_insider_sentiment_daily" in ops.KNOWN_STAGES


def test_daily_insider_sentiment_delta_in_known_stages() -> None:
    assert "daily_insider_sentiment_delta" in ops.KNOWN_STAGES


def test_historical_stage_off_cycle() -> None:
    """One-shot backfill is operator-on-demand — must NOT ride --update.
    A regression here would burn ~30-90 min of FMP fan-out every cycle."""
    assert (
        "historical_insider_sentiment_daily" in ops._OFF_CYCLE_STAGES  # noqa: SLF001
    )


def test_daily_delta_stage_is_in_cycle() -> None:
    """The DELTA stage rides --update — it MUST NOT be off-cycle. The
    operator directive: 'make sure automation works so we aren't
    backfilling all the damn time.'"""
    assert (
        "daily_insider_sentiment_delta" not in ops._OFF_CYCLE_STAGES  # noqa: SLF001
    )


def test_daily_delta_registered_in_feed_stage_map() -> None:
    """The dispatcher needs FEED_STAGE['insider_sentiment_daily'] →
    'daily_insider_sentiment_delta' so the existing data-operations
    daemon picks it up at 21:30 UTC."""
    from tpcore.feeds.dispatcher import FEED_STAGE

    assert FEED_STAGE["insider_sentiment_daily"] == "daily_insider_sentiment_delta"


def test_feed_profile_present() -> None:
    """The dispatcher rejects feeds without a profile — the profile is
    the single source of truth for cadence/trigger."""
    from tpcore.feeds.profile import FEED_PROFILES, FeedTrigger

    p = FEED_PROFILES.get("insider_sentiment_daily")
    assert p is not None
    assert p.trigger == FeedTrigger.CONTINUOUS
    assert p.cadence_days == 1


def test_provider_binding_present() -> None:
    """Every FeedProfile must have a ProviderBinding (drift test
    enforces both directions)."""
    from tpcore.providers import active_provider

    b = active_provider("insider_sentiment_daily")
    assert b is not None
    assert b.provider == "fmp"


# ──────────────────────────────────────────────────────────────────────
# _physical_truth_rows — the row-level gate
# ──────────────────────────────────────────────────────────────────────


_GOOD_ROW: dict = {
    "symbol": "AAPL",
    "filingDate": "2024-06-15",
    "transactionDate": "2024-06-13",
    "reportingCik": "0001214128",
    "companyCik": "0000320193",
    "transactionType": "S-Sale",
    "securitiesOwned": 3920049,
    "reportingName": "LEVINSON ARTHUR D",
    "typeOfOwner": "director",
    "acquisitionOrDisposition": "D",
    "directOrIndirect": "D",
    "formType": "4",
    "securitiesTransacted": 149527,
    "price": 284.57,
    "securityName": "Common Stock",
    "url": "https://www.sec.gov/Archives/edgar/data/320193/...",
}


def test_physical_truth_rows_passes_good_row() -> None:
    rows = ib._physical_truth_rows("AAPL", [_GOOD_ROW])  # noqa: SLF001
    assert len(rows) == 1
    assert rows[0][0] == "AAPL"
    assert rows[0][2] == date(2024, 6, 13)  # transaction_date
    assert rows[0][11] == 149527.0  # shares
    assert rows[0][12] == 284.57  # price


def test_physical_truth_rows_drops_missing_reporting_cik() -> None:
    bad = dict(_GOOD_ROW, reportingCik="")
    assert ib._physical_truth_rows("AAPL", [bad]) == []  # noqa: SLF001


def test_physical_truth_rows_drops_negative_shares() -> None:
    bad = dict(_GOOD_ROW, securitiesTransacted=-10)
    assert ib._physical_truth_rows("AAPL", [bad]) == []  # noqa: SLF001


def test_physical_truth_rows_drops_future_dates() -> None:
    future = (datetime.now(UTC).date().replace(year=datetime.now(UTC).year + 1)).isoformat()
    bad = dict(_GOOD_ROW, transactionDate=future)
    assert ib._physical_truth_rows("AAPL", [bad]) == []  # noqa: SLF001


def test_physical_truth_rows_drops_missing_tx_type() -> None:
    bad = dict(_GOOD_ROW, transactionType="")
    assert ib._physical_truth_rows("AAPL", [bad]) == []  # noqa: SLF001


# ──────────────────────────────────────────────────────────────────────
# Upsert SQL — must target insider_filings + ON CONFLICT DO NOTHING
# ──────────────────────────────────────────────────────────────────────


def test_upsert_sql_targets_insider_filings_table() -> None:
    sql = ib._upsert_sql()  # noqa: SLF001
    assert "INSERT INTO platform.insider_filings" in sql
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql


def test_upsert_sql_writes_all_observable_columns() -> None:
    """Regression guard: if a column is dropped from the table or the
    SQL drifts, the engine downstream silently loses data. Pin every
    column the table holds."""
    sql = ib._upsert_sql()  # noqa: SLF001
    for col in (
        "symbol", "filing_date", "transaction_date", "reporting_cik",
        "company_cik", "transaction_type", "reporting_name",
        "type_of_owner", "acquisition_or_disposition", "direct_or_indirect",
        "form_type", "securities_transacted", "price", "securities_owned",
        "security_name", "url",
    ):
        assert col in sql, f"upsert SQL missing column {col}"


# ──────────────────────────────────────────────────────────────────────
# Resume probe — skips already-completed symbols
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_completed_symbols_returns_set() -> None:
    conn = _FakeConn(fetch_responses=[
        [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": None}],
    ])
    pool = _FakePool(conn)
    done = await ib.already_completed_symbols(pool)
    assert done == {"AAPL", "MSFT"}
    # Query must filter on the canonical progress event-type.
    assert ib.PROGRESS_EVENT_TYPE in conn.fetch_calls[0][1]


@pytest.mark.asyncio
async def test_already_completed_symbols_empty_when_no_log_rows() -> None:
    conn = _FakeConn(fetch_responses=[[]])
    pool = _FakePool(conn)
    assert await ib.already_completed_symbols(pool) == set()


# ──────────────────────────────────────────────────────────────────────
# Universe enumeration — T1+T2 stocks + delisted
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enumerate_insider_universe_merges_active_and_delisted() -> None:
    conn = _FakeConn(fetch_responses=[
        # First fetch: active T1+T2 stocks
        [{"ticker": "AAPL"}, {"ticker": "MSFT"}],
        # Second fetch: delisted tickers in prices_daily
        [{"ticker": "TWTR"}, {"ticker": "ATVI"}],
    ])
    pool = _FakePool(conn)
    universe = await ib.enumerate_insider_universe(pool)
    assert set(universe) == {"AAPL", "MSFT", "TWTR", "ATVI"}
    # Should be sorted (deterministic backfill order).
    assert universe == sorted(universe)


# ──────────────────────────────────────────────────────────────────────
# Backfill universe — resume + failure isolation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_universe_skips_already_done(monkeypatch) -> None:
    """Resume probe must skip symbols already in application_log."""
    conn = _FakeConn(fetch_responses=[
        [{"symbol": "AAPL"}],  # already done
    ])
    pool = _FakePool(conn)
    db_log = _FakeDBLog()
    monkeypatch.setenv("FMP_API_KEY", "test_key")

    async def _fake_one(pool, client, db_log, symbol, **kw):
        raise AssertionError(f"should not have backfilled {symbol}")

    monkeypatch.setattr(ib, "backfill_one_symbol", _fake_one)
    result = await ib.backfill_universe(
        pool, db_log, ["AAPL"], resume=True,
    )
    assert result["symbols_attempted"] == 0
    assert result["resumed_skipped"] == 1


@pytest.mark.asyncio
async def test_backfill_universe_isolates_per_symbol_failures(monkeypatch) -> None:
    """A single bad symbol must NOT abort the run — operator's stream-
    long-running-output rule."""
    conn = _FakeConn(fetch_responses=[
        [],  # resume probe: nothing done
    ])
    pool = _FakePool(conn)
    db_log = _FakeDBLog()
    monkeypatch.setenv("FMP_API_KEY", "test_key")

    async def _fake_one(pool, client, db_log, symbol, **kw):
        if symbol == "BAD":
            raise ValueError("FMP returned junk for BAD")
        return 5

    monkeypatch.setattr(ib, "backfill_one_symbol", _fake_one)
    result = await ib.backfill_universe(
        pool, db_log, ["AAPL", "BAD", "MSFT"], resume=True,
    )
    assert result["symbols_succeeded"] == 2
    assert result["symbols_failed"] == 1
    assert result["rows_written"] == 10  # 2 × 5
    assert any("BAD" in f for f in result["failures_sample"])
