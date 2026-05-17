"""Regression test for Task 4 of the RiskGovernor-enforcement plan.

Verifies that ``MomentumScheduler.run_once`` routes EVERY submitted order
through the shared ``tpcore.risk.batch_gate.gate_batch_order`` before it
hits the broker. Prior to this fix Momentum only honored the kill switch
(see ``test_scheduler_kill_switch.py``) and never called
``RiskGovernor.check_trade`` per name — a blocked / capped position would
still be submitted.

Harness mirrors ``test_scheduler_kill_switch.py``: there is no shared
momentum scheduler fixture, so we monkeypatch the plug classes + broker +
governor + state store on ``momentum.scheduler`` exactly as that test
does, then drive a forced rebalance that yields a known set of orders.
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_t
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from momentum import scheduler as scheduler_module
from momentum.models import (
    RebalanceAction,
    RebalanceDecision,
    RebalanceOrder,
    RebalancePlan,
    TargetPosition,
)
from momentum.scheduler import MomentumScheduler


def _make_order(ticker: str, side: str, action: RebalanceAction) -> RebalanceOrder:
    return RebalanceOrder(
        ticker=ticker,
        action=action,
        qty=10,
        side=side,
        order_payload={
            "client_order_id": f"mo_{ticker}",
            "symbol": ticker,
            "side": side,
            "qty": "10",
        },
        notional_usd=Decimal("1000"),
        constructed_at=datetime.now(UTC),
    )


class _NoopAsyncpgPool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self_inner):
                class _Conn:
                    async def fetch(self_c, *_a, **_k):
                        return []

                return _Conn()

            async def __aexit__(self_inner, *_a):
                return False

        return _Ctx()

    async def close(self) -> None:
        pass


class _StubGovernor:
    """Records register_engine; state_for returns a non-killed state.

    ``record_fill`` is a spy: every call is appended to ``fills`` so the
    exit-side decrement test can assert ``position_delta`` / ``realized_pnl``
    per closed name.
    """

    def __init__(self) -> None:
        self.registered: list[tuple[str, Decimal]] = []
        self.fills: list[tuple[str, Decimal, int]] = []

    async def register_engine(self, engine_id, engine_equity, limits=None):
        self.registered.append((engine_id, engine_equity))

    async def state_for(self, engine_id):
        return None  # not killed → proceeds

    async def record_fill(self, engine_id, realized_pnl, position_delta):
        self.fills.append((engine_id, realized_pnl, position_delta))

    async def check_trade(self, **_kwargs):  # pragma: no cover - patched out
        raise AssertionError("check_trade should go through gate_batch_order")


class _StubAccount:
    equity = Decimal("0")  # forces fallback to self._engine_equity


class _StubBroker:
    def __init__(self) -> None:
        self.placed: list[str] = []

    async def get_account(self):
        return _StubAccount()

    async def get_positions(self):
        return []

    async def list_recent_orders(self, *_a, **_k):
        return []

    async def cancel_order(self, *_a, **_k):
        return None

    async def place_order(self, order):
        self.placed.append(order.symbol)
        order.broker_order_id = f"brk_{order.symbol}"
        return order


class _StubStateStore:
    def __init__(self, *, pool):
        pass


class _StubLifecycle:
    async def assess(self, pool, as_of):
        return RebalancePlan(as_of=as_of, is_rebalance_day=True, reason="test")


class _StubSetup:
    async def scan(self, pool, as_of):
        return []  # candidates unused — decision is stubbed directly


class _StubExecution:
    def __init__(self, *, governor):
        pass

    async def build_decision(self, *, candidates, equity_usd, current_holdings, as_of):
        orders = [
            _make_order("AAA", "sell", RebalanceAction.CLOSE),
            _make_order("BBB", "buy", RebalanceAction.OPEN),
            _make_order("CCC", "buy", RebalanceAction.OPEN),
        ]
        return RebalanceDecision(
            as_of=as_of,
            targets=[
                TargetPosition(
                    ticker="BBB",
                    target_notional_usd=Decimal("1000"),
                    target_shares=10,
                    last_close=Decimal("100"),
                    momentum_score=0.5,
                ),
            ],
            orders=orders,
            total_buy_notional_usd=Decimal("2000"),
            total_sell_notional_usd=Decimal("1000"),
            n_open=2,
            n_close=1,
            n_increase=0,
            n_decrease=0,
            n_hold=0,
        )


class _StubCapitalGate:
    def __init__(self, *, engine_equity_usd):
        pass

    @staticmethod
    def check_drawdown(equity, peak_equity):
        return True

    def check_rebalance(self, total_buy_notional_usd):
        return True


def _patch_scheduler(monkeypatch) -> tuple[_StubGovernor, _StubBroker]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    pool = _NoopAsyncpgPool()
    broker = _StubBroker()
    governor = _StubGovernor()

    async def _fake_build_pool(*_a, **_k):
        return pool

    monkeypatch.setattr(scheduler_module, "build_asyncpg_pool", _fake_build_pool)
    monkeypatch.setattr(scheduler_module, "AlpacaPaperBrokerAdapter", lambda: broker)
    monkeypatch.setattr(scheduler_module, "PostgresRiskStateStore", _StubStateStore)
    monkeypatch.setattr(scheduler_module, "RiskGovernor", lambda **_k: governor)
    monkeypatch.setattr(scheduler_module, "MomentumLifecycleAnalysis", _StubLifecycle)
    monkeypatch.setattr(scheduler_module, "MomentumSetupDetection", _StubSetup)
    monkeypatch.setattr(scheduler_module, "MomentumExecutionRisk", _StubExecution)
    monkeypatch.setattr(scheduler_module, "MomentumCapitalGate", _StubCapitalGate)

    class _NoopDBLog:
        def __init__(self, *_a, **_k):
            pass

        async def log(self, *_a, **_k):
            pass

        async def signal(self, *_a, **_k):
            pass

        async def order_submitted(self, *_a, **_k):
            pass

    monkeypatch.setattr(scheduler_module, "DBLogHandler", _NoopDBLog)
    return governor, broker


async def test_momentum_gates_every_order_through_governor(monkeypatch) -> None:
    """Each of the 3 submitted names must pass through gate_batch_order."""
    governor, broker = _patch_scheduler(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ) as g:
        sched = MomentumScheduler(submit_orders=True, force_rebalance=True)
        result = await sched.run_once(as_of=date_t(2026, 5, 14))

    assert g.await_count == 3
    assert sorted(broker.placed) == ["AAA", "BBB", "CCC"]
    assert sorted(result.submitted_order_ids) == ["brk_AAA", "brk_BBB", "brk_CCC"]
    # Governor was registered with momentum's configured equity.
    assert governor.registered and governor.registered[0][0] == "momentum"


async def test_momentum_skips_governor_blocked_name(monkeypatch) -> None:
    """A False return from the gate skips that name but not the batch."""
    governor, broker = _patch_scheduler(monkeypatch)

    async def _gate(_gov, _eng, *, ticker, notional, direction, expected_edge_pct=None):
        return ticker != "BBB"  # block BBB only

    with patch("momentum.scheduler.gate_batch_order", new=AsyncMock(side_effect=_gate)):
        sched = MomentumScheduler(submit_orders=True, force_rebalance=True)
        await sched.run_once(as_of=date_t(2026, 5, 14))

    assert sorted(broker.placed) == ["AAA", "CCC"]


async def test_momentum_decrements_governor_slot_on_close(monkeypatch) -> None:
    """Each successfully-submitted SELL (a closed prior holding) frees one
    governor position slot via ``record_fill(position_delta=-1)``.

    ``gate_batch_order`` is stubbed to True so its own ALLOW-side
    ``record_fill(+1)`` accounting is out of the picture — the ONLY
    ``record_fill`` calls the spy can see are the new exit-side decrements.
    The decision yields exactly one SELL (AAA, RebalanceAction.CLOSE) and
    two BUYs; only AAA must produce a decrement, and it must carry
    ``realized_pnl == 0`` (P&L is reconciled via the AAR path — adding it
    here would double-count).
    """
    governor, broker = _patch_scheduler(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True, force_rebalance=True)
        await sched.run_once(as_of=date_t(2026, 5, 14))

    # Exactly one SELL was submitted (AAA) → exactly one decrement.
    assert governor.fills == [("momentum", Decimal("0"), -1)]
    # The two BUYs (BBB, CCC) must NOT produce any decrement.
    assert sum(1 for _e, _p, d in governor.fills if d == -1) == 1
    # No realized P&L recorded here (double-count guard).
    assert all(pnl == Decimal("0") for _e, pnl, _d in governor.fills)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
