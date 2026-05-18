"""select_novel_escalations: MUST call engine_ladder.list_undispositioned
(NOT reimplement open-set/grace/escalate-only — the bug
symmetry-not-copy forbids), drop prior ENGINE_LLM_TRIAGE_PROPOSAL,
oldest-first cap at MAX_TRIAGE_PER_CYCLE. MUST NOT test
policy_for() is None (proven dead — spec §7). Fake pool, no LLM.
"""
from __future__ import annotations

import inspect
import types
from datetime import UTC, datetime

import tpcore.engine_llm_triage.select as S
from tpcore.engine_llm_triage.select import (
    MAX_TRIAGE_PER_CYCLE,
    EngineNovelEscalation,
    select_novel_escalations,
)
from tpcore.llm_data_triage.select import (
    MAX_TRIAGE_PER_CYCLE as DATA_MAX,
)


class _Conn:
    def __init__(self, p):
        self._p = p

    async def fetch(self, sql, *a):
        if "ENGINE_LLM_TRIAGE_PROPOSAL" in sql:
            return [{"hold_id": h} for h in self._p.prior_hold_ids]
        return []


class _CM:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *e):
        return None


class _Pool:
    def __init__(self, undispositioned=(), prior_hold_ids=()):
        self.undispositioned = list(undispositioned)
        self.prior_hold_ids = list(prior_hold_ids)

    def acquire(self):
        return _CM(_Conn(self))


def _fake_ladder(rows):
    """A collision-free stand-in for the lazily-imported
    `ops.engine_ladder` read-predicate module."""
    mod = types.SimpleNamespace()

    async def _list_undispositioned(pool, **kw):
        _list_undispositioned.called = True
        return list(rows)

    _list_undispositioned.called = False
    mod.list_undispositioned = _list_undispositioned
    return mod


def _ud(hold_id, *, engine="reversion", fc="scheduler_crash"):
    """A list_undispositioned() row shape."""
    return {
        "hold_id": hold_id, "engine": engine, "failure_class": fc,
        "reason": "r", "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
        "shape": "escalate-only", "policy_default": "structural",
        "policy_rationale": "because",
    }


async def test_reuses_max_cap_from_187() -> None:
    """Reuses the #187 cap value — not a re-derived constant."""
    assert MAX_TRIAGE_PER_CYCLE == DATA_MAX


async def test_calls_list_undispositioned_not_reimplemented(monkeypatch) -> None:
    """The selection delegates the open/grace/escalate-only semantics
    to engine_ladder.list_undispositioned — it does not reimplement
    them (the bug symmetry-not-copy forbids)."""
    ladder = _fake_ladder([_ud("h1"), _ud("h2")])
    monkeypatch.setattr(S, "_engine_ladder", lambda: ladder)
    out = await select_novel_escalations(_Pool())
    assert ladder.list_undispositioned.called is True
    assert [e.hold_id for e in out] == ["h1", "h2"]
    assert all(isinstance(e, EngineNovelEscalation) for e in out)


async def test_does_not_test_policy_for_is_none() -> None:
    """Static guard (AST, executable code only — docstrings excluded):
    the module must NOT call `policy_for` at all (proven dead — spec
    §7) and must NOT reimplement the open-set/candidate SQL."""
    import ast

    tree = ast.parse(inspect.getsource(S))
    # strip every docstring so the prose explaining the dead predicate
    # does not false-positive the guard
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute)
                    else getattr(f, "id", None))
            if name:
                called.add(name)
        if isinstance(node, (ast.Str, ast.Constant)) and isinstance(
                getattr(node, "value", node.s if isinstance(node, ast.Str)
                        else None), str):
            pass  # constants/docstrings are not call sites
    assert "policy_for" not in called  # the dead §7 predicate
    # MUST delegate to the canonical open-set function
    assert "list_undispositioned" in called
    # must NOT define its own candidate/open-set SQL constant (the
    # reimplementation symmetry-not-copy forbids) — check assigned names
    assigned = {
        t.id
        for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert "_CANDIDATE_SQL" not in assigned


async def test_dedup_skips_prior_proposal(monkeypatch) -> None:
    ladder = _fake_ladder([_ud("h1"), _ud("h2"), _ud("h3")])
    monkeypatch.setattr(S, "_engine_ladder", lambda: ladder)
    pool = _Pool(prior_hold_ids=["h2"])
    out = await select_novel_escalations(pool)
    assert [e.hold_id for e in out] == ["h1", "h3"]


async def test_bounded_oldest_first(monkeypatch) -> None:
    # list_undispositioned already returns oldest-first
    rows = [_ud(f"r{i}") for i in range(MAX_TRIAGE_PER_CYCLE + 3)]
    ladder = _fake_ladder(rows)
    monkeypatch.setattr(S, "_engine_ladder", lambda: ladder)
    out = await select_novel_escalations(_Pool())
    assert len(out) == MAX_TRIAGE_PER_CYCLE
    assert [e.hold_id for e in out] == [
        f"r{i}" for i in range(MAX_TRIAGE_PER_CYCLE)]


async def test_policy_default_attached_for_packet(monkeypatch) -> None:
    """policy_for is advisory context (default+rationale) attached to
    the result — NEVER a selection gate."""
    ladder = _fake_ladder([_ud("h1")])
    monkeypatch.setattr(S, "_engine_ladder", lambda: ladder)
    out = await select_novel_escalations(_Pool())
    assert out[0].policy_default == "structural"
    assert out[0].policy_rationale == "because"
