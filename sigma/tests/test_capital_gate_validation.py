"""Wiring test: SigmaCapitalGate.assert_can_graduate composes
is_graduated + assert_passed (Data Validation Suite) +
graduation_ready (BacktestCredibilityRubric)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tpcore.backtest.credibility import CredibilityScoreInsufficientError
from tpcore.quality.validation.capital_gate import ValidationStaleError

from sigma.plugs.capital_gate import GraduationStats, SigmaCapitalGate


class _FakePool:
    """Routes both validation-suite (`fetch`) and credibility (`fetchrow`) queries."""

    def __init__(
        self,
        validation_rows: list[dict[str, Any]] | None = None,
        credibility_score: float | None = None,
        credibility_ts: datetime | None = None,
    ) -> None:
        self.validation_rows = list(validation_rows or [])
        self.credibility_score = credibility_score
        self.credibility_ts = credibility_ts or datetime.now(UTC)

    def acquire(self):  # type: ignore[no-untyped-def]
        return _CM(self)

    # validation suite queries (multi-row)
    async def fetch(self, sql, *args):  # type: ignore[no-untyped-def]
        return list(self.validation_rows)

    # credibility query (single row by source)
    async def fetchrow(self, sql, *args):  # type: ignore[no-untyped-def]
        if self.credibility_score is None:
            return None
        return {"confidence": self.credibility_score, "timestamp": self.credibility_ts}


class _CM:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _FakePool:
        return self.pool

    async def __aexit__(self, *exc) -> None:
        return None


def _validation_passing(ts: datetime | None = None) -> list[dict[str, Any]]:
    ts = ts or datetime.now(UTC) - timedelta(days=1)
    return [
        {"source": s, "timestamp": ts, "stale": False}
        for s in ("validation.delistings", "validation.constituent", "validation.splits")
    ]


# ────────────────────────────────────────────────────────────────────────────
# Pre-grad: stats not met → validation/credibility never consulted
# ────────────────────────────────────────────────────────────────────────────


async def test_returns_false_when_stats_not_met() -> None:
    """Pre-grad case: skip validation + credibility entirely."""
    pool = _FakePool([], credibility_score=None)  # both gates would fail if hit
    stats = GraduationStats(n_trades=10, win_rate=0.7, avg_return=0.02)
    assert await SigmaCapitalGate.assert_can_graduate(stats, pool) is False


# ────────────────────────────────────────────────────────────────────────────
# All three gates green → graduates
# ────────────────────────────────────────────────────────────────────────────


async def test_returns_true_when_stats_validation_and_credibility_all_pass() -> None:
    pool = _FakePool(_validation_passing(), credibility_score=0.75)
    stats = GraduationStats(n_trades=50, win_rate=0.65, avg_return=0.015)
    assert await SigmaCapitalGate.assert_can_graduate(stats, pool) is True


# ────────────────────────────────────────────────────────────────────────────
# Validation suite failure → propagates
# ────────────────────────────────────────────────────────────────────────────


async def test_raises_validation_error_when_stats_met_but_validation_stale() -> None:
    pool = _FakePool([], credibility_score=0.75)  # credibility OK, validation absent
    stats = GraduationStats(n_trades=50, win_rate=0.65, avg_return=0.015)
    with pytest.raises(ValidationStaleError):
        await SigmaCapitalGate.assert_can_graduate(stats, pool)


# ────────────────────────────────────────────────────────────────────────────
# Credibility failure → blocks graduation even when stats + validation are fine
# ────────────────────────────────────────────────────────────────────────────


async def test_raises_credibility_error_when_score_below_60() -> None:
    """Stats + validation pass, but rubric score is 0.55 → blocked."""
    pool = _FakePool(_validation_passing(), credibility_score=0.55)
    stats = GraduationStats(n_trades=50, win_rate=0.65, avg_return=0.015)
    with pytest.raises(CredibilityScoreInsufficientError):
        await SigmaCapitalGate.assert_can_graduate(stats, pool)


async def test_raises_credibility_error_when_no_rubric_run_on_record() -> None:
    """No row at all in data_quality_log for backtest_credibility.sigma → blocked."""
    pool = _FakePool(_validation_passing(), credibility_score=None)
    stats = GraduationStats(n_trades=50, win_rate=0.65, avg_return=0.015)
    with pytest.raises(CredibilityScoreInsufficientError):
        await SigmaCapitalGate.assert_can_graduate(stats, pool)
