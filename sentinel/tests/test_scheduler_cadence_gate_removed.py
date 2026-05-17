"""Regression test: the in-scheduler trading-day cadence gate is GONE.

Operator directive 2026-05-17 (event-driven engine services): cadence is
enforced exactly once, by the Python dispatcher
(``ops/engine_dispatch.py``) via ``tpcore.engine_profile.should_fire``
(sentinel profile = ``DAILY``). The per-scheduler
``if not is_trading_day(...): return {"action": "non_trading_day"}``
early-return was redundant double-gating and has been deleted, so
``engine_profile`` is the SOLE cadence authority.

These tests pin the new reality:

* ``run_once`` invoked on a non-trading day (a weekend) no longer
  early-returns ``{"action": "non_trading_day"}`` — it proceeds into the
  Bear-Score body. The dispatcher is the gate now.
* ``--force`` is now an accepted CLI flag (parity with momentum's
  ``--force-rebalance`` + operator escape hatch for direct manual
  ``python -m sentinel.scheduler`` runs).

Harness mirrors ``sentinel/tests/test_scheduler_governor.py``.
"""
from __future__ import annotations

from datetime import date as date_t
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from sentinel import scheduler as scheduler_module
from sentinel.models import (
    BASKET_WEIGHTS_DEFAULT,
    SentinelDecision,
    SentinelOrder,
    SentinelPhase,
    SentinelState,
    SentinelTarget,
)
from sentinel.scheduler import SentinelScheduler, _parse_args

# 2026-05-16 is a SATURDAY — NOT a trading day. Under the OLD gate this
# date would early-return {"action": "non_trading_day"}.
NON_TRADING_DAY = date_t(2026, 5, 16)
BASKET = sorted(BASKET_WEIGHTS_DEFAULT.keys())


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


class _StubBroker:
    def __init__(self) -> None:
        self.placed: list[str] = []

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
    async def compute_for_range(self, pool, *, start, end):
        return {NON_TRADING_DAY: None}  # only membership of as_of is checked


class _StubLifecycle:
    def walk_states(self, scores, *, spy_close):
        return {
            NON_TRADING_DAY: SentinelState(
                as_of=NON_TRADING_DAY,
                phase=SentinelPhase.ACTIVE,
                bear_score=80,
                consecutive_days_above_threshold=3,
                days_in_phase=1,
                cycle_id=1,
            )
        }


class _StubExecution:
    def __init__(self, *, graduated: bool = False):
        pass

    def build_decision(self, *, as_of, state, equity_usd, prices, current_holdings):
        targets = [
            SentinelTarget(
                ticker=t,
                target_weight=BASKET_WEIGHTS_DEFAULT[t],
                target_notional_usd=Decimal("1000"),
                target_shares=10,
                last_price=Decimal("100"),
            )
            for t in BASKET
        ]
        orders = [
            SentinelOrder(ticker=t, side="buy", qty=10, notional_usd=Decimal("1000"))
            for t in BASKET
        ]
        return SentinelDecision(
            as_of=as_of,
            state=state,
            allocation_cap_pct=Decimal("0.10"),
            deployable_equity_usd=Decimal("10000"),
            targets=targets,
            orders=orders,
            missing_etfs=(),
        )


class _StubCapitalGate:
    def __init__(self, *, graduated: bool = False):
        pass

    def check_rebalance(self, buy_notional, equity):
        return True


def _patch_scheduler(monkeypatch) -> tuple[_StubGovernor, _StubBroker]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    pool = _NoopAsyncpgPool()
    broker = _StubBroker()
    governor = _StubGovernor()

    async def _fake_build_pool(*_a, **_k):
        return pool

    async def _fake_spy_close(*_a, **_k):
        return pd.Series(dtype=float)

    async def _fake_latest_prices(*_a, **_k):
        return {t: Decimal("100") for t in BASKET}

    monkeypatch.setattr(scheduler_module, "build_asyncpg_pool", _fake_build_pool)
    monkeypatch.setattr(scheduler_module, "AlpacaPaperBrokerAdapter", lambda: broker)
    monkeypatch.setattr(scheduler_module, "PostgresRiskStateStore", _StubStateStore)
    monkeypatch.setattr(scheduler_module, "RiskGovernor", lambda **_k: governor)
    monkeypatch.setattr(scheduler_module, "SentinelSetupDetection", _StubSetup)
    monkeypatch.setattr(scheduler_module, "SentinelLifecycleAnalysis", _StubLifecycle)
    monkeypatch.setattr(scheduler_module, "SentinelExecutionRisk", _StubExecution)
    monkeypatch.setattr(scheduler_module, "SentinelCapitalGate", _StubCapitalGate)
    monkeypatch.setattr(scheduler_module, "fetch_spy_close", _fake_spy_close)
    monkeypatch.setattr(scheduler_module, "_latest_prices", _fake_latest_prices)

    class _NoopDBLog:
        def __init__(self, *_a, **_k):
            pass

        async def startup(self, *_a, **_k):
            pass

        async def shutdown(self, *_a, **_k):
            pass

        async def signal(self, *_a, **_k):
            pass

        async def order_submitted(self, *_a, **_k):
            pass

    monkeypatch.setattr(scheduler_module, "DBLogHandler", _NoopDBLog)
    return governor, broker


async def test_run_once_proceeds_on_non_trading_day(monkeypatch) -> None:
    """No in-scheduler trading-day gate: invoking on a Saturday must
    proceed into the Bear-Score body and rebalance — NOT early-return
    ``{"action": "non_trading_day"}``.

    Cadence (incl. the trading-day boundary) is the dispatcher's job
    (engine_profile.should_fire); if the scheduler is invoked at all,
    it acts."""
    governor, broker = _patch_scheduler(monkeypatch)

    with patch(
        "sentinel.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = SentinelScheduler(submit_orders=True)
        result = await sched.run_once(as_of=NON_TRADING_DAY)

    # Old gate would have returned {"action": "non_trading_day"}.
    assert result["action"] != "non_trading_day"
    assert result["action"] == "rebalanced"
    assert sorted(broker.placed) == BASKET


def test_force_flag_parses() -> None:
    """``--force`` is now an accepted CLI flag (parity with momentum +
    operator escape hatch for direct manual invocation)."""
    args = _parse_args(["--force"])
    assert args.force is True

    args_default = _parse_args([])
    assert args_default.force is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
