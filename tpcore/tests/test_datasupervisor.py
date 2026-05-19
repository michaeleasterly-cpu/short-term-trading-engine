"""Unit tests for datasupervise — fake pool whose red-set + open holds
+ per-source hold + cycle-count are scripted. No DB/subprocess.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from tpcore.datasupervisor.supervisor import datasupervise


class _Conn:
    def __init__(self, p):
        self._p = p

    async def fetch(self, sql, *a):
        if "validation.%" in sql:
            return [{"source": f"validation.{c}"} for c in self._p.val_red]
        if "cross_table_audit.%" in sql:
            return [{"source": f"cross_table_audit.{k}"}
                    for k in self._p.ct_red]
        if "AdapterContractDrift" in sql:
            return [{"error": f"adapter_contract_drift: feed={f!r} x"}
                    for f in self._p.contract_red]
        if "c.event_type IS NULL" in sql:           # open-hold discovery
            return [{"source": s} for s in self._p.open_holds]
        return []

    async def fetchrow(self, sql, *a):
        # Dedup-escalation query: SELECT 1 ... event_type=$1 AND data->>'hold_id'=$2 LIMIT 1
        # Discriminator: "SELECT 1" in sql (current_source_hold uses SELECT h.data...
        # with ORDER BY, never "SELECT 1").
        if "SELECT 1" in sql:
            hold_id = a[1]  # $2 is arg index 1 (0-based after sql)
            if hold_id in self._p._already_escalated_hold_ids:  # noqa: SLF001
                return {"?column?": 1}
            return None
        return self._p.open_holds.get(a[2])

    async def fetchval(self, sql, *a):
        return self._p.cycles_since_hold

    async def execute(self, sql, *a):
        self._p.emitted.append((a[2], json.loads(a[5])))  # event_type, data


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, *, val_red=(), ct_red=(), contract_red=(),
                 open_holds=None, cycles_since_hold=0):
        self.val_red = list(val_red)
        self.ct_red = list(ct_red)
        self.contract_red = list(contract_red)
        # open_holds: {source: hold-row-dict} ; also drives discovery
        self.open_holds = open_holds or {}
        self.cycles_since_hold = cycles_since_hold
        self.emitted: list[tuple] = []
        # hold_ids for which DATA_SOURCE_ESCALATED was already emitted
        self._already_escalated_hold_ids: set[str] = set()

    def acquire(self): return _CM(_Conn(self))


def _ets(p): return [et for et, _ in p.emitted]


async def test_green_cycle_no_events() -> None:
    p = _Pool()
    out = await datasupervise(p, "rid")
    assert p.emitted == [] and out.opened == [] and out.cleared == []
    assert out.error is None


async def test_opens_hold_for_each_red_source(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S
    monkeypatch.setattr(S, "_healspec_source", lambda c: "prices_daily")
    p = _Pool(val_red=["prices_daily_freshness"],
              ct_red=["tradier_options_chains/expiration_in_past"],
              contract_red=["fred_macro"])
    out = await datasupervise(p, "rid")
    held = {d["source"] for et, d in p.emitted if et == "DATA_SOURCE_HELD"}
    assert held == {"validation:prices_daily",
                    "cross_table:tradier_options_chains",
                    "contract:fred_macro"}
    assert set(out.opened) == held


async def test_idempotent_when_already_held(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S
    monkeypatch.setattr(S, "_healspec_source", lambda c: "prices_daily")
    row = {"hold_id": "h1", "reason": "r",
           "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}
    p = _Pool(val_red=["prices_daily_freshness"],
              open_holds={"validation:prices_daily": row},
              cycles_since_hold=1)
    await datasupervise(p, "rid")
    assert "DATA_SOURCE_HELD" not in _ets(p)


async def test_autoclear_when_source_green_after_hold() -> None:
    row = {"hold_id": "h9", "reason": "r",
           "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}
    # open hold exists, but NO red this cycle -> source recovered
    p = _Pool(open_holds={"contract:fred_macro": row},
              cycles_since_hold=1)
    out = await datasupervise(p, "rid")
    assert "DATA_SOURCE_CLEARED" in _ets(p)
    assert "DATA_SUPERVISOR_RECOVERED" in _ets(p)
    assert out.cleared == ["contract:fred_macro"]


async def test_bounded_escalate_at_M(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S
    monkeypatch.setattr(S, "_healspec_source", lambda c: "prices_daily")
    row = {"hold_id": "h1", "reason": "r",
           "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}
    p = _Pool(val_red=["prices_daily_freshness"],
              open_holds={"validation:prices_daily": row},
              cycles_since_hold=3)
    await datasupervise(p, "rid")
    assert "DATA_SOURCE_ESCALATED" in _ets(p)


async def test_crash_isolated(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(S, "_red_sources", boom)
    out = await datasupervise(_Pool(val_red=["x"]), "rid")
    assert out.error is not None and out.opened == []


def test_contract_red_sql_uses_jsonb_exception_type() -> None:
    # Regression guard: application_log has no top-level exception_type
    # column; it MUST be data->>'exception_type' (fake pools can't catch
    # this — the final holistic review did).
    import tpcore.datasupervisor.supervisor as S
    assert "data->>'exception_type'" in S._CONTRACT_RED_SQL  # noqa: SLF001
    assert "\n      AND exception_type" not in S._CONTRACT_RED_SQL  # noqa: SLF001


async def test_escalation_deduped_per_hold(monkeypatch) -> None:
    from datetime import UTC, datetime

    import tpcore.datasupervisor.supervisor as S
    monkeypatch.setattr(S, "_healspec_source", lambda c: "prices_daily")
    row = {"hold_id": "h1", "reason": "r",
           "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}

    # First pass: n>=3, no prior escalation -> emits exactly one.
    p1 = _Pool(val_red=["prices_daily_freshness"],
               open_holds={"validation:prices_daily": row},
               cycles_since_hold=3)
    await datasupervise(p1, "rid")
    assert _ets(p1).count("DATA_SOURCE_ESCALATED") == 1

    # Second pass: a prior DATA_SOURCE_ESCALATED for h1 already exists
    # -> must NOT re-emit.
    p2 = _Pool(val_red=["prices_daily_freshness"],
               open_holds={"validation:prices_daily": row},
               cycles_since_hold=4)
    p2._already_escalated_hold_ids = {"h1"}  # noqa: SLF001
    await datasupervise(p2, "rid")
    assert "DATA_SOURCE_ESCALATED" not in _ets(p2)
