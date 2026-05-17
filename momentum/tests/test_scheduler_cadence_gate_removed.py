"""Regression test: the in-scheduler MONTHLY cadence gate is GONE.

Operator directive 2026-05-17 (event-driven engine services): cadence is
enforced exactly once, by the Python dispatcher
(``ops/engine_dispatch.py``) via ``tpcore.engine_profile.should_fire``
(momentum profile = ``MONTHLY_FIRST_TRADING_DAY``). The per-scheduler
``lifecycle.assess`` / ``plan.is_rebalance_day`` early-return was
redundant double-gating and has been deleted, so ``engine_profile`` is
the SOLE cadence authority.

These tests pin the new reality:

* ``run_once`` invoked on a NON-first-trading-day proceeds into the
  rebalance body (it does NOT early-return a ``no_rebalance`` /
  ``is_rebalance_day=False`` summary). The dispatcher is the gate now;
  if the scheduler is invoked it acts.
* ``--force-rebalance`` is still an accepted CLI flag (operator escape
  hatch for direct manual ``python -m momentum.scheduler`` runs).

Harness mirrors ``test_scheduler_governor.py`` — same monkeypatch of
plug classes + broker + governor + state store on ``momentum.scheduler``.
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
    TargetPosition,
)
from momentum.scheduler import MomentumScheduler, _parse_args

# 2026-05-20 is a Wednesday and is NOT the first trading day of May 2026
# (the first session of May 2026 is Fri 2026-05-01). Under the OLD gate
# this date would early-return is_rebalance_day=False.
NON_FIRST_TRADING_DAY = date_t(2026, 5, 20)


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
    def __init__(self) -> None:
        self.registered: list[tuple[str, Decimal]] = []

    async def register_engine(self, engine_id, engine_equity, limits=None):
        self.registered.append((engine_id, engine_equity))

    async def state_for(self, engine_id):
        return None  # not killed → proceeds

    async def record_fill(self, engine_id, realized_pnl, position_delta):
        return None


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
            total_buy_notional_usd=Decimal("1000"),
            total_sell_notional_usd=Decimal("1000"),
            n_open=1,
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
    monkeypatch.setattr(scheduler_module, "MomentumSetupDetection", _StubSetup)
    monkeypatch.setattr(scheduler_module, "MomentumExecutionRisk", _StubExecution)
    monkeypatch.setattr(scheduler_module, "MomentumCapitalGate", _StubCapitalGate)

    class _NoopDBLog:
        def __init__(self, *_a, **_k):
            pass

        async def startup(self, *_a, **_k):
            pass

        async def shutdown(self, *_a, **_k):
            pass

        async def error(self, *_a, **_k):
            pass

        async def log(self, *_a, **_k):
            pass

        async def signal(self, *_a, **_k):
            pass

        async def order_submitted(self, *_a, **_k):
            pass

    monkeypatch.setattr(scheduler_module, "DBLogHandler", _NoopDBLog)
    return governor, broker


async def test_run_once_proceeds_on_non_first_trading_day(monkeypatch) -> None:
    """No in-scheduler cadence gate: a mid-month invocation (NOT the first
    trading day) must proceed into the rebalance body and submit orders —
    NOT early-return an is_rebalance_day=False / no_rebalance summary.

    Cadence is the dispatcher's job (engine_profile.should_fire); if the
    scheduler is invoked at all, it acts. No --force-rebalance is passed,
    proving the gate is gone (not merely bypassed)."""
    governor, broker = _patch_scheduler(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True)  # NO force_rebalance
        result = await sched.run_once(as_of=NON_FIRST_TRADING_DAY)

    # Old gate would have returned is_rebalance_day=False with no orders.
    assert result.is_rebalance_day is True
    assert result.decision is not None
    assert sorted(broker.placed) == ["AAA", "BBB"]
    assert sorted(result.submitted_order_ids) == ["brk_AAA", "brk_BBB"]


def test_force_rebalance_flag_still_parses() -> None:
    """``--force-rebalance`` remains an accepted CLI flag (operator escape
    hatch for direct manual invocation)."""
    args = _parse_args(["--force-rebalance"])
    assert args.force_rebalance is True

    args_default = _parse_args([])
    assert args_default.force_rebalance is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
