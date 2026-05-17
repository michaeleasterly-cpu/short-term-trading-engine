"""Regression test: MomentumScheduler must emit STARTUP / SHUTDOWN.

Prerequisite for the event-driven dispatcher (Sub-project B).
``tpcore.engine_profile.should_fire`` keys its "already ran this cycle"
idempotency off a ``STARTUP`` row in ``platform.application_log`` inside
the cadence window. Momentum previously emitted NO STARTUP/SHUTDOWN
events (only SIGNAL / ORDER_SUBMITTED / EQUITY_SNAPSHOT), so once B
removes momentum's own ``is_rebalance_day`` cadence gate momentum would
re-rebalance on every readiness event in its first-trading-day window
(a double trade for a MONTHLY engine).

These tests assert:

* ``run_once`` emits a ``STARTUP`` event, and it is emitted *before*
  any order submission.
* ``run_once`` emits a ``SHUTDOWN`` event in ``finally:`` even on the
  no-rebalance early-return path (the dominant path for a monthly engine
  called every trading day).

Mirrors the fake-pool / monkeypatch style of
``test_scheduler_kill_switch.py`` — the capture is done with a fake
``DBLogHandler`` recording every event call in order.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from momentum import scheduler as scheduler_module
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


class _StubGovernor:
    """Non-killed state → scheduler proceeds past the kill-switch gate."""

    async def register_engine(self, *_a, **_k) -> None:
        return None

    async def state_for(self, _engine_id):
        return None


class _StubLifecycleNoRebalance:
    """Lifecycle plug that says 'not a rebalance day' → early return."""

    async def assess(self, _pool, _as_of):
        class _Plan:
            is_rebalance_day = False
            reason = "not first trading day of month"

        return _Plan()


def _patch(monkeypatch) -> _CapturingDBLog:
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    captured = _CapturingDBLog()

    async def _fake_build_pool(*_a, **_k):
        return _NoopAsyncpgPool()

    class _NoopBroker:
        async def get_account(self):  # pragma: no cover - not reached
            raise AssertionError("broker must not be touched on no-rebalance path")

        async def get_positions(self):  # pragma: no cover
            raise AssertionError("broker must not be touched on no-rebalance path")

    class _NoopStateStore:
        def __init__(self, *, pool):
            pass

    def _fake_governor_ctor(**_k):
        return _StubGovernor()

    def _fake_dblog_ctor(*_a, **_k):
        return captured

    monkeypatch.setattr(scheduler_module, "build_asyncpg_pool", _fake_build_pool)
    monkeypatch.setattr(scheduler_module, "AlpacaPaperBrokerAdapter", _NoopBroker)
    monkeypatch.setattr(scheduler_module, "PostgresRiskStateStore", _NoopStateStore)
    monkeypatch.setattr(scheduler_module, "RiskGovernor", _fake_governor_ctor)
    monkeypatch.setattr(scheduler_module, "DBLogHandler", _fake_dblog_ctor)
    monkeypatch.setattr(
        scheduler_module, "MomentumLifecycleAnalysis", _StubLifecycleNoRebalance
    )
    return captured


@pytest.mark.asyncio
async def test_run_once_emits_startup_and_shutdown_on_no_rebalance(monkeypatch) -> None:
    """No-rebalance early return must still emit STARTUP and SHUTDOWN.

    This is the dominant operational path: a monthly engine called every
    trading day no-ops on ~21/22 sessions. The idempotency STARTUP row
    must be written on every one of those, and SHUTDOWN must fire in the
    ``finally:`` even though the body returned early.
    """
    captured = _patch(monkeypatch)

    sched = MomentumScheduler(submit_orders=True)
    result = await sched.run_once(as_of=datetime(2026, 5, 14, tzinfo=UTC).date())

    assert result.is_rebalance_day is False

    kinds = [e[0] for e in captured.events]
    assert "STARTUP" in kinds, f"no STARTUP emitted; events={captured.events}"
    assert "SHUTDOWN" in kinds, f"no SHUTDOWN emitted; events={captured.events}"

    # STARTUP first, SHUTDOWN last (finally:).
    assert kinds[0] == "STARTUP", f"STARTUP not first; order={kinds}"
    assert kinds[-1] == "SHUTDOWN", f"SHUTDOWN not last (finally:); order={kinds}"

    # SHUTDOWN on a clean early-return path → exit_code 0.
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

    class _BoomLifecycle:
        async def assess(self, _pool, _as_of):
            raise RuntimeError("boom in lifecycle plug")

    monkeypatch.setattr(scheduler_module, "MomentumLifecycleAnalysis", _BoomLifecycle)

    sched = MomentumScheduler(submit_orders=True)
    with pytest.raises(RuntimeError, match="boom in lifecycle plug"):
        await sched.run_once(as_of=datetime(2026, 5, 14, tzinfo=UTC).date())

    kinds = [e[0] for e in captured.events]
    assert kinds[0] == "STARTUP", f"STARTUP not first; order={kinds}"
    assert "SHUTDOWN" in kinds, f"no SHUTDOWN on exception path; events={captured.events}"
    shutdown_evt = next(e for e in captured.events if e[0] == "SHUTDOWN")
    assert shutdown_evt[2] != 0, f"expected non-zero exit_code on crash, got {shutdown_evt}"
