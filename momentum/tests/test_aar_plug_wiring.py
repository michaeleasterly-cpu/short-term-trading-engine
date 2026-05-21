"""Regression test: MomentumScheduler must wire MomentumAARLogging +
MomentumLifecycleAnalysis into ``run_once``.

Background — finding (``docs/memory/project_momentum_aar_plug_finding.md``):
``momentum.plugs.aar_logging.MomentumAARLogging`` and
``momentum.plugs.lifecycle_analysis.MomentumLifecycleAnalysis`` are
defined-but-never-wired. Momentum's SELL branch (the monthly
CLOSE/DECREASE rebalance leg) does NOT write an AAR — the comment
"reconciled via the AAR/trade_monitor path" is aspirational because
``trade_monitor`` only reconciles tier-1/tier-2 bracket fills (reversion
+ vector); momentum uses day-market orders, so trade_monitor never sees
the close. Effect: momentum has been emitting ZERO AARs since launch.

The fix wires both plugs and writes one AAR per closed momentum position
on every rebalance, using ``tpcore.aar.classify_exit_reason`` (NOT the
hardcoded ``ExitReason.SCHEDULED_REBALANCE`` literal that previously sat
in ``MomentumAARLogging.write_rebalance_close``'s default kwarg —
CLAUDE.md's "AAR uses ``tpcore.aar.classify_exit_reason`` — no hardcoded
``ExitReason`` literals" invariant).

These tests assert (entry-fill caveat resolved by reusing the already-
fetched ``broker.get_positions()`` result instead of an extra
``get_open_position(ticker)`` call — ``Position.avg_entry_price`` +
``Position.qty`` already cover the case):

* ``MomentumLifecycleAnalysis.assess`` is invoked on the proceed path
  (its is_rebalance_day signal is logged structurally so the scheduler's
  intent is auditable, even though the dispatcher is the sole cadence
  authority).
* The SELL branch (CLOSE/DECREASE actions) calls
  ``MomentumAARLogging.write_rebalance_close`` exactly once per closed
  ticker.
* The ``exit_reason`` argument was produced by
  ``tpcore.aar.classify_exit_reason`` — NOT the literal
  ``ExitReason.SCHEDULED_REBALANCE``.
* ``entry_price`` + ``qty`` are populated from the broker's
  ``Position.avg_entry_price`` + ``Position.qty`` for the SELL'd
  symbol (the operator's "extra Alpaca API call" caveat is resolved by
  the existing seam — no new I/O).
* BUY-side orders (OPEN / INCREASE) do NOT trigger an AAR write
  (entries don't close anything).
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
from momentum.scheduler import MomentumScheduler
from tpcore.aar.models import ExitReason
from tpcore.interfaces.broker import Position

# ─── Stubs (mirror test_scheduler_governor / _lifecycle_events idioms) ──────


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
        self.closes: list[tuple[str, str, Decimal]] = []

    async def register_engine(self, *_a, **_k) -> None:
        return None

    async def state_for(self, _engine_id):
        return None

    async def record_close(self, engine_id, trade_id, realized_pnl=Decimal("0")):
        self.closes.append((engine_id, trade_id, realized_pnl))
        return True


class _StubStateStore:
    def __init__(self, *, pool):
        pass


class _StubAccount:
    equity = Decimal("0")


class _StubBroker:
    """Returns one existing AAA position so SELL-AAA hits the AAR-write
    branch with a non-trivial avg_entry_price / qty."""

    def __init__(self) -> None:
        self.placed: list[str] = []

    async def get_account(self):
        return _StubAccount()

    async def get_positions(self):
        # AAA position exists; will be CLOSE'd by the stub execution plug.
        return [
            Position(
                symbol="AAA",
                qty=Decimal("5"),
                avg_entry_price=Decimal("120.50"),
                market_value=Decimal("625.00"),
                unrealized_pl=Decimal("22.50"),
                cost_basis=Decimal("602.50"),
            ),
        ]

    async def list_recent_orders(self, *_a, **_k):
        # AAA is "ours" — its recent order carries the mo_ prefix so
        # ``_filter_to_engine_holdings`` keeps it in ``current_holdings``.
        from tpcore.interfaces.broker import Order, OrderSide, OrderType

        return [
            Order(
                client_order_id="mo_AAA_entry",
                symbol="AAA",
                side=OrderSide.BUY,
                qty=Decimal("5"),
                order_type=OrderType.MARKET,
            ),
        ]

    async def cancel_order(self, *_a, **_k):
        return None

    async def place_order(self, order):
        self.placed.append(order.symbol)
        order.broker_order_id = f"brk_{order.symbol}"
        # Simulate immediate market fill so the AAR write has an exit_price.
        order.avg_fill_price = (
            Decimal("125.00") if order.side.value == "sell" else Decimal("50.00")
        )
        order.filled_qty = order.qty
        return order


class _StubSetup:
    async def scan(self, _pool, _as_of):
        return []  # candidates unused — decision is stubbed directly


def _make_order(ticker: str, side: str, action: RebalanceAction) -> RebalanceOrder:
    return RebalanceOrder(
        ticker=ticker,
        action=action,
        qty=5 if ticker == "AAA" else 10,
        side=side,
        order_payload={
            "client_order_id": f"mo_{ticker}",
            "symbol": ticker,
            "side": side,
            "qty": "5" if ticker == "AAA" else "10",
        },
        notional_usd=Decimal("625") if ticker == "AAA" else Decimal("500"),
        constructed_at=datetime.now(UTC),
    )


class _StubExecution:
    def __init__(self, *, governor):
        pass

    async def build_decision(self, *, candidates, equity_usd, current_holdings, as_of):
        # AAA → CLOSE (sell), BBB → OPEN (buy). The AAR-write branch must
        # fire for AAA only.
        return RebalanceDecision(
            as_of=as_of,
            targets=[
                TargetPosition(
                    ticker="BBB",
                    target_notional_usd=Decimal("500"),
                    target_shares=10,
                    last_close=Decimal("50"),
                    momentum_score=0.5,
                ),
            ],
            orders=[
                _make_order("AAA", "sell", RebalanceAction.CLOSE),
                _make_order("BBB", "buy", RebalanceAction.OPEN),
            ],
            total_buy_notional_usd=Decimal("500"),
            total_sell_notional_usd=Decimal("625"),
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


class _SpyAARLogging:
    """Drop-in for MomentumAARLogging that records every
    write_rebalance_close call so the test can inspect the kwargs."""

    engine_name = "momentum"

    def __init__(self, pool=None) -> None:
        self.pool = pool
        self.calls: list[dict] = []

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": "momentum", "plug": "aar_logging", "ok": True}

    async def write_rebalance_close(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return True


class _SpyLifecycle:
    """Drop-in for MomentumLifecycleAnalysis that records every assess call."""

    engine_name = "momentum"

    def __init__(self) -> None:
        self.assessed: list[date_t] = []

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": "momentum", "plug": "lifecycle_analysis", "ok": True}

    async def assess(self, pool, as_of):
        from momentum.models import RebalancePlan

        self.assessed.append(as_of)
        return RebalancePlan(
            as_of=as_of, is_rebalance_day=True, reason="first trading day of the month",
        )


def _patch(monkeypatch) -> tuple[_StubGovernor, _StubBroker, _SpyAARLogging, _SpyLifecycle]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    pool = _NoopAsyncpgPool()
    broker = _StubBroker()
    governor = _StubGovernor()
    aar_spy = _SpyAARLogging()
    lifecycle_spy = _SpyLifecycle()

    async def _fake_build_pool(*_a, **_k):
        return pool

    monkeypatch.setattr(scheduler_module, "build_asyncpg_pool", _fake_build_pool)
    monkeypatch.setattr(scheduler_module, "AlpacaPaperBrokerAdapter", lambda: broker)
    monkeypatch.setattr(scheduler_module, "PostgresRiskStateStore", _StubStateStore)
    monkeypatch.setattr(scheduler_module, "RiskGovernor", lambda **_k: governor)
    monkeypatch.setattr(scheduler_module, "DBLogHandler", _NoopDBLog)
    monkeypatch.setattr(scheduler_module, "MomentumSetupDetection", _StubSetup)
    monkeypatch.setattr(scheduler_module, "MomentumExecutionRisk", _StubExecution)
    monkeypatch.setattr(scheduler_module, "MomentumCapitalGate", _StubCapitalGate)
    monkeypatch.setattr(
        scheduler_module, "MomentumAARLogging", lambda *_a, **_k: aar_spy,
    )
    monkeypatch.setattr(
        scheduler_module, "MomentumLifecycleAnalysis", lambda *_a, **_k: lifecycle_spy,
    )
    return governor, broker, aar_spy, lifecycle_spy


# ─── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_assess_invoked_on_proceed_path(monkeypatch) -> None:
    """MomentumLifecycleAnalysis must be instantiated and its assess()
    called once per run_once invocation. The dispatcher gates cadence,
    but the in-scheduler call gives the run an auditable "this IS the
    rebalance day" record without re-implementing the calendar lookup."""
    _, _, _, lifecycle_spy = _patch(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True)
        await sched.run_once(as_of=date_t(2026, 6, 1))

    assert lifecycle_spy.assessed == [date_t(2026, 6, 1)]


@pytest.mark.asyncio
async def test_sell_branch_writes_aar_per_closed_ticker(monkeypatch) -> None:
    """The SELL branch (CLOSE / DECREASE actions) must call
    write_rebalance_close exactly once per closed ticker. BUY-side
    orders must NOT trigger an AAR write."""
    _, _, aar_spy, _ = _patch(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True)
        await sched.run_once(as_of=date_t(2026, 6, 1))

    # One AAR write for the CLOSE of AAA; no write for BBB (a fresh OPEN).
    assert len(aar_spy.calls) == 1
    closed = aar_spy.calls[0]
    assert closed["ticker"] == "AAA"


@pytest.mark.asyncio
async def test_aar_exit_reason_from_classifier_not_literal(monkeypatch) -> None:
    """exit_reason must be the return value of
    tpcore.aar.classify_exit_reason — NOT the hardcoded
    ExitReason.SCHEDULED_REBALANCE literal."""
    _, _, aar_spy, _ = _patch(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True)
        await sched.run_once(as_of=date_t(2026, 6, 1))

    assert len(aar_spy.calls) == 1
    er = aar_spy.calls[0]["exit_reason"]
    # classify_exit_reason with take_profit=None and stop_loss=None
    # returns TIME_STOP (the "exited outside the planned brackets" bucket).
    # The SCHEDULED_REBALANCE literal is the FORBIDDEN value — anyone who
    # re-introduces the hardcoded default kwarg flips this assertion.
    assert er is not ExitReason.SCHEDULED_REBALANCE
    assert er is ExitReason.TIME_STOP


@pytest.mark.asyncio
async def test_aar_entry_fill_from_broker_position(monkeypatch) -> None:
    """entry_price / qty must come from the broker's Position record for
    the SELL'd symbol — the operator's 'extra get_open_position call'
    caveat is resolved by reusing the already-fetched get_positions
    result (Position.avg_entry_price + Position.qty)."""
    _, _, aar_spy, _ = _patch(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True)
        await sched.run_once(as_of=date_t(2026, 6, 1))

    assert len(aar_spy.calls) == 1
    call = aar_spy.calls[0]
    assert call["entry_price"] == Decimal("120.50")
    assert call["qty"] == Decimal("5")
    # exit_price comes from the placed sell order's avg_fill_price.
    assert call["exit_price"] == Decimal("125.00")


@pytest.mark.asyncio
async def test_buy_branch_does_not_trigger_aar_write(monkeypatch) -> None:
    """OPEN / INCREASE orders open or grow positions — they don't close
    anything, so write_rebalance_close must NOT be called for them.

    This guards against a future refactor where a developer accidentally
    moves the AAR-write call outside the ``side is SELL`` branch."""
    _, _, aar_spy, _ = _patch(monkeypatch)

    with patch(
        "momentum.scheduler.gate_batch_order",
        new=AsyncMock(return_value=True),
    ):
        sched = MomentumScheduler(submit_orders=True)
        await sched.run_once(as_of=date_t(2026, 6, 1))

    # Only AAA (the CLOSE) — never BBB (the OPEN).
    tickers = [c["ticker"] for c in aar_spy.calls]
    assert tickers == ["AAA"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
