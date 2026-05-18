# Engine SDLC SP1 — Unified Engine Roster SoT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `tpcore.engine_profile` the single mechanically-enforced SoT for the engine roster (existence, dispatch order, cadence, lifecycle classification) and derive every Python shadow from it, with ZERO live-dispatch behavior change.

**Architecture:** Extend the frozen `EngineProfile` with `dispatch_order:int` + `lifecycle_state:LifecycleState` (required) + `allocator_eligible:bool=False`; add 4 public accessors; derive 4 Python shadows (`engine_dispatch.ROSTER`, allocator `_ARCHIVED_ENGINES`, allocator `engines=` default, `check_imports.ENGINE_PACKAGES`); add a `should_fire` lifecycle fail-closed guard; add an N-way CI consistency test. `sigma` enters `_PROFILE` as `RETIRED`. Behavior-preservation is test-pinned to existing literals.

**Tech Stack:** Python 3.11, pydantic v2, `enum.StrEnum`, structlog, pytest (`asyncio_mode="auto"`). venv `/Users/michael/short-term-trading-engine/.venv/bin/python`; `ruff` on PATH. Worktree `/Users/michael/short-term-trading-engine/.claude/worktrees/engine-roster-sot` (branch `worktree-engine-roster-sot`).

**Spec:** `docs/superpowers/specs/2026-05-18-engine-roster-sot-design.md` (§13 hardening H-B1..H-B7 are BINDING).

**Lane discipline:** Touch ONLY `tpcore/engine_profile.py`, `ops/engine_dispatch.py` (1 line + 1 import), `tpcore/allocator/service.py` (2 derivations + 1 import), `tpcore/scripts/check_imports.py` (1 derivation + 1 import), `tpcore/tests/test_engine_profile.py`, `scripts/tests/test_engine_dispatch.py` (NO edits — oracle), `tpcore/tests/test_allocator_prune.py` (stale-docstring fix only), the new `tpcore/tests/test_engine_lifecycle_consistency.py`, and `docs/`. NEVER edit the DATA-SDLC read-only files: `tpcore/providers.py`, `tpcore/tests/test_provider_lifecycle_consistency.py`, `tpcore/feeds/`, `tpcore/selfheal/`, `tpcore/ladder/`, `ops/weekly_digest.py`, `ops/data_repair_service.py`, `scripts/run_data_operations.sh`. Never local-merge into shared main. CI-exact: `ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`; `python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore` (args unchanged — `canary` is already there).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `tpcore/engine_profile.py` | The SoT: enum + extended model + `_PROFILE` + accessors + should_fire guard | Modify (the core) |
| `ops/engine_dispatch.py` | Derive `ROSTER` | Modify (1 import + 1 line) |
| `tpcore/allocator/service.py` | Derive `_ARCHIVED_ENGINES` + `engines=` default | Modify (1 import + 2 lines) |
| `tpcore/scripts/check_imports.py` | Derive `ENGINE_PACKAGES` (fixes sigma/canary drift) | Modify (1 import + 1 line) |
| `tpcore/tests/test_engine_profile.py` | Extend for new fields/accessors/guard | Modify |
| `tpcore/tests/test_engine_lifecycle_consistency.py` | The N-way CI oracle | Create |
| `tpcore/tests/test_allocator_prune.py` | Stale-docstring fix only | Modify (comment) |
| `scripts/tests/test_engine_dispatch.py` | ROSTER behavior oracle | **NO edits** (must stay green) |

---

## Task 1: `LifecycleState` enum + extended `EngineProfile` + populated `_PROFILE` (pure data add)

**Files:** Modify `tpcore/engine_profile.py`; Test `tpcore/tests/test_engine_profile.py`.

No consumer wired yet — pure data add, zero behavior change.

- [ ] **Step 1: Write the failing tests.** Append to `tpcore/tests/test_engine_profile.py` (it already imports `from tpcore.engine_profile import Cadence, EngineProfile, _PROFILE, profile_for` and `pytest`, `ValidationError` — confirm and add `LifecycleState` to the import):

```python
def test_lifecycle_state_enum_values():
    from tpcore.engine_profile import LifecycleState
    assert {s.value for s in LifecycleState} == {"lab", "paper", "live", "retired"}


def test_profile_has_new_fields_all_seven_entries():
    from tpcore.engine_profile import LifecycleState
    # all 5 live engines + allocator are PAPER; sigma is RETIRED
    expected = {
        "allocator": (0, LifecycleState.PAPER, False),
        "reversion": (1, LifecycleState.PAPER, True),
        "vector":    (2, LifecycleState.PAPER, True),
        "momentum":  (3, LifecycleState.PAPER, True),
        "sentinel":  (4, LifecycleState.PAPER, False),
        "canary":    (5, LifecycleState.PAPER, False),
        "sigma":     (99, LifecycleState.RETIRED, False),
    }
    assert set(_PROFILE) == set(expected)
    for name, (order, state, elig) in expected.items():
        p = _PROFILE[name]
        assert p.dispatch_order == order
        assert p.lifecycle_state is state
        assert p.allocator_eligible is elig


def test_profile_for_sigma_returns_retired_profile():
    from tpcore.engine_profile import LifecycleState
    p = profile_for("sigma")
    assert p is not None and p.lifecycle_state is LifecycleState.RETIRED


def test_engine_profile_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        EngineProfile(engine="x", cadence=Cadence.DAILY)  # no dispatch_order/lifecycle_state
```

Also EXTEND the existing `test_profile_for_known_engines` to add `assert profile_for("canary").cadence is Cadence.DAILY` and `test_profile_covers_live_engine_roster`'s `live` set to `{"reversion","vector","momentum","sentinel","canary"}` (canary is in `_PROFILE`, stays green; the old comment referencing run_all_engines.sh:73 is stale — replace with `# SoT: tpcore.engine_profile._PROFILE (sigma RETIRED, excluded from live)`).

- [ ] **Step 2: Run, expect FAIL** — `cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-roster-sot && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -q` → FAIL (`ImportError: LifecycleState`, missing fields).

- [ ] **Step 3: Implement in `tpcore/engine_profile.py`.** The imports already include `from enum import StrEnum`. Add the enum immediately after `Cadence` (lines 31-34) and before `EngineProfile`:

```python
class LifecycleState(StrEnum):
    LAB = "lab"          # SP2 territory; never dispatched/allocated
    PAPER = "paper"      # graduated, paper-trading (current reality for all live engines)
    LIVE = "live"        # reserved; no engine here yet (paper-only mandate)
    RETIRED = "retired"  # snap-out complete; archive/EULOGY exists; never dispatched


_DISPATCHABLE: frozenset[LifecycleState] = frozenset(
    {LifecycleState.PAPER, LifecycleState.LIVE})
```

Replace the `EngineProfile` model (lines 37-41) with:

```python
class EngineProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str
    cadence: Cadence
    market_closed_required: bool = True
    dispatch_order: int
    lifecycle_state: LifecycleState
    allocator_eligible: bool = False
```

Replace the `_PROFILE` dict (lines 44-52) with (note `dispatch_order` 1-5 reproduces `engine_dispatch.ROSTER` order; allocator=0 keeps its pre-loop path; sigma RETIRED inert per D-SDLC1-6):

```python
_PROFILE: dict[str, EngineProfile] = {
    "reversion": EngineProfile(engine="reversion", cadence=Cadence.DAILY,
                               dispatch_order=1, lifecycle_state=LifecycleState.PAPER,
                               allocator_eligible=True),
    "vector":    EngineProfile(engine="vector", cadence=Cadence.DAILY,
                               dispatch_order=2, lifecycle_state=LifecycleState.PAPER,
                               allocator_eligible=True),
    "momentum":  EngineProfile(engine="momentum", cadence=Cadence.MONTHLY_FIRST_TRADING_DAY,
                               dispatch_order=3, lifecycle_state=LifecycleState.PAPER,
                               allocator_eligible=True),
    "sentinel":  EngineProfile(engine="sentinel", cadence=Cadence.DAILY,
                               dispatch_order=4, lifecycle_state=LifecycleState.PAPER),
    "canary":    EngineProfile(engine="canary", cadence=Cadence.DAILY,
                               dispatch_order=5, lifecycle_state=LifecycleState.PAPER),
    # allocator: separate _dispatch_allocator path (NOT in the ROSTER loop, D-SDLC1-4).
    "allocator": EngineProfile(engine="allocator", cadence=Cadence.WEEKLY_FIRST_TRADING_DAY,
                               dispatch_order=0, lifecycle_state=LifecycleState.PAPER),
    # sigma RETIRED (data-SDLC RETIRED symmetry); dispatch_order/cadence inert
    # (filtered out of every dispatch/allocator accessor by construction, D-SDLC1-6).
    "sigma":     EngineProfile(engine="sigma", cadence=Cadence.DAILY,
                               dispatch_order=99, lifecycle_state=LifecycleState.RETIRED),
}
```

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -q` → all pass (new + the extended existing tests; `test_profiles_are_frozen_and_self_consistent` stays green since `p.engine == name` holds for all 7).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "$(cat <<'EOF'
feat(engine_profile): LifecycleState + extended EngineProfile SoT (SDLC SP1 T1)

Add LifecycleState{LAB,PAPER,LIVE,RETIRED} + _DISPATCHABLE; extend the
frozen EngineProfile with required dispatch_order/lifecycle_state +
allocator_eligible=False; populate all 7 _PROFILE entries (5 live +
allocator PAPER; sigma RETIRED, inert). Pure data add — no consumer
wired, zero behavior change. Required fields safe (blast radius zero —
_PROFILE is the sole construction site, H-B4).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: The 4 public accessors + dispatch_order-uniqueness validation

**Files:** Modify `tpcore/engine_profile.py`; Test `tpcore/tests/test_engine_profile.py`.

- [ ] **Step 1: Write failing tests.** Append:

```python
def test_accessors_return_exact_frozen_literals():
    from tpcore.engine_profile import (
        roster_for_dispatch, allocator_eligible_engines,
        archived_engines, engine_package_names,
    )
    assert roster_for_dispatch() == ("reversion", "vector", "momentum", "sentinel", "canary")
    assert allocator_eligible_engines() == ("reversion", "vector", "momentum")
    assert archived_engines() == ("sigma",)
    assert engine_package_names() == frozenset(
        {"reversion", "vector", "momentum", "sentinel", "canary"})


def test_roster_excludes_allocator_and_retired():
    from tpcore.engine_profile import roster_for_dispatch
    r = roster_for_dispatch()
    assert "allocator" not in r and "sigma" not in r


def test_dispatch_order_uniqueness_validation():
    from tpcore.engine_profile import EngineProfile, LifecycleState, _roster_sorted
    bad = {
        "a": EngineProfile(engine="a", cadence=__import__("tpcore.engine_profile",
              fromlist=["Cadence"]).Cadence.DAILY, dispatch_order=1,
              lifecycle_state=LifecycleState.PAPER),
        "b": EngineProfile(engine="b", cadence=__import__("tpcore.engine_profile",
              fromlist=["Cadence"]).Cadence.DAILY, dispatch_order=1,
              lifecycle_state=LifecycleState.PAPER),
    }
    with pytest.raises(ValueError, match="duplicate dispatch_order"):
        _roster_sorted(bad)
```

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -k "accessors or excludes_allocator or uniqueness" -q` → FAIL (accessors missing).

- [ ] **Step 3: Implement.** Add to `tpcore/engine_profile.py` after `profile_for` (lines 55-57):

```python
def _roster_sorted(profiles: dict[str, EngineProfile]) -> list[EngineProfile]:
    """Non-RETIRED, non-allocator profiles sorted by dispatch_order.
    Raises ValueError on a duplicate dispatch_order among them — the
    sort key MUST be total (ROSTER binds at import before tests run)."""
    live = [p for p in profiles.values()
            if p.lifecycle_state in _DISPATCHABLE and p.engine != "allocator"]
    orders = [p.dispatch_order for p in live]
    if len(set(orders)) != len(orders):
        raise ValueError(f"duplicate dispatch_order among dispatchable engines: {orders}")
    return sorted(live, key=lambda p: p.dispatch_order)


def roster_for_dispatch() -> tuple[str, ...]:
    """Engines dispatched in the ROSTER loop: PAPER/LIVE, non-allocator,
    ordered by dispatch_order. The authority for ops.engine_dispatch.ROSTER."""
    return tuple(p.engine for p in _roster_sorted(_PROFILE))


def allocator_eligible_engines() -> tuple[str, ...]:
    """Inverse-vol-pool engines (allocator_eligible), ordered by dispatch_order.
    Replaces the hand-typed allocator `engines=` default."""
    return tuple(p.engine for p in _roster_sorted(_PROFILE) if p.allocator_eligible)


def archived_engines() -> tuple[str, ...]:
    """RETIRED engines (provenance-in-SoT; data-SDLC RETIRED symmetry),
    sorted by name. Consumer is `engine = ANY($1::text[])` (set semantics),
    so order is behavior-equivalent; sorted for stable test diffs."""
    return tuple(sorted(p.engine for p in _PROFILE.values()
                        if p.lifecycle_state is LifecycleState.RETIRED))


def engine_package_names() -> frozenset[str]:
    """Top-level engine package dirs (PAPER/LIVE, non-allocator) — for the
    tpcore-never-imports-an-engine layering invariant (check_imports)."""
    return frozenset(roster_for_dispatch())
```

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -q` → all pass.

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "$(cat <<'EOF'
feat(engine_profile): roster accessors + dispatch_order uniqueness (SDLC SP1 T2)

roster_for_dispatch/allocator_eligible_engines/archived_engines/
engine_package_names — each returns the exact frozen literal it will
replace. _roster_sorted raises on duplicate dispatch_order (binds at
import before tests, H-B2). No consumer wired yet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `should_fire` non-dispatchable fail-closed guard (H-B7)

**Files:** Modify `tpcore/engine_profile.py`; Test `tpcore/tests/test_engine_profile.py`.

Restores the defense-in-depth the RETIRED-in-`_PROFILE` pattern would otherwise remove (`profile_for("sigma")` now returns a profile, so the old "unprofiled" fail-closed no longer fires for sigma).

- [ ] **Step 1: Write the failing test.** Append:

```python
async def test_should_fire_fails_closed_for_non_dispatchable_lifecycle():
    # sigma is RETIRED in _PROFILE → should_fire must fail-closed even
    # though profile_for now returns a profile (H-B7).
    d = await should_fire("sigma", datetime(2026, 5, 18, 21, 0, tzinfo=UTC), pool=None)
    assert d.fire is False
    assert d.reason == "engine not dispatchable (lifecycle)"
    assert d.checks.get("dispatchable") is False
```
(Ensure the test file imports `should_fire`, `datetime`, `UTC` — add to its imports if missing; `pool=None` is safe because the guard returns before any pool use.)

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -k non_dispatchable -q` → FAIL (no guard; would proceed past the profiled check).

- [ ] **Step 3: Implement.** In `should_fire`, the current profiled-check block is exactly:

```python
        profile = profile_for(engine)
        checks["profiled"] = profile is not None
        if profile is None:
            return FireDecision(False, "unprofiled engine", checks)
```

Insert the lifecycle guard IMMEDIATELY AFTER it (before the `checks["cadence"] = ...` line):

```python
        checks["dispatchable"] = profile.lifecycle_state in _DISPATCHABLE
        if not checks["dispatchable"]:
            return FireDecision(False, "engine not dispatchable (lifecycle)", checks)
```

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -q` → all pass. Then the full should_fire-consumer suites stay green: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py tpcore/tests/test_engine_profile.py -q` → green (PAPER engines unaffected — they pass `dispatchable`).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "$(cat <<'EOF'
feat(engine_profile): should_fire lifecycle fail-closed guard (SDLC SP1 T3, H-B7)

A non-PAPER/LIVE engine (e.g. RETIRED sigma now in _PROFILE) fails
closed at "engine not dispatchable (lifecycle)" right after the
profiled check — restores the defense-in-depth the RETIRED-in-SoT
pattern would otherwise remove. PAPER engines unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Derive `ops/engine_dispatch.py` `ROSTER` (first behavior-touch; pinned byte-equivalent)

**Files:** Modify `ops/engine_dispatch.py`; Oracle `scripts/tests/test_engine_dispatch.py` (**NO edits** — ~13 ROSTER assertions are the behavior-preservation oracle).

- [ ] **Step 1: Confirm the oracle is currently green** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py -q` → all pass (baseline).

- [ ] **Step 2: Implement.** In `ops/engine_dispatch.py`, the import line is `from tpcore.engine_profile import cadence_window_start, should_fire` — extend it to `from tpcore.engine_profile import cadence_window_start, roster_for_dispatch, should_fire`. Replace line 28 exactly:

```python
ROSTER: tuple[str, ...] = ("reversion", "vector", "momentum", "sentinel", "canary")
```

with:

```python
ROSTER: tuple[str, ...] = roster_for_dispatch()
```

- [ ] **Step 3: Run the oracle, expect UNCHANGED PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py -q` → ALL pass with ZERO edits to that file (the `ROSTER == ("reversion","vector","momentum","sentinel","canary")`, `list(ROSTER)`, `len(ROSTER)` assertions at ~13 sites are the proof of byte-equivalence). If any fails, the SoT/accessor is wrong — fix Task 1/2, NEVER the oracle test.

- [ ] **Step 4: ruff + commit**
```bash
ruff check ops/engine_dispatch.py
git add ops/engine_dispatch.py
git commit -m "$(cat <<'EOF'
refactor(engine_dispatch): derive ROSTER from the engine_profile SoT (SDLC SP1 T4)

ROSTER = roster_for_dispatch() (import-time snapshot; DAG acyclic —
engine_profile imports nothing from ops; _PROFILE import-frozen, H-B3).
The ~13 test_engine_dispatch.py ROSTER assertions stay green with ZERO
edits — the behavior-preservation oracle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Derive allocator `_ARCHIVED_ENGINES`

**Files:** Modify `tpcore/allocator/service.py`; Oracle `tpcore/tests/test_allocator_prune.py` (assertion stays green; fix only the stale docstring).

- [ ] **Step 1: Confirm oracle green** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_allocator_prune.py -q` → all pass (baseline; `prune_calls[0][2] == ["sigma"]`).

- [ ] **Step 2: Implement.** `tpcore/allocator/service.py` does NOT yet import engine_profile (no cycle: engine_profile imports calendar/capital_gate/supervisor_state, none import allocator). Add to its import block: `from tpcore.engine_profile import archived_engines`. Replace line 87 exactly:

```python
_ARCHIVED_ENGINES: tuple[str, ...] = ("sigma",)
```

with:

```python
_ARCHIVED_ENGINES: tuple[str, ...] = archived_engines()
```

- [ ] **Step 3: Fix the stale docstring (O2).** In `tpcore/tests/test_allocator_prune.py`, the `_make_service_production_default` docstring (~line 227) wrongly claims the default is `("sigma","reversion","vector","momentum")`. Replace that docstring sentence with: `"""Construct AllocatorService EXACTLY as production does — WITHOUT passing ``engines=``, so ``self._engines`` is the ``__init__`` default (sigma-free since 2026-05-16; the prune path separately targets archived_engines())."""`. Do NOT change any assertion.

- [ ] **Step 4: Run, expect UNCHANGED PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_allocator_prune.py -q` → all pass (`prune_calls[0][2] == ["sigma"]` byte-equivalent: `archived_engines()` resolves `("sigma",)`; consumer is `WHERE engine = ANY($1::text[])`, set semantics).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/allocator/service.py tpcore/tests/test_allocator_prune.py
git add tpcore/allocator/service.py tpcore/tests/test_allocator_prune.py
git commit -m "$(cat <<'EOF'
refactor(allocator): derive _ARCHIVED_ENGINES from the SoT (SDLC SP1 T5)

_ARCHIVED_ENGINES = archived_engines() (RETIRED-state-derived;
data-SDLC RETIRED symmetry, D-SDLC1-2). Resolved ("sigma",) —
prune oracle green unchanged. + fix a stale test docstring (O2).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Derive allocator `engines=` default

**Files:** Modify `tpcore/allocator/service.py`; Oracle `tpcore/tests/test_allocator_engine_default.py` (stays green unchanged).

- [ ] **Step 1: Confirm oracle green** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_allocator_engine_default.py -q` → pass (`svc._engines == ("reversion","vector","momentum")`).

- [ ] **Step 2: Implement.** Mutable-default-safe: bind a module constant once at import (an immutable tuple) and use it as the signature default. Extend the existing import added in Task 5 to `from tpcore.engine_profile import allocator_eligible_engines, archived_engines`. Add, near `_ARCHIVED_ENGINES` (module scope):

```python
_DEFAULT_ENGINES: tuple[str, ...] = allocator_eligible_engines()
```

In `__init__`, the param line is exactly:

```python
    engines: tuple[str, ...] = ("reversion", "vector", "momentum"),
```

Replace it with:

```python
    engines: tuple[str, ...] = _DEFAULT_ENGINES,
```

(Keep the explanatory comment block above it intact — it documents WHY the subset excludes allocator/sentinel/canary; still accurate.)

- [ ] **Step 3: Run, expect UNCHANGED PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_allocator_engine_default.py tpcore/tests/test_allocator_prune.py -q` → all pass (`_DEFAULT_ENGINES` resolves `("reversion","vector","momentum")` — `allocator_eligible_engines()` sorts by dispatch_order: reversion(1),vector(2),momentum(3)).

- [ ] **Step 4: ruff + commit**
```bash
ruff check tpcore/allocator/service.py
git add tpcore/allocator/service.py
git commit -m "$(cat <<'EOF'
refactor(allocator): derive engines= default from the SoT (SDLC SP1 T6)

_DEFAULT_ENGINES = allocator_eligible_engines() (module constant,
immutable-tuple — no mutable-default hazard) as the __init__ default.
Resolves ("reversion","vector","momentum") — engine-default oracle
green unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Derive `check_imports.ENGINE_PACKAGES` (H-B1 — fixes sigma/canary drift)

**Files:** Modify `tpcore/scripts/check_imports.py`; Test new assertions in `tpcore/tests/test_engine_profile.py` (or a small dedicated test).

- [ ] **Step 1: Write the failing test.** Append to `tpcore/tests/test_engine_profile.py`:

```python
def test_check_imports_engine_packages_derived_and_drift_fixed():
    from tpcore.scripts.check_imports import ENGINE_PACKAGES
    assert ENGINE_PACKAGES == frozenset(
        {"reversion", "vector", "momentum", "sentinel", "canary"})
    assert "sigma" not in ENGINE_PACKAGES   # archived drift fixed
    assert "canary" in ENGINE_PACKAGES      # missing-live drift fixed
```

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -k engine_packages -q` → FAIL (current frozenset has sigma, lacks canary).

- [ ] **Step 3: Implement.** `tpcore/scripts/check_imports.py` lines 36-38 are exactly:

```python
ENGINE_PACKAGES = frozenset(
    {"sigma", "reversion", "vector", "momentum", "sentinel"}
)
```

Replace with (add the import at the top of the file, ruff-ordered with the existing imports — no cycle: `engine_profile` does not import `tpcore.scripts`):

```python
from tpcore.engine_profile import engine_package_names

ENGINE_PACKAGES = engine_package_names()
```

(`engine_package_names()` already returns a `frozenset`; `scan_dir` uses `FORBIDDEN_MODULES | ENGINE_PACKAGES` — frozenset union is correct.)

- [ ] **Step 4: Run, expect PASS + the CLI still works** —
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -q
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore; echo "rc=$?"
```
Expected: tests pass; `check_imports` prints `ok: no forbidden imports found` and `rc=0` (deriving the frozenset, now correctly including canary + excluding the dead sigma path, does not introduce a forbidden import — the live engines never import each other; tpcore still must not import any of the 5).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/scripts/check_imports.py tpcore/tests/test_engine_profile.py
git add tpcore/scripts/check_imports.py tpcore/tests/test_engine_profile.py
git commit -m "$(cat <<'EOF'
refactor(check_imports): derive ENGINE_PACKAGES from the SoT (SDLC SP1 T7, H-B1)

ENGINE_PACKAGES = engine_package_names() — the layering-invariant
engine set is now SoT-derived, fixing the latent drift (stale sigma
removed, missing canary added). CLI args unchanged (canary already
listed). The expert caught the original spec's grounding error here.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `ENGINE_TABLES` subset CI test (documented seam, no code change)

**Files:** Test only — add to `tpcore/tests/test_engine_lifecycle_consistency.py` (created in Task 9) OR a small standalone; for ordering simplicity add it as part of Task 9's file. **This task = author the seam test spec; it lands in the Task 9 file.** (Kept as a distinct checklist item so the seam is not forgotten.)

- [ ] **Step 1: Note the seam.** `tpcore/quality/validation/capital_gate.py` `ENGINE_TABLES` (lines 60-77) keys = `{reversion,vector,momentum,sentinel,allocator,canary}`. It is a data-dependency map (frozenset of tables per engine), NOT a name list — deliberately NOT collapsed (D-SDLC1-1). The invariant: every key must be a known engine (live roster ∪ {allocator}); no key may be a RETIRED/unknown engine. This assertion is implemented as leg 6 of the Task 9 test (see Task 9 Step 3). No `capital_gate.py` change.

- [ ] **Step 2:** (no-op standalone; verified within Task 9.)

---

## Task 9: The N-way CI consistency test + full gate + finish

**Files:** Create `tpcore/tests/test_engine_lifecycle_consistency.py`; then full-suite/CI/lane gate + finishing-a-development-branch.

- [ ] **Step 1: Write the test (it must pass immediately against the now-consistent SoT).** Create `tpcore/tests/test_engine_lifecycle_consistency.py` — structural analog of `tpcore/tests/test_provider_lifecycle_consistency.py` (the data oracle; READ it for shape, do NOT import its data-lane modules):

```python
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
    LifecycleState,
    _PROFILE,
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
```

(Confirm the exact `pyproject.toml` TOML key path by reading it: `[tool.pytest.ini_options].testpaths` and `[tool.setuptools.packages.find].include`. The recon shows testpaths contains `"reversion/tests"`,…,`"canary/tests"` and include contains `"reversion*"`,…,`"canary*"` — the asserts match. If the TOML structure differs, align the key path; keep the asserted invariant: every live engine has a `tests` testpath and a package glob.)

- [ ] **Step 2: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -q` → all 6 pass against the now-consistent SoT (sigma RETIRED has `archive/sigma/EULOGY.md` and no top-level `sigma/`; the 5 live engines each have pkg+tests+scheduler; smoke loop + pyproject match).

- [ ] **Step 3: Full-suite + CI-exact + lane gate** (report each verbatim tail):
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -3
ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
BASE=$(git merge-base HEAD origin/main); git diff --name-only $BASE..HEAD | grep -E "tpcore/providers\.py|tpcore/tests/test_provider_lifecycle_consistency\.py|tpcore/(feeds|selfheal|ladder)/|ops/weekly_digest\.py|ops/data_repair_service\.py|scripts/run_data_operations\.sh" && echo "LANE VIOLATION" || echo "lane-clean"
```
Expected: full suite green (baseline + new tests; `test_engine_dispatch.py` ZERO-edit green = the behavior oracle); `All checks passed!`; `ok: no forbidden imports found`; `lane-clean`.

- [ ] **Step 4: Commit the test**
```bash
ruff check tpcore/tests/test_engine_lifecycle_consistency.py
git add tpcore/tests/test_engine_lifecycle_consistency.py
git commit -m "$(cat <<'EOF'
test(engine_profile): N-way engine-lifecycle consistency oracle (SDLC SP1 T8+T9)

6 legs (dispatch-order frozen literal; live⇒pkg+tests+find_spec;
RETIRED⇒offboarded incl. EULOGY + no package; no half-state;
ENGINE_TABLES keys⊆live∪allocator seam; structurally-parseable
shadow-drift over run_smoke_test.sh + pyproject). Engine-domain
analog of test_provider_lifecycle_consistency.py (structural
symmetry, not a clone — no data-lane import). Prose-only
run_all_engines.sh/platform_pipeline.py deferred to SP4 (H-B6).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Finish the branch.** Use **superpowers:finishing-a-development-branch**. Per the established pattern: push `worktree-engine-roster-sot`, open a PR, fetch origin/main and resolve conflicts combining intents (data session may have touched shared files — keep BOTH), integrated full suite green, merge when CI green (squash, no `--delete-branch` to avoid the shared-main checkout error; delete the remote branch separately), clean the worktree. Do NOT local-merge into the shared checkout.

---

## Self-Review

**1. Spec coverage:** §3 model+accessors → T1+T2; §4 derive-the-shadows (ROSTER/_ARCHIVED_ENGINES/engines=/ENGINE_PACKAGES) → T4/T5/T6/T7; §5 behavior-preservation (ROSTER literal, allocator separate path, untouched should_fire flow except the additive guard, test-pinned allocator equivalence) → T4/T5/T6 oracle gates + the frozen-literal accessor tests in T2; §6 N-way test (6 legs) → T9; §7 sigma→RETIRED + the "Live⇒wired excludes RETIRED" correctness → T1 + T9 legs; §8 decisions D-SDLC1-1..6 honored (ENGINE_TABLES seam T8/T9-leg6; RETIRED replaces _ARCHIVED_ENGINES T5; field-in-SP1 T1, no transition logic; allocator separate path preserved T1/T4; archive leg partial T9; inert sigma T1); §9 symmetry (data test = structural template, no data-lane import — T9); §13 H-B1 (ENGINE_PACKAGES derive T7), H-B2 (uniqueness validation T2), H-B3 (import-frozen, oracle zero-edit T4), H-B4 (required fields T1), H-B5 (find_spec T9), H-B6 (parseable vs prose split — T9 tests only smoke+pyproject; run_all_engines/platform_pipeline NOT tested, SP4), H-B7 (should_fire guard T3). All mapped.

**2. Placeholder scan:** every step has literal code + exact command + expected result. The few "confirm exact TOML key path / import is present, align if it differs, keep the invariant" notes are explicit verify-against-reality contingencies (the recon already supplies the verbatim answers) — the accepted style, not deferred work. Task 8 is intentionally a thin checklist marker whose assertion lands in Task 9's file (stated explicitly) so the ENGINE_TABLES seam is not forgotten — not a placeholder.

**3. Type/name consistency:** `LifecycleState`, `_DISPATCHABLE`, `_roster_sorted`, `roster_for_dispatch`, `allocator_eligible_engines`, `archived_engines`, `engine_package_names` consistent across T1/T2 defs, T3 guard, T4-T7 consumers, T9 test, and every commit msg. `EngineProfile(engine,cadence,market_closed_required,dispatch_order,lifecycle_state,allocator_eligible)` consistent T1↔T2↔T9. `FireDecision(False, "engine not dispatchable (lifecycle)", checks)` positional — matches the verbatim existing `FireDecision(False, "reason", checks)` signature. Oracle files (`test_engine_dispatch.py`) explicitly ZERO-edit. No mismatches.
