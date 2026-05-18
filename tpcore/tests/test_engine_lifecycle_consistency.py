"""Engine-lifecycle cross-SoT consistency — the clockwork guard (SDLC SP1).

Engine-domain analog of test_provider_lifecycle_consistency.py: a live
engine must be coherently wired (package + tests + scheduler); a
RETIRED engine must be fully offboarded (archive/EULOGY, no package);
the dispatch order must be the frozen literal; no half-state; the
structurally-parseable shadows must not drift from the SoT.
"""
from __future__ import annotations

import importlib.util
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
    # roster-order changes are high-risk (Sub-C/DA-3); pin it.
    assert roster_for_dispatch() == (
        "reversion", "vector", "momentum", "sentinel", "canary")


def test_live_engine_is_wired():
    for name, p in _PROFILE.items():
        if p.lifecycle_state not in (LifecycleState.PAPER, LifecycleState.LIVE):
            continue
        if name == "allocator":
            continue  # not a top-level package (separate dispatch path)
        assert (REPO / name).is_dir(), f"{name}: PAPER/LIVE but no top-level package"
        assert (REPO / name / "tests").is_dir(), f"{name}: no {name}/tests/"
        assert importlib.util.find_spec(f"{name}.scheduler") is not None, (
            f"{name}: no importable {name}.scheduler (python -m target)")


def test_retired_engine_fully_offboarded():
    for name, p in _PROFILE.items():
        if p.lifecycle_state is not LifecycleState.RETIRED:
            continue
        assert name not in roster_for_dispatch(), f"{name}: RETIRED but in roster"
        assert name not in allocator_eligible_engines(), f"{name}: RETIRED but allocator-eligible"
        assert name in archived_engines(), f"{name}: RETIRED but not in archived_engines()"
        assert (REPO / "archive" / name / "EULOGY.md").is_file(), (
            f"{name}: RETIRED but no archive/{name}/EULOGY.md")
        assert not (REPO / name).is_dir(), (
            f"{name}: RETIRED but a top-level {name}/ package still exists")


def test_no_half_state():
    seen_orders = []
    for name, p in _PROFILE.items():
        if p.lifecycle_state is LifecycleState.RETIRED:
            assert not p.allocator_eligible, f"{name}: RETIRED and allocator_eligible"
        else:
            seen_orders.append(p.dispatch_order)
    assert len(seen_orders) == len(set(seen_orders)), (
        f"duplicate dispatch_order among non-RETIRED: {seen_orders}")
    assert len(set(_PROFILE)) == len(_PROFILE)  # unique names (dict ⇒ trivially true; explicit)


def test_engine_tables_keys_are_known_engines():
    # Documented seam (D-SDLC1-1): ENGINE_TABLES is a data-dep map, not
    # collapsed into the SoT — but every key MUST be a known engine.
    allowed = set(roster_for_dispatch()) | {"allocator"}
    assert set(ENGINE_TABLES) <= allowed, (
        f"ENGINE_TABLES keys not in the live roster: {set(ENGINE_TABLES) - allowed}")


def test_structurally_parseable_shadows_match_sot():
    live = set(roster_for_dispatch())
    # scripts/run_smoke_test.sh step-3 loop
    smoke = (REPO / "scripts" / "run_smoke_test.sh").read_text()
    import re
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
