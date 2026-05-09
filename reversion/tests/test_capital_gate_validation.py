"""Wiring test: ReversionCapitalGate.assert_can_graduate."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tpcore.quality.validation.capital_gate import ValidationStaleError

from reversion.plugs.capital_gate import GraduationStats, ReversionCapitalGate


class _DQLogFakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def acquire(self):  # type: ignore[no-untyped-def]
        return _CM(self)

    async def fetch(self, sql, *args):  # type: ignore[no-untyped-def]
        return list(self.rows)


class _CM:
    def __init__(self, pool: _DQLogFakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _DQLogFakePool:
        return self.pool

    async def __aexit__(self, *exc) -> None:
        return None


def _all_three(ts: datetime) -> list[dict[str, Any]]:
    return [
        {"source": s, "timestamp": ts, "stale": False}
        for s in ("validation.delistings", "validation.constituent", "validation.splits")
    ]


async def test_assert_can_graduate_returns_false_when_stats_not_met() -> None:
    pool = _DQLogFakePool([])
    stats = GraduationStats(n_trades=10, win_rate=0.7, avg_return=0.03)
    assert await ReversionCapitalGate.assert_can_graduate(stats, pool) is False


async def test_assert_can_graduate_returns_true_when_stats_and_validation_pass() -> None:
    pool = _DQLogFakePool(_all_three(datetime.now(UTC) - timedelta(days=1)))
    stats = GraduationStats(n_trades=30, win_rate=0.65, avg_return=0.02)
    assert await ReversionCapitalGate.assert_can_graduate(stats, pool) is True


async def test_assert_can_graduate_raises_when_stats_met_but_validation_stale() -> None:
    pool = _DQLogFakePool([])
    stats = GraduationStats(n_trades=30, win_rate=0.65, avg_return=0.02)
    with pytest.raises(ValidationStaleError):
        await ReversionCapitalGate.assert_can_graduate(stats, pool)
