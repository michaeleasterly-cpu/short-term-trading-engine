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
    assert el.escalation_drift() == (set(), set())


def test_escalation_drift_reports_missing_for_uncovered_class():
    missing, extra = el._drift_for(
        known=el.KNOWN_ESCALATION_CLASSES | {"_synthetic_probe"},
        policies=el.DISPOSITION_POLICIES)
    assert "_synthetic_probe" in missing
    assert extra == set()


def test_policy_for_unknown_is_none():
    assert el.policy_for("not_a_class") is None
