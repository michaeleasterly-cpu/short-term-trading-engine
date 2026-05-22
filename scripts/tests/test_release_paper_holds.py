"""Test — operator-on-demand stage
``release_paper_holds_above_paper_floor``.

PR ``feat/lifecycle-pause-mode-aware-credibility-floor`` companion.
Verifies the stage:

1. Releases ENGINE_HELD rows for PAPER engines whose latest
   credibility is at or above ``MIN_PAPER_SCORE/100`` (0.30 default).
2. SKIPS PAPER engines whose latest credibility is below the paper
   floor (still genuinely degraded — keep the hold).
3. NEVER releases LIVE engines (the strict live floor still applies;
   live releases are supervisor/operator territory).
4. Is idempotent — a second run on an already-cleared hold is a no-op.
"""
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import json  # noqa: E402

from scripts.ops import _stage_release_paper_holds_above_paper_floor  # noqa: E402


class _ProgrammedConn:
    """Conn whose ``fetchrow`` returns a queued sequence and whose
    ``execute`` records inserts. We model the production sequence:

    For each engine (sorted) we observe:
    1. ``current_hold`` query (a LEFT-JOIN ENGINE_HELD/ENGINE_CLEARED).
       Either a non-None hold row (engine currently held) or None.
    2. If held + PAPER: latest credibility SELECT (one row or None).
    3. If above floor: an INSERT INTO platform.application_log (the
       ENGINE_CLEARED event).
    """

    def __init__(self, hold_rows: dict, credibility_rows: dict) -> None:
        # hold_rows: engine_name -> hold dict (or None for no hold)
        self.hold_rows = hold_rows
        # credibility_rows: engine_name -> latest confidence float (or None)
        self.credibility_rows = credibility_rows
        self.inserts: list[tuple] = []

    async def fetchrow(self, sql, *args):
        if "FROM platform.application_log h" in sql and "ENGINE_HELD" not in sql:
            # current_hold's LEFT-JOIN query — args=(HELD_EVENT, CLEARED_EVENT, engine)
            engine = args[2]
            hold = self.hold_rows.get(engine)
            return hold
        if "FROM platform.data_quality_log" in sql:
            # latest credibility — args=(source,)
            source = args[0]
            # source = "backtest_credibility.<engine>"
            engine = source.split(".", 1)[1]
            conf = self.credibility_rows.get(engine)
            if conf is None:
                return None
            return {"confidence": Decimal(str(conf))}
        return None

    async def execute(self, sql, *args):
        self.inserts.append((sql, args))
        return None


class _ProgrammedPool:
    def __init__(self, hold_rows, credibility_rows) -> None:
        self.conn = _ProgrammedConn(hold_rows, credibility_rows)

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self):
                return outer.conn

            async def __aexit__(self, *a):
                return False

        return _CM()


def _hold_row(engine: str, failure_class: str = "behavioral_credibility"):
    return {
        "hold_id": f"hold-{engine}",
        "failure_class": failure_class,
        "reason": "test",
        "held_at": datetime(2026, 5, 22, 11, 0, tzinfo=UTC),
        "cleared": None,  # uncleared — current_hold returns HoldState
    }


async def test_release_paper_hold_above_paper_floor_emits_cleared():
    """The four currently-paused PAPER engines all in 0.40-0.55 →
    every one should be released."""
    holds = {
        "reversion": _hold_row("reversion"),
        "vector": _hold_row("vector"),
        "momentum": _hold_row("momentum"),
        "sentinel": _hold_row("sentinel"),
    }
    credibilities = {
        "reversion": 0.45,
        "vector": 0.45,
        "momentum": 0.55,
        "sentinel": 0.40,
    }
    pool = _ProgrammedPool(holds, credibilities)
    result = await _stage_release_paper_holds_above_paper_floor(pool)

    assert result["released_count"] == 4
    assert result["paper_floor_pct"] == 0.30
    for engine in holds:
        assert result["engines"][engine]["action"] == "released"
        assert result["engines"][engine]["lifecycle_state"] == "paper"
        assert result["engines"][engine]["hold_id"] == f"hold-{engine}"

    # Four ENGINE_CLEARED inserts emitted, one per engine.
    cleared_inserts = [
        ins for ins in pool.conn.inserts
        if "INSERT INTO platform.application_log" in ins[0]
        and ins[1][2] == "ENGINE_CLEARED"
    ]
    assert len(cleared_inserts) == 4
    # Each carries the right payload shape.
    payloads = [json.loads(ins[1][5]) for ins in cleared_inserts]
    for p in payloads:
        assert p["released_by_stage"] == "release_paper_holds_above_paper_floor"
        assert "mode_aware_floor_release" in p["clear_reason"]


async def test_release_skips_paper_engine_below_paper_floor():
    """PAPER engine at 0.20 (below the 0.30 paper floor) → keep the hold."""
    holds = {"reversion": _hold_row("reversion")}
    credibilities = {"reversion": 0.20}
    pool = _ProgrammedPool(holds, credibilities)
    result = await _stage_release_paper_holds_above_paper_floor(pool)

    assert result["released_count"] == 0
    assert result["engines"]["reversion"]["action"] == "skipped_below_paper_floor"
    # No ENGINE_CLEARED emitted.
    cleared = [
        ins for ins in pool.conn.inserts
        if "INSERT INTO platform.application_log" in ins[0]
        and ins[1][2] == "ENGINE_CLEARED"
    ]
    assert cleared == []


async def test_release_never_clears_non_paper_lifecycle_state():
    """RETIRED + LAB engines are filtered out at the iteration level
    (not in ``roster_for_dispatch``), so any stale held rows on them
    remain untouched — proves the stage scopes only to dispatchable
    PAPER/LIVE engines.

    The scan set is ``roster_for_dispatch() ∪ {allocator}``: sigma
    (RETIRED) and carver (LAB) NEVER appear in ``result['engines']``
    even if the fixture pretends they have open holds.
    """
    holds = {
        "sigma": _hold_row("sigma"),  # RETIRED — never in roster
        "carver": _hold_row("carver"),  # LAB — never in roster
    }
    credibilities = {"sigma": 0.95, "carver": 0.95}
    pool = _ProgrammedPool(holds, credibilities)
    result = await _stage_release_paper_holds_above_paper_floor(pool)

    assert result["released_count"] == 0
    # The two non-PAPER engines are silently excluded from the scan.
    assert "sigma" not in result["engines"]
    assert "carver" not in result["engines"]
    cleared = [
        ins for ins in pool.conn.inserts
        if "INSERT INTO platform.application_log" in ins[0]
        and ins[1][2] == "ENGINE_CLEARED"
    ]
    assert cleared == []


async def test_release_skips_live_engine_in_dispatchable_roster():
    """A LIVE engine in the dispatchable roster with an open hold and
    high credibility is still skipped — only PAPER engines may be
    auto-cleared by this stage."""
    from unittest.mock import patch

    from tpcore.engine_profile import LifecycleState

    # Synthesise: pretend reversion is LIVE for the duration of the
    # call. ``current_hold`` returns an open hold; latest credibility
    # is high (0.95) — would normally release if it were PAPER.
    holds = {"reversion": _hold_row("reversion")}
    credibilities = {"reversion": 0.95}
    pool = _ProgrammedPool(holds, credibilities)

    class _LiveProfileStub:
        lifecycle_state = LifecycleState.LIVE

    with patch(
        "tpcore.engine_profile.profile_for",
        side_effect=lambda eng: (
            _LiveProfileStub() if eng == "reversion" else None
        ),
    ):
        result = await _stage_release_paper_holds_above_paper_floor(pool)

    assert result["released_count"] == 0
    assert result["engines"]["reversion"]["action"] == "skipped_non_paper"
    assert result["engines"]["reversion"]["lifecycle_state"] == "live"


async def test_release_idempotent_no_open_hold_noop():
    """No open holds → no-op; the result map is empty."""
    holds: dict = {}
    credibilities = {"reversion": 0.45}
    pool = _ProgrammedPool(holds, credibilities)
    result = await _stage_release_paper_holds_above_paper_floor(pool)

    assert result["released_count"] == 0
    assert result["engines"] == {}
    cleared = [
        ins for ins in pool.conn.inserts
        if "INSERT INTO platform.application_log" in ins[0]
    ]
    assert cleared == []


async def test_release_skips_paper_engine_with_no_credibility_row():
    """PAPER engine, open hold, NO credibility ever recorded →
    skipped_no_credibility_row (don't blindly release; require evidence)."""
    holds = {"reversion": _hold_row("reversion")}
    credibilities: dict = {}  # no row for reversion
    pool = _ProgrammedPool(holds, credibilities)
    result = await _stage_release_paper_holds_above_paper_floor(pool)

    assert result["released_count"] == 0
    assert (
        result["engines"]["reversion"]["action"]
        == "skipped_no_credibility_row"
    )
