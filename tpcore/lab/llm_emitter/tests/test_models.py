"""SP-G — pydantic v2 contract model tests.

Covers:
 - EmissionContext / EmittedSpec / RosterTarget are frozen + extra=forbid.
 - EmittedSpec validators enforce Readiness §1 + §2:
     * malformed param_ranges 3-tuple shape is rejected;
     * malformed 'choice:' kind is rejected;
     * fold_existing with !=1 'choice:' toggle is rejected;
     * promote_new with multiple ranges is accepted (allowed).
 - candidate_name / target_engine slug shape is enforced.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tpcore.lab.llm_emitter.models import (
    EmissionContext,
    EmittedSpec,
    LedgerEntry,
    ReferenceExcerpt,
    RosterTarget,
)
from tpcore.lab.target import LabPrimaryMetric


def _sample_spec(**overrides) -> EmittedSpec:
    """Build a valid fold_existing EmittedSpec; override keys for the
    failure cases."""
    base = {
        "candidate_name": "sample_candidate",
        "target_engine": "sentinel",
        "intent": "fold_existing",
        "primary_hypothesis": "lower threshold reduces holdout max drawdown",
        "primary_metric": LabPrimaryMetric.MAXDD_REDUCTION,
        "param_ranges": {"activation_score_threshold": (60, 55, "choice:60,55")},
        "rationale": "the rationale text",
        "falsification_criterion": "55 produces strictly shallower mean holdout drawdown than 60",
        "expected_trials": 50,
    }
    base.update(overrides)
    return EmittedSpec(**base)


# ─── frozen / extra=forbid ─────────────────────────────────────────────


def test_emission_context_is_frozen() -> None:
    ctx = EmissionContext(
        roster_targets=(),
        ledger_state=(),
        readiness_checklist_version="v1",
        reference_excerpts=(),
        persona_version="vsha",
        emission_quota_remaining=10,
    )
    with pytest.raises((AttributeError, ValidationError)):
        ctx.persona_version = "v2"  # type: ignore[misc]


def test_emitted_spec_is_frozen() -> None:
    spec = _sample_spec()
    with pytest.raises((AttributeError, ValidationError)):
        spec.candidate_name = "evil"  # type: ignore[misc]


def test_emission_context_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        EmissionContext(
            roster_targets=(),
            ledger_state=(),
            readiness_checklist_version="v1",
            reference_excerpts=(),
            persona_version="v",
            emission_quota_remaining=10,
            unknown_key="boom",  # extra="forbid" rejects
        )


def test_emitted_spec_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(unknown_key="boom")


def test_roster_target_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        RosterTarget(
            name="sentinel",
            lifecycle_state="PAPER",
            primary_metric=LabPrimaryMetric.MAXDD_REDUCTION,
            declared_param_ranges={},
            unknown_key="boom",  # extra="forbid"
        )


# ─── param_ranges shape validation ─────────────────────────────────────


def test_param_ranges_must_be_3_tuple() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(param_ranges={"x": (1, 2)})  # missing kind


def test_param_ranges_kind_must_be_known() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(param_ranges={"x": (1, 2, "nonsense")})


def test_param_ranges_empty_choice_rejected() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(param_ranges={"x": (1, 2, "choice:")})


def test_param_ranges_empty_dict_rejected() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(param_ranges={})


# ─── Readiness §1 + §2 fold_existing single-toggle mandate ─────────────


def test_fold_existing_with_two_choice_toggles_rejected() -> None:
    """Readiness §2: fold_existing has exactly ONE 'choice:' toggle."""
    with pytest.raises(ValidationError):
        _sample_spec(
            param_ranges={
                "a": (60, 55, "choice:60,55"),
                "b": (1, 0, "choice:1,0"),
            }
        )


def test_fold_existing_with_zero_choice_toggles_rejected() -> None:
    """A fold_existing with a float-only range has no toggle — also bad."""
    with pytest.raises(ValidationError):
        _sample_spec(param_ranges={"a": (0.1, 0.9, "float")})


def test_promote_new_allows_multiple_ranges() -> None:
    """promote_new is a new engine — multiple swept ranges are allowed
    (still single-hypothesis by ``primary_hypothesis`` declaration)."""
    spec = _sample_spec(
        intent="promote_new",
        param_ranges={
            "alpha": (0.1, 0.9, "float"),
            "lookback": (5, 30, "int"),
        },
    )
    assert spec.intent == "promote_new"


def test_promote_new_allows_choice_too() -> None:
    spec = _sample_spec(
        intent="promote_new",
        param_ranges={
            "mode": (0, 1, "choice:on,off"),
            "lookback": (5, 30, "int"),
        },
    )
    assert "mode" in spec.param_ranges


# ─── candidate_name / target_engine slug shape ─────────────────────────


def test_candidate_name_rejects_uppercase() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(candidate_name="EvilName")


def test_candidate_name_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(candidate_name="../etc/passwd")


def test_candidate_name_rejects_shell_meta() -> None:
    with pytest.raises(ValidationError):
        _sample_spec(candidate_name="x;ls")


def test_target_engine_rejects_dashed_form() -> None:
    """``target_engine`` is a python package; hyphens are forbidden."""
    with pytest.raises(ValidationError):
        _sample_spec(target_engine="re-version")


# ─── ledger budget helpers ─────────────────────────────────────────────


def test_ledger_entry_rejects_negative_cumulative() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry(target="sentinel", cumulative_n_trials=-1, quota=20)


def test_ledger_entry_rejects_negative_quota() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry(target="sentinel", cumulative_n_trials=5, quota=-1)


def test_reference_excerpt_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        ReferenceExcerpt(name="carver_systematic_trading", text="")


# ─── as_dict shape (sidecar serialisation) ─────────────────────────────


def test_as_dict_is_json_serializable() -> None:
    import json

    spec = _sample_spec()
    d = spec.as_dict()
    # Round-trips through json (no tuples, no enums leftover).
    s = json.dumps(d, sort_keys=True)
    re_loaded = json.loads(s)
    assert re_loaded["candidate_name"] == "sample_candidate"
    assert re_loaded["target_engine"] == "sentinel"
    assert re_loaded["primary_metric"] == "maxdd_reduction"
    # param_ranges tuple becomes a list (JSON has no tuples).
    assert re_loaded["param_ranges"]["activation_score_threshold"] == [60, 55, "choice:60,55"]
