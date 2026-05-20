"""SP-G — emitter pure-Python helper tests.

Covers:
 - render_candidate_spec emits a markdown spec carrying all ten
   Readiness sections (numbered §1–§10);
 - the rendered spec carries the [OPERATOR-DRAFT] human-in-the-loop
   seams for §3, §8, §9;
 - the rendered spec NEVER contains --dsr-threshold /
   --credibility-threshold (the safety §8.3 grep);
 - validate_no_gate_override raises on a contrived rendering containing
   a gate-override flag.
"""
from __future__ import annotations

import pytest

from tpcore.lab.llm_emitter.emitter import (
    GATE_OVERRIDE_FORBIDDEN_FLAGS,
    GateOverrideRejected,
    render_candidate_spec,
    validate_no_gate_override,
)
from tpcore.lab.llm_emitter.models import EmittedSpec
from tpcore.lab.target import LabPrimaryMetric


def _sample_spec() -> EmittedSpec:
    return EmittedSpec(
        candidate_name="threshold-tune",
        target_engine="sentinel",
        intent="fold_existing",
        primary_hypothesis="lowering the threshold reduces maxdd",
        primary_metric=LabPrimaryMetric.MAXDD_REDUCTION,
        param_ranges={"activation_score_threshold": (60, 55, "choice:60,55")},
        rationale="rationale text",
        falsification_criterion="55 produces strictly shallower mean drawdown than 60",
        expected_trials=50,
    )


def test_render_emits_all_ten_readiness_sections() -> None:
    md = render_candidate_spec(_sample_spec())
    for n in range(1, 11):
        assert f"## {n}." in md, f"missing Readiness section §{n}"


def test_render_emits_operator_draft_for_sections_3_8_9() -> None:
    md = render_candidate_spec(_sample_spec())
    # Sections §3, §8, §9 are the operator-review seams (spec §3.5).
    assert "**[OPERATOR-DRAFT]**" in md
    assert md.count("**[OPERATOR-DRAFT]**") >= 3


def test_render_includes_run_command_with_candidate_and_engine() -> None:
    spec = _sample_spec()
    md = render_candidate_spec(spec)
    assert "python -m ops.lab" in md
    assert f"--candidate {spec.candidate_name}" in md
    assert f"--target-engine {spec.target_engine}" in md
    assert f"--intent {spec.intent}" in md
    assert f"--trials {spec.expected_trials}" in md


def test_render_carries_no_gate_override_flag() -> None:
    """Spec §8.3: the rendered run command must contain NO
    --dsr-threshold / --credibility-threshold flag."""
    md = render_candidate_spec(_sample_spec())
    for flag in GATE_OVERRIDE_FORBIDDEN_FLAGS:
        assert flag not in md, (
            f"rendered spec leaked forbidden gate-override flag {flag!r}"
        )


def test_render_calls_validate_no_gate_override() -> None:
    """The renderer calls ``validate_no_gate_override`` internally and
    raises ``GateOverrideRejected`` on any leak. We can't easily make
    the renderer produce a forbidden flag (the template is hardcoded);
    instead, exercise the validator directly."""
    contrived = "python -m ops.lab --dsr-threshold 0.5"
    with pytest.raises(GateOverrideRejected):
        validate_no_gate_override(contrived)


def test_validate_no_gate_override_rejects_credibility_too() -> None:
    contrived = "python -m ops.lab --credibility-threshold 30"
    with pytest.raises(GateOverrideRejected):
        validate_no_gate_override(contrived)


def test_validate_no_gate_override_accepts_clean_markdown() -> None:
    clean = "python -m ops.lab --candidate x --target-engine sentinel"
    validate_no_gate_override(clean)  # no raise


def test_render_mentions_ledger_acknowledgement() -> None:
    """Readiness §4: the rendered spec must explicitly acknowledge
    cumulative-n_trials deflation + name ``tpcore.lab.ledger``."""
    md = render_candidate_spec(_sample_spec())
    assert "tpcore.lab.ledger" in md
    assert "cumulative" in md.lower()
    assert "record_trial_spend" in md


def test_render_mentions_diff_fence() -> None:
    """The rendered §10 mentions the SP-G diff fence so the operator
    sees the structural enforcement explicitly."""
    md = render_candidate_spec(_sample_spec())
    assert "diff_fence" in md
