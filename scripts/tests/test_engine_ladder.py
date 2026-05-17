import contextlib  # noqa: F401  (used by later tasks' fake pools)
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import aar_autotune as at  # noqa: E402
from ops import engine_ladder as el  # noqa: E402
from ops import engine_supervisor as es  # noqa: E402


def test_disposition_enum_is_converted_structural_removed():
    vals = {d.value for d in el.EngineEscalationDisposition}
    assert vals == {"converted", "structural", "removed"}
    assert "auto_converted" not in vals


def test_known_classes_derived_from_real_constants():
    assert el.KNOWN_ESCALATION_CLASSES == (
        es.INFRA_FAILURE_CLASSES | {at._BEHAVIORAL})


def test_every_known_class_has_a_policy():
    for cls in el.KNOWN_ESCALATION_CLASSES:
        p = el.policy_for(cls)
        assert p is not None
        assert isinstance(p.default, el.EngineEscalationDisposition)
        assert p.rationale.strip()


def test_data_repair_escalated_default_is_structural_not_removed():
    p = el.policy_for("data_repair_escalated")
    assert p.default is el.EngineEscalationDisposition.STRUCTURAL


def test_escalation_drift_empty_in_lockstep():
    missing, extra = el.escalation_drift()
    assert missing == set(), f"classes with no disposition policy: {missing}"
    assert extra == set(), f"disposition policies for unknown classes: {extra}"


def test_escalation_drift_reports_missing_for_uncovered_class():
    missing, extra = el._drift_for(
        known=el.KNOWN_ESCALATION_CLASSES | {"_synthetic_probe"},
        policies=el.DISPOSITION_POLICIES)
    assert "_synthetic_probe" in missing
    assert extra == set()


def test_policy_for_unknown_is_none():
    assert el.policy_for("not_a_class") is None


import json as _json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=10)   # past 7d grace
FRESH = NOW - timedelta(days=1)  # within grace


def _conn(esc_rows, open_fps):
    """esc_rows: rows the candidate-SQL returns; open_fps: fingerprints
    still open in forensics_triggers."""
    class _C:
        async def fetch(self, sql, *a):
            if "forensics_triggers" in sql:
                return [{"fp": fp} for fp in open_fps]
            return esc_rows
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield _C()
    return _P()


def _row(hold_id, *, engine="reversion", failure_class="crashed_startup",
         reason="x", recorded_at=OLD, has_held=True, triggers=None):
    return {"hold_id": hold_id, "engine": engine,
            "failure_class": failure_class, "reason": reason,
            "recorded_at": recorded_at, "has_held": has_held,
            "triggers": _json.dumps(triggers or [])}


async def test_list_includes_past_grace_held_open():
    pool = _conn([_row("h1")], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["h1"]
    assert out[0]["shape"] == "held"


async def test_list_excludes_within_grace():
    pool = _conn([_row("h2", recorded_at=FRESH)], set())
    assert await el.list_undispositioned(pool, now=NOW, grace_days=7) == []


async def test_list_escalate_only_included_when_fps_still_open():
    pool = _conn([_row("e1", failure_class="behavioral", has_held=False,
                        triggers=["fp-a", "fp-b"])], {"fp-b"})
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["e1"]
    assert out[0]["shape"] == "escalate-only"


async def test_list_escalate_only_excluded_when_all_fps_resolved():
    pool = _conn([_row("e2", failure_class="behavioral", has_held=False,
                        triggers=["fp-x"])], set())
    assert await el.list_undispositioned(pool, now=NOW, grace_days=7) == []


async def test_list_carries_policy_default():
    pool = _conn([_row("h3", failure_class="data_repair_escalated")], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert out[0]["policy_default"] == "structural"
