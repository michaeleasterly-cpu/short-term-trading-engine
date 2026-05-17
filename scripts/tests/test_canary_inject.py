import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import json  # noqa: E402

import pytest  # noqa: E402

from scripts.ops import _CANARY_INJECTION_SOURCE, _stage_canary_inject_trigger  # noqa: E402


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *a):
        self.calls.append(("fetchrow", sql, a))
        return None

    async def execute(self, sql, *a):
        self.calls.append(("execute", sql, a))
        return "DELETE 1"


class _Pool:
    def __init__(self): self.conn = _Conn()
    def acquire(self):
        pool = self
        class _Cm:
            async def __aenter__(self): return pool.conn
            async def __aexit__(self, *a): return False
        return _Cm()


async def test_inject_loss_cluster_writes_canary_only_row():
    pool = _Pool()
    out = await _stage_canary_inject_trigger(
        pool, {"kind": "loss_cluster", "streak": 5})
    ins = [c for c in pool.conn.calls
           if "INSERT INTO platform.forensics_triggers" in c[1]]
    assert len(ins) == 1
    kind, payload_json = ins[0][2][0], ins[0][2][1]
    p = json.loads(payload_json)
    assert kind == "loss_cluster"
    assert p["engine"] == "canary"
    assert p["streak_length"] == 5
    assert p["source"] == "canary_injection"
    assert p["fingerprint"]
    assert out["injected"] == "loss_cluster"


async def test_inject_rejects_non_canary_engine_param():
    pool = _Pool()
    with pytest.raises(ValueError, match="canary"):
        await _stage_canary_inject_trigger(
            pool, {"kind": "loss_cluster", "streak": 5, "engine": "reversion"})


async def test_inject_drawdown_and_outlier_kinds_shape():
    pool = _Pool()
    await _stage_canary_inject_trigger(pool, {"kind": "drawdown_period"})
    await _stage_canary_inject_trigger(pool, {"kind": "outlier_loss"})
    payloads = [json.loads(c[2][1]) for c in pool.conn.calls
                if "INSERT INTO platform.forensics_triggers" in c[1]]
    dd = next(p for p in payloads if "drawdown_pct" in p)
    ol = next(p for p in payloads if "pnl_net" in p)
    assert dd["engine"] == "canary" and dd["source"] == "canary_injection"
    assert ol["engine"] == "canary" and ol["source"] == "canary_injection"


async def test_inject_rejects_unknown_kind():
    pool = _Pool()
    with pytest.raises(ValueError, match="kind"):
        await _stage_canary_inject_trigger(pool, {"kind": "bogus"})


async def test_teardown_deletes_only_injection_marked_rows():
    pool = _Pool()
    out = await _stage_canary_inject_trigger(pool, {"teardown": True})
    dels = [c for c in pool.conn.calls
            if c[0] == "execute"
            and "DELETE FROM platform.forensics_triggers" in c[1]]
    assert len(dels) == 1
    # The source marker must scope the DELETE (prevents deleting non-injected rows).
    # With the parameterized query the literal no longer appears in the SQL text;
    # it flows in as a query arg so teardown still only removes canary-injected rows.
    assert "payload->>'source'" in dels[0][1]   # scoping predicate in SQL
    assert _CANARY_INJECTION_SOURCE in dels[0][2]  # marker passed as query arg
    assert out["teardown"] is True


async def test_inject_is_idempotent_skips_insert_when_fingerprint_exists():
    """Re-running the same kind/params must NOT write a duplicate:
    when the dedup SELECT finds an existing fingerprint, no INSERT."""
    class _ConnExists:
        def __init__(self):
            self.calls = []
        async def fetchrow(self, sql, *a):
            self.calls.append(("fetchrow", sql, a))
            return {"1": 1}  # fingerprint already present
        async def execute(self, sql, *a):
            self.calls.append(("execute", sql, a))
            return "INSERT 0 1"

    class _PoolExists:
        def __init__(self):
            self.conn = _ConnExists()
        def acquire(self):
            pool = self
            class _Cm:
                async def __aenter__(self):
                    return pool.conn
                async def __aexit__(self, *a):
                    return False
            return _Cm()

    pool = _PoolExists()
    out = await _stage_canary_inject_trigger(
        pool, {"kind": "loss_cluster", "streak": 5})
    inserts = [c for c in pool.conn.calls
               if c[0] == "execute"
               and "INSERT INTO platform.forensics_triggers" in c[1]]
    assert len(inserts) == 0          # dedup hit → no duplicate INSERT
    assert out["injected"] == "loss_cluster"  # still reports the (deduped) kind
