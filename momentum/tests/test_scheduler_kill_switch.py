"""Regression test for F3 of the 2026-05-14 engine pipeline audit.

Verifies that ``MomentumScheduler.run_once`` honors the platform-wide
``RiskGovernor`` kill switch before scanning or submitting. Mirrors the
pre-existing pattern in sigma/reversion/vector schedulers — Momentum
was the only per-engine scheduler with zero ``kill_switch_active``
references prior to this fix.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from momentum import scheduler as scheduler_module
from momentum.scheduler import MomentumScheduler
from tpcore.risk.governor import RiskState


class _NoopAsyncpgPool:
    """Stand-in pool — ``DBLogHandler`` accepts anything with ``acquire``."""

    def acquire(self):  # pragma: no cover — never reached in this test
        raise AssertionError("pool.acquire should not be reached when kill switch is active")

    async def close(self) -> None:
        pass


class _StubGovernor:
    """Returns a kill-switch-active state for state_for(); broker submit
    paths should never be exercised once the early-return fires."""

    def __init__(self, state: RiskState | None) -> None:
        self._state = state

    async def register_engine(self, *_args, **_kwargs) -> None:
        # No-op: the scheduler registers momentum with the governor before
        # the kill-switch check. Idempotent on the real governor; here it
        # just must not raise so the early-return path is still exercised.
        return None

    async def state_for(self, engine_id: str) -> RiskState | None:
        return self._state


@pytest.mark.asyncio
async def test_run_once_returns_early_when_kill_switch_active(monkeypatch) -> None:
    """Active kill switch → no rebalance, no broker submit, no scan."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    pool = _NoopAsyncpgPool()
    broker_calls: list[str] = []

    async def _fake_build_pool(*_args, **_kwargs):
        return pool

    class _NoopBroker:
        async def get_account(self):
            broker_calls.append("get_account")
            return None  # Should never reach here.

        async def get_positions(self):
            broker_calls.append("get_positions")
            return []

        async def list_recent_orders(self, *_, **__):
            broker_calls.append("list_recent_orders")
            return []

        async def place_order(self, *_, **__):
            broker_calls.append("place_order")
            raise AssertionError("place_order should not fire when kill switch is active")

    class _NoopStateStore:
        def __init__(self, *, pool):
            pass

    now = datetime.now(UTC)
    killed_state = RiskState(
        engine="momentum",
        daily_pnl=Decimal("0"),
        weekly_pnl=Decimal("0"),
        open_positions=0,
        engine_equity=Decimal("10000"),
        daily_reset_at=now,
        weekly_reset_at=now,
        kill_switch_active=True,
        kill_switch_reason="audit_test",
        updated_at=now,
    )
    governor = _StubGovernor(state=killed_state)

    def _fake_governor_ctor(**_kwargs):
        return governor

    monkeypatch.setattr(scheduler_module, "build_asyncpg_pool", _fake_build_pool)
    monkeypatch.setattr(scheduler_module, "AlpacaPaperBrokerAdapter", _NoopBroker)
    monkeypatch.setattr(scheduler_module, "PostgresRiskStateStore", _NoopStateStore)
    monkeypatch.setattr(scheduler_module, "RiskGovernor", _fake_governor_ctor)

    sched = MomentumScheduler(submit_orders=True)
    result = await sched.run_once(as_of=datetime(2026, 5, 14, tzinfo=UTC).date())

    # No rebalance, no decision, no submitted orders.
    assert result.is_rebalance_day is False
    assert result.decision is None
    assert result.submitted_order_ids == []
    # Broker should never have been touched after the early return.
    assert broker_calls == []


