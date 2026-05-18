"""Deterministic fence — pure, no git, no LLM. The load-bearing
'bug-fix-yes / system-breaking-no' boundary."""
from __future__ import annotations

from tpcore.llm_data_triage.fence import (
    DENIED_PREFIXES,
    hard_denied_paths,
    provenance_violations,
)


def test_hard_denied_flags_body_paths() -> None:
    paths = [
        "tpcore/risk/governor.py",
        "tpcore/order_management/base.py",
        "tpcore/risk/limits_profile.py",
        "platform/migrations/versions/x.py",
        "tpcore/finra/providers.py",
        "scripts/run_data_operations.sh",
        "scripts/ops.py",
        "tpcore/quality/validation/capital_gate.py",
    ]
    flagged = hard_denied_paths(paths)
    assert set(flagged) == set(paths)  # every body path flagged


def test_hard_denied_allows_registry_and_dossier() -> None:
    assert hard_denied_paths([
        "tpcore/selfheal/registry.py",
        "tpcore/auditheal/registry.py",
        "docs/sprints/dossier-x.md",
    ]) == []


def _spec(stage, params, act=True, maxa=3):
    return {"stage": stage, "params": params, "act": act,
            "max_attempts": maxa}


def test_provenance_ok_additive_binding_to_proven_stage() -> None:
    base = {"a": _spec("daily_bars", {"repair_gaps": "true"})}
    head = dict(base)
    head["b"] = _spec("daily_bars", {"repair_gaps": "true"})  # additive
    assert provenance_violations(base, head, {"daily_bars"}) == []


def test_provenance_rejects_new_stage() -> None:
    base = {"a": _spec("daily_bars", {})}
    head = {**base, "b": _spec("brand_new_stage", {})}
    v = provenance_violations(base, head, {"daily_bars"})
    assert v and any("new mechanism" in x or "stage" in x for x in v)


def test_provenance_rejects_new_param_key() -> None:
    base = {"a": _spec("daily_bars", {"repair_gaps": "true"})}
    head = {**base, "b": _spec("daily_bars", {"force_full": "true"})}
    assert provenance_violations(base, head, {"daily_bars"})


def test_provenance_rejects_edit_to_existing_spec() -> None:
    base = {"a": _spec("daily_bars", {}, maxa=3)}
    head = {"a": _spec("daily_bars", {}, maxa=9)}  # widened bound
    assert provenance_violations(base, head, {"daily_bars"})


def test_provenance_rejects_removed_spec() -> None:
    base = {"a": _spec("daily_bars", {}), "b": _spec("daily_bars", {})}
    head = {"a": _spec("daily_bars", {})}
    assert provenance_violations(base, head, {"daily_bars"})


def test_provenance_rejects_non_acting_new_binding() -> None:
    # a new entry that doesn't actually bind a repair (act False) is
    # not a conversion — reject (no escalate-only specs from the LLM).
    base = {"a": _spec("daily_bars", {})}
    head = {**base, "b": _spec("daily_bars", {}, act=False)}
    assert provenance_violations(base, head, {"daily_bars"})


def test_denied_prefixes_constant_is_explicit() -> None:
    assert "tpcore/risk/" in DENIED_PREFIXES
    assert "platform/migrations/" in DENIED_PREFIXES
