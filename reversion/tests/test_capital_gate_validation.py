"""Wiring test: ReversionCapitalGate.assert_can_graduate composes
is_graduated + assert_passed (Data Validation Suite) +
graduation_ready (BacktestCredibilityRubric)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reversion.plugs.capital_gate import GraduationStats, ReversionCapitalGate
from tpcore.backtest.credibility import CredibilityScoreInsufficientError
from tpcore.quality.validation.capital_gate import ValidationStaleError


class _FakePool:
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

    async def fetch(self, sql, *args):  # type: ignore[no-untyped-def]
        return list(self.validation_rows)

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


def _validation_passing() -> list[dict[str, Any]]:
    ts = datetime.now(UTC) - timedelta(days=1)
    return [
        {"source": s, "timestamp": ts, "stale": False}
        for s in ("validation.delistings", "validation.constituent", "validation.splits")
    ]


# Pre-grad: stats not met → other gates never consulted.
async def test_returns_false_when_stats_not_met() -> None:
    pool = _FakePool([], credibility_score=None)
    stats = GraduationStats(n_trades=5, win_rate=0.7, avg_return=0.03, profit_factor=2.0)
    assert await ReversionCapitalGate.assert_can_graduate(stats, pool) is False


# All three gates green → graduates.
async def test_returns_true_when_stats_validation_and_credibility_all_pass() -> None:
    pool = _FakePool(_validation_passing(), credibility_score=0.75)
    stats = GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5)
    assert await ReversionCapitalGate.assert_can_graduate(stats, pool) is True


# Validation suite failure → propagates.
async def test_raises_validation_error_when_stats_met_but_validation_stale() -> None:
    pool = _FakePool([], credibility_score=0.75)
    stats = GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5)
    with pytest.raises(ValidationStaleError):
        await ReversionCapitalGate.assert_can_graduate(stats, pool)


# Credibility failure → blocks graduation.
async def test_raises_credibility_error_when_score_below_60() -> None:
    pool = _FakePool(_validation_passing(), credibility_score=0.55)
    stats = GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5)
    with pytest.raises(CredibilityScoreInsufficientError):
        await ReversionCapitalGate.assert_can_graduate(stats, pool)


async def test_raises_credibility_error_when_no_rubric_run_on_record() -> None:
    pool = _FakePool(_validation_passing(), credibility_score=None)
    stats = GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5)
    with pytest.raises(CredibilityScoreInsufficientError):
        await ReversionCapitalGate.assert_can_graduate(stats, pool)
