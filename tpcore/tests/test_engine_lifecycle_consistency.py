"""Engine-lifecycle cross-SoT consistency — the clockwork guard (SDLC SP1).

Engine-domain analog of test_provider_lifecycle_consistency.py: a live
engine must be coherently wired (package + tests + scheduler); a
RETIRED engine must be fully offboarded (archive/EULOGY, no package);
the dispatch order must be the frozen literal; no half-state; the
structurally-parseable shadows must not drift from the SoT.

Motivating incident: Sigma-archival (PR #170) drifted across ~10 sites
(rosters, importers, smoke loop, pyproject, docs) before a cohesive
cleanup pass. This guard makes a new, removed, or archived engine
fail the build unless it is coherently wired or fully offboarded in
the same change — exactly as test_provider_lifecycle_consistency.py
does for data feeds.
"""
from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

from tpcore.engine_profile import (
    _PROFILE,
    LifecycleState,
    allocator_eligible_engines,
    archived_engines,
    roster_for_dispatch,
)
from tpcore.quality.validation.capital_gate import ENGINE_TABLES

REPO = Path(__file__).resolve().parents[2]


def test_dispatch_order_invariant_is_the_frozen_literal():
    """Roster order is frozen; changes are high-risk (Sub-C/DA-3) and must be explicit."""
    # roster-order changes are high-risk (Sub-C/DA-3); pin it.
    assert roster_for_dispatch() == (
        "reversion", "vector", "momentum", "sentinel", "canary")


def test_live_engine_is_wired():
    """A PAPER/LIVE engine must be coherently wired: package + tests + runnable scheduler."""
    for name, p in _PROFILE.items():
        if p.lifecycle_state not in (LifecycleState.PAPER, LifecycleState.LIVE):
            continue
        if name == "allocator":
            continue  # allocator: separate _dispatch_allocator path, not a top-level package (D-SDLC1-4)
        assert (REPO / name).is_dir(), (
            f"{name}: PAPER/LIVE but no top-level {name}/ package — scaffold from "
            f"tpcore/templates/engine_template/ or set lifecycle_state=RETIRED in "
            f"engine_profile._PROFILE")
        assert (REPO / name / "tests").is_dir(), (
            f"{name}: no {name}/tests/ — add {name}/tests/ (engine_readiness §6)")
        try:
            spec = importlib.util.find_spec(f"{name}.scheduler")
        except ModuleNotFoundError:
            spec = None
        assert spec is not None, (
            f"{name}: PAPER/LIVE engine has no importable {name}.scheduler "
            f"(python -m {name}.scheduler target) — scaffold from "
            f"tpcore/templates/engine_template/ or set lifecycle_state=RETIRED")


def test_retired_engine_fully_offboarded():
    """A RETIRED engine must be fully offboarded: out of roster/allocator, eulogy written, package moved."""
    for name, p in _PROFILE.items():
        if p.lifecycle_state is not LifecycleState.RETIRED:
            continue
        assert name not in roster_for_dispatch(), (
            f"{name}: RETIRED but still dispatched — it must not be in "
            f"roster_for_dispatch(); ensure lifecycle_state=RETIRED (not PAPER/LIVE) "
            f"in engine_profile._PROFILE")
        assert name not in allocator_eligible_engines(), (
            f"{name}: RETIRED but allocator_eligible — set allocator_eligible=False "
            f"in engine_profile._PROFILE")
        assert name in archived_engines(), (
            f"{name}: RETIRED but absent from archived_engines() — check "
            f"lifecycle_state in engine_profile._PROFILE")
        assert (REPO / "archive" / name / "EULOGY.md").is_file(), (
            f"{name}: RETIRED but archive/{name}/EULOGY.md missing — write the "
            f"eulogy (see archive/sigma/EULOGY.md as the template) before archiving")
        assert not (REPO / name).is_dir(), (
            f"{name}: RETIRED but top-level {name}/ package still exists — move it "
            f"to archive/{name}/ (the snap-out checklist)")


def test_no_half_state():
    """No engine may be in an inconsistent intermediate state
    (RETIRED+allocator_eligible, duplicate dispatch_order, key/engine mismatch)."""
    seen_orders = []
    for name, p in _PROFILE.items():
        if p.lifecycle_state is LifecycleState.RETIRED:
            assert not p.allocator_eligible, f"{name}: RETIRED and allocator_eligible"
        else:
            seen_orders.append(p.dispatch_order)
    assert len(seen_orders) == len(set(seen_orders)), (
        f"duplicate dispatch_order among non-RETIRED: {seen_orders}")
    for key, p in _PROFILE.items():
        assert p.engine == key, (
            f"_PROFILE key {key!r} != EngineProfile.engine {p.engine!r} "
            f"(the key must be the engine name)")


def test_engine_tables_keys_are_known_engines():
    """Every ENGINE_TABLES key must be a known live engine (documented seam D-SDLC1-1)."""
    # Documented seam (D-SDLC1-1): ENGINE_TABLES is a data-dep map, not
    # collapsed into the SoT — but every key MUST be a known engine.
    allowed = set(roster_for_dispatch()) | {"allocator"}
    assert set(ENGINE_TABLES) <= allowed, (
        f"ENGINE_TABLES keys not in the live roster: {set(ENGINE_TABLES) - allowed}")
    # SP4 will also assert the reverse (roster_for_dispatch() ⊆ ENGINE_TABLES) — a live engine
    # with no data-dep entry. Deferred per spec H-B6/§4 (ENGINE_TABLES is a documented seam in SP1).


def test_structurally_parseable_shadows_match_sot():
    """Structural-shadow files (run_smoke_test.sh, pyproject.toml) must stay in sync with the SoT roster."""
    live = set(roster_for_dispatch())
    # scripts/run_smoke_test.sh step-3 loop
    smoke = (REPO / "scripts" / "run_smoke_test.sh").read_text()
    m = re.search(r"for engine in ([^\n;]+);\s*do", smoke)
    assert m, "could not find the run_smoke_test.sh step-3 engine loop"
    assert set(m.group(1).split()) == live, (
        f"run_smoke_test.sh engine loop {set(m.group(1).split())} != SoT {live}")
    # pyproject testpaths engine dirs + packages.find.include globs
    pp = tomllib.loads((REPO / "pyproject.toml").read_text())
    testpaths = set(pp["tool"]["pytest"]["ini_options"]["testpaths"])
    for e in live:
        assert f"{e}/tests" in testpaths, f"{e}/tests missing from pyproject testpaths"
    includes = pp["tool"]["setuptools"]["packages"]["find"]["include"]
    for e in live:
        assert f"{e}*" in includes, f"{e}* missing from packages.find.include"


def test_lab_sentinel_is_not_wired():
    """The durable LAB sentinel proves LifecycleState.LAB is a real
    exercised state, but is NOT a runnable engine: absent from
    dispatch/allocator, no top-level package, and LAB is the ONLY
    non-{PAPER,LIVE,RETIRED} state (closes the half-state gap
    symmetric to the RETIRED leg)."""
    lab = [n for n, p in _PROFILE.items()
           if p.lifecycle_state is LifecycleState.LAB]
    assert lab == ["lab"], f"expected exactly one LAB sentinel, got {lab}"
    assert "lab" not in roster_for_dispatch()
    assert "lab" not in allocator_eligible_engines()
    assert not (REPO / "lab").is_dir()  # not a top-level package
    states = {p.lifecycle_state for p in _PROFILE.values()}
    assert states <= {LifecycleState.PAPER, LifecycleState.LIVE,
                      LifecycleState.RETIRED, LifecycleState.LAB}
