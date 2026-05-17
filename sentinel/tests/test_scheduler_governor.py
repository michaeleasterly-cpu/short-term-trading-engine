"""Regression test for Task 5 of the RiskGovernor-enforcement plan.

Verifies that ``SentinelScheduler.run_once`` routes EVERY submitted ETF
order through the shared ``tpcore.risk.batch_gate.gate_batch_order``
before it hits the broker. Prior to this fix Sentinel only honored the
kill switch (the ``governor.state_for("sentinel")`` pre-flight) and never
called ``RiskGovernor.check_trade`` per name — a blocked / capped basket
member would still be submitted.

Harness mirrors the Momentum governor test: there is no shared sentinel
scheduler fixture, so we monkeypatch the plug classes + broker + governor
+ state store on ``sentinel.scheduler`` and drive a forced ACTIVE-phase
rebalance that yields the full 5-ETF ``BASKET_WEIGHTS_DEFAULT`` basket.
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
from sentinel.scheduler import SentinelScheduler

# A trading day so the is_trading_day gate passes (2026-05-14 = Thursday).
AS_OF = date_t(2026, 5, 14)
BASKET = sorted(BASKET_WEIGHTS_DEFAULT.keys())  # ['GLD','PSQ','SH','SQQQ','TLT']


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
    """Records register_engine; state_for returns a non-killed state."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, Decimal]] = []

    async def register_engine(self, engine_id, engine_equity, limits=None):
        self.registered.append((engine_id, engine_equity))

    async def state_for(self, engine_id):
        return None  # not killed → proceeds

    async def check_trade(self, **_kwargs):  # pragma: no cover - patched out
        raise AssertionError("check_trade should go through gate_batch_order")


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
    """compute_for_range returns a non-empty breakdown map keyed by AS_OF."""

    async def compute_for_range(self, pool, *, start, end):
        return {AS_OF: None}  # only membership of AS_OF is checked


class _StubLifecycle:
    def walk_states(self, scores, *, spy_close):
        return {
            AS_OF: SentinelState(
                as_of=AS_OF,
                phase=SentinelPhase.ACTIVE,
                bear_score=80,
                consecutive_days_above_threshold=3,
                days_in_phase=1,
                cycle_id=1,
            )
        }


class _StubExecution:
    """Full 5-ETF ACTIVE basket deployed from zero holdings → 5 buys."""

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
            SentinelOrder(
                ticker=t, side="buy", qty=10, notional_usd=Decimal("1000")
            )
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


async def test_sentinel_gates_every_etf_through_governor(monkeypatch) -> None:
    """Each of the 5 deployed basket members passes through gate_batch_order."""
    governor, broker = _patch_scheduler(monkeypatch)

    with patch(
        "sentinel.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ) as g:
        sched = SentinelScheduler(submit_orders=True)
        result = await sched.run_once(as_of=AS_OF)

    assert g.await_count == len(BASKET)
    assert sorted(broker.placed) == BASKET
    assert result["action"] == "rebalanced"
    # Governor was registered with sentinel's configured equity.
    assert governor.registered and governor.registered[0][0] == "sentinel"


async def test_sentinel_skips_governor_blocked_etf(monkeypatch) -> None:
    """A False return from the gate skips that ETF but not the batch."""
    _governor, broker = _patch_scheduler(monkeypatch)

    async def _gate(_gov, _eng, *, ticker, notional, direction, expected_edge_pct=None):
        return ticker != "TLT"  # block TLT only

    with patch("sentinel.scheduler.gate_batch_order", new=AsyncMock(side_effect=_gate)):
        sched = SentinelScheduler(submit_orders=True)
        await sched.run_once(as_of=AS_OF)

    assert sorted(broker.placed) == [t for t in BASKET if t != "TLT"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
