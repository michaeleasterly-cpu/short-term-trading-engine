"""`excluded_confirmed_data_gap` validator-semantics — hermetic tests.

Per spec PR #450 + plan PR #451. The validator
``fundamentals_quarterly_completeness`` extends its existing
``excluded_confirmed_data_gap`` bucket to also cover
**dual-source-evidenced** period-level unavailability. These tests
pin the load-bearing wiring:

  1. ARDT watchlist override forces ``excluded_dark`` even when the
     dual-source evidence would otherwise qualify.
  2. Evidence join routes dual-source-evidenced periods to
     ``excluded_confirmed_data_gap_evidenced`` + decrements the
     ticker's gap list.
  3. Freshness gate (180 days) — evidence > 180 days old stays in FAIL.
  4. Fetch-failure rejection — fetch_failure in window stays in FAIL.
  5. AEVA-shape (SEC yielded) — does NOT exclude; ticker still FAILs
     on the affected period until the actual row lands.
  6. Table-missing graceful skip (post-rollback) — evidence join is
     bypassed; bucket's narrow semantic continues to fire.
  7. CheckResult frozen-shape sentinel — no new fields on CheckResult.
  8. ``_infer_missing_period_ends`` byte-freeze sentinel — the
     inference function source is sha256-pinned.
  9. ``_FILING_DATES_SQL`` byte-freeze sentinel — pinned to the P1
     hash (no validator semantics change at the universe SQL).

Stdlib + ``unittest.mock`` only. No DB, no network.
"""
from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.quality.validation.checks import (
    fundamentals_quarterly_completeness as fqc,
)
from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
    _evaluate,
    check_fundamentals_quarterly_completeness,
)
from tpcore.quality.validation.models import CheckResult

# ──────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — dispatches reads on SQL substring + arg shape.
# ──────────────────────────────────────────────────────────────────────


def _make_pool(
    *,
    filing_rows: list[dict],
    evidence_rows_by_ticker: dict[str, list[dict]] | None = None,
) -> MagicMock:
    """Build an asyncpg-pool stub.

    First fetch: the universe SQL (matches by substring of
    ``platform.liquidity_tiers``).
    Subsequent fetches against the confirmed-data-gap evidence (Plan 2:
    ``platform.data_quality_log`` with ``kind='confirmed_data_gap_evidence'``)
    return rows keyed by ticker. The old ``to_regclass`` existence probe is gone
    — the dql table always exists, so the evidence join always runs.
    """
    evidence_by_t = evidence_rows_by_ticker or {}

    # P3: derive the store's anchored / expected / have row sets from the
    # universe ``filing_rows`` (which now carry classification_id +
    # ``_sec_report_dates``). A real set-difference gap is what the
    # evidence join then routes.
    by_cid: dict[str, dict[str, Any]] = {}
    for fr in filing_rows:
        cid = fr.get("classification_id")
        if cid is None:
            continue
        rec = by_cid.setdefault(
            cid, {"ticker": fr["ticker"], "sec": set(), "have": set()},
        )
        if fr.get("period_end_date") is not None:
            rec["have"].add(fr["period_end_date"])
        for rd in fr.get("_sec_report_dates", ()):
            rec["sec"].add(rd)

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "WITH liquid AS" in sql:
            return filing_rows
        if "SELECT DISTINCT classification_id" in sql:
            wanted = set(args[0])
            return [
                {"classification_id": cid}
                for cid in wanted if by_cid.get(cid, {}).get("sec")
            ]
        if "FROM platform.sec_periodic_filings" in sql:
            wanted = set(args[0])
            out: list[dict[str, Any]] = []
            for cid in wanted:
                for rd in by_cid.get(cid, {}).get("sec", ()):
                    out.append({"classification_id": cid, "report_date": rd})
            return out
        if ("FROM platform.fundamentals_quarterly" in sql
                and "classification_id = ANY" in sql):
            wanted = set(args[0])
            out = []
            for cid in wanted:
                for pe in by_cid.get(cid, {}).get("have", ()):
                    out.append(
                        {"classification_id": cid, "period_end_date": pe}
                    )
            return out
        if "confirmed_data_gap_evidence" in sql:
            ticker = args[0]
            return evidence_by_t.get(ticker, [])
        return []

    async def _fetchval(sql: str, *args: Any) -> Any:
        return None

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _today() -> date:
    return datetime.now(UTC).date()


def _quarterly_filings(
    ticker: str, anchor_a: date, anchor_b: date,
) -> list[dict]:
    """P3 set-difference shape: two PRESENT filings (anchor_a, anchor_b)
    in fundamentals, plus SEC-filed reportDates that INCLUDE intermediate
    periods the fundamentals lacks → a genuine set-difference gap the
    evidence join can then route.

    The intermediate missing reportDates are the dates the evidence-join
    tests reference (today-200 / today-300 / today-400), restricted to
    those strictly between the anchors. When the anchors are close (e.g.
    AEVA's 90/10-day pair) no intermediate date qualifies → no gap →
    natural PASS, exactly the old behavior these tests pinned."""
    today = _today()
    candidate_missing = [
        today - timedelta(days=200),
        today - timedelta(days=300),
        today - timedelta(days=400),
    ]
    lo, hi = min(anchor_a, anchor_b), max(anchor_a, anchor_b)
    missing = [d for d in candidate_missing if lo < d < hi]
    sec_dates = [anchor_a, anchor_b, *missing]
    cid = f"c-{ticker}"
    return [
        {
            "ticker": ticker,
            "classification_id": cid,
            "cik": "0001",
            "period_end_date": pe,
            "sec_document_type_primary": "10-Q",
            "issuer_lifecycle_state": None,
            "issuer_lifecycle_event_date": None,
            "_sec_report_dates": sec_dates,
        }
        for pe in (anchor_a, anchor_b)
    ]


# ──────────────────────────────────────────────────────────────────────
# Test 1 — ARDT watchlist override
# ──────────────────────────────────────────────────────────────────────


async def test_ardt_watchlist_forces_excluded_dark() -> None:
    today = _today()
    # ARDT has 2 quarterly filings spanning a >100-day gap. Last filed
    # is recent so not "dark" by liveness. Without the ARDT override
    # the ticker would FAIL on the inferred gap (which the evidence
    # join could then turn into `excluded_confirmed_data_gap_evidenced`).
    # WITH the override, ARDT routes to `excluded_dark` instead.
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "ARDT",
            today - timedelta(days=500),
            today - timedelta(days=10),
        ),
    )
    ev = await _evaluate(pool)
    assert ev.excluded_dark == 1, (
        f"ARDT must route to excluded_dark; got {ev}"
    )
    # Should NOT appear in routed-eligible (we decremented after
    # the routing was assigned).
    assert ev.evaluated_routed == 0
    # ARDT must NOT have any gap rows.
    assert "ARDT" not in ev.gaps


# ──────────────────────────────────────────────────────────────────────
# Test 2 — Evidence join routes dual-source-empty periods to bucket
# ──────────────────────────────────────────────────────────────────────


async def test_evidence_join_routes_dual_source_empty_to_bucket() -> None:
    today = _today()
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "AAA",
            today - timedelta(days=500),
            today - timedelta(days=10),
        ),
        evidence_rows_by_ticker={
            # All inferred missing periods qualify — the dispatcher
            # returns them all as evidenced.
            "AAA": [
                {"period_end_date": today - timedelta(days=200)},
                {"period_end_date": today - timedelta(days=300)},
                {"period_end_date": today - timedelta(days=400)},
            ],
        },
    )
    ev = await _evaluate(pool)
    # The mock returns "all inferred missing periods" as evidenced;
    # by construction this empties the gap list → AAA is NOT in gaps.
    assert ev.excluded_confirmed_data_gap_evidenced >= 1, (
        f"Evidenced sub-counter must increment; got {ev}"
    )
    assert ev.excluded_confirmed_data_gap >= 1, (
        "Parent counter must mirror evidenced when no sparse rows"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 3 — Freshness gate: stale evidence does NOT exclude
# ──────────────────────────────────────────────────────────────────────


async def test_freshness_gate_stale_evidence_stays_in_fail() -> None:
    """The mock simulates the freshness gate at the SQL level — when
    evidence is stale, the join returns 0 rows. This test asserts that
    when the evidence table returns nothing (mirroring stale rows),
    the ticker's inferred gaps stay in the FAIL list."""
    today = _today()
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "BBB",
            today - timedelta(days=500),
            today - timedelta(days=10),
        ),
        # Empty evidence rows for BBB → mimics 180-day filter excluding
        # stale rows at the SQL level.
        evidence_rows_by_ticker={"BBB": []},
    )
    ev = await _evaluate(pool)
    assert ev.excluded_confirmed_data_gap_evidenced == 0, (
        "stale evidence must NOT increment the evidenced sub-counter"
    )
    # BBB should remain in the gap list (the join returned nothing).
    assert "BBB" in ev.gaps, "BBB must remain in FAIL when evidence is stale"


# ──────────────────────────────────────────────────────────────────────
# Test 4 — Fetch-failure rejection: mirror of freshness gate
# ──────────────────────────────────────────────────────────────────────


async def test_fetch_failure_in_window_stays_in_fail() -> None:
    """The SQL HAVING clause explicitly excludes `fetch_failure` rows.
    With the mock returning empty (simulating the SQL filter), the
    ticker stays in FAIL — same surface as the stale-evidence test
    but the semantic distinction is documented per spec §4 #4."""
    today = _today()
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "CCC",
            today - timedelta(days=500),
            today - timedelta(days=10),
        ),
        evidence_rows_by_ticker={"CCC": []},  # SQL filter rejected fetch_failure
    )
    ev = await _evaluate(pool)
    assert ev.excluded_confirmed_data_gap_evidenced == 0
    assert "CCC" in ev.gaps


# ──────────────────────────────────────────────────────────────────────
# Test 5 — AEVA-shape: SEC yielded → does NOT exclude
# ──────────────────────────────────────────────────────────────────────


async def test_aeva_shape_sec_yielded_does_not_exclude() -> None:
    """SEC's `yielded` outcome means the row landed in fundamentals_quarterly
    — the validator's inferred gap should not exist anyway because the
    period IS there. Modeled here as: AEVA has all its inferred missing
    periods filled (no gaps) → AEVA passes naturally, no bucket increment."""
    today = _today()
    # AEVA's filings include the formerly-missing period (the "yielded"
    # path) → the cadence check sees no gap at all → no evidence join
    # fires for it. AEVA lands in evaluated_routed → PASS.
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "AEVA",
            today - timedelta(days=90),
            today - timedelta(days=10),
        ),
    )
    ev = await _evaluate(pool)
    assert "AEVA" not in ev.gaps, (
        "AEVA must have no gap to evaluate when the period is yielded"
    )
    assert ev.evaluated_routed == 1
    assert ev.excluded_confirmed_data_gap_evidenced == 0


# ──────────────────────────────────────────────────────────────────────
# Test 6 — Evidence join always runs (no existence gate)
# ──────────────────────────────────────────────────────────────────────


async def test_evidence_join_always_runs_no_existence_probe() -> None:
    """Plan 2: the standalone evidence table was dropped (migration 0300) and
    evidence now lives in the always-present ``platform.data_quality_log``
    (kind='confirmed_data_gap_evidence'). The old ``to_regclass`` existence
    probe is gone — the evidence join unconditionally runs. When evidence is
    present for an inferred gap, that period is excluded (decremented from the
    ticker's gap list); the evidenced sub-counter increments."""
    today = _today()
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "DDD",
            today - timedelta(days=500),
            today - timedelta(days=10),
        ),
        evidence_rows_by_ticker={
            "DDD": [
                {"period_end_date": today - timedelta(days=200)},
                {"period_end_date": today - timedelta(days=300)},
                {"period_end_date": today - timedelta(days=400)},
            ],
        },
    )
    ev = await _evaluate(pool)
    assert ev.excluded_confirmed_data_gap_evidenced >= 1, (
        "evidence join must run unconditionally and exclude evidenced periods"
    )
    # The fetchval existence probe is no longer called.
    pool.acquire.return_value.__aenter__.return_value.fetchval.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Test 7 — CheckResult frozen-shape sentinel
# ──────────────────────────────────────────────────────────────────────


def test_check_result_shape_unchanged() -> None:
    """`CheckResult` must remain frozen with no new fields added.
    Per spec §6.3 + §10 + plan §9, the new sub-counter lives on
    ``_Evaluation`` + structlog only — NOT on `CheckResult`."""
    fields = set(CheckResult.model_fields.keys())
    # Pin the existing surface explicitly. Any future widening here
    # MUST be a deliberate model-version bump, not a side effect of
    # the evidenced-counter wiring.
    expected = {
        "name", "passed", "total", "failed", "duration_ms", "failures",
    }
    assert fields == expected, (
        f"CheckResult shape drift: expected {expected}, got {fields}"
    )
    # Frozen invariant pinned.
    assert CheckResult.model_config.get("frozen") is True, (
        "CheckResult must stay frozen"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 8 — gap math delegates to the shared store (P3)
# ──────────────────────────────────────────────────────────────────────


def test_gap_math_delegates_to_shared_store() -> None:
    """P3: the validator no longer carries a private interpolation
    helper (``_infer_missing_period_ends`` was deleted); the gap math is
    the shared store's authoritative set-difference. Pin that the
    validator imports + uses ``compute_filing_gaps`` so detector/healer
    parity is real (one helper)."""
    assert not hasattr(fqc, "_infer_missing_period_ends"), (
        "the interpolation helper must be gone — the gap is now the SEC "
        "reportDate set-difference via the shared store"
    )
    src = inspect.getsource(fqc._evaluate)
    assert "compute_filing_gaps" in src, (
        "_evaluate must delegate the gap math to the shared store's "
        "compute_filing_gaps"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 9 — ``_FILING_DATES_SQL`` byte-freeze (mirror of P0 sentinel)
# ──────────────────────────────────────────────────────────────────────


def test_filing_dates_sql_byte_frozen_to_p3_hash() -> None:
    """Mirror the P0 sentinel — the P3 universe SQL is anchored on
    ticker_classifications + carries classification_id / cik. Pin the
    hash so the evidence-join extension can't silently drift it."""
    sha = hashlib.sha256(
        fqc._FILING_DATES_SQL.encode("utf-8"),
    ).hexdigest()
    assert sha == (
        # 2026-06-07: re-pinned for the non-operating-entity routing change
        # (added ``tc.asset_class`` to the universe SELECT). Deliberate.
        "db4cf04c78114439c621ca0179e3208c423bd9550dd3a526c2e0fcbde5c57be7"
    ), (
        "_FILING_DATES_SQL drifted from the P3 set-difference shape. "
        "Update this hash only if the universe SQL change is deliberate."
    )


# ──────────────────────────────────────────────────────────────────────
# Test 10 — Constants surface
# ──────────────────────────────────────────────────────────────────────


def test_freshness_constant_is_180() -> None:
    assert fqc.CONFIRMED_DATA_GAP_FRESHNESS_DAYS == 180


def test_ardt_watchlist_is_frozenset() -> None:
    assert isinstance(fqc.ARDT_WATCHLIST, frozenset)
    assert "ARDT" in fqc.ARDT_WATCHLIST


# ──────────────────────────────────────────────────────────────────────
# Test 11 — End-to-end check returns CheckResult unchanged-shape
# ──────────────────────────────────────────────────────────────────────


async def test_end_to_end_check_returns_check_result() -> None:
    today = _today()
    pool = _make_pool(
        filing_rows=_quarterly_filings(
            "EEE",
            today - timedelta(days=500),
            today - timedelta(days=10),
        ),
        evidence_rows_by_ticker={"EEE": []},
    )
    result = await check_fundamentals_quarterly_completeness(pool)
    assert isinstance(result, CheckResult)
    assert result.name == "fundamentals_quarterly_completeness"
    # No new fields on the result.
    assert set(result.model_dump().keys()) == {
        "name", "passed", "total", "failed", "duration_ms", "failures",
    }


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
