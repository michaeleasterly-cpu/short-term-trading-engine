from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

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

    async def _fake_gate(gov, eng, *, ticker, notional, direction, **k):
        rec["gated"].append((getattr(direction, "value", str(direction)).lower(), ticker))
        return True

    class _Gov:
        async def record_fill(self, *a, **k): ...

    async def _aar_write(aar): rec["aars"].append(aar)
    async def _place(o): rec["placed"].append(o)

    with patch.object(cs, "is_trading_day", lambda *_a, **_k: True), \
         patch.object(cs, "gate_batch_order", _fake_gate), \
         patch.object(cs, "_run_components",
                      new=AsyncMock(return_value=cs._Components(
                          db_log=_DBLog(), price=Decimal("500"), prior_qty=1,
                          aar_write=_aar_write, place=_place, governor=_Gov()))), \
         patch.object(cs, "build_asyncpg_pool",
                      new=AsyncMock(return_value=_FakePool())), \
         patch.object(cs, "AlpacaPaperBrokerAdapter", lambda *a, **k: object()), \
         patch.object(cs, "RiskGovernor", lambda *a, **k: _RGStub()), \
         patch.object(cs, "PostgresRiskStateStore", lambda *a, **k: object()), \
         patch.dict("os.environ", {"DATABASE_URL": "postgres://x"}):
        out = await cs.run_once(as_of=datetime(2026, 5, 6).date())

    assert out["action"] == "round_trip"
    assert rec["startup"] == 1 and rec["shutdown"] == 1
    sides = [d for d, _ in rec["gated"]]
    assert sides.count("sell") == 1 and sides.count("buy") == 1
    assert len(rec["aars"]) == 1


class _FakePool:
    async def close(self): ...

class _RGStub:
    async def register_engine(self, *a, **k): ...
    async def state_for(self, *a, **k): return None
