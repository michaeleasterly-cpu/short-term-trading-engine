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

import pytest

from tpcore.engine_profile import (
    _PROFILE,
    LifecycleState,
    allocator_eligible_engines,
    archived_engines,
    roster_for_dispatch,
)
from tpcore.quality.validation.capital_gate import ENGINE_TABLES

REPO = Path(__file__).resolve().parents[2]


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


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
    # SP4 §10.4 / H-S4-8: the closed reverse — every live PAPER/LIVE
    # roster engine MUST have an ENGINE_TABLES data-dep row. Grounded
    # against the shipped ENGINE_TABLES keys ({reversion, vector,
    # momentum, sentinel, allocator, canary}); allocator is excluded
    # from the roster (its own _dispatch_allocator path) so it is
    # subtracted here. A live engine with no row is a silent un-gated
    # half-state (the _required_sources fail-safe would mask it).
    missing = set(roster_for_dispatch()) - (set(ENGINE_TABLES) - {"allocator"})
    assert not missing, (
        f"live roster engines with NO ENGINE_TABLES data-dep row "
        f"(silent un-gated engines): {missing}")


def test_structurally_parseable_shadows_match_sot():
    """Folded SP4 §10.5: leg 6 is no longer an independent parsed-roster
    assertion (that would be a SECOND shadow mechanism that can disagree
    with the generator's byte-identity verdict). It delegates to the
    generator's PURE in-process regenerate-and-diff (one mechanism). The
    clockwork stays the structure/lifecycle oracle; the generator is the
    sole shadow-shape/bytes oracle; zero overlap."""
    import sys
    sys.path.insert(0, str(REPO))
    from scripts.gen_engine_manifest import divergences
    diff = divergences(REPO)
    assert diff is None, (
        f"engine shadow-manifest DRIFT — a roster/SoT change did not "
        f"regenerate the non-Python shadows. Run "
        f"`python scripts/gen_engine_manifest.py` and commit:\n{diff}")


def test_lab_sentinel_is_not_wired():
    """The durable LAB sentinel ``"lab"`` proves LifecycleState.LAB is a
    real exercised state, but the SENTINEL itself is NOT a runnable
    engine: absent from dispatch/allocator and from the top-level
    package surface. Other LAB entries — real engines registered via
    the SDLC planner's ADD path (which always lands LAB; see
    ``ops.engine_sdlc.planner._apply_add``) — ARE allowed and DO have
    top-level packages; they're Lab-targetable until they graduate to
    PAPER. This test pins ONLY the sentinel's inertness, not the
    population of LAB."""
    lab = [n for n, p in _PROFILE.items()
           if p.lifecycle_state is LifecycleState.LAB]
    assert "lab" in lab, f"durable LAB sentinel missing from _PROFILE: {lab}"
    assert "lab" not in roster_for_dispatch()
    assert "lab" not in allocator_eligible_engines()
    assert not (REPO / "lab").is_dir()  # not a top-level package
    states = {p.lifecycle_state for p in _PROFILE.values()}
    assert states <= {LifecycleState.PAPER, LifecycleState.LIVE,
                      LifecycleState.RETIRED, LifecycleState.LAB}


def test_retired_engine_eulogy_content_floor():
    """H-S3-5: a RETIRED engine's EULOGY must be a REAL artifact — a
    non-empty `## Cause of death` AND `## Retirement checklist` section
    (header present + ≥1 non-blank line under each). A zero-byte/stub
    EULOGY (the data-lane fake-healable-HealSpec analog) fails CI."""
    for name, p in _PROFILE.items():
        if p.lifecycle_state is not LifecycleState.RETIRED:
            continue
        body = (REPO / "archive" / name / "EULOGY.md").read_text()
        for header in ("## Cause of death", "## Retirement checklist"):
            assert header in body, f"{name}: EULOGY missing {header!r}"
            after = body.split(header, 1)[1]
            nxt = after.find("\n## ")
            section = after[:nxt] if nxt != -1 else after
            assert any(ln.strip() for ln in section.splitlines()), (
                f"{name}: EULOGY {header!r} section is empty (stub)")


def test_retired_engine_absent_from_structural_shadows():
    """H-S3-5: a RETIRED engine's name must be ABSENT from the
    run_smoke_test.sh step-3 loop AND the pyproject testpaths/include
    (the explicit RETIRED-absent assertion, so a forgotten shadow fails
    on the retire leg, not only indirectly)."""
    retired = [n for n, p in _PROFILE.items()
               if p.lifecycle_state is LifecycleState.RETIRED]
    smoke = (REPO / "scripts" / "run_smoke_test.sh").read_text()
    m = re.search(r"for engine in ([^\n;]+);\s*do", smoke)
    loop = set(m.group(1).split())
    pp = tomllib.loads((REPO / "pyproject.toml").read_text())
    testpaths = set(pp["tool"]["pytest"]["ini_options"]["testpaths"])
    includes = set(pp["tool"]["setuptools"]["packages"]["find"]["include"])
    for name in retired:
        assert name not in loop, f"{name}: RETIRED but still in smoke loop"
        assert f"{name}/tests" not in testpaths, (
            f"{name}: RETIRED but still a pyproject testpath")
        assert f"{name}*" not in includes, (
            f"{name}: RETIRED but still in packages.find.include")


def test_no_orphan_archive():
    """H-S3-5: every archive/<dir>/ that contains an EULOGY.md must
    correspond to a _PROFILE entry with lifecycle_state == RETIRED
    (catches an archive with no SoT entry)."""
    arc = REPO / "archive"
    if not arc.is_dir():
        return
    for child in arc.iterdir():
        if not (child / "EULOGY.md").is_file():
            continue
        name = child.name
        p = _PROFILE.get(name)
        assert p is not None and p.lifecycle_state is LifecycleState.RETIRED, (
            f"archive/{name}/EULOGY.md exists but {name} is not a RETIRED "
            f"_PROFILE entry (orphan archive)")


def test_retired_engine_not_importable_as_live():
    """H-S3-5: a RETIRED engine must NOT be importable as a live
    <name>.scheduler (symmetric to the live-engine positive leg)."""
    for name, p in _PROFILE.items():
        if p.lifecycle_state is not LifecycleState.RETIRED:
            continue
        try:
            spec = importlib.util.find_spec(f"{name}.scheduler")
        except ModuleNotFoundError:
            spec = None
        assert spec is None, (
            f"{name}: RETIRED but {name}.scheduler still importable — "
            f"the package was not moved to archive/")


# ─── SP4 T4: leg-6 fold → one generator mechanism + closed reverse leg ───


def test_leg6_green_on_clean_tree():
    """The folded leg 6 (manifest-delegation) passes on the committed
    repo (the shadows are in sync — same diagnostic surface as the old
    parsed assertion, one mechanism)."""
    import sys
    sys.path.insert(0, str(REPO))
    from scripts.gen_engine_manifest import divergences
    assert divergences(REPO) is None


def test_leg6_fails_on_roster_drift(tmp_path):
    """H-S4-7: a drifted shadow in a staged tree makes the delegated
    in-process divergences() return a diff naming the file/region.

    Roster-agnostic drift injection (APPROVED deviation from the plan's
    hardcoded literal): the plan's literal
    ``"for engine in reversion vector momentum sentinel canary; do"``
    is a SILENT no-op the moment the smoke loop's roster changes or in
    a fence-augmented staged tree (the regex below matches the
    SoT-generated body regardless of the current roster), so the drift
    is guaranteed to actually mutate the file — keeps the leg
    NON-VACUOUS."""
    import re as _re
    import shutil
    import sys
    staged = tmp_path / "tree"
    shutil.copytree(
        REPO, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    smoke = staged / "scripts" / "run_smoke_test.sh"
    txt = smoke.read_text()
    drifted = _re.sub(
        r"for engine in [^\n;]+;\s*do",
        "for engine in reversion vector; do", txt)
    assert drifted != txt, (
        "drift injection was a no-op — the test would be vacuous")
    smoke.write_text(drifted)
    sys.path.insert(0, str(REPO))
    from scripts.gen_engine_manifest import divergences
    diff = divergences(staged)
    assert diff is not None, "drift not detected by the folded leg 6"
    assert "run_smoke_test.sh" in diff


def test_clockwork_imports_no_ops():
    """H-S4-7 / H-S4-9: the folded leg-6 delegation imports the
    generator's PURE in-process divergences() (tpcore.engine_profile +
    stdlib only) — exercising it pulls in NO ops.* (no subprocess-in-
    subprocess + scripts/ops.py collision surface).

    H-S4-9 deviation from the plan's literal in-process global-
    sys.modules snapshot (APPROVED — non-vacuous + zero global side
    effect): the plan's body either (a) false-REDs on an UNRELATED
    earlier test's scripts/ops.py pollution, or (b) needs a global
    ``del sys.modules[...]`` eviction which is itself a collection-
    order side effect that perturbs other tests' (pre-existing,
    fragile) isolation — empirically the SP2 oracle's _FakePool
    monkeypatch leak. A PRISTINE subprocess is strictly more faithful
    to H-S4-7's "in-process pure, no ops import": a fresh interpreter
    has zero collection-order pollution AND this test mutates no
    shared state. The subprocess does EXACTLY the folded-leg-6 import
    and reports any non-package ``ops`` it pulled (non-vacuous: if
    gen_engine_manifest imported ops, ``bad`` would be non-empty)."""
    import json
    import subprocess
    import sys
    probe = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from scripts.gen_engine_manifest import divergences\n"
        "bad=[m for m in sys.modules "
        "if m=='ops' or m.startswith('ops.')]\n"
        "bad=[m for m in bad "
        "if not hasattr(sys.modules[m],'__path__')]\n"
        "print(json.dumps(bad))\n"
    )
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", probe],
        cwd=str(REPO), capture_output=True, text=True, check=True)
    bad = json.loads(proc.stdout.strip().splitlines()[-1])
    assert bad == [], (
        f"the folded leg-6 delegation pulled a non-package ops into "
        f"sys.modules: {bad}")


def test_live_engine_has_engine_tables_row():
    """H-S4-8: the closed reverse leg — every live PAPER/LIVE roster
    engine MUST have an ENGINE_TABLES data-dep row (a live engine with
    no row is a silent un-gated half-state). Grounded predicate (the
    shipped ENGINE_TABLES keys are exactly {reversion, vector,
    momentum, sentinel, allocator, canary}; allocator is excluded from
    the roster but legitimately keyed via its own _dispatch_allocator
    path, so it is subtracted on the reverse side)."""
    missing = set(roster_for_dispatch()) - (set(ENGINE_TABLES) - {"allocator"})
    assert not missing, (
        f"live roster engines with NO ENGINE_TABLES data-dep row "
        f"(silent un-gated engines): {missing}")


def test_reverse_engine_tables_leg_catches_a_missing_row(tmp_path):
    """H-S4-8: a synthetic roster with an engine absent from
    ENGINE_TABLES trips the reverse predicate (proves the leg is a
    real detector, not a tautology)."""
    synthetic_roster = set(roster_for_dispatch()) | {"phantomengine"}
    missing = synthetic_roster - (set(ENGINE_TABLES) - {"allocator"})
    assert missing == {"phantomengine"}, (
        "the reverse predicate must flag a roster engine that has no "
        "ENGINE_TABLES row")
