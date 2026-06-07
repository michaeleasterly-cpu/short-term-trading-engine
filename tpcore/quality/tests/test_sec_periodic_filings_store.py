"""Unit tests for ``tpcore.quality.sec_periodic_filings_store`` (2026-06-07).

Covers the PURE logic of the shared SEC-periodic-filings store:

  * the routed-form SoT parity vs the migration CHECK + the validator,
  * ``base_form`` amendment collapse,
  * the ``_pure_gap`` set-difference + anchored discriminator (the
    false-green guard),
  * ``compute_filing_gaps`` set-based dispatch over a fake connection,
  * ``write_periodic_filings`` chunked-INSERT column wiring over a fake
    connection (no live DB).
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from tpcore.quality.sec_periodic_filings_store import (
    ROUTED_ANNUAL_FORMS,
    ROUTED_FORMS,
    ROUTED_QUARTERLY_FORMS,
    FilingGapResult,
    _pure_gap,
    base_form,
    compute_filing_gaps,
    write_periodic_filings,
)

# ── Routed-form SoT parity ─────────────────────────────────────────────


def test_routed_forms_equal_migration_check_set() -> None:
    """ROUTED_FORMS MUST equal the migration 20260607_0200 CHECK set."""
    migration_check_set = frozenset({
        "10-Q", "10-K", "20-F", "40-F",
        "10-Q/A", "10-K/A", "20-F/A", "40-F/A",
    })
    assert ROUTED_FORMS == migration_check_set


def test_routed_forms_equal_validator_routing() -> None:
    """The store's base-form cadence routing MUST equal the validator's
    ``_QUARTERLY_FORMS`` / ``_ANNUAL_FORMS`` (after /A collapse)."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (  # noqa: E501
        _ANNUAL_FORMS,
        _QUARTERLY_FORMS,
    )

    store_q_base = {base_form(f) for f in ROUTED_QUARTERLY_FORMS}
    store_a_base = {base_form(f) for f in ROUTED_ANNUAL_FORMS}
    assert store_q_base == set(_QUARTERLY_FORMS)
    assert store_a_base == set(_ANNUAL_FORMS)


def test_quarterly_and_annual_disjoint_and_union() -> None:
    assert ROUTED_QUARTERLY_FORMS & ROUTED_ANNUAL_FORMS == frozenset()
    assert ROUTED_QUARTERLY_FORMS | ROUTED_ANNUAL_FORMS == ROUTED_FORMS


def test_base_form() -> None:
    assert base_form("10-Q/A") == "10-Q"
    assert base_form("20-F/A") == "20-F"
    assert base_form("10-K") == "10-K"
    assert base_form("40-F") == "40-F"


# ── _pure_gap: the anchored discriminator (false-green guard) ───────────


def test_pure_gap_unanchored_is_not_pass() -> None:
    """Zero SEC rows ⇒ anchored=False with empty missing_periods —
    DISTINCT from an anchored PASS. Caller must NOT treat as PASS."""
    res = _pure_gap(
        anchored=False,
        expected=set(),
        have=set(),  # fundamentals empty too
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.anchored is False
    assert res.missing_periods == ()


def test_pure_gap_anchored_no_gap_is_pass() -> None:
    p = {date(2026, 3, 31), date(2025, 12, 31)}
    res = _pure_gap(
        anchored=True, expected=p, have=p,
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.anchored is True
    assert res.missing_periods == ()


def test_pure_gap_anchored_with_missing() -> None:
    res = _pure_gap(
        anchored=True,
        expected={date(2026, 3, 31), date(2025, 12, 31)},
        have={date(2025, 12, 31)},
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.anchored is True
    assert res.missing_periods == (date(2026, 3, 31),)


def test_pure_gap_restatement_dedup_neutral() -> None:
    """Restatement / amendment (same report_date, different accession)
    is set-difference neutral because both sides are DISTINCT sets."""
    # expected has one logical period (even if SEC has a base + /A row
    # for it, the report_date is identical → one set element). have has
    # the same period → no gap.
    period = date(2026, 3, 31)
    res = _pure_gap(
        anchored=True,
        expected={period},
        have={period},
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.missing_periods == ()


def test_unanchored_distinct_from_anchored_empty() -> None:
    """The two outcomes must be DISTINGUISHABLE on .anchored alone."""
    unanchored = _pure_gap(
        anchored=False, expected=set(), have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    anchored_pass = _pure_gap(
        anchored=True, expected=set(), have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert unanchored.missing_periods == anchored_pass.missing_periods == ()
    assert unanchored.anchored != anchored_pass.anchored


# ── compute_filing_gaps over a fake connection ─────────────────────────


class _FakeConn:
    """Dispatches fetch() on a substring of the SQL to the right rows."""

    def __init__(
        self,
        *,
        anchored: list[str],
        expected: list[dict[str, Any]],
        have: list[dict[str, Any]],
    ) -> None:
        self._anchored = [{"classification_id": c} for c in anchored]
        self._expected = expected
        self._have = have
        self.calls: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if "DISTINCT classification_id" in sql:
            cids = set(args[0])
            return [r for r in self._anchored
                    if r["classification_id"] in cids]
        if "FROM platform.sec_periodic_filings" in sql:
            cids = set(args[0])
            return [r for r in self._expected
                    if r["classification_id"] in cids]
        if "FROM platform.fundamentals_quarterly" in sql:
            cids = set(args[0])
            return [r for r in self._have
                    if r["classification_id"] in cids]
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_compute_filing_gaps_anchored_false_when_no_sec_rows() -> None:
    conn = _FakeConn(anchored=[], expected=[], have=[])
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-A"],
        {"cid-A": "quarterly"},
    )
    assert out["cid-A"].anchored is False
    assert out["cid-A"].missing_periods == ()


@pytest.mark.asyncio
async def test_compute_filing_gaps_pass_and_gap() -> None:
    conn = _FakeConn(
        anchored=["cid-A", "cid-B"],
        expected=[
            {"classification_id": "cid-A", "report_date": date(2026, 3, 31)},
            {"classification_id": "cid-A", "report_date": date(2025, 12, 31)},
            {"classification_id": "cid-B", "report_date": date(2025, 12, 31)},
        ],
        have=[
            {"classification_id": "cid-A", "period_end_date": date(2026, 3, 31)},
            {"classification_id": "cid-A",
             "period_end_date": date(2025, 12, 31)},
            # cid-B is missing 2025-12-31 in fundamentals.
        ],
    )
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-A", "cid-B"],
        {"cid-A": "quarterly", "cid-B": "annual"},
    )
    assert out["cid-A"].anchored is True
    assert out["cid-A"].missing_periods == ()
    assert out["cid-B"].anchored is True
    assert out["cid-B"].missing_periods == (date(2025, 12, 31),)


@pytest.mark.asyncio
async def test_compute_filing_gaps_skips_unrouted_cids() -> None:
    conn = _FakeConn(anchored=["cid-A"], expected=[], have=[])
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-A", "cid-X"],
        {"cid-A": "quarterly"},  # cid-X absent → skipped
    )
    assert "cid-X" not in out
    assert "cid-A" in out


@pytest.mark.asyncio
async def test_compute_filing_gaps_empty_input() -> None:
    conn = _FakeConn(anchored=[], expected=[], have=[])
    out = await compute_filing_gaps(
        conn, [], {},  # type: ignore[arg-type]
    )
    assert out == {}


# ── write_periodic_filings column wiring + chunking ────────────────────


class _Filing:
    def __init__(
        self, form_type: str, filing_date: date,
        report_date: date | None, accession_number: str,
    ) -> None:
        self.form_type = form_type
        self.filing_date = filing_date
        self.report_date = report_date
        self.accession_number = accession_number


class _RecordingConn:
    def __init__(self) -> None:
        self.executes: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.executes.append((sql, *args))


@pytest.mark.asyncio
async def test_write_periodic_filings_empty_is_noop() -> None:
    conn = _RecordingConn()
    n = await write_periodic_filings(conn, [], cik="0001", ticker="AAA")
    assert n == 0
    assert conn.executes == []


@pytest.mark.asyncio
async def test_write_periodic_filings_columns() -> None:
    conn = _RecordingConn()
    rows = [
        _Filing("10-Q", date(2026, 5, 1), date(2026, 3, 31), "acc-1"),
        _Filing("10-K", date(2025, 9, 15), None, "acc-2"),
    ]
    n = await write_periodic_filings(
        conn, rows, cik="0000320193", ticker="AAPL",
    )
    assert n == 2
    assert len(conn.executes) == 1
    sql, ciks, tickers, forms, report_dates, filing_dates, accs = (
        conn.executes[0]
    )
    assert "ON CONFLICT (cik, accession_number) DO NOTHING" in sql
    # classification_id intentionally absent (trigger fills it).
    assert "classification_id" not in sql
    assert ciks == ["0000320193", "0000320193"]
    assert tickers == ["AAPL", "AAPL"]
    assert forms == ["10-Q", "10-K"]
    assert report_dates == [date(2026, 3, 31), None]
    assert filing_dates == [date(2026, 5, 1), date(2025, 9, 15)]
    assert accs == ["acc-1", "acc-2"]


@pytest.mark.asyncio
async def test_write_periodic_filings_chunks() -> None:
    from tpcore.quality import sec_periodic_filings_store as store

    conn = _RecordingConn()
    # 501 rows with chunk size 500 → 2 execute calls.
    rows = [
        _Filing("10-Q", date(2026, 1, 1), None, f"acc-{i}")
        for i in range(store.WRITE_CHUNK_SIZE + 1)
    ]
    n = await write_periodic_filings(conn, rows, cik="c", ticker="T")
    assert n == store.WRITE_CHUNK_SIZE + 1
    assert len(conn.executes) == 2
    # first chunk full, second chunk has the remainder.
    assert len(conn.executes[0][2]) == store.WRITE_CHUNK_SIZE
    assert len(conn.executes[1][2]) == 1


def test_filing_gap_result_is_frozen() -> None:
    res = FilingGapResult(
        anchored=True, missing_periods=(), routed_forms=ROUTED_FORMS,
    )
    with pytest.raises(Exception):  # noqa: B017,PT011
        res.anchored = False  # type: ignore[misc]
