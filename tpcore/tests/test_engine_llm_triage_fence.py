"""Engine-lane fence reuse — the SHIPPED pure data-lane fence
(`tpcore.llm_data_triage.fence`) consumed via injected parameters
(FORK-A: one fence object, no clone). These tests assert the engine
path; the data-lane byte-no-op is proven by the UNCHANGED #187 suite
(`test_llm_data_triage_fence.py`) still passing.
"""
from __future__ import annotations

from tpcore.engine_llm_triage.fence import (
    ENGINE_DENIED_EXACT,
    ENGINE_DENIED_GLOBS,
    ENGINE_DENIED_PREFIXES,
    engine_hard_denied_paths,
    engine_provenance_violations,
)
from tpcore.llm_data_triage.fence import hard_denied_paths


def test_engine_denied_set_includes_engine_mechanism_files() -> None:
    """The engine hard-denied set MUST include the engine
    deterministic-mechanism files PLUS the shared protected paths."""
    must_deny = [
        # engine deterministic mechanism (the agent may add a policy
        # binding but NEVER edit the ladder/supervisor/autotune mechanism)
        "ops/engine_supervisor.py",
        "ops/aar_autotune.py",
        "tpcore/supervisor_state.py",
        "ops/engine_ladder.py",
        # shared protected paths the data lane already denies
        "tpcore/risk/governor.py",
        "tpcore/order_management/base.py",
        "tpcore/risk/limits_profile.py",
        "platform/migrations/versions/x.py",
        "tpcore/finra/providers.py",
        "scripts/ops.py",
        "tpcore/quality/validation/capital_gate.py",
    ]
    flagged = engine_hard_denied_paths(must_deny)
    assert set(flagged) == set(must_deny)


def test_engine_denied_allows_additive_binding_and_dossier() -> None:
    assert engine_hard_denied_paths([
        "ops/engine_ladder_policies.py",  # hypothetical additive-only file
        "docs/sprints/engine-dossier-x.md",
        "tpcore/engine_llm_triage/select.py",
    ]) == []


def test_denied_set_passed_as_data_not_hardcoded() -> None:
    """The denied set is DATA — passing a custom denied set changes
    behavior; the function does not hardcode the engine paths."""
    from tpcore.llm_data_triage.fence import hard_denied_paths as hdp

    # With NO injected sets the shared fence does not flag the engine
    # mechanism files (proves the engine set is injected data).
    assert hdp(["ops/engine_supervisor.py", "ops/engine_ladder.py"]) == []
    # The engine wrapper injects them and they ARE flagged.
    assert set(engine_hard_denied_paths(
        ["ops/engine_supervisor.py", "ops/engine_ladder.py"])
    ) == {"ops/engine_supervisor.py", "ops/engine_ladder.py"}


def test_shared_fence_accepts_injected_denied_set_kw() -> None:
    """The shipped pure fence accepts injected keyword-only denied
    sets; omitted ⇒ byte-identical data-lane behavior."""
    # injected engine set behaves engine-correctly through the SAME
    # shared code object
    out = hard_denied_paths(
        ["ops/engine_ladder.py", "docs/x.md"],
        denied_exact=ENGINE_DENIED_EXACT,
        denied_prefixes=ENGINE_DENIED_PREFIXES,
        denied_globs=ENGINE_DENIED_GLOBS,
    )
    assert out == ["ops/engine_ladder.py"]


def _spec(stage, params, act=True, maxa=3):
    return {"stage": stage, "params": params, "act": act,
            "max_attempts": maxa}


def test_engine_provenance_additive_binding_ok() -> None:
    """Engine baseline is DISPOSITION_POLICIES-shaped (normalised to
    the spec dict the shared evaluator expects). An additive binding
    to an already-proven verb is allowed."""
    base = {"crashed_startup": _spec("structural", {})}
    head = dict(base)
    head["novel_pattern"] = _spec("structural", {})  # additive, proven verb
    assert engine_provenance_violations(
        base, head, baseline_stages={"structural"}) == []


def test_engine_provenance_rejects_new_disposition_verb() -> None:
    base = {"crashed_startup": _spec("structural", {})}
    head = {**base, "novel": _spec("brand_new_verb", {})}
    v = engine_provenance_violations(
        base, head, baseline_stages={"structural"})
    assert v


def test_engine_provenance_rejects_edit_to_existing_policy() -> None:
    base = {"crashed_startup": _spec("structural", {}, maxa=3)}
    head = {"crashed_startup": _spec("removed", {}, maxa=3)}
    assert engine_provenance_violations(
        base, head, baseline_stages={"structural", "removed"})
