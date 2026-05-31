"""P2b — fundamentals_quarterly_completeness lifecycle-evidence wiring tests.

Hermetic tests of the new ``excluded_lifecycle_terminated`` bucket and
the evidence-first routing precedence (Form 25/15 evidence outranks
the silence-based ``excluded_dark`` heuristic).

Coverage matrix:
  TEST-P2B-A  ticker with state='deregistered' + cadence gap →
              excluded_lifecycle_terminated (NOT cadence FAIL)
  TEST-P2B-B  ticker with state='delist_effective' + cadence gap →
              excluded_lifecycle_terminated
  TEST-P2B-C  ticker with state='active' + cadence gap → cadence FAIL
              (no behavior change from P1)
  TEST-P2B-D  ticker with state=NULL + cadence gap → cadence FAIL
              (no behavior change from P1 — pre-backfill default)
  TEST-P2B-E  ticker silent > 120d with state=NULL → excluded_dark
              (heuristic survives when no evidence)
  TEST-P2B-F  ticker silent > 120d with state='deregistered' →
              excluded_lifecycle_terminated (evidence WINS over
              heuristic — operator-correct: a known-deregistered
              ticker is NOT just "dark", it's terminally ended)
  TEST-P2B-G  repair targets exclude lifecycle_terminated tickers
              (fundamentals_refresh would burn SEC budget on a
              ticker that will never file again)
  TEST-P2B-H  TERMINAL_LIFECYCLE_STATES surface check — frozenset
              {'deregistered', 'delist_effective'}; 'active' and
              'delist_pending' NOT terminal
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
    LIVE_WITHIN_DAYS_QUARTERLY,
    TERMINAL_LIFECYCLE_STATES,
    _evaluate,
    check_fundamentals_quarterly_completeness,
    compute_fundamentals_repair_targets,
)


def _today() -> date:
    return datetime.now(UTC).date()


def _mock_pool(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _row(
    ticker: str,
    period_end: date,
    primary: str | None = "10-Q",
    lifecycle_state: str | None = None,
    lifecycle_event_date: date | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "period_end_date": period_end,
        "sec_document_type_primary": primary,
        "issuer_lifecycle_state": lifecycle_state,
        "issuer_lifecycle_event_date": lifecycle_event_date,
    }


# ─── A. deregistered + gap → terminated, NOT FAIL ────────────────────


@pytest.mark.asyncio
async def test_p2b_a_deregistered_with_gap_routes_to_terminated() -> None:
    """A ticker with Form 15 evidence (state='deregistered') AND an
    obvious cadence gap MUST land in excluded_lifecycle_terminated,
    NOT in the failures list.

    Operator rationale: a deregistered issuer will never file again;
    demanding cadence from them is a category error."""
    today = _today()
    rows = [
        # ATVI-like — Microsoft acquired 2023-10-13, Form 15 2023-10-23
        # → terminal. Even with a gap, we don't fail it.
        _row("DEREG", today - timedelta(days=400), "10-Q",
             lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        _row("DEREG", today - timedelta(days=200), "10-Q",
             lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        _row("DEREG", today - timedelta(days=30), "10-Q",
             lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        # Fillers so metadata-coverage sentinel doesn't fire.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_lifecycle_terminated == 1
    # DEREG must NOT contribute a cadence-FAIL.
    assert "DEREG" not in ev.gaps
    result = await check_fundamentals_quarterly_completeness(pool)
    assert not any(f.ticker == "DEREG" for f in result.failures)


# ─── B. delist_effective + gap → terminated ──────────────────────────


@pytest.mark.asyncio
async def test_p2b_b_delist_effective_with_gap_routes_to_terminated() -> None:
    """Form 25 only (delist_effective — pre-deregistration) also
    routes to the terminated bucket."""
    today = _today()
    rows = [
        _row("DELIST", today - timedelta(days=300), "10-Q",
             lifecycle_state="delist_effective",
             lifecycle_event_date=today - timedelta(days=150)),
        _row("DELIST", today - timedelta(days=150), "10-Q",
             lifecycle_state="delist_effective",
             lifecycle_event_date=today - timedelta(days=150)),
        # Fillers.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_lifecycle_terminated == 1
    assert "DELIST" not in ev.gaps


# ─── C. active + gap → cadence FAIL (P1 unchanged) ───────────────────


@pytest.mark.asyncio
async def test_p2b_c_active_state_does_NOT_route_to_terminated() -> None:
    """state='active' is NOT in the terminal set — cadence routing
    fires normally (P1 behavior preserved)."""
    today = _today()
    rows = [
        # 250-day gap → quarterly miss.
        _row("LIVE", today - timedelta(days=400), "10-Q",
             lifecycle_state="active"),
        _row("LIVE", today - timedelta(days=150), "10-Q",
             lifecycle_state="active"),
        _row("LIVE", today - timedelta(days=30), "10-Q",
             lifecycle_state="active"),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_lifecycle_terminated == 0
    assert "LIVE" in ev.gaps  # cadence-FAIL — P1 behavior intact


# ─── D. NULL state + gap → cadence FAIL (P1 unchanged) ───────────────


@pytest.mark.asyncio
async def test_p2b_d_null_state_falls_through_to_cadence_routing() -> None:
    """state=NULL (the pre-backfill default — most of the universe today)
    has the P1 behavior: cadence routing decides FAIL vs PASS."""
    today = _today()
    rows = [
        _row("UNK", today - timedelta(days=400), "10-Q",
             lifecycle_state=None),
        _row("UNK", today - timedelta(days=150), "10-Q",
             lifecycle_state=None),
        _row("UNK", today - timedelta(days=30), "10-Q",
             lifecycle_state=None),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_lifecycle_terminated == 0
    assert "UNK" in ev.gaps


# ─── E. NULL state + silent > 120d → excluded_dark heuristic ──────────


@pytest.mark.asyncio
async def test_p2b_e_null_state_silent_falls_to_excluded_dark() -> None:
    """Without lifecycle evidence, the P1 silence-based heuristic
    still fires — a 10-Q filer silent > 120d is excluded_dark."""
    today = _today()
    rows = [
        _row("SILENT", today - timedelta(days=400), "10-Q",
             lifecycle_state=None),
        _row("SILENT",
             today - timedelta(days=LIVE_WITHIN_DAYS_QUARTERLY + 60),
             "10-Q", lifecycle_state=None),
        # Fillers so coverage sentinel doesn't fire.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_dark >= 1
    assert ev.excluded_lifecycle_terminated == 0


# ─── F. deregistered + silent → evidence wins over heuristic ─────────


@pytest.mark.asyncio
async def test_p2b_f_evidence_wins_over_dark_heuristic() -> None:
    """The dispositive P2b refinement: when both could apply (Form 15
    evidence AND > 120d silent), evidence routes the ticker to
    excluded_lifecycle_terminated, NOT excluded_dark.

    Why this matters: 'dark' is a heuristic ('we don't know why this
    is silent — maybe halted? maybe data hole?'); 'deregistered' is
    dispositive evidence ('SEC says they're done filing'). Surfacing
    the strongest available evidence improves operator triage."""
    today = _today()
    rows = [
        _row("EVID", today - timedelta(days=600), "10-Q",
             lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        _row("EVID",
             today - timedelta(days=LIVE_WITHIN_DAYS_QUARTERLY + 60),
             "10-Q", lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        # Fillers.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_lifecycle_terminated == 1
    assert ev.excluded_dark == 0  # evidence pre-empted the heuristic


# ─── G. repair targets exclude lifecycle_terminated tickers ──────────


@pytest.mark.asyncio
async def test_p2b_g_repair_targets_exclude_terminated() -> None:
    """Operator hard rule: ``fundamentals_refresh`` MUST NOT be
    invoked against terminated tickers — they will never file again,
    re-pulling burns the SEC rate budget for zero gain."""
    today = _today()
    rows = [
        # Real cadence-FAIL on an active ticker — IS a repair target.
        _row("LIVE_FAIL", today - timedelta(days=400), "10-Q",
             lifecycle_state="active"),
        _row("LIVE_FAIL", today - timedelta(days=150), "10-Q",
             lifecycle_state="active"),
        _row("LIVE_FAIL", today - timedelta(days=30), "10-Q",
             lifecycle_state="active"),
        # Terminated ticker — NOT a repair target.
        _row("DEREG_GAP", today - timedelta(days=500), "10-Q",
             lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        _row("DEREG_GAP", today - timedelta(days=100), "10-Q",
             lifecycle_state="deregistered",
             lifecycle_event_date=today - timedelta(days=200)),
        # Fillers.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    targets, lookback = await compute_fundamentals_repair_targets(pool)
    assert "LIVE_FAIL" in targets
    assert "DEREG_GAP" not in targets
    assert lookback > 0


# ─── H. TERMINAL_LIFECYCLE_STATES set surface ────────────────────────


def test_p2b_h_terminal_lifecycle_states_set() -> None:
    """The terminal set is pinned: Form 25 (delist_effective) and
    Form 15 (deregistered). delist_pending (Form 25 announced but not
    effective) is NOT terminal — issuer can still file fundamentals
    between announcement and effectiveness. active is NOT terminal."""
    assert TERMINAL_LIFECYCLE_STATES == frozenset({
        "delist_effective", "deregistered",
    })
    assert "active" not in TERMINAL_LIFECYCLE_STATES
    assert "delist_pending" not in TERMINAL_LIFECYCLE_STATES
    assert isinstance(TERMINAL_LIFECYCLE_STATES, frozenset)


# ─── I. evaluation surface — new counter exposed ─────────────────────


@pytest.mark.asyncio
async def test_p2b_i_evaluation_exposes_terminated_counter() -> None:
    """_Evaluation.excluded_lifecycle_terminated is the new public
    counter. Hardcoded zero when no terminated tickers exist —
    consumers (P2c capital gate, operator-facing logs) can rely on
    it being a non-None int."""
    today = _today()
    rows = [
        _row("ACTIVE", today - timedelta(days=300), "10-Q",
             lifecycle_state="active"),
        _row("ACTIVE", today - timedelta(days=60), "10-Q",
             lifecycle_state="active"),
    ]
    pool = _mock_pool(rows)
    ev = await _evaluate(pool)
    assert ev.excluded_lifecycle_terminated == 0
    assert isinstance(ev.excluded_lifecycle_terminated, int)
