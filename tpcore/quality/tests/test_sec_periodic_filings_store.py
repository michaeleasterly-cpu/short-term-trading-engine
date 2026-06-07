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
    _FISCAL_QUARTER_TOLERANCE_DAYS,
    _HAVE_SQL,
    ROUTED_ANNUAL_FORMS,
    ROUTED_FORMS,
    ROUTED_QUARTERLY_FORMS,
    FilingGapResult,
    _expected_is_satisfied,
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


# ── fiscal-quarter tolerance (_expected_is_satisfied / _pure_gap) ──────


def test_fiscal_quarter_tolerance_constant_cannot_absorb_adjacent_quarter() -> None:
    """+/-tolerance must stay well under a 13-week (~91 day) quarter so it can
    never collapse two adjacent real quarters."""
    assert _FISCAL_QUARTER_TOLERANCE_DAYS < 76  # nearest distinct quarter-end


def test_expected_satisfied_within_tolerance() -> None:
    """reportDate 2020-12-26 is SATISFIED by a have period_end 2020-12-31
    (+5 days) — same fiscal quarter, convention skew only."""
    assert _expected_is_satisfied(date(2020, 12, 26), [date(2020, 12, 31)])


def test_expected_not_satisfied_when_nearest_have_far_away() -> None:
    """A reportDate whose nearest have-date is 30 days away is still MISSING."""
    assert not _expected_is_satisfied(date(2020, 12, 26), [date(2021, 1, 25)])


def test_pure_gap_tolerant_match_not_missing() -> None:
    """An expected 2020-12-26 with a have 2020-12-31 (+5d) is NOT missing."""
    res = _pure_gap(
        anchored=True,
        expected={date(2020, 12, 26)},
        have={date(2020, 12, 31)},
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.missing_periods == ()


def test_pure_gap_far_have_is_still_missing() -> None:
    """nearest have-date 30 days from the expected reportDate => MISSING."""
    res = _pure_gap(
        anchored=True,
        expected={date(2020, 12, 26)},
        have={date(2021, 1, 25)},
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.missing_periods == (date(2020, 12, 26),)


def test_pure_gap_tolerance_does_not_collapse_two_adjacent_quarters() -> None:
    """Two adjacent real quarters (~91 days apart) must NOT be collapsed: a have
    date for one quarter cannot satisfy the expected date for the next."""
    q3 = date(2020, 9, 30)
    q4 = date(2020, 12, 31)
    # We HAVE only Q3; we EXPECT both Q3 and Q4. Q4 must remain missing —
    # the +/-15d window cannot reach from Q3's have-date to Q4's expected.
    res = _pure_gap(
        anchored=True,
        expected={q3, q4},
        have={q3},
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.missing_periods == (q4,)


def test_pure_gap_restatement_dedup_still_neutral_with_tolerance() -> None:
    """Restatement dedup remains neutral under tolerant matching: identical
    expected/have period sets produce no gap."""
    p = {date(2026, 3, 31), date(2025, 12, 31)}
    res = _pure_gap(
        anchored=True, expected=p, have=p,
        routed_forms=ROUTED_QUARTERLY_FORMS,
    )
    assert res.missing_periods == ()


# ── ≥2016 horizon (policy-free store pass-through) ─────────────────────


def test_pure_gap_horizon_none_keeps_pre_horizon_periods() -> None:
    """horizon=None is the historical behaviour: a pre-2016 expected
    reportDate with no matching have is STILL missing, and
    excluded_pre_horizon is 0."""
    old = date(2014, 3, 31)
    res = _pure_gap(
        anchored=True, expected={old}, have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS, horizon=None,
    )
    assert res.missing_periods == (old,)
    assert res.excluded_pre_horizon == 0


def test_pure_gap_pre_horizon_excluded_not_missing() -> None:
    """A pre-horizon expected reportDate (no have) is EXCLUDED from the gap
    and counted in excluded_pre_horizon — not a missing period."""
    old = date(2014, 3, 31)
    res = _pure_gap(
        anchored=True, expected={old}, have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS, horizon=date(2016, 1, 1),
    )
    assert res.missing_periods == ()
    assert res.excluded_pre_horizon == 1


def test_pure_gap_on_or_after_horizon_still_missing() -> None:
    """A reportDate ON/AFTER the horizon that is missing STILL fails — the
    horizon never masks a recent gap. The boundary date (== horizon) is
    in-scope (the filter is rd >= horizon)."""
    boundary = date(2016, 1, 1)
    later = date(2020, 6, 30)
    res = _pure_gap(
        anchored=True, expected={boundary, later}, have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS, horizon=date(2016, 1, 1),
    )
    assert res.missing_periods == (boundary, later)
    assert res.excluded_pre_horizon == 0


def test_pure_gap_mixed_pre_and_post_horizon() -> None:
    """Mixed expected set: pre-horizon excluded (counted), post-horizon
    missing still fails."""
    res = _pure_gap(
        anchored=True,
        expected={date(2013, 12, 31), date(2014, 12, 31), date(2020, 12, 31)},
        have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS, horizon=date(2016, 1, 1),
    )
    assert res.missing_periods == (date(2020, 12, 31),)
    assert res.excluded_pre_horizon == 2


def test_pure_gap_unanchored_horizon_is_zero() -> None:
    """An un-anchored issuer carries excluded_pre_horizon=0 regardless of
    horizon — there is no expected set to filter."""
    res = _pure_gap(
        anchored=False, expected=set(), have=set(),
        routed_forms=ROUTED_QUARTERLY_FORMS, horizon=date(2016, 1, 1),
    )
    assert res.anchored is False
    assert res.excluded_pre_horizon == 0


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


# ── NULL-cid period_end-window credit (_HAVE_SQL defensive arm) ────────


def test_have_sql_credits_via_period_end_window_not_raw_ticker() -> None:
    """The _HAVE_SQL UNION's second arm credits a NULL-cid fundamentals row to a
    cid through that cid's OWN ticker_history window, anchored on period_end_date
    (the same half-open predicate the trigger uses) — NOT a raw ticker-only
    match (which would miscredit recycled-ticker reuse rows).

    This is a structural guard on the recycled-ticker-safe design: assert the
    SQL joins ticker_history with the half-open window predicate and filters on
    classification_id IS NULL.
    """
    sql = _HAVE_SQL
    # Defensive arm present.
    assert "JOIN platform.ticker_history" in sql
    assert "fq.classification_id IS NULL" in sql
    # Half-open window predicate anchored on period_end_date (NOT filing_date,
    # NOT a bare ticker equality without the window).
    assert "th.valid_from <= fq.period_end_date" in sql
    assert "fq.period_end_date < th.valid_to" in sql
    # The credited cid is the ticker_history holder for that period window.
    assert "th.classification_id" in sql


@pytest.mark.asyncio
async def test_compute_filing_gaps_credits_null_cid_window_row() -> None:
    """A NULL-cid fundamentals row whose period_end falls in cid C's window is
    credited toward C (the _HAVE_SQL UNION surfaces it as a have-date for C), so
    an expected reportDate it covers is NOT reported missing.

    The _FakeConn returns the rows _HAVE_SQL would yield AFTER the UNION (both
    arms), which is the contract compute_filing_gaps consumes; the window join
    itself is exercised against live data in the migration verification.
    """
    conn = _FakeConn(
        anchored=["cid-A"],
        expected=[
            {"classification_id": "cid-A", "report_date": date(2020, 3, 31)},
        ],
        # cid-A has NO directly-stamped row, but the UNION's window arm credits
        # the NULL-cid 2020-03-31 row to cid-A — so have includes it.
        have=[
            {"classification_id": "cid-A", "period_end_date": date(2020, 3, 31)},
        ],
    )
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-A"],
        {"cid-A": "quarterly"},
    )
    assert out["cid-A"].anchored is True
    assert out["cid-A"].missing_periods == ()


@pytest.mark.asyncio
async def test_compute_filing_gaps_recycled_gap_row_not_credited() -> None:
    """Recycled-ticker safety: a NULL-cid reuse-ticker row that falls in a
    GAP/predecessor period (NOT in the current holder's window) is NOT surfaced
    by the _HAVE_SQL window arm for the current holder, so the current holder's
    own expected reportDate stays missing if it truly has no covering have-date.

    Modeled at the contract level: the predecessor row does NOT appear in
    cid-CURRENT's have set (the window join would not credit it), so an expected
    reportDate for cid-CURRENT with no covering have-date IS missing.
    """
    conn = _FakeConn(
        anchored=["cid-CURRENT"],
        expected=[
            {"classification_id": "cid-CURRENT",
             "report_date": date(2025, 6, 30)},
        ],
        # The predecessor-period NULL-cid row (e.g. 2016-06-30) is window-scoped
        # to the OLD entity, so it is NOT credited to cid-CURRENT → have is empty
        # for cid-CURRENT.
        have=[],
    )
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-CURRENT"],
        {"cid-CURRENT": "quarterly"},
    )
    assert out["cid-CURRENT"].anchored is True
    assert out["cid-CURRENT"].missing_periods == (date(2025, 6, 30),)


# ── _HAVE_SQL UNION: real arm-A + arm-B window-join modeling ───────────


class _UnionHaveConn:
    """Fake conn that MODELS the ``_HAVE_SQL`` two-arm UNION semantics.

    Unlike ``_FakeConn`` (which returns the post-UNION have-set directly),
    this conn is fed the RAW substrate the UNION reads — directly-stamped
    fundamentals rows (arm A: ``classification_id`` non-NULL) and NULL-cid
    fundamentals rows (arm B candidates) + the ``ticker_history`` window
    rows — and computes the UNION itself, EXERCISING the half-open window
    predicate (``valid_from <= period_end_date < valid_to``). This proves a
    NULL-cid predecessor-period row is NOT credited to the current holder
    while an in-window NULL-cid period IS.
    """

    def __init__(
        self,
        *,
        anchored: list[str],
        expected: list[dict[str, Any]],
        stamped_have: list[dict[str, Any]],
        null_cid_have: list[dict[str, Any]],
        ticker_history: list[dict[str, Any]],
    ) -> None:
        self._anchored = [{"classification_id": c} for c in anchored]
        self._expected = expected
        # arm A: directly-stamped (classification_id, period_end_date) rows.
        self._stamped = stamped_have
        # NULL-cid fundamentals rows: (ticker, period_end_date).
        self._null_cid = null_cid_have
        # ticker_history windows: (ticker, classification_id, valid_from,
        # valid_to|None).
        self._history = ticker_history

    def _union_have(self, cids: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # arm A — directly-stamped rows for the requested cids.
        for r in self._stamped:
            if r["classification_id"] in cids:
                out.append({
                    "classification_id": r["classification_id"],
                    "period_end_date": r["period_end_date"],
                })
        # arm B — NULL-cid rows credited via the ticker_history half-open
        # window (valid_from <= period_end_date < valid_to|+inf).
        for nc in self._null_cid:
            for th in self._history:
                if th["ticker"] != nc["ticker"]:
                    continue
                if th["classification_id"] not in cids:
                    continue
                pe = nc["period_end_date"]
                if th["valid_from"] <= pe and (
                    th["valid_to"] is None or pe < th["valid_to"]
                ):
                    out.append({
                        "classification_id": th["classification_id"],
                        "period_end_date": pe,
                    })
        return out

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "DISTINCT classification_id" in sql:
            cids = set(args[0])
            return [r for r in self._anchored
                    if r["classification_id"] in cids]
        if "FROM platform.sec_periodic_filings" in sql:
            cids = set(args[0])
            return [r for r in self._expected
                    if r["classification_id"] in cids]
        if "FROM platform.fundamentals_quarterly" in sql:
            return self._union_have(set(args[0]))
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_union_arm_b_window_credits_in_window_not_predecessor() -> None:
    """Recycled-ticker safety at the WINDOW-PREDICATE level (the real
    arm-A + arm-B UNION, not a stub).

    Ticker ``RCYC`` was used by a PREDECESSOR entity (cid-OLD, window
    2010-01-01..2018-01-01) then re-issued to the CURRENT holder (cid-CUR,
    window 2018-01-01..open). Two NULL-cid fundamentals rows exist:

      * 2016-06-30 — falls in the PREDECESSOR window ⇒ must NOT be credited
        to cid-CUR.
      * 2025-06-30 — falls in the CURRENT-holder window ⇒ MUST be credited
        to cid-CUR.

    cid-CUR expects both reportDates. Only the predecessor period stays
    missing; the in-window period is satisfied via the arm-B window join."""
    conn = _UnionHaveConn(
        anchored=["cid-CUR"],
        expected=[
            {"classification_id": "cid-CUR", "report_date": date(2016, 6, 30)},
            {"classification_id": "cid-CUR", "report_date": date(2025, 6, 30)},
        ],
        stamped_have=[],  # no directly-stamped rows for cid-CUR
        null_cid_have=[
            {"ticker": "RCYC", "period_end_date": date(2016, 6, 30)},
            {"ticker": "RCYC", "period_end_date": date(2025, 6, 30)},
        ],
        ticker_history=[
            {"ticker": "RCYC", "classification_id": "cid-OLD",
             "valid_from": date(2010, 1, 1), "valid_to": date(2018, 1, 1)},
            {"ticker": "RCYC", "classification_id": "cid-CUR",
             "valid_from": date(2018, 1, 1), "valid_to": None},
        ],
    )
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-CUR"],
        {"cid-CUR": "quarterly"},
    )
    assert out["cid-CUR"].anchored is True
    # Predecessor-period reportDate NOT credited → stays missing; the
    # in-window period IS credited → satisfied.
    assert out["cid-CUR"].missing_periods == (date(2016, 6, 30),)


@pytest.mark.asyncio
async def test_union_arm_b_boundary_is_half_open() -> None:
    """The window predicate is HALF-OPEN: ``valid_from <= pe < valid_to``.
    A period_end exactly on the predecessor's ``valid_to`` (= the current
    holder's ``valid_from``) belongs to the CURRENT holder, not the
    predecessor — so it IS credited to cid-CUR."""
    boundary = date(2018, 1, 1)
    conn = _UnionHaveConn(
        anchored=["cid-CUR"],
        expected=[
            {"classification_id": "cid-CUR", "report_date": boundary},
        ],
        stamped_have=[],
        null_cid_have=[
            {"ticker": "RCYC", "period_end_date": boundary},
        ],
        ticker_history=[
            {"ticker": "RCYC", "classification_id": "cid-OLD",
             "valid_from": date(2010, 1, 1), "valid_to": boundary},
            {"ticker": "RCYC", "classification_id": "cid-CUR",
             "valid_from": boundary, "valid_to": None},
        ],
    )
    out = await compute_filing_gaps(
        conn,  # type: ignore[arg-type]
        ["cid-CUR"],
        {"cid-CUR": "quarterly"},
    )
    # pe == cid-CUR.valid_from (>=) and pe == cid-OLD.valid_to (NOT < ) ⇒
    # credited to cid-CUR only ⇒ no gap.
    assert out["cid-CUR"].missing_periods == ()


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
