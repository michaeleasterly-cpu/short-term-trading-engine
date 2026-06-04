import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import json  # noqa: E402

import pytest  # noqa: E402

from scripts.ops import _CANARY_INJECTION_SOURCE, _stage_canary_inject_trigger  # noqa: E402

# Plan 2: canary triggers are injected into platform.data_quality_log
# (kind='forensics_trigger') via tpcore.forensics.dql_store. The EXISTS check
# + the INSERT…RETURNING both run through conn.fetchval; teardown is a DELETE
# scoped on notes->>'source'. The fakes below mirror that surface.


class _Conn:
    def __init__(self, fingerprint_exists: bool = False):
        self.calls = []
        self._fp_exists = fingerprint_exists

    async def fetchval(self, sql, *a):
        self.calls.append(("fetchval", sql, a))
        if "INSERT INTO platform.data_quality_log" in sql:
            return "ca-uuid-0001"  # RETURNING id
        # EXISTS check
        return 1 if self._fp_exists else None

    async def execute(self, sql, *a):
        self.calls.append(("execute", sql, a))
        return "DELETE 1"


class _Pool:
    def __init__(self, fingerprint_exists: bool = False):
        self.conn = _Conn(fingerprint_exists=fingerprint_exists)

    def acquire(self):
        pool = self
        class _Cm:
            async def __aenter__(self): return pool.conn
            async def __aexit__(self, *a): return False
        return _Cm()


def _inserts(conn):
    # dql_store.insert_trigger binds (kind_const, source, fired_at, notes_json).
    return [c for c in conn.calls
            if c[0] == "fetchval" and "INSERT INTO platform.data_quality_log" in c[1]]


async def test_inject_loss_cluster_writes_canary_only_row():
    pool = _Pool()
    out = await _stage_canary_inject_trigger(
        pool, {"kind": "loss_cluster", "streak": 5})
    ins = _inserts(pool.conn)
    assert len(ins) == 1
    notes_json = ins[0][2][3]  # 4th bind arg
    p = json.loads(notes_json)
    assert p["trigger_kind"] == "loss_cluster"
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
    payloads = [json.loads(c[2][3]) for c in _inserts(pool.conn)]
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
            and "DELETE FROM platform.data_quality_log" in c[1]]
    assert len(dels) == 1
    # The source marker must scope the DELETE (prevents deleting non-injected
    # rows). The kind discriminator + notes->>'source' predicate are in the SQL;
    # the marker value flows in as a query arg.
    assert "notes->>'source'" in dels[0][1]   # scoping predicate in SQL
    assert _CANARY_INJECTION_SOURCE in dels[0][2]  # marker passed as query arg
    assert out["teardown"] is True


async def test_inject_is_idempotent_skips_insert_when_fingerprint_exists():
    """Re-running the same kind/params must NOT write a duplicate:
    when the EXISTS check finds an existing fingerprint, no INSERT."""
    pool = _Pool(fingerprint_exists=True)
    out = await _stage_canary_inject_trigger(
        pool, {"kind": "loss_cluster", "streak": 5})
    assert len(_inserts(pool.conn)) == 0       # dedup hit → no duplicate INSERT
    assert out["injected"] == "loss_cluster"   # still reports the (deduped) kind
