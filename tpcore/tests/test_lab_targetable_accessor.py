"""SP-B — lab_targetable_engines() predicate table (spec §2.1)."""
from __future__ import annotations


def test_lab_targetable_is_the_roster_predicate_today():
    from tpcore.engine_profile import lab_targetable_engines

    # PAPER/LIVE/LAB ∧ not allocator ∧ not 'lab' sentinel ∧ not 'canary'.
    # Today: reversion, vector, momentum, sentinel, carver, catalyst.
    # Sentinel is eligible by predicate even though undeclared (SP-E
    # forward dep); carver is the first real LAB engine (dispatch_order=6,
    # ECR-ADD lands LAB); catalyst is the first engine activated via the
    # autonomous Lab criteria path (dispatch_order=7, PAPER via
    # source=existing_code criteria pass — see
    # docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md).
    # Ordered by dispatch_order for stable diffs.
    assert lab_targetable_engines() == (
        "reversion", "vector", "momentum", "sentinel", "carver", "catalyst")


def test_canary_excluded_by_explicit_clause():
    """canary is non-graduating by construction (CLAUDE.md / spec §4b /
    canary test_backtest_deliberately_never_writes_credibility) — a Lab
    graduation verdict against it is a category error that would still
    spend SP-A ledger budget. Excluded with a named clause + this pin."""
    from tpcore.engine_profile import lab_targetable_engines

    assert "canary" not in lab_targetable_engines()


def test_lab_sentinel_excluded():
    """The durable LAB sentinel proves LifecycleState.LAB is exercised
    but is NOT a runnable engine (no package). Excluded by name clause."""
    from tpcore.engine_profile import lab_targetable_engines

    assert "lab" not in lab_targetable_engines()


def test_retired_and_allocator_excluded():
    from tpcore.engine_profile import lab_targetable_engines

    targetable = lab_targetable_engines()
    assert "sigma" not in targetable      # RETIRED ∉ _LAB_TARGETABLE
    assert "allocator" not in targetable   # reuse _ALLOCATOR_ENGINE filter


def test_lab_state_inclusion_is_real_not_vestigial(monkeypatch):
    """Positively pin that LifecycleState.LAB is genuinely IN _LAB_TARGETABLE.

    Today the only LAB profile is the ``lab`` sentinel, excluded BY NAME —
    so every other test nets to *exclusion* and a silent narrowing of
    _LAB_TARGETABLE to _DISPATCHABLE (dropping LifecycleState.LAB) would
    leave the whole suite green. This test injects a synthetic LAB engine
    with a name ≠ "lab" (so no name-clause excludes it) and asserts it IS
    targetable — it FAILS iff _LAB_TARGETABLE no longer contains LAB.

    monkeypatch.setitem auto-restores the module-global _PROFILE dict
    after the test (no permanent mutation of the real roster)."""
    from tpcore.engine_profile import (
        _PROFILE,
        Cadence,
        EngineProfile,
        LifecycleState,
        lab_targetable_engines,
    )

    synthetic = EngineProfile(
        engine="labcandidate",
        cadence=Cadence.DAILY,
        dispatch_order=51,  # unique among non-RETIRED (lab sentinel=50)
        lifecycle_state=LifecycleState.LAB,
    )
    monkeypatch.setitem(_PROFILE, "labcandidate", synthetic)

    assert "labcandidate" in lab_targetable_engines()


def test_accessor_equals_recomputed_predicate_over_profile():
    """The accessor IS the predicate over _PROFILE — not a hand-list."""
    from tpcore.engine_profile import (
        _PROFILE,
        LifecycleState,
        lab_targetable_engines,
    )

    expected = {
        n for n, p in _PROFILE.items()
        if p.lifecycle_state in {LifecycleState.LAB, LifecycleState.PAPER,
                                 LifecycleState.LIVE}
        and n not in {"allocator", "lab", "canary"}
    }
    assert set(lab_targetable_engines()) == expected
