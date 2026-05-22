"""Unit tests for the survivorship-backfill stages in ``scripts/ops.py``.

Two stages cover the survivorship-gap closure for ``platform.prices_daily``:

* ``historical_delisted_universe`` — one-shot operator backfill via FMP.
* ``daily_delisted_universe_check`` — nightly newly-delisted probe.

These tests verify:

1. Both stages are registered in ``KNOWN_STAGES`` (CLI resolves them).
2. Both stages are in ``_OFF_CYCLE_STAGES`` (NOT in the daily cadence).
3. The universe enumerator merges + dedupes the source candidates.
4. The per-ticker upsert SQL writes ``delisted=true`` + ``delisting_date``
   + ``source='fmp'`` — the survivorship audit's exact requirement.
5. Resume probes against ``application_log`` skip already-completed
   tickers.

Live FMP/DB integration is the operator's post-merge ``--stage
historical_delisted_universe`` invocation; this test stays hermetic.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tpcore.data import survivorship_backfill as sb

_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_survivorship", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_survivorship"] = ops
_spec.loader.exec_module(ops)

# pytest-xdist: ops-shadow tests pin to a single worker.
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ──────────────────────────────────────────────────────────────────────
# Fake asyncpg machinery — same pattern as test_stage_seed_monotone_snapshots.
# ──────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Stand-in for an asyncpg connection. Records every call and
    serves canned responses keyed on a queue per method."""

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
        self.executemany_calls = executemany_calls if executemany_calls is not None else []
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
        # Mimic asyncpg's "UPDATE N" / "INSERT 0 N" reply.
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
    """Stand-in for DBLogHandler — captures log calls so we can assert
    that the per-ticker progress events fire on every backfill call."""

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


def test_historical_delisted_universe_in_known_stages() -> None:
    assert "historical_delisted_universe" in ops.KNOWN_STAGES


def test_daily_delisted_universe_check_in_known_stages() -> None:
    assert "daily_delisted_universe_check" in ops.KNOWN_STAGES


def test_historical_delisted_universe_off_cycle() -> None:
    """Survivorship backfill is operator-on-demand — must NOT ride the
    daily ``--update`` cadence. A regression here would cause the daily
    job to attempt a 10-15min FMP fan-out every run."""
    assert "historical_delisted_universe" in ops._OFF_CYCLE_STAGES  # noqa: SLF001


def test_daily_delisted_universe_check_off_cycle() -> None:
    """Same off-cycle rule for the nightly probe today — operator opts
    in by running it manually. Future: promote into OPS_UPDATE_STAGES
    once the structural backfill is stable."""
    assert "daily_delisted_universe_check" in ops._OFF_CYCLE_STAGES  # noqa: SLF001


# ──────────────────────────────────────────────────────────────────────
# Known-delisting manifest — the operator-visible anchor list.
# ──────────────────────────────────────────────────────────────────────


def test_known_delistings_manifest_has_required_anchors() -> None:
    """The operator instruction list specifies these anchor delistings
    by name; the sentinel and the manifest must keep them in sync."""
    tickers = {t for t, _, _ in sb.KNOWN_DELISTINGS}
    required = {"SIVB", "FRC", "ATVI", "VMW", "SPLK", "WORK", "TWTR", "ABMD", "ANSS", "ALXN"}
    missing = required - tickers
    assert not missing, f"KNOWN_DELISTINGS is missing required anchors: {missing}"


def test_known_delistings_dates_are_iso_parseable() -> None:
    """Every manifest entry's date must be ISO-parseable so the resume
    + sentinel paths can date-compare against the FMP final-bar date."""
    for ticker, hint_iso, _note in sb.KNOWN_DELISTINGS:
        date.fromisoformat(hint_iso)  # raises ValueError on bad input
        assert ticker.isupper(), f"ticker {ticker!r} must be uppercase"


# ──────────────────────────────────────────────────────────────────────
# Universe enumeration — corpus + manifest + fixture dedup.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enumerate_dedups_across_sources() -> None:
    """Multiple sources can surface the same ticker; the enumerator
    must return one record per ticker, with the first source winning."""
    # Corpus marker lists SIVB; fixture lists SIVB and SIVBQ; manifest
    # also includes SIVB. Expect one SIVB record (source='corpus_marker')
    # and one SIVBQ record (source='fixture').
    conn = _FakeConn(
        fetch_responses=[
            # corpus_markers query
            [{"ticker": "SIVB"}, {"ticker": "WORK"}],
            # corpus_orphans query
            [{"ticker": "ENRN"}],  # historical orphan
        ],
    )
    pool = _FakePool(conn)
    out = await sb.enumerate_delisted_universe(pool, probe_fmp=False)
    by_ticker = {c.ticker: c.source for c in out}
    # Corpus-marker SIVB wins over the manifest's SIVB.
    assert by_ticker.get("SIVB") == "corpus_marker"
    # Corpus-orphan ENRN survives (not in manifest or fixture).
    assert "ENRN" in by_ticker
    # Manifest-only tickers (e.g. SPLK) make it in.
    assert "SPLK" in by_ticker
    assert by_ticker["SPLK"] == "known_manifest"


@pytest.mark.asyncio
async def test_enumerate_skips_garbage_tickers() -> None:
    """Defensive: empty / overlong tickers from FMP probe responses
    must be filtered out before they hit the backfill loop."""
    conn = _FakeConn(
        fetch_responses=[
            [{"ticker": ""}, {"ticker": "TOOLONGNAME"}, {"ticker": "AAPL"}],
            [],
        ],
    )
    pool = _FakePool(conn)
    out = await sb.enumerate_delisted_universe(pool, probe_fmp=False)
    tickers = {c.ticker for c in out}
    assert "" not in tickers
    assert "TOOLONGNAME" not in tickers
    assert "AAPL" in tickers


# ──────────────────────────────────────────────────────────────────────
# Upsert SQL — the survivorship contract.
# ──────────────────────────────────────────────────────────────────────


def test_upsert_sql_writes_delisted_true() -> None:
    """The audit's core requirement: every survivorship backfill row
    MUST carry delisted=true. A regression that drops the literal
    would silently re-introduce the bias the audit caught."""
    sql = sb._upsert_sql()  # noqa: SLF001
    assert "delisted = true" in sql.lower() or "delisted=true" in sql.lower() \
        or "true, $9" in sql  # value-position literal in VALUES
    # Confirm the VALUES literal binds delisted=true unconditionally.
    assert "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, $9, 'fmp')" in sql


def test_upsert_sql_sets_source_fmp() -> None:
    """Provenance audit: survivorship rows must be tagged ``source='fmp'``
    so a later corpus audit can attribute the bars correctly."""
    sql = sb._upsert_sql()  # noqa: SLF001
    assert "'fmp'" in sql


def test_upsert_sql_idempotent_on_conflict() -> None:
    """Re-running the backfill must not duplicate rows — the upsert
    MUST be an ON CONFLICT (ticker, date) DO UPDATE."""
    sql = sb._upsert_sql()  # noqa: SLF001
    assert "ON CONFLICT (ticker, date) DO UPDATE" in sql


# ──────────────────────────────────────────────────────────────────────
# Physical-truth gate — the OHLC sanity bar in delisted bars too.
# ──────────────────────────────────────────────────────────────────────


def test_physical_truth_rejects_negative_close() -> None:
    """A negative close MUST be dropped — corrupt FMP rows cannot be
    allowed to land in the corpus, the same rule as the Alpaca path."""
    bars = [{
        "t": "2020-01-01T00:00:00Z",
        "o": 10.0, "h": 11.0, "l": 9.0, "c": -1.0, "v": 1_000_000,
    }]
    rows = sb._physical_truth_rows("FOO", bars, date(2020, 1, 2))  # noqa: SLF001
    assert rows == []


def test_physical_truth_rejects_inconsistent_ohlc() -> None:
    """high < max(open, close) is impossible — drop it."""
    bars = [{
        "t": "2020-01-01T00:00:00Z",
        "o": 50.0, "h": 10.0, "l": 5.0, "c": 45.0, "v": 1_000_000,
    }]
    rows = sb._physical_truth_rows("FOO", bars, date(2020, 1, 2))  # noqa: SLF001
    assert rows == []


def test_physical_truth_keeps_good_bars() -> None:
    """Sanity: a well-formed bar makes it through."""
    bars = [{
        "t": "2020-01-01T00:00:00Z",
        "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1_000_000,
    }]
    rows = sb._physical_truth_rows("FOO", bars, date(2020, 1, 2))  # noqa: SLF001
    assert len(rows) == 1
    row = rows[0]
    # (ticker, session_date, o, h, l, c, v, adjusted_close, delisting_date)
    assert row[0] == "FOO"
    assert row[1] == date(2020, 1, 1)
    assert row[-1] == date(2020, 1, 2)  # delisting_date


def test_physical_truth_rejects_future_dates() -> None:
    """A bar timestamped in the future cannot be real — drop it."""
    future = datetime.now(UTC).replace(year=datetime.now(UTC).year + 5).date()
    bars = [{
        "t": f"{future.isoformat()}T00:00:00Z",
        "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1_000_000,
    }]
    rows = sb._physical_truth_rows("FOO", bars, date(2020, 1, 2))  # noqa: SLF001
    assert rows == []


# ──────────────────────────────────────────────────────────────────────
# Resume probe — read application_log + skip done tickers.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_completed_tickers_reads_progress_events() -> None:
    """The resume probe queries application_log for the canonical
    progress event type. A mismatch on the event type would silently
    re-fetch every ticker each run."""
    conn = _FakeConn(
        fetch_responses=[[{"ticker": "SIVB"}, {"ticker": "FRC"}, {"ticker": None}]],
    )
    pool = _FakePool(conn)
    done = await sb.already_completed_tickers(pool)
    assert done == {"SIVB", "FRC"}
    # Confirm the query targeted the right event type.
    assert conn.fetch_calls
    sql, args = conn.fetch_calls[0]
    assert sb.PROGRESS_EVENT_TYPE in args


# ──────────────────────────────────────────────────────────────────────
# mark_delisted — the nightly stage's write path.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_delisted_writes_only_when_undelisted() -> None:
    """The UPDATE must be guarded by ``delisted=false OR
    delisting_date IS NULL`` so a re-run never overwrites a known
    delisting date the operator manually corrected."""
    conn = _FakeConn()
    pool = _FakePool(conn)
    await sb.mark_delisted(pool, "FOO", date(2024, 1, 1))
    assert conn.execute_calls
    sql = conn.execute_calls[0]
    assert "delisted = true" in sql.lower() or "delisted=true" in sql.lower()
    assert "delisted = false OR delisting_date IS NULL" in sql


# ──────────────────────────────────────────────────────────────────────
# Progress events — the stream-long-running-output contract.
# ──────────────────────────────────────────────────────────────────────


def test_progress_event_type_constant_is_stable() -> None:
    """Renaming PROGRESS_EVENT_TYPE without coordinating with operator
    dashboards/queries would silently break resumability. Pin the
    literal so a rename is a deliberate, reviewed change."""
    assert sb.PROGRESS_EVENT_TYPE == "SURVIVORSHIP_BACKFILL_TICKER_DONE"
