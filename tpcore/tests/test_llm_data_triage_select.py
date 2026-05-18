"""select_novel_escalations: only open+undispositioned+
policy_for==ESCALATE_OPERATOR+no-prior-DATA_LLM_TRIAGE_PROPOSAL,
bounded oldest-first. Fake pool, no LLM."""
from __future__ import annotations

from datetime import UTC, datetime

from tpcore.llm_data_triage.select import (
    MAX_TRIAGE_PER_CYCLE,
    select_novel_escalations,
)


class _Conn:
    def __init__(self, p): self._p = p
    async def fetch(self, sql, *a):
        if "OPEN_ESCALATIONS" in sql:
            return [dict(r) for r in self._p.open_rows]
        if "DATA_LLM_TRIAGE_PROPOSAL" in sql:
            return [{"ref": r} for r in self._p.prior_refs]
        return []


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, open_rows=(), prior_refs=()):
        self.open_rows = list(open_rows)
        self.prior_refs = list(prior_refs)
    def acquire(self): return _CM(_Conn(self))


def _row(ref, cls, etype="DATA_SOURCE_ESCALATED"):
    return {"ref": ref, "etype": etype, "cls": cls,
            "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
            "message": "m"}


async def test_only_escalate_operator_class(monkeypatch) -> None:
    import tpcore.llm_data_triage.select as S
    monkeypatch.setattr(S, "_is_novel_class",
                        lambda c: c == "event:DATA_SOURCE_ESCALATED")
    pool = _Pool(open_rows=[_row("h1", "event:DATA_SOURCE_ESCALATED"),
                            _row("h2", "selfheal:prices_daily_freshness")])
    out = await select_novel_escalations(pool)
    assert [e.ref for e in out] == ["h1"]


async def test_dedup_skips_prior_proposal(monkeypatch) -> None:
    import tpcore.llm_data_triage.select as S
    monkeypatch.setattr(S, "_is_novel_class", lambda c: True)
    pool = _Pool(open_rows=[_row("h1", "x"), _row("h2", "x")],
                 prior_refs=["h1"])
    out = await select_novel_escalations(pool)
    assert [e.ref for e in out] == ["h2"]


async def test_bounded_oldest_first(monkeypatch) -> None:
    import tpcore.llm_data_triage.select as S
    monkeypatch.setattr(S, "_is_novel_class", lambda c: True)
    rows = [_row(f"r{i}", "x") for i in range(MAX_TRIAGE_PER_CYCLE + 3)]
    out = await select_novel_escalations(_Pool(open_rows=rows))
    assert len(out) == MAX_TRIAGE_PER_CYCLE
    assert [e.ref for e in out] == [f"r{i}" for i in range(MAX_TRIAGE_PER_CYCLE)]
