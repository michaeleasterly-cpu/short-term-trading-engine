"""Rung-3 instance teeth: the weekly digest surfaces OPEN
undispositioned data-lane escalations; DATA_ESCALATION_DISPOSITIONED
clears them."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime

import pytest

# Multi-line spec_from_file_location of an ops/ module — grouped by the
# over-inclusion rule (verified non-poisoning, but safe to co-locate).
pytestmark = pytest.mark.xdist_group("ops_shadow")

_spec = importlib.util.spec_from_file_location(
    "wd", pathlib.Path(__file__).resolve().parents[1] / "ops" / "weekly_digest.py")
wd = importlib.util.module_from_spec(_spec)
sys.modules["wd"] = wd
_spec.loader.exec_module(wd)


class _Conn:
    def __init__(self, rows_by_marker):
        self._m = rows_by_marker
        self.emitted = []

    async def fetch(self, sql, *a):
        for marker, rows in self._m.items():
            if marker in sql:
                return [dict(r) for r in rows]
        return []

    async def execute(self, sql, *a):
        self.emitted.append(a)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, rows_by_marker=None):
        self.conn = _Conn(rows_by_marker or {})

    def acquire(self): return _CM(self.conn)


async def test_open_escalation_listed_and_rendered() -> None:
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "h1", "etype": "DATA_SOURCE_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "source prices_daily stuck"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    assert any("prices_daily" in x for x in d.undispositioned)
    assert "UNDISPOSITIONED" in d.render().upper()


async def test_no_open_escalations_renders_none() -> None:
    d = await wd.build_weekly_digest(_Pool(), datetime(2026, 5, 17, tzinfo=UTC))
    assert d.undispositioned == []
    assert "UNDISPOSITIONED" in d.render().upper()  # section still shown


async def test_disposition_cli_emits_event() -> None:
    pool = _Pool()
    rc = await wd.disposition_escalation(pool, "h1", "converted", "added HealSpec X")
    assert rc == 0
    assert len(pool.conn.emitted) == 1
    a = pool.conn.emitted[0]
    # _INSERT_SQL positional args: (engine, run_id, event_type, sev,
    # message, data). event_type idx 2, data json idx 5.
    assert a[2] == "DATA_ESCALATION_DISPOSITIONED"
    assert "h1" in a[5] and "converted" in a[5]


async def test_invalid_disposition_rejected() -> None:
    pool = _Pool()
    rc = await wd.disposition_escalation(pool, "h1", "bogus", "")
    assert rc != 0 and pool.conn.emitted == []


async def test_undispositioned_line_annotated_with_policy() -> None:
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "h1", "etype": "DATA_SOURCE_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "source prices_daily stuck"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    line = next(x for x in d.undispositioned if "prices_daily" in x)
    # the disposition policy for event:DATA_SOURCE_ESCALATED is
    # escalate_operator — it MUST appear inline on the line.
    assert "escalate_operator" in line
    # and the line still has the existing fields
    assert "h1" in line and "DATA_SOURCE_ESCALATED" in line


async def test_unregistered_etype_degrades_gracefully() -> None:
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "x9", "etype": "TOTALLY_NEW_ESCALATION",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "novel"},
        ],
    })
    # must NOT crash the whole digest if an etype has no event: policy.
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    line = next(x for x in d.undispositioned if "x9" in x)
    assert "UNREGISTERED" in line


# 2026-05-22 — the LT-P3 §5 LLM-triage proposal annotation tests have
# been REMOVED. Operator directive ("we aren't going to use the llm
# triage... take it out") deleted ``ops.llm_data_triage`` and the
# ``DATA_LLM_TRIAGE_PROPOSAL`` event class. The undispositioned digest
# line is now purely deterministic — no LLM suffix, ever.


async def test_undispositioned_line_has_no_llm_suffix() -> None:
    """The undispositioned digest line is purely deterministic.

    Sentinel against accidental re-introduction of the LLM-triage suffix
    (the entire LLM-triage stack was REMOVED 2026-05-22 per operator
    directive)."""
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "h-sentinel", "etype": "DATA_SOURCE_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "source x stuck"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    line = next(x for x in d.undispositioned if "h-sentinel" in x)
    assert "LLM:" not in line, (
        "weekly_digest undispositioned line re-acquired an LLM suffix — "
        "the entire LLM-triage stack was REMOVED 2026-05-22."
    )


async def test_undispositioned_entries_exposes_structured_ref() -> None:
    """CONTRACT TEST — the structured surface the consolidated defect
    register now consumes. ``undispositioned_entries`` MUST expose a
    clean ``ref`` (+ the fields the register's DefectRow needs:
    etype/recorded_at/message/policy/rendered) for every open
    undispositioned escalation. This is what bites if the digest's
    STRUCTURED surface drifts — it replaces the brittle coupling to the
    rendered display string's format."""
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "req-77", "etype": "DATA_REPAIR_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "fred_macro stalled"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    assert len(d.undispositioned_entries) == 1
    e = d.undispositioned_entries[0]
    assert isinstance(e, wd.UndispositionedEntry)
    # the clean ref — read off the struct, no regex-scrape needed
    assert e.ref == "req-77"
    assert e.etype == "DATA_REPAIR_ESCALATED"
    assert e.recorded_at == datetime(2026, 5, 1, tzinfo=UTC)
    assert "fred_macro stalled" in e.message
    assert e.policy and "policy:" in e.policy
    # single source: the struct's rendered IS the human line, byte-equal
    assert e.rendered == d.undispositioned[0]


async def test_undispositioned_string_is_byte_identical_pure_add() -> None:
    """PURE-ADD PROOF: adding ``undispositioned_entries`` did NOT alter
    the existing rendered ``undispositioned: list[str]`` surface by a
    single byte (existing consumers — dashboard.py, render() — are
    unaffected). Locks the exact representative line format."""
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "h1", "etype": "DATA_SOURCE_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "source prices_daily stuck"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    # The exact pre-change f-string output:
    # f"{recorded_at:%Y-%m-%d} [{etype}] ref={ref} {message} | {policy}"
    # (DATA_SOURCE_ESCALATED → policy:escalate_operator; no LLM suffix).
    expected = (
        "2026-05-01 [DATA_SOURCE_ESCALATED] ref=h1 "
        "source prices_daily stuck | "
        + wd._disposition_label("DATA_SOURCE_ESCALATED")  # noqa: SLF001
    )
    assert d.undispositioned == [expected]


def test_open_escalation_sql_has_all_exclusion_clauses() -> None:
    # The anti-join correctness can't be exercised by a fake pool
    # (it doesn't run SQL). Static guard: the open-escalation query
    # MUST keep all three exclusions or a regression ships silently.
    import inspect
    src = inspect.getsource(wd.build_weekly_digest)
    # The query is identified by its leading marker comment.
    assert "-- OPEN_ESCALATIONS" in src
    # (a) resolving-terminal anti-join (both terminals + both ref cols)
    assert "DATA_REPAIR_COMPLETE" in src and "DATA_SOURCE_CLEARED" in src
    assert "t.data->>'request_id' = x.ref" in src
    assert "t.data->>'hold_id' = x.ref" in src
    assert "t.recorded_at > x.recorded_at" in src
    # (b) already-dispositioned exclusion
    assert "DATA_ESCALATION_DISPOSITIONED" in src
    assert "dp.data->>'ref' = x.ref" in src
    # (c) grace-window age bound
    assert "x.recorded_at < $1" in src
