"""Regression test: MomentumScheduler must emit STARTUP / SHUTDOWN.

Prerequisite for the event-driven dispatcher (Sub-project B).
``tpcore.engine_profile.should_fire`` keys its "already ran this cycle"
idempotency off a ``STARTUP`` row in ``platform.application_log`` inside
the cadence window. Momentum previously emitted NO STARTUP/SHUTDOWN
events (only SIGNAL / ORDER_SUBMITTED / EQUITY_SNAPSHOT), so once the
dispatcher became the sole cadence authority (the in-scheduler
``is_rebalance_day`` gate was deleted 2026-05-17) momentum would
re-rebalance on every readiness event in its first-trading-day window
without a STARTUP idempotency marker (a double trade for a MONTHLY
engine).

These tests assert:

* ``run_once`` emits a ``STARTUP`` event, and it is emitted *before*
  any order submission.
* ``run_once`` emits a ``SHUTDOWN`` event in ``finally:`` on the normal
  proceed path AND on the exception path.

NOTE (2026-05-17): the in-scheduler MONTHLY cadence gate
(``lifecycle.assess`` / ``plan.is_rebalance_day``) was deleted — there
is no longer a "no-rebalance early-return" path to exercise (cadence is
the dispatcher's job). The STARTUP/SHUTDOWN bookend assertions are
unchanged; they are now asserted on the normal proceed path (the path a
dispatcher-invoked momentum run always takes) and on the
exception path. The crash is now injected via the first plug actually
called after STARTUP (``MomentumSetupDetection``) instead of the
deleted lifecycle plug.

Mirrors the fake-pool / monkeypatch style of
``test_scheduler_kill_switch.py`` — the capture is done with a fake
``DBLogHandler`` recording every event call in order.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from momentum import scheduler as scheduler_module
from momentum.models import (
    RebalanceAction,
    RebalanceDecision,
    RebalanceOrder,
    TargetPosition,
)
from momentum.scheduler import MomentumScheduler


class _NoopAsyncpgPool:
    """Stand-in pool — only needs ``close``; DB writes are captured by the
    fake DBLogHandler so the pool is never acquired in these tests."""

    async def close(self) -> None:
        pass


class _CapturingDBLog:
    """Records every lifecycle/event call in invocation order.

    Drop-in for the real ``tpcore.logging.DBLogHandler`` — same async
    surface the scheduler uses (``startup``, ``shutdown``, ``log``,
    ``signal``, ``order_submitted``). ``events`` is the ordered ledger
    of ``(kind, ...)`` tuples the assertions inspect.
    """

    def __init__(self, *_a, **_k) -> None:
        self.events: list[tuple] = []

    async def startup(self, commit_sha: str | None = None) -> None:
        self.events.append(("STARTUP", commit_sha))

    async def shutdown(self, duration_ms: int, exit_code: int) -> None:
        self.events.append(("SHUTDOWN", duration_ms, exit_code))

    async def log(self, event_type, message, severity="INFO", data=None) -> None:
        self.events.append(("LOG", event_type))

    async def signal(self, *_a, **_k) -> None:
        self.events.append(("SIGNAL",))

    async def order_submitted(self, ticker, quantity, order_id=None) -> None:
        self.events.append(("ORDER_SUBMITTED", ticker))

    async def error(self, exception, context) -> None:
        self.events.append(("ERROR", context))


class _NoopAsyncpgPoolWithAcquire(_NoopAsyncpgPool):
    """Proceed-path pool — _fetch_peak_equity acquires a connection."""

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


class _StubGovernor:
    """Non-killed state → scheduler proceeds past the kill-switch gate."""

    async def register_engine(self, *_a, **_k) -> None:
        return None

    async def state_for(self, _engine_id):
        return None

    async def record_fill(self, *_a, **_k) -> None:
        return None


class _StubAccount:
    equity = Decimal("0")  # forces fallback to engine equity


class _StubBroker:
    async def get_account(self):
        return _StubAccount()

    async def get_positions(self):
        return []

    async def list_recent_orders(self, *_a, **_k):
        return []

    async def cancel_order(self, *_a, **_k):
        return None

    async def place_order(self, order):
        order.broker_order_id = f"brk_{order.symbol}"
        return order


class _StubSetup:
    async def scan(self, _pool, _as_of):
        return []  # candidates unused — decision stubbed directly


class _StubExecution:
    def __init__(self, *, governor):
        pass

    async def build_decision(self, *, candidates, equity_usd, current_holdings, as_of):
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
            orders=[
                RebalanceOrder(
                    ticker="BBB",
                    action=RebalanceAction.OPEN,
                    qty=10,
                    side="buy",
                    order_payload={
                        "client_order_id": "mo_BBB",
                        "symbol": "BBB",
                        "side": "buy",
                        "qty": "10",
                    },
                    notional_usd=Decimal("1000"),
                    constructed_at=datetime.now(UTC),
                ),
            ],
            total_buy_notional_usd=Decimal("1000"),
            total_sell_notional_usd=Decimal("0"),
            n_open=1,
            n_close=0,
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


def _patch(monkeypatch) -> _CapturingDBLog:
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    captured = _CapturingDBLog()

    async def _fake_build_pool(*_a, **_k):
        return _NoopAsyncpgPoolWithAcquire()

    class _NoopStateStore:
        def __init__(self, *, pool):
            pass

    def _fake_governor_ctor(**_k):
        return _StubGovernor()

    def _fake_dblog_ctor(*_a, **_k):
        return captured

    async def _fake_gate(*_a, **_k):
        return True

    monkeypatch.setattr(scheduler_module, "build_asyncpg_pool", _fake_build_pool)
    monkeypatch.setattr(scheduler_module, "AlpacaPaperBrokerAdapter", _StubBroker)
    monkeypatch.setattr(scheduler_module, "PostgresRiskStateStore", _NoopStateStore)
    monkeypatch.setattr(scheduler_module, "RiskGovernor", _fake_governor_ctor)
    monkeypatch.setattr(scheduler_module, "DBLogHandler", _fake_dblog_ctor)
    monkeypatch.setattr(scheduler_module, "MomentumSetupDetection", _StubSetup)
    monkeypatch.setattr(scheduler_module, "MomentumExecutionRisk", _StubExecution)
    monkeypatch.setattr(scheduler_module, "MomentumCapitalGate", _StubCapitalGate)
    monkeypatch.setattr(scheduler_module, "gate_batch_order", _fake_gate)
    return captured


@pytest.mark.asyncio
async def test_run_once_emits_startup_and_shutdown_on_proceed_path(monkeypatch) -> None:
    """The normal proceed path must emit STARTUP and SHUTDOWN.

    Since the in-scheduler cadence gate was deleted (cadence is the
    dispatcher's job), every dispatcher-invoked momentum run takes the
    proceed path. The idempotency STARTUP row must be written on every
    one of those, and SHUTDOWN must fire in the ``finally:``.
    """
    captured = _patch(monkeypatch)

    sched = MomentumScheduler(submit_orders=True)
    result = await sched.run_once(as_of=datetime(2026, 5, 14, tzinfo=UTC).date())

    # No in-scheduler cadence gate → the run proceeds (rebalances).
    assert result.is_rebalance_day is True

    kinds = [e[0] for e in captured.events]
    assert "STARTUP" in kinds, f"no STARTUP emitted; events={captured.events}"
    assert "SHUTDOWN" in kinds, f"no SHUTDOWN emitted; events={captured.events}"

    # STARTUP first, SHUTDOWN last (finally:).
    assert kinds[0] == "STARTUP", f"STARTUP not first; order={kinds}"
    assert kinds[-1] == "SHUTDOWN", f"SHUTDOWN not last (finally:); order={kinds}"

    # SHUTDOWN on a clean path → exit_code 0.
    shutdown_evt = next(e for e in captured.events if e[0] == "SHUTDOWN")
    assert shutdown_evt[2] == 0, f"expected exit_code=0, got {shutdown_evt}"
    assert isinstance(shutdown_evt[1], int), "duration_ms must be an int"


@pytest.mark.asyncio
async def test_startup_emitted_before_any_order_submission(monkeypatch) -> None:
    """STARTUP must precede ORDER_SUBMITTED so the idempotency row exists
    before the engine can trade. We assert ordering on the event ledger:
    no ORDER_SUBMITTED may appear before the STARTUP marker."""
    captured = _patch(monkeypatch)

    sched = MomentumScheduler(submit_orders=True)
    await sched.run_once(as_of=datetime(2026, 5, 14, tzinfo=UTC).date())

    kinds = [e[0] for e in captured.events]
    startup_idx = kinds.index("STARTUP")
    order_idxs = [i for i, k in enumerate(kinds) if k == "ORDER_SUBMITTED"]
    assert all(
        i > startup_idx for i in order_idxs
    ), f"ORDER_SUBMITTED emitted before STARTUP; order={kinds}"


@pytest.mark.asyncio
async def test_shutdown_emitted_in_finally_on_exception(monkeypatch) -> None:
    """If the run body raises, SHUTDOWN must still fire (finally:) with a
    non-zero exit_code so the dispatcher sees the cycle terminated."""
    captured = _patch(monkeypatch)

    class _BoomSetup:
        async def scan(self, _pool, _as_of):
            raise RuntimeError("boom in setup plug")

    monkeypatch.setattr(scheduler_module, "MomentumSetupDetection", _BoomSetup)

    sched = MomentumScheduler(submit_orders=True)
    with pytest.raises(RuntimeError, match="boom in setup plug"):
        await sched.run_once(as_of=datetime(2026, 5, 14, tzinfo=UTC).date())

    kinds = [e[0] for e in captured.events]
    assert kinds[0] == "STARTUP", f"STARTUP not first; order={kinds}"
    assert "SHUTDOWN" in kinds, f"no SHUTDOWN on exception path; events={captured.events}"
    shutdown_evt = next(e for e in captured.events if e[0] == "SHUTDOWN")
    assert shutdown_evt[2] != 0, f"expected non-zero exit_code on crash, got {shutdown_evt}"
