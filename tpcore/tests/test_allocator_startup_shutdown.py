from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from tpcore.allocator import AllocatorService


def _svc() -> AllocatorService:
    # pool=None keeps _db_log lazily None; we inject a fake handler.
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    svc._db_log = AsyncMock()  # noqa: SLF001
    return svc


async def test_run_once_emits_startup_then_shutdown_on_success():
    svc = _svc()
    with patch.object(svc, "_load_histories", AsyncMock(return_value={})), \
         patch.object(svc, "_decide", return_value=[]), \
         patch.object(svc, "_compute_drift",
                      AsyncMock(return_value=(Decimal("0"), {}))), \
         patch.object(svc, "_fetch_market_regime",
                      AsyncMock(return_value=("trending", None))), \
         patch.object(svc, "_classify_rebalance",
                      return_value=(None, "drift ok")), \
         patch.object(svc, "_persist", AsyncMock(return_value=[])):
        await svc.run_once()

    svc._db_log.startup.assert_awaited_once()  # noqa: SLF001
    svc._db_log.shutdown.assert_awaited_once()  # noqa: SLF001
    assert svc._db_log.shutdown.call_args[0][1] == 0  # exit_code  # noqa: SLF001
    svc._db_log.error.assert_not_awaited()  # noqa: SLF001


async def test_run_once_shutdown_exit1_and_error_on_exception():
    svc = _svc()
    boom = RuntimeError("histories failed")
    with patch.object(svc, "_load_histories", AsyncMock(side_effect=boom)):
        with pytest.raises(RuntimeError, match="histories failed"):
            await svc.run_once()

    svc._db_log.startup.assert_awaited_once()  # noqa: SLF001
    svc._db_log.error.assert_awaited_once()  # noqa: SLF001
    svc._db_log.shutdown.assert_awaited_once()  # noqa: SLF001
    assert svc._db_log.shutdown.call_args[0][1] == 1  # exit_code  # noqa: SLF001


async def test_run_once_no_db_log_is_a_noop_not_a_crash():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    assert svc._db_log is None  # noqa: SLF001
    with patch.object(svc, "_load_histories", AsyncMock(return_value={})), \
         patch.object(svc, "_decide", return_value=[]), \
         patch.object(svc, "_compute_drift",
                      AsyncMock(return_value=(Decimal("0"), {}))), \
         patch.object(svc, "_fetch_market_regime",
                      AsyncMock(return_value=("trending", None))), \
         patch.object(svc, "_classify_rebalance",
                      return_value=(None, "drift ok")), \
         patch.object(svc, "_persist", AsyncMock(return_value=[])):
        await svc.run_once()  # must not raise
