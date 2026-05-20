"""Tests for the zero-tolerance corporate_actions_completeness invariant.

The check compares live DB row count vs the latest CSV-archive snapshot
via ``tpcore.ingestion.csv_archive.detect_shrinkage``. Tests stub the
detector + a fake asyncpg pool to pin each branch of behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from tpcore.quality.validation.checks import corporate_actions_completeness as cac
from tpcore.quality.validation.checks.corporate_actions_completeness import (
    ARCHIVE_SOURCE,
    GATE_SHRINKAGE_THRESHOLD_PCT,
    check_corporate_actions_completeness,
    compute_corp_actions_repair_targets,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetchrow(self, sql: str, *args) -> dict[str, Any]:
        return {"n": self._owner.live_rows}


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, live_rows: int) -> None:
        self.live_rows = live_rows

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


@dataclass
class _ShrinkReport:
    """Mock for tpcore.ingestion.csv_archive.ShrinkageReport."""
    source: str
    current_rows: int
    previous_rows: int
    previous_archive: str
    shrinkage_pct: float
    over_threshold: bool


# ── C1 — no shrinkage → pass ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_C1_no_shrinkage_passes() -> None:
    """Live rows == archived rows → 0% shrinkage → pass."""
    pool = _Pool(live_rows=109581)
    report = _ShrinkReport(
        source=ARCHIVE_SOURCE, current_rows=109581, previous_rows=109581,
        previous_archive="data/archive/.../alpaca_corporate_actions_20260519.csv.gz",
        shrinkage_pct=0.0, over_threshold=False,
    )
    with patch.object(cac, "_evaluate") as mock_eval:
        # Bypass the lazy import; just route to our _Evaluation shape.
        from tpcore.quality.validation.checks.corporate_actions_completeness import (
            _Evaluation,
        )
        async def _fake_evaluate(p):
            return _Evaluation(
                sentinel=None,
                live_rows=109581, archived_rows=109581,
                archived_path=report.previous_archive,
                shrinkage_pct=0.0,
            )
        mock_eval.side_effect = _fake_evaluate
        result = await check_corporate_actions_completeness(pool)
    assert result.passed is True
    assert result.failed == 0
    assert result.name == "corporate_actions_completeness"


# ── C2 — any positive shrinkage fails ────────────────────────────────


@pytest.mark.asyncio
async def test_C2_any_shrinkage_fails_zero_tolerance() -> None:
    """Even 0.01% shrinkage fails — zero-tolerance, no slack knob."""
    pool = _Pool(live_rows=109580)
    with patch.object(cac, "_evaluate") as mock_eval:
        from tpcore.quality.validation.checks.corporate_actions_completeness import (
            _Evaluation,
        )
        async def _fake_evaluate(p):
            return _Evaluation(
                sentinel=None,
                live_rows=109580, archived_rows=109581,
                archived_path="data/archive/alpaca_corporate_actions_prior.csv.gz",
                shrinkage_pct=0.0001,  # 0.01% shrinkage — well below 20% warn
            )
        mock_eval.side_effect = _fake_evaluate
        result = await check_corporate_actions_completeness(pool)
    assert result.passed is False
    assert result.failed == 1
    assert result.failures[0].reason == "db_shrunk_vs_archive"


@pytest.mark.asyncio
async def test_C2b_large_shrinkage_fails() -> None:
    """30% shrinkage (well over the 20% warn band): GATE-fail."""
    pool = _Pool(live_rows=76000)
    with patch.object(cac, "_evaluate") as mock_eval:
        from tpcore.quality.validation.checks.corporate_actions_completeness import (
            _Evaluation,
        )
        async def _fake_evaluate(p):
            return _Evaluation(
                sentinel=None,
                live_rows=76000, archived_rows=109000,
                archived_path="data/archive/prior.csv.gz",
                shrinkage_pct=0.30,
            )
        mock_eval.side_effect = _fake_evaluate
        result = await check_corporate_actions_completeness(pool)
    assert result.passed is False
    # Observed string must surface the shrinkage percentage.
    assert "30.00%" in result.failures[0].observed


# ── C3 — sentinel: no prior archive ──────────────────────────────────


@pytest.mark.asyncio
async def test_C3_no_prior_archive_sentinel() -> None:
    """detect_shrinkage returns None (no prior snapshot) → sentinel
    FailureDetail with reason='no_prior_archive'."""
    pool = _Pool(live_rows=109581)
    with patch.object(cac, "_evaluate") as mock_eval:
        from tpcore.quality.validation.checks.corporate_actions_completeness import (
            _Evaluation,
        )
        from tpcore.quality.validation.models import FailureDetail
        async def _fake_evaluate(p):
            return _Evaluation(
                sentinel=FailureDetail(
                    ticker="<corporate_actions>",
                    reason="no_prior_archive",
                    expected="≥1 prior CSV-archive snapshot",
                    observed="no prior archive found",
                ),
                live_rows=109581, archived_rows=0,
                archived_path="<none>", shrinkage_pct=0.0,
            )
        mock_eval.side_effect = _fake_evaluate
        result = await check_corporate_actions_completeness(pool)
    assert result.passed is False
    assert result.failures[0].reason == "no_prior_archive"


# ── C4 — healer symmetry ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_C4_repair_targets_returns_empty_on_clean() -> None:
    pool = _Pool(live_rows=109581)
    with patch.object(cac, "_evaluate") as mock_eval:
        from tpcore.quality.validation.checks.corporate_actions_completeness import (
            _Evaluation,
        )
        async def _fake_evaluate(p):
            return _Evaluation(
                sentinel=None, live_rows=109581, archived_rows=109581,
                archived_path="x", shrinkage_pct=0.0,
            )
        mock_eval.side_effect = _fake_evaluate
        targets, lookback = await compute_corp_actions_repair_targets(pool)
    assert targets == []
    assert lookback == 0


@pytest.mark.asyncio
async def test_C4b_repair_targets_returns_empty_full_universe_on_shrinkage() -> None:
    """Per the docstring: heal scopes to full universe (canonical
    stage does NOT accept ticker subset) → empty targets list."""
    pool = _Pool(live_rows=76000)
    with patch.object(cac, "_evaluate") as mock_eval:
        from tpcore.quality.validation.checks.corporate_actions_completeness import (
            _Evaluation,
        )
        async def _fake_evaluate(p):
            return _Evaluation(
                sentinel=None, live_rows=76000, archived_rows=109000,
                archived_path="x", shrinkage_pct=0.30,
            )
        mock_eval.side_effect = _fake_evaluate
        targets, lookback = await compute_corp_actions_repair_targets(pool)
    assert targets == []
    assert lookback == 0


# ── Constants pin ─────────────────────────────────────────────────────


def test_gate_threshold_is_zero_tolerance() -> None:
    """Zero-tolerance contract — any positive shrinkage fails."""
    assert GATE_SHRINKAGE_THRESHOLD_PCT == 0.0


def test_archive_source_matches_producer_constant() -> None:
    """Routes to the same canonical source name the producer writes
    (tpcore/ingestion/handlers.py:221)."""
    assert ARCHIVE_SOURCE == "alpaca_corporate_actions"
