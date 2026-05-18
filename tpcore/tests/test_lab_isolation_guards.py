"""T4 — fail-closed reentrancy guards at the 5 live-side-effect boundaries.

Inside an active ``LabContext`` a Lab bug that reaches a live-write
constructor (or the STARTUP-row write) must fail closed with
``LabIsolationViolation``. The guards are additive + INERT outside a
Lab run (they only raise when ``_LAB_ACTIVE`` is set).
"""
from __future__ import annotations

import uuid

import pytest

from tpcore.lab.context import LabContext, LabIsolationViolation


async def test_live_constructors_fail_closed_in_lab():
    async with LabContext(db_url="postgres://x/y", build_pools=False):
        from tpcore.aar.writer import AARWriter

        with pytest.raises(LabIsolationViolation):
            AARWriter(None)

        from tpcore.alpaca.broker_adapter import AlpacaPaperBrokerAdapter

        with pytest.raises(LabIsolationViolation):
            AlpacaPaperBrokerAdapter()

        # RiskGovernor.__init__ — the guard is the FIRST body line so it
        # fires before any argument is used; None args are fine here.
        from tpcore.risk.governor import RiskGovernor

        with pytest.raises(LabIsolationViolation):
            RiskGovernor(None, None)  # type: ignore[arg-type]

        # DBLogHandler.startup — the guard lives in startup(), NOT
        # __init__ (the latter is constructed widely with None pools in
        # tests). Construct with a dummy non-None pool, then await
        # startup() inside the Lab — the STARTUP-row write must fail closed.
        from tpcore.logging.db_handler import DBLogHandler

        handler = DBLogHandler(object(), "lab-test", uuid.uuid4())  # type: ignore[arg-type]
        with pytest.raises(LabIsolationViolation):
            await handler.startup()

        # BaseOrderManager.__init__ — the guard is the FIRST body line so
        # it fires before any argument is dereferenced; None/dummy args safe.
        from tpcore.order_management.base_order_manager import BaseOrderManager

        with pytest.raises(LabIsolationViolation):
            BaseOrderManager(  # type: ignore[call-arg]
                broker=None,
                governor=None,
                capital_gate=None,
                lifecycle=None,
                aar=None,
            )


def test_live_constructors_ok_outside_lab():
    from tpcore.aar.writer import AARWriter

    AARWriter(None)  # no raise outside a Lab run


async def test_db_handler_startup_ok_outside_lab():
    """The startup() guard is inert outside a Lab run — startup() must
    not raise ``LabIsolationViolation`` (DB errors are swallowed by
    ``log()`` so the call completes without surfacing anything)."""
    import uuid as _uuid

    from tpcore.logging.db_handler import DBLogHandler

    class _DummyPool:
        # acquire() IS called by DBLogHandler.log() on the inert-outside
        # path (startup() does not raise outside a Lab run so log() runs
        # and does `async with self._pool.acquire()`); its error is swallowed
        # by log()'s except Exception — that swallow path is what this test
        # exercises (startup() must NOT raise LabIsolationViolation).
        def acquire(self):
            raise AssertionError("dummy pool — error swallowed by log()")

    handler = DBLogHandler(_DummyPool(), "outside-lab", _uuid.uuid4())  # type: ignore[arg-type]
    await handler.startup()  # no LabIsolationViolation outside a Lab run
