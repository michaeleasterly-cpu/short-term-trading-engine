"""Tests for ops.allocator_heartbeat — the cron safety-net for the
event-driven allocator.

Mirrors the engine_service test scaffolding (asyncpg Pool/Conn stubs,
ops-shadow xdist_group pin, sys.modules eviction of the scripts/ops.py
shadow). The heartbeat's outcome surface is the contract: gate_closed
(no spawn), fired_inline (spawn), check_failed (raise isolated).
"""
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Same pre-test sys.modules hygiene as test_engine_service.py: full-suite
# collection may have cached scripts/ops.py as the top-level `ops`
# module first; evict any non-package `ops`/`ops.*` so the real
# regular package (ops/__init__.py) resolves and the heartbeat module
# can be imported.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import allocator_heartbeat as ah  # noqa: E402
from tpcore.engine_profile import FireDecision  # noqa: E402

# pytest-xdist: pin to one worker — the heartbeat imports
# tpcore.engine_profile + ops/engine_dispatch's neighbor module, and
# its spawn invokes scripts/ops.py. Same ops-shadow constraint as
# test_engine_service.py.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _FakePool:
    """asyncpg.Pool-shaped stub. The heartbeat passes it to
    should_fire; the patched should_fire never touches it."""


async def test_heartbeat_gate_closed_when_should_fire_returns_false():
    """should_fire(fire=False) → no spawn, return 'gate_closed'."""
    pool = _FakePool()
    decision = FireDecision(
        fire=False, reason="not a cadence boundary",
        checks={"cadence": False},
    )
    with patch.object(ah, "should_fire", AsyncMock(return_value=decision)), \
         patch.object(ah, "fire_allocator_subprocess",
                      AsyncMock(return_value=0)) as spawn:
        outcome = await ah.heartbeat(pool, now=datetime(2026, 5, 19, 22, 30, tzinfo=UTC))
    assert outcome == "gate_closed"
    spawn.assert_not_called()


async def test_heartbeat_fires_inline_when_should_fire_returns_true():
    """should_fire(fire=True) → spawn ops.py --allocate, return 'fired_inline'."""
    pool = _FakePool()
    decision = FireDecision(
        fire=True, reason="ready",
        checks={"cadence": True, "not_already_run": True},
    )
    with patch.object(ah, "should_fire", AsyncMock(return_value=decision)), \
         patch.object(ah, "fire_allocator_subprocess",
                      AsyncMock(return_value=0)) as spawn:
        outcome = await ah.heartbeat(pool, now=datetime(2026, 5, 18, 22, 30, tzinfo=UTC))
    assert outcome == "fired_inline"
    spawn.assert_awaited_once()


async def test_heartbeat_check_failed_when_should_fire_raises():
    """should_fire raise → 'check_failed', NEVER raises, no spawn.

    Defense in depth: should_fire is itself fail-CLOSED (returns
    FireDecision(False, ...) on any exception), but if its outer
    boundary somehow leaks, the heartbeat must still isolate.
    """
    pool = _FakePool()
    boom = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(ah, "should_fire", boom), \
         patch.object(ah, "fire_allocator_subprocess",
                      AsyncMock()) as spawn:
        outcome = await ah.heartbeat(pool, now=datetime(2026, 5, 18, 22, 30, tzinfo=UTC))
    assert outcome == "check_failed"
    spawn.assert_not_called()


async def test_heartbeat_returns_fired_inline_even_when_subprocess_fails():
    """A non-zero subprocess rc is logged but the heartbeat outcome is
    still 'fired_inline' (the decision-to-spawn was correct; the spawn
    outcome is the subprocess's responsibility). Operator-grep-able
    surface = the structlog 'allocator_heartbeat.fired_failed' event,
    NOT a different outcome tag."""
    pool = _FakePool()
    decision = FireDecision(fire=True, reason="ready", checks={})
    with patch.object(ah, "should_fire", AsyncMock(return_value=decision)), \
         patch.object(ah, "fire_allocator_subprocess",
                      AsyncMock(return_value=1)) as spawn:
        outcome = await ah.heartbeat(pool, now=datetime(2026, 5, 18, 22, 30, tzinfo=UTC))
    assert outcome == "fired_inline"
    spawn.assert_awaited_once()


async def test_heartbeat_idempotent_when_already_ran_this_cycle():
    """The brief's idempotency test case: re-firing the heartbeat after
    the daemon already ran the allocator this cycle is a no-op. The
    'already ran this cycle' verdict is should_fire's responsibility
    (it checks STARTUP rows via _already_ran); the heartbeat trusts
    that verdict.
    """
    pool = _FakePool()
    decision = FireDecision(
        fire=False, reason="already ran this cycle",
        checks={"cadence": True, "not_already_run": False},
    )
    with patch.object(ah, "should_fire", AsyncMock(return_value=decision)), \
         patch.object(ah, "fire_allocator_subprocess",
                      AsyncMock()) as spawn:
        outcome = await ah.heartbeat(pool, now=datetime(2026, 5, 18, 23, 0, tzinfo=UTC))
    assert outcome == "gate_closed"
    spawn.assert_not_called()


async def test_fire_allocator_subprocess_isolates_spawn_exception():
    """asyncio.create_subprocess_exec raising (OSError, etc.) returns
    -1 and logs, NEVER raises. Mirrors _invoke_allocator's spawn-time
    isolation (a launchd-fired cron must never bubble an exception
    into the launchd error file as a 'spawn failed' loop)."""
    with patch("ops.allocator_heartbeat.asyncio.create_subprocess_exec",
               AsyncMock(side_effect=OSError("no such file"))):
        rc = await ah.fire_allocator_subprocess()
    assert rc == -1


async def test_fire_allocator_subprocess_returns_actual_returncode():
    """Happy path: spawn returns rc, heartbeat surfaces it untouched."""
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("ops.allocator_heartbeat.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        rc = await ah.fire_allocator_subprocess()
    assert rc == 0


async def test_allocator_engine_constant_is_canonical():
    """The heartbeat keys off the SAME engine string the dispatcher
    uses — never re-hardcode the engine name. (Single-string-pin
    sentinel — a typo here would silently route to a phantom
    application_log filter.)"""
    assert ah.ALLOCATOR_ENGINE == "allocator"
