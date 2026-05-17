from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from canary import scheduler as cs


async def test_non_trading_day_early_return():
    with patch.object(cs, "is_trading_day", return_value=False):
        out = await cs.run_once(as_of=datetime(2026, 5, 17).date())
    assert out["action"] == "non_trading_day"


async def test_trading_day_round_trip_writes_one_aar_and_logs_lifecycle():
    rec = {"aars": [], "gated": [], "placed": [], "startup": 0, "shutdown": 0}

    class _DBLog:
        async def startup(self, *a, **k): rec["startup"] += 1
        async def shutdown(self, *a, **k): rec["shutdown"] += 1
        async def signal(self, *a, **k): ...
        async def order_submitted(self, *a, **k): ...

    fake_db = _DBLog()

    async def _fake_gate(gov, eng, *, ticker, notional, direction, **k):
        rec["gated"].append(
            (getattr(direction, "value", str(direction)).lower(), ticker))
        return True

    class _Gov:
        async def register_engine(self, *a, **k): ...
        async def state_for(self, *a, **k): return None
        async def record_fill(self, *a, **k): ...

    async def _aar_write(aar): rec["aars"].append(aar)
    async def _place(o): rec["placed"].append(o)

    class _FakePool:
        async def close(self): ...

    async def _components(pool, broker, governor, db_log):
        # production threads the SAME real db_log through; mirror that.
        return cs._Components(db_log=db_log, price=Decimal("500"),
                              prior_qty=1, aar_write=_aar_write,
                              place=_place, governor=_Gov())

    with patch.object(cs, "is_trading_day", lambda *_a, **_k: True), \
         patch.object(cs, "gate_batch_order", _fake_gate), \
         patch.object(cs, "_run_components", _components), \
         patch.object(cs, "build_asyncpg_pool",
                      new=AsyncMock(return_value=_FakePool())), \
         patch.object(cs, "DBLogHandler", lambda *a, **k: fake_db), \
         patch.object(cs, "AlpacaPaperBrokerAdapter", lambda *a, **k: object()), \
         patch.object(cs, "RiskGovernor", lambda *a, **k: _Gov()), \
         patch.object(cs, "PostgresRiskStateStore", lambda *a, **k: object()), \
         patch.dict("os.environ", {"DATABASE_URL": "postgres://x"}):
        out = await cs.run_once(as_of=datetime(2026, 5, 6).date())

    assert out["action"] == "round_trip"
    assert rec["startup"] == 1 and rec["shutdown"] == 1
    sides = [d for d, _ in rec["gated"]]
    assert sides.count("sell") == 1 and sides.count("buy") == 1
    assert len(rec["aars"]) == 1


async def test_startup_emitted_before_setup_crash():
    """DA-1 substrate: a setup-time failure (broker/governor/components)
    MUST still leave a STARTUP (so crashed_startup can detect it).
    startup() is emitted FIRST, before any fallible setup work."""
    rec = {"startup": 0, "shutdown": 0}

    class _DBLog:
        async def startup(self, *a, **k): rec["startup"] += 1
        async def shutdown(self, *a, **k): rec["shutdown"] += 1

    fake_db = _DBLog()

    class _FakePool:
        async def close(self): ...

    def _boom(*a, **k):
        raise RuntimeError("broker setup blew up")

    with patch.object(cs, "is_trading_day", lambda *_a, **_k: True), \
         patch.object(cs, "build_asyncpg_pool",
                      new=AsyncMock(return_value=_FakePool())), \
         patch.object(cs, "DBLogHandler", lambda *a, **k: fake_db), \
         patch.object(cs, "AlpacaPaperBrokerAdapter", _boom), \
         patch.dict("os.environ", {"DATABASE_URL": "postgres://x"}):
        with pytest.raises(RuntimeError, match="broker setup blew up"):
            await cs.run_once(as_of=datetime(2026, 5, 6).date())

    assert rec["startup"] == 1  # STARTUP emitted BEFORE the crash
    assert rec["shutdown"] == 1  # SHUTDOWN emitted in finally (exit_code=1)
