"""Tests for the authoritative-SEC-reportDate fundamentals-completeness gate.

**P3 rewrite (2026-06-07)** — the gap is now an authoritative
set-difference against ``platform.sec_periodic_filings`` (via the shared
``tpcore.quality.sec_periodic_filings_store``), NOT an even-spacing
interpolation heuristic. The old C1-C12 tests that encoded the heuristic
(``_infer_missing_period_ends`` math, day-gap caps, new-listing grace
windows) are REWRITTEN here to express scenarios in the new vocabulary:

  * the universe row set (tier≤2 issuers + identity + routing metadata),
  * the SEC-filed reportDates per issuer (``expected``),
  * the fundamentals period_end_dates per issuer (``have``),
  * confirmed-data-gap evidence rows (the dual-source exclusion).

The fake connection dispatches each SQL by a distinctive substring to
the matching row set, so the check exercises the REAL store helper
``compute_filing_gaps`` (set-difference + the ``anchored`` discriminator,
the false-green guard).

What these tests pin is the BEHAVIOR the old heuristic got wrong:
53-week fiscal years, 10-K-replaces-Q4 annual filers, restatement
amendments — none of which produce a false gap when we demand only the
reportDates SEC literally filed. And the cases the gate MUST get right:
a genuine missing 10-Q FAILs (named), and an ``anchored=False`` issuer
NEVER silently passes.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
    LIVE_WITHIN_DAYS_ANNUAL,
    LIVE_WITHIN_DAYS_QUARTERLY,
    MAX_ANNUAL_GAP_DAYS,
    MAX_QUARTERLY_GAP_DAYS,
    _cadence_for,
    check_fundamentals_quarterly_completeness,
    compute_fundamentals_repair_targets,
)

_TODAY = datetime.now(UTC).date()


# ── Fake DB substrate ──────────────────────────────────────────────────


class _Issuer:
    """One tier≤2 issuer's full substrate footprint for the fake DB.

    ``sec_report_dates`` is what SEC FILED (``expected``); ``have`` is the
    fundamentals_quarterly period_end_dates. ``anchored`` is derived: an
    issuer with NO sec_periodic_filings rows at all is un-anchored. Set
    ``has_sec_rows`` to model an issuer that HAS some sec_periodic_filings
    rows but none at the routed cadence (still anchored).
    """

    def __init__(
        self,
        ticker: str,
        *,
        cid: str,
        cik: str | None,
        primary: str | None,
        sec_report_dates: list[date] | None = None,
        have: list[date] | None = None,
        lifecycle_state: str | None = None,
        has_sec_rows: bool | None = None,
        asset_class: str | None = "stock",
    ) -> None:
        self.ticker = ticker
        self.cid = cid
        self.cik = cik
        self.primary = primary
        self.asset_class = asset_class
        self.sec_report_dates = sec_report_dates or []
        self.have = have or []
        self.lifecycle_state = lifecycle_state
        # Anchored iff the issuer has ANY sec_periodic_filings row.
        self.has_sec_rows = (
            has_sec_rows
            if has_sec_rows is not None
            else bool(self.sec_report_dates)
        )


class _Conn:
    def __init__(self, issuers: list[_Issuer]) -> None:
        self._issuers = issuers

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # 1) Universe SQL (the check's _FILING_DATES_SQL).
        if "WITH liquid AS" in sql:
            out: list[dict[str, Any]] = []
            for iss in self._issuers:
                base = {
                    "ticker": iss.ticker,
                    "classification_id": iss.cid,
                    "cik": iss.cik,
                    "asset_class": iss.asset_class,
                    "sec_document_type_primary": iss.primary,
                    "issuer_lifecycle_state": iss.lifecycle_state,
                    "issuer_lifecycle_event_date": None,
                }
                if iss.have:
                    for pe in sorted(iss.have):
                        out.append({**base, "period_end_date": pe})
                else:
                    out.append({**base, "period_end_date": None})
            return out

        # 2) Store _ANCHORED_SQL.
        if "SELECT DISTINCT classification_id" in sql:
            wanted = set(args[0])
            return [
                {"classification_id": iss.cid}
                for iss in self._issuers
                if iss.cid in wanted and iss.has_sec_rows
            ]

        # 3) Store _EXPECTED_SQL (SEC reportDates, routed-form filtered).
        if "FROM platform.sec_periodic_filings" in sql:
            wanted = set(args[0])
            rows: list[dict[str, Any]] = []
            for iss in self._issuers:
                if iss.cid not in wanted:
                    continue
                for rd in iss.sec_report_dates:
                    rows.append(
                        {"classification_id": iss.cid, "report_date": rd}
                    )
            return rows

        # 4) Store _HAVE_SQL (fundamentals period_end_dates by cid).
        if ("FROM platform.fundamentals_quarterly" in sql
                and "period_end_date" in sql
                and "classification_id = ANY" in sql):
            wanted = set(args[0])
            rows = []
            for iss in self._issuers:
                if iss.cid not in wanted:
                    continue
                for pe in iss.have:
                    rows.append(
                        {"classification_id": iss.cid, "period_end_date": pe}
                    )
            return rows

        # 5) Evidence-join SQL — default: no dual-source evidence.
        if "confirmed_data_gap_evidence" in sql:
            return []

        raise AssertionError(f"unexpected SQL: {sql[:80]}")


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, issuers: list[_Issuer]) -> None:
        self._issuers = issuers

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._issuers))


def _quarter_ends(start: date, n: int) -> list[date]:
    """n calendar quarter-ends starting near ``start`` (Mar/Jun/Sep/Dec)."""
    anchors = [date(2000, 3, 31), date(2000, 6, 30),
               date(2000, 9, 30), date(2000, 12, 31)]
    out: list[date] = []
    y, qi = start.year, 0
    # pick the first anchor >= start's quarter
    while date(y, anchors[qi].month, anchors[qi].day) < start:
        qi += 1
        if qi == 4:
            qi, y = 0, y + 1
    for _ in range(n):
        out.append(date(y, anchors[qi].month, anchors[qi].day))
        qi += 1
        if qi == 4:
            qi, y = 0, y + 1
    return out


# Padding issuers keep the metadata-coverage sentinel from firing in
# scenarios that aren't about it (clean 10-Q filers with full data).
def _clean_padding(n: int) -> list[_Issuer]:
    today = _TODAY
    rd = _quarter_ends(today - timedelta(days=370), 4)
    rd = [d for d in rd if d <= today]
    return [
        _Issuer(
            f"PAD{i}", cid=f"pad-{i}", cik=f"00{i:05d}", primary="10-Q",
            sec_report_dates=rd, have=rd,
        )
        for i in range(n)
    ]


# ── C1 — clean quarterly cadence passes ──────────────────────────────


async def test_C1_clean_quarterly_cadence_passes() -> None:
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    iss = _Issuer(
        "AAPL", cid="c-aapl", cik="0000320193", primary="10-Q",
        sec_report_dates=rd, have=rd,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.name == "fundamentals_quarterly_completeness"


# ── C2 — genuine missing 10-Q FAILs with the date NAMED ──────────────


async def test_C2_genuine_missing_quarter_fails_named() -> None:
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    missing = rd[1]  # SEC filed it, fundamentals lacks it
    have = [d for d in rd if d != missing]
    iss = _Issuer(
        "AAPL", cid="c-aapl", cik="0000320193", primary="10-Q",
        sec_report_dates=rd, have=have,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is False
    aapl = [f for f in result.failures if f.ticker == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0].reason == "missing_period_10-Q"
    # The genuine missing reportDate is named, not interpolated.
    assert missing.isoformat() in aapl[0].observed


# ── C3 — two genuine missing quarters, both named ────────────────────


async def test_C3_two_missing_quarters_named() -> None:
    rd = _quarter_ends(_TODAY - timedelta(days=500), 5)
    rd = [d for d in rd if d <= _TODAY]
    miss = {rd[1], rd[2]}
    have = [d for d in rd if d not in miss]
    iss = _Issuer(
        "AAPL", cid="c-aapl", cik="0000320193", primary="10-Q",
        sec_report_dates=rd, have=have,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is False
    aapl = [f for f in result.failures if f.ticker == "AAPL"]
    assert len(aapl) == 1
    assert "2 SEC-filed reportDate(s) missing" in aapl[0].observed


# ── 53-week fiscal year: no false gap (old heuristic false-fired) ─────


async def test_53_week_fiscal_year_no_false_gap() -> None:
    """A 53-week fiscal year stretches one quarter to ~98 days. The old
    day-gap heuristic (>100d quarterly cap) would false-fire on the next
    quarter; the set-difference does NOT, because we only demand the
    reportDates SEC actually filed and fundamentals has them all."""
    # SEC reportDates with a 53-week stretch (one ~371-day-spanning year).
    rd = [
        date(2024, 1, 28),   # FY end (53-week year ends late Jan)
        date(2024, 4, 28),
        date(2024, 7, 28),
        date(2024, 10, 27),
        _TODAY - timedelta(days=30),
    ]
    rd = [d for d in rd if d <= _TODAY]
    iss = _Issuer(
        "RETAIL", cid="c-retail", cik="0000111111", primary="10-Q",
        sec_report_dates=rd, have=rd,  # fundamentals has every filed date
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True, [f.observed for f in result.failures]


# ── 10-K-replaces-Q4: annual filer, no false gap ─────────────────────


async def test_10k_replaces_q4_annual_no_false_gap() -> None:
    """An annual-cadence issuer (10-K primary) whose Q4 is a 10-K: SEC
    filed annual reportDates; fundamentals has them. No false gap —
    routed at annual cadence, only annual reportDates demanded."""
    rd = [
        _TODAY - timedelta(days=365 * 2),
        _TODAY - timedelta(days=365),
        _TODAY - timedelta(days=30),
    ]
    iss = _Issuer(
        "BIGCO", cid="c-bigco", cik="0000222222", primary="10-K",
        sec_report_dates=rd, have=rd,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True, [f.observed for f in result.failures]


# ── Restatement / amendment: same reportDate twice → set-neutral ─────


async def test_restatement_amendment_set_difference_neutral() -> None:
    """An original + a 10-Q/A amendment carry the SAME report_date — the
    store's expected set is DISTINCT, so the duplicate is set-difference
    neutral. No fabricated gap when fundamentals has that one period."""
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    # Model the amendment by listing one reportDate twice in expected;
    # the store SELECTs DISTINCT, so it collapses to one logical period.
    sec_rows = [*rd, rd[1]]  # rd[1] appears twice (orig + /A)
    iss = _Issuer(
        "AMEND", cid="c-amend", cik="0000333333", primary="10-Q",
        sec_report_dates=sec_rows, have=rd,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True, [f.observed for f in result.failures]


# ── anchored=False, CIK-less → excluded-with-evidence, NOT pass ──────


async def test_anchored_false_cikless_excluded_not_pass() -> None:
    """A CIK-LESS tier≤2 routed name with ZERO sec_periodic_filings rows
    is ``anchored=False``: no SEC obligation we can verify ⇒ routes to
    confirmed_data_gap (excluded-with-evidence), NEVER a silent PASS and
    NEVER a fabricated gap. It must NOT appear in the denominator and must
    NOT be a per-ticker failure."""
    iss = _Issuer(
        "DARKSPAC", cid="c-darkspac", cik=None, primary="10-Q",
        sec_report_dates=[],  # no SEC periodic rows ⇒ anchored=False
        have=[],
    )
    # Pad with clean filers so the metadata-coverage sentinel doesn't fire
    # (excluded_confirmed_data_gap doesn't count toward metadata coverage,
    # but evaluated_routed must be non-trivial for a clean read).
    pool = _Pool([iss, *_clean_padding(5)])
    result = await check_fundamentals_quarterly_completeness(pool)
    # Not a per-ticker failure.
    assert [f for f in result.failures if f.ticker == "DARKSPAC"] == []
    # And NOT passed-into-the-denominator: the padding filers carry the
    # PASS; DARKSPAC is excluded. The whole check passes (padding is
    # clean) but DARKSPAC contributed no green of its own.
    assert result.passed is True, [f.observed for f in result.failures]


# ── anchored=False, CIK-backed → METADATA_REQUIRED (surfaces) ────────


async def test_anchored_false_cik_backed_surfaces_metadata_required() -> None:
    """A CIK-BACKED issuer that is ``anchored=False`` (its
    periodic-filings backfill has not populated yet) must NOT silently
    pass — it routes to METADATA_REQUIRED and, when it dominates the
    universe, trips the metadata-coverage sentinel."""
    # 4 CIK-backed un-anchored issuers vs 1 clean → 80% metadata-required
    # → coverage sentinel fires (threshold 25%).
    unanchored = [
        _Issuer(
            f"NEW{i}", cid=f"c-new-{i}", cik=f"00{i:05d}", primary="10-Q",
            sec_report_dates=[], have=[],  # anchored=False
        )
        for i in range(4)
    ]
    clean = _clean_padding(1)
    result = await check_fundamentals_quarterly_completeness(
        _Pool([*unanchored, *clean])
    )
    assert result.passed is False
    sentinel = [
        f for f in result.failures
        if f.reason == "metadata_coverage_insufficient"
    ]
    assert len(sentinel) == 1
    # No CIK-backed un-anchored issuer is a fabricated per-ticker gap.
    assert [f for f in result.failures
            if f.ticker.startswith("NEW")] == []


# ── Zero-anchored universe → structural FAIL (safety review #1) ──────


async def test_zero_anchored_universe_with_exclusions_fails() -> None:
    """A routed universe that anchored ZERO issuers while excluding some
    must HARD-FAIL with the ``<zero_anchored_universe>`` sentinel — never a
    vacuous PASS. Here every issuer is CIK-LESS un-anchored ⇒ all route to
    confirmed_data_gap (which is OMITTED from the coverage-ratio denominator,
    so the metadata-coverage sentinel alone would NOT fire — this is exactly
    the gap the structural guard closes)."""
    cikless = [
        _Issuer(
            f"DARK{i}", cid=f"c-dark-{i}", cik=None, primary="10-Q",
            sec_report_dates=[], have=[],  # anchored=False, CIK-less
        )
        for i in range(3)
    ]
    result = await check_fundamentals_quarterly_completeness(_Pool(cikless))
    assert result.passed is False, [f.observed for f in result.failures]
    sentinel = [
        f for f in result.failures
        if f.reason == "zero_anchored_universe"
    ]
    assert len(sentinel) == 1
    assert sentinel[0].ticker == "<zero_anchored_universe>"
    # The confirmed_data_gap count surfaces in the observed text.
    assert "confirmed_data_gap=3" in sentinel[0].observed


async def test_zero_anchored_metadata_required_only_also_fails() -> None:
    """Same structural guard when the entire universe routes to
    metadata_required (CIK-backed un-anchored): zero anchored ⇒ FAIL."""
    cik_backed = [
        _Issuer(
            f"NEW{i}", cid=f"c-new-{i}", cik=f"00{i:05d}", primary="10-Q",
            sec_report_dates=[], have=[],  # anchored=False, CIK-backed
        )
        for i in range(3)
    ]
    result = await check_fundamentals_quarterly_completeness(_Pool(cik_backed))
    assert result.passed is False
    assert any(
        f.reason == "zero_anchored_universe" for f in result.failures
    )


async def test_some_anchored_does_not_trip_zero_anchored_guard() -> None:
    """A single anchored clean filer keeps the zero-anchored guard silent
    even when the rest of the universe is excluded — the guard fires only
    on a TRULY zero-anchored universe."""
    cikless = [
        _Issuer(
            f"DARK{i}", cid=f"c-dark-{i}", cik=None, primary="10-Q",
            sec_report_dates=[], have=[],
        )
        for i in range(3)
    ]
    pool = _Pool([*cikless, *_clean_padding(1)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert [
        f for f in result.failures if f.reason == "zero_anchored_universe"
    ] == []


# ── Empty-string CIK routes to metadata_required (safety review #2) ──


async def test_empty_string_cik_routes_metadata_required_not_confirmed_gap() -> None:
    """A CIK-BACKED issuer whose ``cik`` is corrupt (empty string) and is
    ``anchored=False`` must surface as METADATA_REQUIRED (sentinel-visible),
    NOT confirmed_data_gap (sentinel-blind). With it dominating the universe
    the metadata-coverage sentinel must fire — proving the empty-string CIK
    landed in the surfacing bucket. A genuinely CIK-less (None) issuer in the
    SAME universe stays in confirmed_data_gap (does NOT count toward
    coverage), so only the empty-string defect drives the sentinel."""
    empty_cik = [
        _Issuer(
            f"CORRUPT{i}", cid=f"c-corrupt-{i}", cik="", primary="10-Q",
            sec_report_dates=[], have=[],  # anchored=False, empty CIK
        )
        for i in range(4)
    ]
    clean = _clean_padding(1)
    result = await check_fundamentals_quarterly_completeness(
        _Pool([*empty_cik, *clean])
    )
    # 4 metadata_required / (4 + 1 evaluated) = 80% > 25% ⇒ sentinel fires.
    assert result.passed is False
    assert any(
        f.reason == "metadata_coverage_insufficient" for f in result.failures
    )
    # The empty-CIK issuers are NOT fabricated per-ticker gaps.
    assert [f for f in result.failures
            if f.ticker.startswith("CORRUPT")] == []


async def test_whitespace_cik_routes_metadata_required() -> None:
    """A whitespace-only CIK (``'  '``) is treated identically to empty —
    routes to METADATA_REQUIRED, not confirmed_data_gap."""
    ws_cik = [
        _Issuer(
            f"WS{i}", cid=f"c-ws-{i}", cik="   ", primary="10-Q",
            sec_report_dates=[], have=[],
        )
        for i in range(4)
    ]
    result = await check_fundamentals_quarterly_completeness(
        _Pool([*ws_cik, *_clean_padding(1)])
    )
    assert result.passed is False
    assert any(
        f.reason == "metadata_coverage_insufficient" for f in result.failures
    )


# ── Liveness gate: dark quarterly filer excluded, not flagged ────────


async def test_dark_quarterly_filer_excluded_not_flagged() -> None:
    """A 10-Q filer silent past LIVE_WITHIN_DAYS_QUARTERLY (120d) is dark
    and excluded BEFORE the set-difference — even if SEC filed a date
    fundamentals lacks, a dark issuer is not gap-flagged."""
    last = _TODAY - timedelta(days=LIVE_WITHIN_DAYS_QUARTERLY + 60)
    rd = _quarter_ends(last - timedelta(days=300), 3)
    have = rd[:-1]  # missing the last → would be a gap if not dark
    iss = _Issuer(
        "DEAD", cid="c-dead", cik="0000444444", primary="10-Q",
        sec_report_dates=rd, have=have,
    )
    result = await check_fundamentals_quarterly_completeness(
        _Pool([iss, *_clean_padding(3)])
    )
    assert [f for f in result.failures if f.ticker == "DEAD"] == []


# ── Annual liveness gate is wider ────────────────────────────────────


async def test_annual_liveness_gate_is_wider() -> None:
    """A 20-F filer 200 days past their last filing is NOT dark
    (LIVE_WITHIN_DAYS_ANNUAL=540 covers it) and PASSES when fundamentals
    has every SEC-filed annual reportDate."""
    rd = [
        _TODAY - timedelta(days=560),
        _TODAY - timedelta(days=200),
    ]
    iss = _Issuer(
        "AER", cid="c-aer", cik="0000555555", primary="20-F",
        sec_report_dates=rd, have=rd,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True, [f.observed for f in result.failures]


# ── 20-F / 40-F annual routing passes ────────────────────────────────


async def test_20f_annual_routing_passes() -> None:
    rd = [_TODAY - timedelta(days=730), _TODAY - timedelta(days=365),
          _TODAY - timedelta(days=20)]
    iss = _Issuer(
        "ARCO", cid="c-arco", cik="0000666666", primary="20-F",
        sec_report_dates=rd, have=rd,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True


async def test_40f_annual_routing_passes() -> None:
    rd = [_TODAY - timedelta(days=730), _TODAY - timedelta(days=365),
          _TODAY - timedelta(days=20)]
    iss = _Issuer(
        "ASTL", cid="c-astl", cik="0000777777", primary="40-F",
        sec_report_dates=rd, have=rd,
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is True


# ── METADATA_REQUIRED: NULL primary form, no per-ticker fail ─────────


async def test_null_primary_form_metadata_required_not_failed() -> None:
    null_form = [
        _Issuer(
            f"NOROUTE{i}", cid=f"c-nr-{i}", cik=f"00{i:05d}",
            primary=None, sec_report_dates=[], have=[],
        )
        for i in range(4)
    ]
    result = await check_fundamentals_quarterly_completeness(
        _Pool([*null_form, *_clean_padding(1)])
    )
    # No NULL-primary issuer is a per-ticker failure.
    assert [f for f in result.failures
            if f.ticker.startswith("NOROUTE")] == []
    # 80% metadata-required → sentinel fires.
    assert any(
        f.reason == "metadata_coverage_insufficient" for f in result.failures
    )


# ── OTHER_FORM: non-routed form excluded silently ────────────────────


async def test_other_form_excluded_silently() -> None:
    """A closed-end fund (N-1A primary) is non-routed → OTHER_FORM
    bucket; not metadata-required, not a per-ticker fail."""
    fund = _Issuer(
        "CEFUND", cid="c-cefund", cik="0000888888", primary="N-1A",
        sec_report_dates=[], have=[],
    )
    result = await check_fundamentals_quarterly_completeness(
        _Pool([fund, *_clean_padding(3)])
    )
    assert result.passed is True, [f.observed for f in result.failures]
    assert [f for f in result.failures if f.ticker == "CEFUND"] == []


# ── Lifecycle-terminated excluded before cadence ─────────────────────


async def test_lifecycle_terminated_excluded() -> None:
    iss = _Issuer(
        "GONE", cid="c-gone", cik="0000999999", primary="10-Q",
        sec_report_dates=_quarter_ends(_TODAY - timedelta(days=400), 4),
        have=[],  # would be a huge gap, but terminated → excluded
        lifecycle_state="deregistered",
    )
    result = await check_fundamentals_quarterly_completeness(
        _Pool([iss, *_clean_padding(3)])
    )
    assert [f for f in result.failures if f.ticker == "GONE"] == []


# ── Confirmed-data-gap evidence routes the period out of the gap ─────


class _EvidenceConn(_Conn):
    """Variant that returns evidence for a specific (ticker, period)."""

    def __init__(self, issuers: list[_Issuer], evidenced: set[date]) -> None:
        super().__init__(issuers)
        self._evidenced = evidenced

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "confirmed_data_gap_evidence" in sql:
            requested = set(args[1])
            return [
                {"period_end_date": d}
                for d in self._evidenced & requested
            ]
        return await super().fetch(sql, *args)


class _EvidencePool(_Pool):
    def __init__(self, issuers: list[_Issuer], evidenced: set[date]) -> None:
        super().__init__(issuers)
        self._evidenced = evidenced

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_EvidenceConn(self._issuers, self._evidenced))


async def test_dual_source_evidence_excludes_period_from_gap() -> None:
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    missing = rd[1]
    have = [d for d in rd if d != missing]
    iss = _Issuer(
        "EVID", cid="c-evid", cik="0001010101", primary="10-Q",
        sec_report_dates=rd, have=have,
    )
    # With dual-source evidence for the missing period, it routes to
    # excluded_confirmed_data_gap → ticker PASSES.
    pool = _EvidencePool([iss], {missing})
    result = await check_fundamentals_quarterly_completeness(pool)
    assert [f for f in result.failures if f.ticker == "EVID"] == []


# ── Healer parity: targets match the check's gaps ────────────────────


async def test_healer_symmetry_with_check() -> None:
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    missing = rd[1]
    have = [d for d in rd if d != missing]
    iss = _Issuer(
        "AAPL", cid="c-aapl", cik="0000320193", primary="10-Q",
        sec_report_dates=rd, have=have,
    )
    pool = _Pool([iss])
    result = await check_fundamentals_quarterly_completeness(pool)
    targets, lookback = await compute_fundamentals_repair_targets(pool)
    assert result.passed is False
    assert targets == ["AAPL"]
    assert lookback > 0


async def test_clean_state_returns_empty_targets() -> None:
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    iss = _Issuer(
        "AAPL", cid="c-aapl", cik="0000320193", primary="10-Q",
        sec_report_dates=rd, have=rd,
    )
    targets, lookback = await compute_fundamentals_repair_targets(_Pool([iss]))
    assert targets == []
    assert lookback == 0


# ── Empty universe → sentinel, no targets ────────────────────────────


async def test_empty_universe_returns_sentinel_no_targets() -> None:
    pool = _Pool([])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert result.failures[0].reason == "empty_liquid_universe"
    targets, lookback = await compute_fundamentals_repair_targets(pool)
    assert targets == []
    assert lookback == 0


# ── check / healer universe parity ───────────────────────────────────


def test_check_and_healer_select_same_universe_predicate() -> None:
    """The check's _FILING_DATES_SQL and the healer's universe SQL must
    select the SAME classification_ids: both anchor on tier≤2 active
    ticker_classifications routed by sec_document_type_primary. Assert
    the structural predicates that define that set match."""
    import inspect

    from tpcore.ingestion import handlers
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (  # noqa: E501
        _FILING_DATES_SQL as check_sql,
    )

    # The healer's universe SQL is inlined in the handler; isolate the
    # SQL literal (the triple-quoted block) so prose comments don't count
    # toward the predicate assertions. A regression here means the check
    # and healer universe SELECTs drifted.
    full_src = inspect.getsource(handlers.handle_sec_fundamentals_fallback)
    # The universe SQL is the first triple-quoted block containing
    # ``liquidity_tiers``.
    blocks = full_src.split('"""')
    healer_sql = next(b for b in blocks if "liquidity_tiers" in b)

    # Both route on sec_document_type_primary (NOT asset_class='stock').
    assert "sec_document_type_primary" in check_sql
    assert "sec_document_type_primary" in healer_sql
    assert "asset_class = 'stock'" not in healer_sql
    # Both carry classification_id (tc.id) for the shared store helper.
    assert "tc.id AS classification_id" in healer_sql
    assert "classification_id" in check_sql
    # Both gate on active lifetime + tier ceiling.
    assert "lifetime_end" in check_sql and "lifetime_end" in healer_sql
    assert "lt.tier" in check_sql and "lt.tier" in healer_sql


# ── Routing helpers ──────────────────────────────────────────────────


def test_cadence_for_routes_base_forms() -> None:
    assert _cadence_for("10-Q")[0] == "quarterly"
    assert _cadence_for("10-Q/A")[0] == "quarterly"  # /A collapses
    assert _cadence_for("10-K")[0] == "annual"
    assert _cadence_for("20-F")[0] == "annual"
    assert _cadence_for("40-F")[0] == "annual"
    assert _cadence_for("20-F/A")[0] == "annual"
    assert _cadence_for("N-1A") is None
    assert _cadence_for(None) is None


def test_healer_cadence_helper_matches_check() -> None:
    from tpcore.ingestion.handlers import _cadence_for_primary

    assert _cadence_for_primary("10-Q") == "quarterly"
    assert _cadence_for_primary("10-Q/A") == "quarterly"
    assert _cadence_for_primary("10-K") == "annual"
    assert _cadence_for_primary("20-F") == "annual"
    assert _cadence_for_primary("40-F") == "annual"
    assert _cadence_for_primary("N-1A") is None
    assert _cadence_for_primary(None) is None


def test_max_gap_constants_retained() -> None:
    # Retained for liveness/reporting; the gap math is now the SEC
    # set-difference, not a day-gap cap.
    assert MAX_QUARTERLY_GAP_DAYS == 100
    assert MAX_ANNUAL_GAP_DAYS == 450


def test_live_within_days_constants_per_cadence() -> None:
    assert LIVE_WITHIN_DAYS_QUARTERLY == 120
    assert LIVE_WITHIN_DAYS_ANNUAL == 540


# ── CHANGE 1: ≥2016 horizon scoping (evidenced) ──────────────────────


async def test_pre_horizon_reportdate_excluded_not_failing() -> None:
    """A pre-horizon (pre-2016) SEC reportDate missing from fundamentals is
    EXCLUDED-WITH-EVIDENCE (bucketed in excluded_pre_horizon, logged) — NOT
    a per-ticker FAIL. The issuer's only post-horizon period is present, so
    the ticker PASSES."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    recent = _TODAY - timedelta(days=30)
    pre = date(2014, 3, 31)  # SEC filed it; fundamentals lacks it
    iss = _Issuer(
        "OLDCO", cid="c-oldco", cik="0000320193", primary="10-Q",
        sec_report_dates=[pre, recent], have=[recent], asset_class="stock",
    )
    pool = _Pool([iss, *_clean_padding(3)])
    result = await check_fundamentals_quarterly_completeness(pool)
    # Pre-2016 gap is NOT a per-ticker failure.
    assert [f for f in result.failures if f.ticker == "OLDCO"] == []
    assert result.passed is True, [f.observed for f in result.failures]
    # The exclusion is SURFACED (bucketed), not silently dropped.
    ev = await _evaluate(pool)
    assert ev.excluded_pre_horizon >= 1


async def test_on_or_after_horizon_missing_still_fails() -> None:
    """A reportDate ON/AFTER the horizon that is missing STILL FAILS — the
    horizon never masks a recent gap. A pre-2016 reportDate in the same
    issuer is excluded (bucketed) while the ≥2016 one fails (named)."""
    pre = date(2015, 6, 30)
    recent_missing = _TODAY - timedelta(days=120)
    recent_have = _TODAY - timedelta(days=30)
    iss = _Issuer(
        "MIXCO", cid="c-mixco", cik="0000320193", primary="10-Q",
        sec_report_dates=[pre, recent_missing, recent_have],
        have=[recent_have], asset_class="stock",
    )
    result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
    assert result.passed is False
    mix = [f for f in result.failures if f.ticker == "MIXCO"]
    assert len(mix) == 1
    # The ≥2016 missing reportDate is NAMED; the pre-2016 one is NOT.
    assert recent_missing.isoformat() in mix[0].observed
    assert pre.isoformat() not in mix[0].observed
    # Only ONE (the post-horizon) reportDate is reported missing.
    assert "1 SEC-filed reportDate(s) missing" in mix[0].observed


async def test_horizon_env_override_widens_scope() -> None:
    """The STE_FUNDAMENTALS_HORIZON env override is honoured at evaluation
    time. Setting it earlier than a reportDate that the default would
    exclude makes that reportDate in-scope (and therefore FAIL)."""
    import os

    pre = date(2014, 3, 31)
    recent = _TODAY - timedelta(days=30)
    iss = _Issuer(
        "ENVCO", cid="c-envco", cik="0000320193", primary="10-Q",
        sec_report_dates=[pre, recent], have=[recent], asset_class="stock",
    )
    old = os.environ.get("STE_FUNDAMENTALS_HORIZON")
    try:
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "2010-01-01"
        result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
        # With the horizon pushed back to 2010, the 2014 reportDate is now
        # in-scope and missing → FAIL.
        assert result.passed is False
        envco = [f for f in result.failures if f.ticker == "ENVCO"]
        assert len(envco) == 1
        assert pre.isoformat() in envco[0].observed
    finally:
        if old is None:
            os.environ.pop("STE_FUNDAMENTALS_HORIZON", None)
        else:
            os.environ["STE_FUNDAMENTALS_HORIZON"] = old


def test_horizon_default_and_bad_env_falls_back() -> None:
    """The default horizon is 2016-01-01; a malformed env override falls back
    to the default (fail-safe — a bad env var must not silently move the
    safety gate)."""
    import os

    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT as _DEFAULT,
    )
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _resolve_horizon,
    )
    assert _DEFAULT == date(2016, 1, 1)
    old = os.environ.get("STE_FUNDAMENTALS_HORIZON")
    try:
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "not-a-date"
        assert _resolve_horizon() == _DEFAULT
        # A BACKWARD (earlier-than-default) override widens the gate and is
        # honoured. (A forward override is rejected — see
        # test_resolve_horizon_rejects_forward_override_keeps_default.)
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "2012-07-01"
        assert _resolve_horizon() == date(2012, 7, 1)
    finally:
        if old is None:
            os.environ.pop("STE_FUNDAMENTALS_HORIZON", None)
        else:
            os.environ["STE_FUNDAMENTALS_HORIZON"] = old


# ── CHANGE 2: non-operating-entity routing refinement ────────────────


async def test_non_operating_etf_anchored_false_excluded_non_filer() -> None:
    """An anchored=False ETF (asset_class='etf') has NO 10-Q obligation →
    routes to excluded_non_filer (excluded-WITH-evidence), NOT
    metadata_required (so it does not inflate the coverage sentinel)."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    # 4 CIK-backed ETFs that would have inflated metadata_required pre-change.
    etfs = [
        _Issuer(
            f"ETF{i}", cid=f"c-etf-{i}", cik=f"00{i:05d}", primary="10-Q",
            sec_report_dates=[], have=[], asset_class="etf",
        )
        for i in range(4)
    ]
    pool = _Pool([*etfs, *_clean_padding(1)])
    ev = await _evaluate(pool)
    assert ev.excluded_non_filer == 4
    assert ev.excluded_metadata_required == 0
    result = await check_fundamentals_quarterly_completeness(pool)
    # No ETF is a per-ticker fail, AND the coverage sentinel does NOT fire
    # (the funds were the only would-be metadata_required inflators).
    assert [f for f in result.failures if f.ticker.startswith("ETF")] == []
    assert [
        f for f in result.failures
        if f.reason == "metadata_coverage_insufficient"
    ] == []
    assert result.passed is True, [f.observed for f in result.failures]


async def test_non_operating_fund_and_etn_excluded_non_filer() -> None:
    """asset_class ∈ {fund, etn} also route to excluded_non_filer."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    nonop = [
        _Issuer("AFUND", cid="c-afund", cik="0000111111", primary="10-Q",
                sec_report_dates=[], have=[], asset_class="fund"),
        _Issuer("AETN", cid="c-aetn", cik="0000222222", primary="10-Q",
                sec_report_dates=[], have=[], asset_class="etn"),
    ]
    ev = await _evaluate(_Pool([*nonop, *_clean_padding(2)]))
    assert ev.excluded_non_filer == 2
    assert ev.excluded_metadata_required == 0


async def test_operating_stock_anchored_false_stays_metadata_required() -> None:
    """An anchored=False OPERATING company (asset_class='stock') that lacks
    metadata STAYS metadata_required — a REAL coverage gap that MUST remain
    sentinel-visible (NOT excluded as non-filer)."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    stocks = [
        _Issuer(
            f"STK{i}", cid=f"c-stk-{i}", cik=f"00{i:05d}", primary="10-Q",
            sec_report_dates=[], have=[], asset_class="stock",
        )
        for i in range(4)
    ]
    pool = _Pool([*stocks, *_clean_padding(1)])
    ev = await _evaluate(pool)
    assert ev.excluded_metadata_required == 4
    assert ev.excluded_non_filer == 0
    result = await check_fundamentals_quarterly_completeness(pool)
    # 4/(4+1) = 80% > 25% ⇒ the coverage sentinel MUST fire (real gap).
    assert any(
        f.reason == "metadata_coverage_insufficient" for f in result.failures
    )


async def test_operating_reit_anchored_false_stays_metadata_required() -> None:
    """A REIT (asset_class='reit') is an OPERATING filer (10-K obligation) —
    anchored=False ⇒ metadata_required, NOT excluded_non_filer."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    reit = _Issuer(
        "AREIT", cid="c-areit", cik="0000333333", primary="10-K",
        sec_report_dates=[], have=[], asset_class="reit",
    )
    ev = await _evaluate(_Pool([reit, *_clean_padding(3)]))
    assert ev.excluded_metadata_required == 1
    assert ev.excluded_non_filer == 0


async def test_null_asset_class_fails_closed_to_metadata_required() -> None:
    """A NULL asset_class is AMBIGUOUS — fail-closed: it is NOT classified
    non-operating; it stays metadata_required (sentinel-visible)."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    null_ac = [
        _Issuer(
            f"NULLAC{i}", cid=f"c-nullac-{i}", cik=f"00{i:05d}", primary="10-Q",
            sec_report_dates=[], have=[], asset_class=None,
        )
        for i in range(4)
    ]
    pool = _Pool([*null_ac, *_clean_padding(1)])
    ev = await _evaluate(pool)
    assert ev.excluded_metadata_required == 4
    assert ev.excluded_non_filer == 0
    result = await check_fundamentals_quarterly_completeness(pool)
    assert any(
        f.reason == "metadata_coverage_insufficient" for f in result.failures
    )


async def test_spac_anchored_false_not_excluded_non_filer() -> None:
    """A SPAC (asset_class='spac') is NOT in the non-operating set: a
    CIK-backed un-anchored SPAC stays metadata_required (sentinel-visible),
    a CIK-less one stays confirmed_data_gap — neither is excluded_non_filer.
    Report-how-SPACs-land: they route by their normal CIK-based path."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    spac_cik = _Issuer(
        "SPACA", cid="c-spaca", cik="0000444444", primary="10-Q",
        sec_report_dates=[], have=[], asset_class="spac",
    )
    spac_cikless = _Issuer(
        "SPACB", cid="c-spacb", cik=None, primary="10-Q",
        sec_report_dates=[], have=[], asset_class="spac",
    )
    ev = await _evaluate(_Pool([spac_cik, spac_cikless, *_clean_padding(3)]))
    assert ev.excluded_non_filer == 0
    assert ev.excluded_metadata_required == 1  # the CIK-backed SPAC
    assert ev.excluded_confirmed_data_gap == 1  # the CIK-less SPAC


async def test_non_operating_routing_precedes_cik_classification() -> None:
    """A CIK-backed ETF lands in excluded_non_filer (NOT metadata_required)
    AND a CIK-less ETF also lands in excluded_non_filer (NOT
    confirmed_data_gap) — the non-operating evidence is dispositive
    regardless of CIK presence."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    etf_cik = _Issuer(
        "ETFA", cid="c-etfa", cik="0000555555", primary="10-Q",
        sec_report_dates=[], have=[], asset_class="etf",
    )
    etf_cikless = _Issuer(
        "ETFB", cid="c-etfb", cik=None, primary="10-Q",
        sec_report_dates=[], have=[], asset_class="etf",
    )
    ev = await _evaluate(_Pool([etf_cik, etf_cikless, *_clean_padding(2)]))
    assert ev.excluded_non_filer == 2
    assert ev.excluded_metadata_required == 0
    assert ev.excluded_confirmed_data_gap == 0


# ── FAKE-GREEN FIX #1 — non_filer exclusion must be ANCHORED-gated ────
#
# An anchored=True issuer whose asset_class label is non-operating (etf /
# etn / fund) is a misclassified OPERATING filer (asset_class is
# OpenFIGI-derived, NOT authoritative). It MUST be evaluated for gaps — the
# pre-fix code short-circuited it to excluded_non_filer in Pass 1, masking a
# real ≥2016 gap before evidence was consulted. These tests FAIL pre-fix
# (the ticker is excluded, no failure, no contradiction counter) and PASS
# post-fix.


async def test_anchored_etf_with_post_horizon_gap_FAILS_not_non_filer() -> None:
    """A misclassified OPERATING filer (asset_class='etf' but anchored=True
    with real 10-Q filings AND a real ≥2016 missing reportDate) must FAIL on
    that ticker — NOT be masked as excluded_non_filer — and the
    ``non_operating_anchored_contradiction`` counter must increment."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    rd = _quarter_ends(_TODAY - timedelta(days=400), 4)
    rd = [d for d in rd if d <= _TODAY]
    missing = rd[1]  # SEC filed it; fundamentals lacks it; ≥2016
    have = [d for d in rd if d != missing]
    # asset_class wrongly 'etf', but it FILES 10-Qs (anchored=True via
    # sec_report_dates) — an identity contradiction that must SURFACE.
    iss = _Issuer(
        "FAKEETF", cid="c-fakeetf", cik="0000320193", primary="10-Q",
        sec_report_dates=rd, have=have, asset_class="etf",
    )
    pool = _Pool([iss])
    ev = await _evaluate(pool)
    # The contradiction is COUNTED (operator-visible), not masked.
    assert ev.non_operating_anchored_contradiction == 1
    # It was evaluated normally (in the denominator), NOT non_filer.
    assert ev.excluded_non_filer == 0
    assert ev.evaluated_routed == 1
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    fake = [f for f in result.failures if f.ticker == "FAKEETF"]
    assert len(fake) == 1
    assert fake[0].reason == "missing_period_10-Q"
    assert missing.isoformat() in fake[0].observed


async def test_anchored_false_etf_still_excluded_non_filer_unchanged() -> None:
    """REGRESSION KEEPER: an anchored=False ETF (no real filings) still
    routes to excluded_non_filer — the anchored-gate only changes the
    anchored=True contradiction case, not the genuine non-filer case."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    iss = _Issuer(
        "REALETF", cid="c-realetf", cik="0000999000", primary="10-Q",
        sec_report_dates=[], have=[], asset_class="etf",  # anchored=False
    )
    ev = await _evaluate(_Pool([iss, *_clean_padding(2)]))
    assert ev.excluded_non_filer == 1
    assert ev.non_operating_anchored_contradiction == 0
    assert ev.excluded_metadata_required == 0


# ── FAKE-GREEN FIX #2 — zero-anchored guard counts ALL exclusions ────
#
# A universe whose only routed entities are all excluded_non_filer
# (evaluated_routed==0, NO anchored green evidence) read GREEN pre-fix
# because the guard only summed metadata_required + confirmed_data_gap.
# Post-fix the guard sums ALL exclusion buckets → structural FAIL.


async def test_zero_anchored_all_non_filer_fails() -> None:
    """A universe that is ENTIRELY excluded_non_filer (every routed issuer is
    an anchored=False ETF) has zero anchored green evidence and MUST FAIL the
    zero-anchored structural guard. Pre-fix this read GREEN (non_filer was
    omitted from the guard's sum)."""
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _evaluate,
    )
    etfs = [
        _Issuer(
            f"ONLYETF{i}", cid=f"c-onlyetf-{i}", cik=f"00{i:05d}",
            primary="10-Q", sec_report_dates=[], have=[], asset_class="etf",
        )
        for i in range(3)
    ]
    pool = _Pool(etfs)
    ev = await _evaluate(pool)
    assert ev.evaluated_routed == 0
    assert ev.excluded_non_filer == 3
    assert ev.zero_anchored_with_exclusions is True
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False, [f.observed for f in result.failures]
    sentinel = [
        f for f in result.failures if f.reason == "zero_anchored_universe"
    ]
    assert len(sentinel) == 1
    assert "non_filer=3" in sentinel[0].observed


async def test_zero_anchored_guard_sums_pre_horizon_bucket() -> None:
    """The zero-anchored guard's exclusion sum INCLUDES excluded_pre_horizon
    (and every other bucket). Because pre_horizon can only accrue on an
    anchored=True issuer (which lands in evaluated_routed) the all-pre_horizon
    zero-anchored case is unreachable via real data — so this test injects a
    constructed _Evaluation with ONLY excluded_pre_horizon set and asserts the
    check function still FAILs on the structural guard. Pre-fix the guard
    summed only metadata_required + confirmed_data_gap, so a pre_horizon-only
    flag would not have produced the sentinel."""
    from unittest.mock import patch

    from tpcore.quality.validation.checks import (
        fundamentals_quarterly_completeness as mod,
    )
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _Evaluation,
    )
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        check_fundamentals_quarterly_completeness as _check,
    )

    ev = _Evaluation(
        sentinel=None,
        evaluated_routed=0,
        excluded_dark=0,
        excluded_metadata_required=0,
        excluded_confirmed_data_gap=0,
        excluded_other_form=0,
        excluded_pre_horizon=2,
        zero_anchored_with_exclusions=True,
    )

    async def _fake_eval(_pool: Any) -> Any:
        return ev

    with patch.object(mod, "_evaluate", _fake_eval):
        result = await _check(_Pool([]))
    assert result.passed is False
    sentinel = [
        f for f in result.failures if f.reason == "zero_anchored_universe"
    ]
    assert len(sentinel) == 1
    assert "pre_horizon=2" in sentinel[0].observed


# ── FAKE-GREEN FIX #3 — horizon override may only WIDEN, never narrow ─
#
# A forward (later-than-default) STE_FUNDAMENTALS_HORIZON silently shrank the
# failing set pre-fix. Post-fix a later override is REJECTED (ignored, warned)
# and the default is used; an earlier override still WIDENS (honoured).


async def test_forward_horizon_override_rejected_does_not_shrink_failing_set() -> None:
    """STE_FUNDAMENTALS_HORIZON set FORWARD (later than the 2016 default) is
    IGNORED — the horizon stays at the default, so a 2018 gap that the default
    catches STILL FAILS. Pre-fix the forward override would have moved the
    horizon to 2025 and silently dropped the 2018 gap (fake green)."""
    import os

    pre_default_safe = date(2018, 3, 31)  # ≥ default(2016), < forward override
    recent = _TODAY - timedelta(days=30)
    iss = _Issuer(
        "FWDCO", cid="c-fwdco", cik="0000320193", primary="10-Q",
        sec_report_dates=[pre_default_safe, recent], have=[recent],
        asset_class="stock",
    )
    old = os.environ.get("STE_FUNDAMENTALS_HORIZON")
    try:
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "2025-01-01"  # FORWARD
        result = await check_fundamentals_quarterly_completeness(_Pool([iss]))
        # The 2018 gap is still in scope (override rejected) → FAIL.
        assert result.passed is False, [f.observed for f in result.failures]
        fwd = [f for f in result.failures if f.ticker == "FWDCO"]
        assert len(fwd) == 1
        assert pre_default_safe.isoformat() in fwd[0].observed
    finally:
        if old is None:
            os.environ.pop("STE_FUNDAMENTALS_HORIZON", None)
        else:
            os.environ["STE_FUNDAMENTALS_HORIZON"] = old


def test_resolve_horizon_rejects_forward_override_keeps_default() -> None:
    """``_resolve_horizon`` returns the DEFAULT (not the override) when the
    override is later than the default (narrowing is forbidden); a backward
    override is honoured (widening is safe)."""
    import os

    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT as _DEFAULT,
    )
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        _resolve_horizon,
    )
    old = os.environ.get("STE_FUNDAMENTALS_HORIZON")
    try:
        # Forward (narrowing) → REJECTED, default used.
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "2025-01-01"
        assert _resolve_horizon() == _DEFAULT
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "2020-06-30"
        assert _resolve_horizon() == _DEFAULT
        # Backward (widening) → honoured.
        os.environ["STE_FUNDAMENTALS_HORIZON"] = "2010-01-01"
        assert _resolve_horizon() == date(2010, 1, 1)
    finally:
        if old is None:
            os.environ.pop("STE_FUNDAMENTALS_HORIZON", None)
        else:
            os.environ["STE_FUNDAMENTALS_HORIZON"] = old
