from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from canary import scheduler as cs


async def test_non_trading_day_early_return():
    with patch.object(cs, "is_trading_day", return_value=False):
        out = await cs.run_once(as_of=datetime(2026, 5, 17).date())  # noqa: DTZ001
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
        return cs._Components(db_log=db_log, price=Decimal("500"),  # noqa: SLF001
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
        out = await cs.run_once(as_of=datetime(2026, 5, 6).date())  # noqa: DTZ001

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
            await cs.run_once(as_of=datetime(2026, 5, 6).date())  # noqa: DTZ001

    assert rec["startup"] == 1  # STARTUP emitted BEFORE the crash
    assert rec["shutdown"] == 1  # SHUTDOWN emitted in finally (exit_code=1)


async def test_run_components_detects_prior_canary_spy_holding():
    """CAN-CR-1 regression: prior_qty must be >0 when a SPY position
    exists that the canary opened (cid prefixed 'ca_'). The old
    getattr(p,'engine_id') guard made this permanently 0."""
    from canary.scheduler import _run_components

    class _Pos:
        def __init__(self, symbol, qty):
            self.symbol = symbol
            self.qty = qty

    class _Ord:
        def __init__(self, symbol, cid):
            self.symbol = symbol
            self.client_order_id = cid

    class _Conn:
        async def fetchrow(self, *a, **k): return {"close": Decimal("500")}

    class _Pool:
        def acquire(self):
            class _Cm:
                async def __aenter__(s): return _Conn()
                async def __aexit__(s, *a): return False
            return _Cm()

    class _Broker:
        async def get_positions(self):
            return [_Pos("SPY", 1), _Pos("AAPL", 9)]
        async def list_recent_orders(self, limit=500):
            return [_Ord("SPY", "ca_SPY_20260506"),   # canary's own
                    _Ord("AAPL", "rv_AAPL_20260506")]  # another engine
        async def place_order(self, o): ...

    comp = await _run_components(_Pool(), _Broker(), object(), object())
    assert comp.prior_qty == 1          # SPY held via a ca_ order
    assert comp.price == Decimal("500")
    # negative control: a SPY position with NO canary order ⇒ not counted
    class _Broker2(_Broker):
        async def list_recent_orders(self, limit=500):
            return [_Ord("SPY", "sn_SPY_x")]  # sentinel's, not canary's
    comp2 = await _run_components(_Pool(), _Broker2(), object(), object())
    assert comp2.prior_qty == 0


async def test_dry_run_skips_real_order_placement():
    """--dry-run path: gates/startup/shutdown still run, but no real
    broker order is placed (smoke-safe)."""
    rec = {"placed": [], "startup": 0, "shutdown": 0}

    class _DBLog:
        async def startup(self, *a, **k): rec["startup"] += 1
        async def shutdown(self, *a, **k): rec["shutdown"] += 1
        async def signal(self, *a, **k): ...
        async def order_submitted(self, *a, **k): ...

    async def _place(o): rec["placed"].append(o)

    class _Gov:
        async def register_engine(self, *a, **k): ...
        async def state_for(self, *a, **k): return None
        async def record_fill(self, *a, **k): ...

    class _FakePool:
        async def close(self): ...

    async def _components(pool, broker, governor, db_log):
        return cs._Components(db_log=db_log, price=Decimal("500"),  # noqa: SLF001
                              prior_qty=1, aar_write=lambda a: None,
                              place=_place, governor=_Gov())

    with patch.object(cs, "is_trading_day", lambda *_a, **_k: True), \
         patch.object(cs, "gate_batch_order",
                      new=AsyncMock(return_value=True)), \
         patch.object(cs, "_run_components", _components), \
         patch.object(cs, "build_asyncpg_pool",
                      new=AsyncMock(return_value=_FakePool())), \
         patch.object(cs, "DBLogHandler", lambda *a, **k: _DBLog()), \
         patch.object(cs, "AlpacaPaperBrokerAdapter", lambda *a, **k: object()), \
         patch.object(cs, "RiskGovernor", lambda *a, **k: _Gov()), \
         patch.object(cs, "PostgresRiskStateStore", lambda *a, **k: object()), \
         patch.dict("os.environ", {"DATABASE_URL": "postgres://x"}):
        out = await cs.run_once(as_of=datetime(2026, 5, 6).date(),  # noqa: DTZ001
                                dry_run=True)

    assert out["action"] == "round_trip"
    assert rec["startup"] == 1 and rec["shutdown"] == 1
    assert rec["placed"] == []   # NO real order placed under --dry-run


def test_scheduler_module_has_main_entrypoint():
    """The dispatcher runs `python -m canary.scheduler`; the smoke loop
    runs it with --dry-run. The module MUST be invocable (a __main__
    block) or the dispatcher silently no-ops."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    # Assert the __main__ guard is present in the source.
    src = (repo / "scheduler.py").read_text()
    assert 'if __name__ == "__main__":' in src
    assert "--dry-run" in src
