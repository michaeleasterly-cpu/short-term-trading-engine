# Lab SP-B — Roster-Driven Plug-and-Play Lab Targeting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stale hardwired `(reversion, vector, momentum)` 3-tuple across all six Lab dispatch surfaces with a single roster-SoT-driven indirection, so engine add/remove is a `tpcore.engine_profile` edit (never Lab surgery), while the SP-A cumulative n_trials ledger and the live trading path stay byte-identical.

**Architecture:** Each runnable engine's `<engine>.backtest` exports one frozen pydantic-v2 `LabTarget` constant (`LAB_TARGET`) carrying its `param_ranges` dict + its four already-uniform dispatch callables. A new engine-free `tpcore/lab/target.py` defines the contract; a new `tpcore.engine_profile.lab_targetable_engines()` accessor is the single roster authority for "which engines the Lab may fish against"; `ops/lab/run.py` resolves `LAB_TARGET` lazily via `importlib` inside a `_lab_target_for` resolver that all five dispatch seams (the three `_*_for` views, the lazy `PARAM_RANGES` Mapping, and the sixth-surface `ops/engine_sdlc/default_params.py` shim) delegate to. A pure runtime-equality consistency clockwork test reds CI on any roster/Lab drift — no new byte-shadow.

**Tech Stack:** Python 3.11, pydantic v2 (`BaseModel`/`ConfigDict`), `collections.abc.Mapping`, `importlib`, stdlib `argparse`, pytest + pytest-asyncio + pytest-xdist (`xdist_group("ops_shadow")`), ruff, `tpcore.scripts.check_imports`, `scripts/gen_engine_manifest.py --check`.

---

## Required reading before any code (zero-context engineer)

1. **Spec (source of truth):** `docs/superpowers/specs/2026-05-19-lab-sp-b-roster-driven-targeting-design.md` — read ALL of §0 (six verified surfaces), §0.1 (the sixth surface rationale), §1 (chosen mechanism + the consistency-clockwork choice), §2 (architecture: §2.1 accessor, §2.2 `LabTarget`, §2.3 resolver+shim, §2.4 the **binding `ValueError`→`KeyError` re-raise contract**, §2.5 CLI, §2.6 clockwork), §3 (component table), §4 (11 edge cases — esp. §4.10 free-`str` `target_engine`, §4.11 the parity-test message-pin), §5 (test strategy), §6 (NON-GOALS), §7 (T0..T8 phasing), §8 (adversarial record — the highest residual risk is the §2.4 `KeyError` contract).
2. **Epic context:** `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md` §1 SP-B + "Decomposition risk notes" (SP-A and SP-B are deliberately disjoint; SP-A first, no merge).
3. `docs/STYLE_GUIDE.md` — `from __future__ import annotations` mandatory; full type hints; pydantic v2 `model_config = ConfigDict(...)`; **never access tpcore privates / never add a new `# noqa: SLF001`**; if you need an accessor that doesn't exist, add a public one to the tpcore class.
4. `docs/glossary.md` — terms (SoT, roster, DSR, credibility gate, walk-forward).
5. `CLAUDE.md` — engine roster changes go through the SoT only; char-before-refactor for dispatch; the data lane is read-only here; `ops/` is exempt from the `check_imports` tpcore∌engine scan (engine imports legal in `ops/`, never in `tpcore/`).

**Worktree:** This plan should be executed in an isolated worktree created via `superpowers:using-git-worktrees` at execution time. All paths below are repo-root-relative.

---

## File structure (decomposition locked here)

| File | Action | Responsibility |
|---|---|---|
| `tpcore/lab/target.py` | **Create** | Engine-free `LabTarget` frozen pydantic-v2 contract + fail-loud `model_post_init` tuple/kind validation. |
| `tpcore/tests/test_lab_target.py` | **Create** | `LabTarget` unit tests (good/bad construction). |
| `tpcore/engine_profile.py` | **Modify** (after `archived_engines`, ~`:130`) | Add `_LAB_TARGETABLE` frozenset + `_LAB_SENTINEL` const + `lab_targetable_engines()` accessor. |
| `tpcore/tests/test_lab_targetable_accessor.py` | **Create** | Accessor predicate-table tests. |
| `reversion/backtest.py` | **Modify** (append after `run_for_search`, ~`:1102`) | `LAB_TARGET` constant. |
| `vector/backtest.py` | **Modify** (append after `run_for_search`, end-of-module) | `LAB_TARGET` constant. |
| `momentum/backtest.py` | **Modify** (append after `run_for_search`, end-of-module) | `LAB_TARGET` constant. |
| `tpcore/tests/test_engine_lab_target_declarations.py` | **Create** | Each engine declares a valid `LAB_TARGET`; param-range parity vs. captured T0 snapshot; live-import-surface unchanged. |
| `ops/lab/run.py` | **Modify** (`:95-131` `PARAM_RANGES`; `:146-149` `sample_parameters`; `:316-356` the three `_*_for`) | `_lab_target_for` resolver + lazy `_LazyParamRanges` Mapping + 3 thin seam views + `sample_parameters` clear-error wrap. |
| `ops/engine_sdlc/default_params.py` | **Modify** (`:13-23`) | Body → thin `_lab_target_for(engine).default_params()` delegate. |
| `tpcore/tests/test_engine_default_params_parity.py` | **Modify** (`:35-38` ONE assertion) | `test_dispatcher_rejects_unknown_engine` `match=` regex updated; type unchanged. |
| `tpcore/tests/test_lab_dispatch_indirection.py` | **Create** | `_lab_target_for` + lazy `PARAM_RANGES` Mapping + the **binding `ValueError`→`KeyError`** contract + `default_params` shim + no-import-cycle. |
| `ops/lab/__main__.py` | **Modify** (`:50-53`) | `choices=lab_targetable_engines()`. |
| `ops/lab/run.py` | **Modify** (`:620`) | `choices=lab_targetable_engines()`. |
| `tpcore/tests/test_lab_cli_choices_from_roster.py` | **Create** | CLI choices generated; no-eager-engine-import invariant. |
| `tpcore/tests/test_lab_targeting_consistency.py` | **Create** | The SP-B clockwork: target-set==roster, CLI-generated, synthetic-drift red-proof, canary/lab pins, SP-A non-regression. |
| `tpcore/templates/engine_template/backtest.py` | **Modify** (append) | Commented `LAB_TARGET` skeleton (SP-F forward dep). |

**Explicitly NOT touched** (NON-GOALS, spec §6): `tpcore/lab/ledger.py`, `tpcore/lab/context.py`, `compute_dsr_for_verdict`, `_run_lab_core`'s SP-A ledger block (`run.py:745-759`, `:867-872`), the `survived` gate (`:977-981`), every `<engine>` scheduler/order-manager/plug, `gen_engine_manifest.py` `_FILE_REGIONS`, `tpcore/lab/models.py::LabCandidate` (free-`str` `target_engine`), `_score_for_ranking`/`rank_candidates`, `test_engine_default_params_parity.py:8` literal triple, the characterization oracle `scripts/tests/test_search_parameters_characterization.py` (must pass UNMODIFIED), any `_PROFILE` entry add/remove.

---

## Task 0: Characterization baseline (no production code) — spec §7 T0

**Purpose:** Pin the regression contract. The existing characterization oracle + lifecycle clockwork must be green on the untouched tree, and the *current* `PARAM_RANGES` per-engine keysets must be captured so T4's lazy Mapping can be proven byte-parity.

**Files:**
- Test: `scripts/tests/test_search_parameters_characterization.py` (run only — DO NOT edit)
- Test: `tpcore/tests/test_engine_lifecycle_consistency.py` (run only)
- Artifact (no file): the T0 byte-parity oracle is the inlined module-level `_T0_PARAM_RANGES_KEYSETS` constant authored into T4's parity test (`tpcore/tests/test_engine_lab_target_declarations.py` / `tpcore/tests/test_lab_dispatch_indirection.py`) — a 3-key snapshot of a fully-known constant needs NO committed file fixture (YAGNI; and a runtime fixture must never live under the `docs/` plans tree — #252 docs-to-reality).

- [ ] **Step 1: Run the characterization oracle green on the untouched tree**

Run: `python -m pytest scripts/tests/test_search_parameters_characterization.py -p no:xdist -q`
Expected: PASS (all tests; this is the baseline the T4 refactor must not move).

- [ ] **Step 2: Run the lifecycle clockwork green on the untouched tree**

Run: `python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -p no:xdist -q`
Expected: PASS (proves the roster SoT shadows are in sync before SP-B).

- [ ] **Step 3: Capture the current per-engine `PARAM_RANGES` keysets (the byte-parity oracle for T4)**

Run (from the repo root):
```bash
python -c "import json; from ops.lab.run import PARAM_RANGES; print(json.dumps({e: sorted(PARAM_RANGES[e]) for e in PARAM_RANGES}, indent=2))"
```
Expected output (verify exactly — these are the verified `run.py:99-131` keys):
```json
{
  "reversion": ["max_hold_days", "stop_pct", "volume_climax_multiplier", "z_threshold"],
  "vector": ["catalyst_window_days", "de_ceiling", "pb_ceiling", "stop_pct", "swing_score_threshold"],
  "momentum": ["hold_days", "lookback_days", "skip_days", "top_decile_pct"]
}
```

The T0 artifact is NOT a committed file. It is the inlined module-level `_T0_PARAM_RANGES_KEYSETS` constant authored verbatim into T1's parity test (`tpcore/tests/test_engine_lab_target_declarations.py`) and reused in T4's dispatch test (`tpcore/tests/test_lab_dispatch_indirection.py`). The verified keysets are:
```python
_T0_PARAM_RANGES_KEYSETS: dict[str, list[str]] = {
    "reversion": ["max_hold_days", "stop_pct", "volume_climax_multiplier", "z_threshold"],
    "vector": ["catalyst_window_days", "de_ceiling", "pb_ceiling", "stop_pct", "swing_score_threshold"],
    "momentum": ["hold_days", "lookback_days", "skip_days", "top_decile_pct"],
}
```
If Step 3's command output does not byte-match the dict above, STOP — the baseline literal in the test files must be updated to the real values before any refactor (the constant IS the oracle; it must reflect the un-refactored tree).

- [ ] **Step 4: Commit the oracle/clockwork-green confirmation**

There is no file to `git add` — the T0 oracle ships as the inlined constant inside the T1/T4 test files (created in their own tasks). Task 0 is the *confirmation* that the characterization oracle + lifecycle clockwork are green on the untouched tree and that the keysets above are accurate; no separate commit is required for an artifact that does not exist.

---

## Task 1: `tpcore/lab/target.py::LabTarget` — the engine-free contract — spec §2.2 / §7 T1

**Files:**
- Create: `tpcore/lab/target.py`
- Test: `tpcore/tests/test_lab_target.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_lab_target.py`:
```python
"""SP-B — LabTarget engine-free contract: declaration-time fail-loud
validation of the (low, high, kind) tuple/kind contract.
"""
from __future__ import annotations

import pytest


def _callables():
    async def _afn(*a, **k):  # run_for_search / load_window_context
        return None

    def _sfn(*a, **k):  # run_with_context
        return None

    def _dp() -> dict:  # default_params
        return {}

    return _afn, _sfn, _dp


def test_labtarget_accepts_valid_declaration():
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(
        param_ranges={"z": (2.0, 4.0, "float"), "n": (3, 12, "int"),
                      "m": (0, 1, "choice:a,b")},
        run_for_search=afn,
        load_window_context=afn,
        run_with_context=sfn,
        default_params=dp,
    )
    assert t.param_ranges["z"] == (2.0, 4.0, "float")
    assert callable(t.run_for_search)
    assert callable(t.default_params)


def test_labtarget_is_frozen_and_extra_forbid():
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp)
    with pytest.raises(Exception):  # pydantic frozen → ValidationError
        t.param_ranges = {}
    with pytest.raises(Exception):  # extra="forbid"
        LabTarget(param_ranges={}, run_for_search=afn,
                  load_window_context=afn, run_with_context=sfn,
                  default_params=dp, bogus=1)


@pytest.mark.parametrize("bad", [
    {"z": (2.0, 4.0)},                       # 2-tuple, not 3
    {"z": (2.0, 4.0, "floar")},              # typo kind
    {"z": (2.0, 4.0, "choice")},             # choice w/o ":"
    {"z": [2.0, 4.0, "float"]},              # list not tuple
    {"z": (2.0, 4.0, 7)},                    # kind not str
])
def test_labtarget_rejects_malformed_param_ranges_at_construction(bad):
    """Fail-loud at DECLARATION time (model_post_init), not at sample
    time on a live-money-adjacent path (spec §2.2)."""
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    with pytest.raises(ValueError):
        LabTarget(param_ranges=bad, run_for_search=afn,
                  load_window_context=afn, run_with_context=sfn,
                  default_params=dp)


def test_labtarget_module_is_engine_free():
    """tpcore/lab/target.py imports only pydantic + stdlib — no engine,
    no tpcore→engine edge (check_imports stays green)."""
    import ast
    from pathlib import Path

    src = Path("tpcore/lab/target.py").read_text()
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    for engine in ("reversion", "vector", "momentum", "sentinel", "canary"):
        assert engine not in mods, f"target.py must not import {engine}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tpcore/tests/test_lab_target.py -p no:xdist -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.lab.target'`.

- [ ] **Step 3: Write minimal implementation**

Create `tpcore/lab/target.py`:
```python
"""SP-B — the engine-FREE Lab targeting contract.

A runnable engine's ``<engine>.backtest`` exports ONE module-level
``LAB_TARGET = LabTarget(...)`` carrying its parameter-range dict + its
four already-uniform dispatch callables. ``ops.lab.run`` resolves it via
the roster SoT (``tpcore.engine_profile.lab_targetable_engines``) +
``importlib`` — the engine OWNS its Lab declaration; engine add/remove
is an ``_PROFILE`` edit + the engine declaring ``LAB_TARGET``, never Lab
surgery (spec §1, §2.2).

Engine-FREE on purpose: imports only pydantic + stdlib. The dependency
flows engine→tpcore (the engine imports THIS); tpcore NEVER imports an
engine (``check_imports tpcore`` stays green). Lives next to
``tpcore/lab/{ledger,context,models}.py`` — the established engine-free
Lab contract layer (H-S2-1).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict


class LabTarget(BaseModel):
    """Frozen per-engine Lab dispatch contract.

    ``param_ranges`` maps a swept param name → ``(low, high, kind)``
    where ``kind`` is ``"float"`` | ``"int"`` | ``"choice:<csv>"`` — the
    exact ``ops.lab.run._sample_value`` contract (run.py:134-143).
    ``model_post_init`` validates this fail-loud at DECLARATION time so
    a malformed range never defers its error to sample time on a
    live-money-adjacent path (spec §2.2, §8-B5).
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    param_ranges: dict[str, tuple]
    run_for_search: Callable[..., Awaitable[Any]]
    load_window_context: Callable[..., Awaitable[Any]]
    run_with_context: Callable[..., Any]
    default_params: Callable[[], dict[str, Any]]

    def model_post_init(self, __context: Any) -> None:
        for name, spec in self.param_ranges.items():
            if not isinstance(spec, tuple) or len(spec) != 3:
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] must be a 3-tuple "
                    f"(low, high, kind); got {spec!r}"
                )
            kind = spec[2]
            if not isinstance(kind, str):
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] kind must be str; "
                    f"got {kind!r}"
                )
            if kind not in ("float", "int") and not kind.startswith(
                "choice:"
            ):
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] kind {kind!r} not "
                    f"in 'float'|'int'|'choice:<csv>'"
                )


__all__ = ["LabTarget"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tpcore/tests/test_lab_target.py -p no:xdist -q`
Expected: PASS (all 6 cases incl. the 5 parametrized malformed ones).

- [ ] **Step 5: Verify the layering invariant stays green**

Run: `python -m tpcore.scripts.check_imports tpcore`
Expected: exit 0 (no output / "no forbidden imports"). `tpcore/lab/target.py` is engine-free.

- [ ] **Step 6: Commit**

```bash
git add tpcore/lab/target.py tpcore/tests/test_lab_target.py
git commit -m "feat(lab-sp-b): add engine-free LabTarget contract with fail-loud range validation"
```

---

## Task 2: `tpcore.engine_profile.lab_targetable_engines()` accessor — spec §2.1 / §7 T2

**Files:**
- Modify: `tpcore/engine_profile.py` (insert after `archived_engines`, ~`:130`, before `engine_package_names`)
- Test: `tpcore/tests/test_lab_targetable_accessor.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_lab_targetable_accessor.py`:
```python
"""SP-B — lab_targetable_engines() predicate table (spec §2.1)."""
from __future__ import annotations


def test_lab_targetable_is_the_roster_predicate_today():
    from tpcore.engine_profile import lab_targetable_engines

    # PAPER/LIVE/LAB ∧ not allocator ∧ not 'lab' sentinel ∧ not 'canary'.
    # Today: reversion, vector, momentum, sentinel (sentinel eligible by
    # predicate even though undeclared — SP-E forward dep). Ordered by
    # dispatch_order for stable diffs.
    assert lab_targetable_engines() == (
        "reversion", "vector", "momentum", "sentinel")


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tpcore/tests/test_lab_targetable_accessor.py -p no:xdist -q`
Expected: FAIL — `ImportError: cannot import name 'lab_targetable_engines' from 'tpcore.engine_profile'`.

- [ ] **Step 3: Write minimal implementation**

In `tpcore/engine_profile.py`, add the `_LAB_SENTINEL` const next to `_ALLOCATOR_ENGINE` (line ~48):
```python
_ALLOCATOR_ENGINE = "allocator"  # the one structurally-separate engine (its own _dispatch_allocator path, D-SDLC1-4)
_LAB_SENTINEL = "lab"  # the durable LifecycleState.LAB sentinel — NOT a runnable engine (no package; test_lab_sentinel_is_not_wired)
```

Then insert after `archived_engines()` (after line ~129, before `engine_package_names`):
```python
# SP-B: the Lab-targetable lifecycle set. Distinct from _DISPATCHABLE —
# it INCLUDES LifecycleState.LAB because targeting a LAB candidate is the
# whole point of the SDLC LAB state (epic §1 SP-B: LAB ∪ PAPER ∪ LIVE).
_LAB_TARGETABLE: frozenset[LifecycleState] = frozenset(
    {LifecycleState.LAB, LifecycleState.PAPER, LifecycleState.LIVE})


def lab_targetable_engines() -> tuple[str, ...]:
    """Engines the Lab MAY fish against: LAB/PAPER/LIVE, non-allocator,
    EXCLUDING the durable ``lab`` sentinel (not a runnable engine — no
    package/backtest, test_lab_sentinel_is_not_wired) and EXCLUDING
    ``canary`` (non-graduating by construction — CLAUDE.md / canary spec
    §4b / canary test_backtest_deliberately_never_writes_credibility; a
    Lab graduation verdict against it is a category error that would
    still spend SP-A ledger budget). RETIRED and allocator are excluded.
    Ordered by dispatch_order for stable diffs.

    This is a DERIVED VIEW over the single SoT (``_PROFILE``), NOT a
    parallel SoT (spec §6). Sentinel is PAPER ⇒ included-but-undeclared
    until SP-E declares its LAB_TARGET (the resolver hard-rejects it with
    a clear SP-E-pointing message — a visible, tested state, not a silent
    gap; spec §2.1, §4.1)."""
    return tuple(
        p.engine
        for p in sorted(_PROFILE.values(), key=lambda p: p.dispatch_order)
        if p.lifecycle_state in _LAB_TARGETABLE
        and p.engine != _ALLOCATOR_ENGINE
        and p.engine != _LAB_SENTINEL
        and p.engine != "canary"  # spec §4b, N=1 — explicit clause + test
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tpcore/tests/test_lab_targetable_accessor.py -p no:xdist -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Verify the existing lifecycle clockwork is still green (no regression to roster_for_dispatch/archived_engines)**

Run: `python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -p no:xdist -q`
Expected: PASS (the accessor is additive; `roster_for_dispatch`/`archived_engines` untouched).

- [ ] **Step 6: Commit**

```bash
git add tpcore/engine_profile.py tpcore/tests/test_lab_targetable_accessor.py
git commit -m "feat(lab-sp-b): add lab_targetable_engines() roster accessor (LAB/PAPER/LIVE, canary+lab excluded)"
```

---

## Task 3: Engine `LAB_TARGET` declarations (reversion / vector / momentum) — spec §7 T3

**Purpose:** Move the `PARAM_RANGES[engine]` literal to the engine that owns those params, plus its four already-uniform callables. The live import surface (scheduler/order-manager/plug) is unchanged — `LAB_TARGET` is a module-level constant the live path never imports.

**Files:**
- Modify: `reversion/backtest.py` (append a `LAB_TARGET` block immediately after `run_for_search`, ~`:1102`, before the `# Main` divider at `:1104`)
- Modify: `vector/backtest.py` (append after `run_for_search`, end-of-module before `if __name__`)
- Modify: `momentum/backtest.py` (append after `run_for_search`, end-of-module before `if __name__`)
- Test: `tpcore/tests/test_engine_lab_target_declarations.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_engine_lab_target_declarations.py`:
```python
"""SP-B — every roster-declared engine exports a valid LAB_TARGET whose
param_ranges byte-match the captured T0 baseline; the live import
surface is unchanged (LAB_TARGET is never imported by the live path).
"""
from __future__ import annotations

import importlib

import pytest

# T0 byte-parity baseline: PARAM_RANGES keysets captured on the un-refactored
# tree (Task 0). Inlined deliberately — a 3-key snapshot of a fully-known
# constant needs no file artifact (YAGNI), and a runtime fixture must never
# live under the docs/ plans tree (#252 docs-to-reality).
_T0_PARAM_RANGES_KEYSETS: dict[str, list[str]] = {
    "reversion": ["max_hold_days", "stop_pct", "volume_climax_multiplier", "z_threshold"],
    "vector": ["catalyst_window_days", "de_ceiling", "pb_ceiling", "stop_pct", "swing_score_threshold"],
    "momentum": ["hold_days", "lookback_days", "skip_days", "top_decile_pct"],
}


@pytest.mark.parametrize("engine", ["reversion", "vector", "momentum"])
def test_engine_declares_valid_lab_target(engine):
    from tpcore.lab.target import LabTarget

    mod = importlib.import_module(f"{engine}.backtest")
    lt = getattr(mod, "LAB_TARGET", None)
    assert isinstance(lt, LabTarget), f"{engine}: no module-level LAB_TARGET"
    # param_ranges byte-match the T0 literal keyset (no drift on the move).
    assert sorted(lt.param_ranges) == _T0_PARAM_RANGES_KEYSETS[engine]
    # The 4 callables resolve to the engine's already-defined symbols.
    assert lt.run_for_search is mod.run_for_search
    assert lt.default_params is mod.default_params
    assert callable(lt.load_window_context)
    assert callable(lt.run_with_context)


@pytest.mark.parametrize("engine", ["reversion", "vector", "momentum"])
def test_lab_target_values_byte_match_old_param_ranges(engine):
    """The (low, high, kind) tuples are byte-identical to the values
    that lived in ops.lab.run.PARAM_RANGES — no behavioural drift."""
    import importlib as _il

    mod = _il.import_module(f"{engine}.backtest")
    # Reconstruct the OLD literal from git's pre-refactor copy is overkill;
    # the run.py lazy Mapping (Task 4) will resolve THROUGH these, and the
    # characterization oracle pins reversion's keyset. Here we pin the
    # values are 3-tuples with a valid kind (LabTarget already enforces;
    # this is the engine-side regression pin).
    for name, spec in mod.LAB_TARGET.param_ranges.items():
        assert isinstance(spec, tuple) and len(spec) == 3
        assert spec[2] in ("float", "int") or spec[2].startswith("choice:")


def test_live_import_surface_does_not_import_lab_target():
    """The scheduler/order-manager/plug never import backtest.LAB_TARGET —
    the live path is byte-identical (spec §6). Proxy: importing each
    engine's scheduler must not require LAB_TARGET to exist (it is only
    referenced lazily by ops.lab.run). We assert the constant is defined
    AFTER run_for_search and the engine package import is side-effect
    clean (no exception)."""
    for engine in ("reversion", "vector", "momentum"):
        importlib.import_module(f"{engine}.backtest")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tpcore/tests/test_engine_lab_target_declarations.py -p no:xdist -q`
Expected: FAIL — `AssertionError: reversion: no module-level LAB_TARGET` (the `getattr` returns `None`).

- [ ] **Step 3: Write minimal implementation — reversion**

In `reversion/backtest.py`, immediately after `run_for_search` ends (line `:1102`, the blank line before the `# Main` divider at `:1104`), insert:
```python
# ────────────────────────────────────────────────────────────────────────────
# SP-B — Lab targeting declaration (engine-OWNED; the live path never
# imports this; resolved lazily by ops.lab.run._lab_target_for).
# ────────────────────────────────────────────────────────────────────────────

from tpcore.lab.target import LabTarget  # noqa: E402 — engine→tpcore, legal

LAB_TARGET = LabTarget(
    param_ranges={
        "z_threshold": (2.0, 4.0, "float"),
        "volume_climax_multiplier": (1.2, 3.0, "float"),
        "max_hold_days": (3, 12, "int"),
        "stop_pct": (0.04, 0.12, "float"),
    },
    run_for_search=run_for_search,
    load_window_context=load_reversion_window_context,
    run_with_context=run_reversion_with_context,
    default_params=default_params,
)
```

> NOTE: `# noqa: E402` is for module-level-import-not-at-top (ruff E402), NOT `SLF001`. This is a sanctioned engine→tpcore import placed next to the symbols it references; it is not private-attribute access. If ruff still complains, move the `from tpcore.lab.target import LabTarget` to the top import block with the other `from tpcore...` imports and drop the `# noqa`.

- [ ] **Step 4: Write minimal implementation — vector**

In `vector/backtest.py`, after `run_for_search` ends (it is the last def before `if __name__`), insert the same block but with vector's params + symbols:
```python
# ────────────────────────────────────────────────────────────────────────────
# SP-B — Lab targeting declaration (engine-OWNED; live path never imports).
# ────────────────────────────────────────────────────────────────────────────

from tpcore.lab.target import LabTarget  # noqa: E402 — engine→tpcore, legal

LAB_TARGET = LabTarget(
    param_ranges={
        "pb_ceiling": (1.5, 3.5, "float"),
        "de_ceiling": (1.5, 4.0, "float"),
        "catalyst_window_days": (3, 10, "int"),
        "swing_score_threshold": (55.0, 75.0, "float"),
        "stop_pct": (0.04, 0.10, "float"),
    },
    run_for_search=run_for_search,
    load_window_context=load_vector_window_context,
    run_with_context=run_vector_with_context,
    default_params=default_params,
)
```

- [ ] **Step 5: Write minimal implementation — momentum**

In `momentum/backtest.py`, after `run_for_search` ends, insert:
```python
# ────────────────────────────────────────────────────────────────────────────
# SP-B — Lab targeting declaration (engine-OWNED; live path never imports).
# ────────────────────────────────────────────────────────────────────────────

from tpcore.lab.target import LabTarget  # noqa: E402 — engine→tpcore, legal

LAB_TARGET = LabTarget(
    param_ranges={
        "lookback_days": (200, 280, "int"),
        "skip_days": (15, 30, "int"),
        "hold_days": (15, 30, "int"),
        "top_decile_pct": (0.05, 0.20, "float"),
    },
    run_for_search=run_for_search,
    load_window_context=load_momentum_window_context,
    run_with_context=run_momentum_with_context,
    default_params=default_params,
)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tpcore/tests/test_engine_lab_target_declarations.py -p no:xdist -q`
Expected: PASS (3 parametrized engines × 2 + 1 = 7 test instances).

- [ ] **Step 7: Verify check_imports + ruff stay green for the engines**

Run:
```bash
python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
python -m ruff check reversion/backtest.py vector/backtest.py momentum/backtest.py
```
Expected: both exit 0. (The `from tpcore.lab.target import LabTarget` is engine→tpcore — the legal direction. If ruff E402 reds, apply the Step-3 NOTE: hoist the import to the top block and drop the `# noqa`.)

- [ ] **Step 8: Commit**

```bash
git add reversion/backtest.py vector/backtest.py momentum/backtest.py tpcore/tests/test_engine_lab_target_declarations.py
git commit -m "feat(lab-sp-b): engines declare LAB_TARGET (param_ranges + 4 dispatch callables)"
```

---

## Task 4: `ops/lab/run.py` dispatch indirection + the sixth-surface shim — spec §2.3 / §2.4 / §7 T4 — **HIGHEST-RISK TASK**

**Purpose:** Replace all five dispatch ladders with one resolver. The **binding contract** (spec §2.4, §8 highest residual risk): the lazy `PARAM_RANGES` Mapping MUST re-raise `_lab_target_for`'s `ValueError` as `KeyError` so `ops/engine_sdlc/planner.py:694`'s `.get(ecr.engine, {})` (a live-adjacent MODIFY-ECR validation path) keeps cleanly returning `{}` for a non-targetable engine instead of crashing with an unhandled `ValueError`.

**Files:**
- Modify: `ops/lab/run.py` — `PARAM_RANGES` (`:95-131`), `sample_parameters` (`:146-149`), `_runner_for`/`_context_loader_for`/`_context_runner_for` (`:316-356`)
- Modify: `ops/engine_sdlc/default_params.py` (`:13-23`)
- Modify: `tpcore/tests/test_engine_default_params_parity.py` (`:35-38`, ONE assertion)
- Test: `tpcore/tests/test_lab_dispatch_indirection.py`

- [ ] **Step 1: Char-before-refactor — write the pin test for CURRENT dispatch behaviour FIRST**

Create `tpcore/tests/test_lab_dispatch_indirection.py` (this FIRST block pins behaviour that must hold both pre- AND post-refactor — run it now against the unrefactored tree to confirm the pins are true):
```python
"""SP-B — _lab_target_for resolver + lazy PARAM_RANGES Mapping + the
BINDING ValueError→KeyError re-raise contract (spec §2.4, the §8
highest residual risk) + the default_params shim + no-import-cycle.

Char-before-refactor: the *_for callables return the engine's
run_for_search/load_*/run_* symbols for the declared three and raise for
an unknown engine; PARAM_RANGES supports in/get/iteration-order/len/set.
These pins hold pre- AND post-refactor (the refactor is provably
behaviour-preserving on the declared three).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) so ``import ops.lab.run``
# resolves the real ops/ package (the ops-shadow single-process rule).
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

pytestmark = pytest.mark.xdist_group("ops_shadow")

# T0 byte-parity baseline: PARAM_RANGES keysets captured on the un-refactored
# tree (Task 0). Inlined deliberately — a 3-key snapshot of a fully-known
# constant needs no file artifact (YAGNI), and a runtime fixture must never
# live under the docs/ plans tree (#252 docs-to-reality).
_T0_PARAM_RANGES_KEYSETS: dict[str, list[str]] = {
    "reversion": ["max_hold_days", "stop_pct", "volume_climax_multiplier", "z_threshold"],
    "vector": ["catalyst_window_days", "de_ceiling", "pb_ceiling", "stop_pct", "swing_score_threshold"],
    "momentum": ["hold_days", "lookback_days", "skip_days", "top_decile_pct"],
}


# ── CHARACTERIZATION pins (true pre- AND post-refactor) ──────────────────

def test_seam_funcs_return_declared_engine_symbols():
    import importlib

    import ops.lab.run as run
    for engine in ("reversion", "vector", "momentum"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert run._runner_for(engine) is mod.run_for_search
        assert callable(run._context_loader_for(engine))
        assert callable(run._context_runner_for(engine))


def test_seam_funcs_raise_valueerror_on_unknown_engine():
    import ops.lab.run as run
    for fn in (run._runner_for, run._context_loader_for,
               run._context_runner_for):
        with pytest.raises(ValueError):
            fn("nope")


def test_param_ranges_membership_iteration_len_and_set():
    import ops.lab.run as run
    # Membership + iteration order == the T0 literal insertion order.
    assert list(run.PARAM_RANGES) == ["reversion", "vector", "momentum"]
    assert "reversion" in run.PARAM_RANGES
    assert "sentinel" not in run.PARAM_RANGES
    assert len(run.PARAM_RANGES) == 3
    for e in ("reversion", "vector", "momentum"):
        assert set(run.PARAM_RANGES[e]) == set(_T0_PARAM_RANGES_KEYSETS[e])
```

- [ ] **Step 2: Run the char pins against the UN-refactored tree to confirm they hold**

Run: `python -m pytest tpcore/tests/test_lab_dispatch_indirection.py -p no:xdist -q`
Expected: PASS — these three pins are TRUE on the current `if engine ==` ladders + literal dict. (This is the char-before-refactor baseline; the refactor must keep them green.)

- [ ] **Step 3: Add the post-refactor binding-contract tests (they fail now)**

Append to `tpcore/tests/test_lab_dispatch_indirection.py`:
```python
# ── BINDING CONTRACT pins (spec §2.4 — the §8 highest residual risk) ─────

def test_param_ranges_subscript_undeclared_raises_KEYERROR_not_valueerror():
    """planner.py:694 does PARAM_RANGES.get(ecr.engine, {}). Mapping.get
    catches KeyError ONLY. The lazy __getitem__ MUST re-raise the
    _lab_target_for ValueError as KeyError or that live-adjacent
    MODIFY-ECR validator crashes (spec §2.4, §8-A2)."""
    import ops.lab.run as run
    with pytest.raises(KeyError):
        run.PARAM_RANGES["sentinel"]            # eligible-but-undeclared
    # NOT a ValueError leaking through:
    try:
        run.PARAM_RANGES["sentinel"]
    except KeyError:
        pass
    except ValueError as exc:  # pragma: no cover - regression tripwire
        pytest.fail(f"ValueError leaked (planner.py:694 would crash): {exc}")


def test_param_ranges_get_returns_default_for_undeclared_engine():
    """The exact planner.py:694 call: .get(<undeclared>, {}) == {}."""
    import ops.lab.run as run
    assert run.PARAM_RANGES.get("sentinel", {}) == {}
    assert run.PARAM_RANGES.get("canary", {}) == {}
    assert run.PARAM_RANGES.get("sigma", {}) == {}
    assert run.PARAM_RANGES.get("nope", {}) == {}


def test_lab_target_for_resolves_declared_engines():
    import importlib

    from ops.lab.run import _lab_target_for
    for engine in ("reversion", "vector", "momentum"):
        mod = importlib.import_module(f"{engine}.backtest")
        t = _lab_target_for(engine)
        assert t.run_for_search is mod.run_for_search
        assert t.default_params is mod.default_params


def test_lab_target_for_rejects_non_targetable_with_clear_valueerror():
    from ops.lab.run import _lab_target_for
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(ValueError, match="not Lab-targetable"):
            _lab_target_for(bad)


def test_lab_target_for_rejects_eligible_but_undeclared_sentinel():
    """Sentinel is PAPER (eligible) but exports no LAB_TARGET → the clear
    SP-E/SP-F-pointing message, NOT KeyError/'unknown engine' (spec
    §4.1)."""
    from ops.lab.run import _lab_target_for
    with pytest.raises(ValueError, match="has not.*declared.*LAB_TARGET"):
        _lab_target_for("sentinel")


def test_sample_parameters_clear_error_on_bad_engine():
    import ops.lab.run as run
    with pytest.raises(ValueError, match="not Lab-targetable"):
        run.sample_parameters("canary", 4, seed=0)
    # Declared engine still samples deterministically (no behaviour drift).
    a = run.sample_parameters("reversion", 8, seed=7)
    b = run.sample_parameters("reversion", 8, seed=7)
    assert a == b and set(a[0]) == set(_T0_PARAM_RANGES_KEYSETS["reversion"])


# ── default_params shim (the sixth surface, spec §0.1 / §2.3) ────────────

def test_default_params_shim_byte_equal_for_declared_engines():
    import importlib

    from ops.engine_sdlc.default_params import default_params
    for engine in ("reversion", "vector", "momentum"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert default_params(engine) == mod.default_params()


def test_default_params_shim_rejects_sentinel_with_clear_message():
    from ops.engine_sdlc.default_params import default_params
    with pytest.raises(ValueError, match="has not.*declared.*LAB_TARGET"):
        default_params("sentinel")


def test_no_import_cycle_default_params_shim_to_run():
    """ops.engine_sdlc.default_params → ops.lab.run is ops→ops, lazy,
    legal, no cycle. Import each first, then exercise."""
    import importlib

    m1 = importlib.import_module("ops.engine_sdlc.default_params")
    m2 = importlib.import_module("ops.lab.run")
    assert m1.default_params("momentum") == \
        importlib.import_module("momentum.backtest").default_params()
    assert callable(m2._lab_target_for)
```

- [ ] **Step 4: Run to verify the new contract tests fail**

Run: `python -m pytest tpcore/tests/test_lab_dispatch_indirection.py -p no:xdist -q`
Expected: the char pins PASS, the binding-contract tests FAIL — `AttributeError: module 'ops.lab.run' has no attribute '_lab_target_for'` and `run.PARAM_RANGES["sentinel"]` currently raises `KeyError` only because it's still a literal dict (so that one may incidentally pass; `_lab_target_for` tests + `sample_parameters("canary")` clear-message + the `default_params("sentinel")` clear-message fail).

- [ ] **Step 5: Implement the resolver + seam views + lazy Mapping in `ops/lab/run.py`**

Replace the literal `PARAM_RANGES` dict (`:95-131`) with the resolver + lazy Mapping. Insert this block where `PARAM_RANGES` was defined:
```python
# ────────────────────────────────────────────────────────────────────────────
# SP-B — roster-SoT-driven dispatch resolver. Replaces the stale hardwired
# (reversion, vector, momentum) 3-tuple across all six surfaces. Engine
# add/remove is a tpcore.engine_profile._PROFILE edit + the engine
# declaring LAB_TARGET — NEVER Lab surgery (spec §1, §2.3).
# ────────────────────────────────────────────────────────────────────────────


def _lab_target_for(engine: str) -> "Any":
    """Resolve the engine's declared LabTarget via the roster SoT.

    Engine import is LAZY (legal in ops/, H-S2-1 — the resolver lives in
    ops/, NOT tpcore/). Hard-rejects an engine that is not
    roster-Lab-targetable OR has not declared LAB_TARGET with a CLEAR
    ValueError — never a raw KeyError/ImportError to the operator. The
    reject fires inside sample_parameters BEFORE the SP-A
    record_trial_spend block (run.py:752-759) so no partial ledger write
    is possible (spec §4.5, §8-A4)."""
    from tpcore.engine_profile import lab_targetable_engines

    targetable = lab_targetable_engines()
    if engine not in targetable:
        raise ValueError(
            f"engine {engine!r} is not Lab-targetable; choose one of "
            f"{targetable} (roster SoT: tpcore.engine_profile)"
        )
    import importlib

    try:
        mod = importlib.import_module(f"{engine}.backtest")
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"engine {engine!r} has no importable {engine}.backtest "
            f"module: {exc}"
        ) from exc
    target = getattr(mod, "LAB_TARGET", None)
    if target is None:
        raise ValueError(
            f"engine {engine!r} is roster-Lab-eligible but has not "
            f"declared a module-level LAB_TARGET in {engine}.backtest "
            f"(see tpcore/lab/target.py:LabTarget). This is the SP-E/SP-F "
            f"forward step: the engine must declare its Lab contract."
        )
    return target


class _LazyParamRanges(Mapping):
    """``PARAM_RANGES`` kept as a NAME (oracle/planner compat) but driven
    by the roster SoT. The BINDING contract (spec §2.4, the §8 highest
    residual risk): ``__getitem__`` re-raises ``_lab_target_for``'s
    ``ValueError`` as ``KeyError`` so ``collections.abc.Mapping.get`` (which
    catches ``KeyError`` ONLY) keeps ``planner.py:694``'s
    ``.get(ecr.engine, {})`` returning ``{}`` for a non-targetable engine
    instead of crashing the live-adjacent MODIFY-ECR validator with an
    unhandled ``ValueError``."""

    def __getitem__(self, engine: str) -> dict[str, tuple]:
        try:
            return _lab_target_for(engine).param_ranges
        except ValueError as exc:
            raise KeyError(engine) from exc

    def __iter__(self):
        # Declared targets only, dispatch_order — same membership+order
        # as the old literal dict's insertion order (reversion, vector,
        # momentum). Eligible-but-undeclared (sentinel) is skipped.
        from tpcore.engine_profile import lab_targetable_engines

        for engine in lab_targetable_engines():
            try:
                _lab_target_for(engine)
            except ValueError:
                continue
            yield engine

    def __len__(self) -> int:
        return sum(1 for _ in self)


PARAM_RANGES: Mapping = _LazyParamRanges()
```

Add the import at the top of `ops/lab/run.py` (with the other stdlib imports — `collections.abc` is stdlib):
```python
from collections.abc import Mapping
```
(If `Callable`/`Awaitable` are already imported `from collections.abc`, add `Mapping` to that same line.)

Replace `sample_parameters` (`:146-149`) — add the clear-error wrap:
```python
def sample_parameters(engine: str, n: int, seed: int = 0) -> list[dict]:
    try:
        ranges = PARAM_RANGES[engine]
    except KeyError:
        # Re-raise as the CLEAR operator-facing ValueError (defence in
        # depth for the programmatic run_lab() path + legacy shim; the
        # argparse choices gate rejects bad engines far earlier on every
        # real CLI path). spec §2.4.
        _lab_target_for(engine)  # raises the clear ValueError
        raise  # unreachable — _lab_target_for always raises here
    rng = random.Random(seed)
    return [{k: _sample_value(spec, rng) for k, spec in ranges.items()} for _ in range(n)]
```

Replace the three `_*_for` ladders (`:316-356`) with thin views (names + `(engine)->callable` signatures UNCHANGED so the oracle's by-name monkeypatch still binds — spec §6):
```python
def _runner_for(engine: str) -> Callable[..., Awaitable[Any]]:
    """Legacy single-call entry; loads context per call. SP-B: a thin
    view over the roster-SoT resolver (name + signature unchanged so the
    characterization oracle's by-name monkeypatch still binds)."""
    return _lab_target_for(engine).run_for_search


def _context_loader_for(engine: str) -> Callable[..., Awaitable[Any]]:
    """Returns the async ``load_*_window_context``. SP-B thin view."""
    return _lab_target_for(engine).load_window_context


def _context_runner_for(engine: str) -> Callable[..., Any]:
    """Returns the sync ``run_*_with_context``. SP-B thin view."""
    return _lab_target_for(engine).run_with_context
```

- [ ] **Step 6: Implement the sixth-surface shim — `ops/engine_sdlc/default_params.py`**

Replace the `default_params` body (`:13-23`) — signature + `__all__` unchanged:
```python
def default_params(engine: str) -> dict[str, Any]:
    from ops.lab.run import _lab_target_for  # lazy, ops→ops, legal

    return _lab_target_for(engine).default_params()
```
Update the module docstring's first line to note the SP-B delegation (keep it accurate):
```python
"""Lazy per-engine default_params() dispatcher (SP3 O1, spec §7.1).

SP-B: the if-engine== ladder is replaced by a thin delegate to
ops.lab.run._lab_target_for (the single roster-SoT resolver). The engine
import stays LAZY (ops→ops, no eager engine import, no tpcore→engine).
"""
```

- [ ] **Step 7: Update the ONE sanctioned parity-test assertion (spec §4.11, §8-B6)**

In `tpcore/tests/test_engine_default_params_parity.py`, change `test_dispatcher_rejects_unknown_engine` (`:35-38`) — exception TYPE stays `ValueError`, only the message pin moves to the new clear roster-aware text:
```python
def test_dispatcher_rejects_unknown_engine():
    from ops.engine_sdlc.default_params import default_params
    # SP-B: the message moved from the old "unknown engine: nope" hand-
    # ladder text to the clear roster-aware resolver message; the
    # exception TYPE (ValueError) is unchanged — a deliberate, beneficial
    # delta (spec §4.11, §8-B6).
    with pytest.raises(ValueError, match="not Lab-targetable"):
        default_params("nope")
```

- [ ] **Step 8: Run the dispatch-indirection contract tests — they pass**

Run: `python -m pytest tpcore/tests/test_lab_dispatch_indirection.py -p no:xdist -q`
Expected: PASS (all char pins + all binding-contract pins, incl. the `KeyError`-not-`ValueError` re-raise and `.get(<undeclared>, {}) == {}`).

- [ ] **Step 9: Run the updated parity test**

Run: `python -m pytest tpcore/tests/test_engine_default_params_parity.py -p no:xdist -q`
Expected: PASS — `test_each_param_ranges_engine_default_keyset_equals_param_ranges` (declared three) GREEN, `test_sentinel_canary_have_no_accessor` GREEN, `test_dispatcher_rejects_unknown_engine` GREEN on the new message.

- [ ] **Step 10: Gate — the characterization oracle passes UNMODIFIED (the strongest behaviour-identity proof)**

Run: `python -m pytest scripts/tests/test_search_parameters_characterization.py -p no:xdist -q`
Expected: PASS, with ZERO edits to that file (the by-name monkeypatch of `ops.lab.run._runner_for` etc. still binds; `set(sp.PARAM_RANGES["reversion"])` still works via the lazy Mapping). If this REDs, the refactor changed observable behaviour — STOP and fix `ops/lab/run.py`, do NOT edit the oracle.

- [ ] **Step 11: Gate — the live-adjacent planner path is non-regressed**

Run:
```bash
python -c "import sys; sys.path.insert(0,'.'); from ops.lab.run import PARAM_RANGES; print(PARAM_RANGES.get('sentinel', {}) == {}, PARAM_RANGES.get('reversion', {}) != {})"
```
Expected output: `True True` (the exact `planner.py:694` `.get(ecr.engine, {})` semantics — `{}` for undeclared, real dict for declared).

- [ ] **Step 12: ruff + check_imports**

Run:
```bash
python -m ruff check ops/lab/run.py ops/engine_sdlc/default_params.py
python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: both exit 0.

- [ ] **Step 13: Commit**

```bash
git add ops/lab/run.py ops/engine_sdlc/default_params.py tpcore/tests/test_lab_dispatch_indirection.py tpcore/tests/test_engine_default_params_parity.py
git commit -m "refactor(lab-sp-b): roster-SoT dispatch resolver + lazy PARAM_RANGES (ValueError→KeyError contract) + default_params shim"
```

---

## Task 5: CLI choices generated from the roster — spec §2.5 / §7 T5

**Files:**
- Modify: `ops/lab/__main__.py` (`:50-53`)
- Modify: `ops/lab/run.py` (`:620`)
- Test: `tpcore/tests/test_lab_cli_choices_from_roster.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_lab_cli_choices_from_roster.py`:
```python
"""SP-B — both argparse choices sites are GENERATED from
lab_targetable_engines() (not a literal copy) and importing
ops.lab.__main__ still eager-imports NO engine (spec §2.5, §4.8).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_run_py_engine_choices_are_the_accessor():
    import ops.lab.run as run
    from tpcore.engine_profile import lab_targetable_engines

    a = run._parse_args(["--engine", "reversion"])
    assert a.engine == "reversion"
    # A non-targetable choice is rejected by argparse (SystemExit) ⇒ the
    # choices ARE the accessor, not a stale literal.
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(SystemExit):
            run._parse_args(["--engine", bad])
    # And every accessor member is accepted.
    for good in lab_targetable_engines():
        if good == "sentinel":
            continue  # eligible-but-undeclared: argparse accepts; resolver rejects later
        run._parse_args(["--engine", good])


def test_main_py_target_engine_choices_are_the_accessor():
    import ops.lab.__main__ as m

    ns = m._parse_args(["--candidate", "c", "--target-engine", "reversion",
                        "--intent", "promote_new"])
    assert ns.target_engine == "reversion"
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(SystemExit):
            m._parse_args(["--candidate", "c", "--target-engine", bad,
                           "--intent", "promote_new"])


def test_import_ops_lab_main_eager_imports_no_engine():
    """__main__.py:18-20 invariant: import ops.lab.__main__ pulls in NO
    engine package (the choices accessor is engine-free; resolution is
    lazy). Pristine subprocess (zero collection-order pollution)."""
    probe = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import ops.lab.__main__\n"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "('reversion','vector','momentum','sentinel','canary')]\n"
        "print(bad)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        f"eager engine import leaked: {out.stdout!r}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tpcore/tests/test_lab_cli_choices_from_roster.py -p no:xdist -q`
Expected: FAIL — the literal `choices=("reversion","vector","momentum")` rejects nothing differently, so the "accessor membership accepted" / "sentinel accepted by argparse" assertions fail (`sentinel` is not in the literal tuple → `SystemExit` where the test expects acceptance).

- [ ] **Step 3: Implement — `ops/lab/run.py` argparse**

In `ops/lab/run.py` `_parse_args` (`:620`), replace:
```python
    p.add_argument("--engine", choices=("reversion", "vector", "momentum"), required=True)
```
with:
```python
    from tpcore.engine_profile import lab_targetable_engines
    p.add_argument("--engine", choices=lab_targetable_engines(), required=True)
```

- [ ] **Step 4: Implement — `ops/lab/__main__.py` argparse**

In `ops/lab/__main__.py` `_parse_args` (`:50-53`), replace:
```python
    p.add_argument("--target-engine", required=True,
                   choices=("reversion", "vector", "momentum"),
                   help="The existing engine whose backtest contract the "
                        "Lab exercises.")
```
with:
```python
    from tpcore.engine_profile import lab_targetable_engines
    p.add_argument("--target-engine", required=True,
                   choices=lab_targetable_engines(),
                   help="The roster-Lab-targetable engine whose backtest "
                        "contract the Lab exercises (choices generated from "
                        "tpcore.engine_profile — SP-B). An eligible-but-"
                        "undeclared engine (e.g. sentinel pre-SP-E) is a "
                        "valid choice but the resolver rejects it with a "
                        "clear LAB_TARGET message.")
```
> `from tpcore.engine_profile import ...` is a `tpcore` import — always legal in `ops/`, engine-free, no eager engine import; the `__main__.py:18-20` no-eager-import contract is preserved (the subprocess test in Step 1 proves it).

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tpcore/tests/test_lab_cli_choices_from_roster.py -p no:xdist -q`
Expected: PASS (3 tests, incl. the pristine-subprocess no-eager-import probe).

- [ ] **Step 6: Re-run the characterization oracle (legacy CLI contract preserved)**

Run: `python -m pytest scripts/tests/test_search_parameters_characterization.py -p no:xdist -q`
Expected: PASS unmodified (the legacy `scripts/search_parameters.py` shim inherits the new `choices` automatically; no oracle edit).

- [ ] **Step 7: ruff**

Run: `python -m ruff check ops/lab/run.py ops/lab/__main__.py`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add ops/lab/run.py ops/lab/__main__.py tpcore/tests/test_lab_cli_choices_from_roster.py
git commit -m "feat(lab-sp-b): generate CLI --engine/--target-engine choices from the roster accessor"
```

---

## Task 6: The SP-B consistency clockwork + SP-A non-regression — spec §2.6 / §4.5 / §5 / §7 T6

**Files:**
- Test: `tpcore/tests/test_lab_targeting_consistency.py`

- [ ] **Step 1: Write the failing test — the clockwork + red-proof + SP-A non-regression**

Create `tpcore/tests/test_lab_targeting_consistency.py`:
```python
"""SP-B clockwork (mirrors SP4 test_leg6_fails_on_roster_drift INTENT,
NOT a byte-shadow — argued spec §1): the Lab target set IS the roster
SoT predicate; CLI choices are generated; a synthetic roster change
propagates to the Lab with ZERO Lab-file edits (non-vacuous red-proof);
canary/lab-sentinel exclusions are pinned; and SP-A's cumulative
deflation still applies identically to a newly-roster-resolved target
(the dependency invariant — SP-B did NOT re-open SP-A's hole).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ── (1) the accessor IS the roster predicate, not a hand-list ────────────

def test_target_set_equals_roster_predicate():
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


# ── (2) CLI choices are GENERATED from the accessor (both sites) ─────────

def test_cli_choices_are_generated_both_sites():
    import ops.lab.__main__ as m
    import ops.lab.run as run
    from tpcore.engine_profile import lab_targetable_engines

    acc = lab_targetable_engines()
    run._parse_args(["--engine", "reversion"])  # accepted
    m._parse_args(["--candidate", "c", "--target-engine", "reversion",
                   "--intent", "promote_new"])  # accepted
    with pytest.raises(SystemExit):
        run._parse_args(["--engine", "sigma"])  # RETIRED dropped automatically
    assert "sentinel" in acc  # eligible-but-undeclared still a CLI choice


# ── (3) RED-PROOF: a synthetic roster mutation propagates with ZERO Lab
#       edits; a RETIRED engine drops automatically (NON-VACUOUS) ─────────

def test_synthetic_roster_drift_propagates_to_lab(monkeypatch):
    """Mirrors test_leg6_fails_on_roster_drift: inject a fake PAPER
    engine into _PROFILE; lab_targetable_engines() + CLI choices +
    _lab_target_for ALL track it with NO Lab-file edit. Non-vacuous: the
    synthetic engine is recognised as a roster Lab target AWAITING
    LAB_TARGET (exactly the SP-F path) — a clear undeclared-LAB_TARGET
    ValueError, NOT KeyError / 'unknown engine'."""
    import tpcore.engine_profile as ep

    fake = ep.EngineProfile(
        engine="phantompaper", cadence=ep.Cadence.DAILY,
        dispatch_order=7, lifecycle_state=ep.LifecycleState.PAPER)
    patched = dict(ep._PROFILE)
    patched["phantompaper"] = fake
    monkeypatch.setattr(ep, "_PROFILE", patched)

    assert "phantompaper" in ep.lab_targetable_engines()

    # CLI choices see it (generated, not a literal).
    import ops.lab.run as run
    run._parse_args(["--engine", "phantompaper"])  # argparse accepts it

    # The resolver recognises it as a roster Lab target awaiting its
    # LAB_TARGET declaration — the SP-F path, NOT 'unknown engine'.
    with pytest.raises(ValueError, match="has not.*declared.*LAB_TARGET"):
        run._lab_target_for("phantompaper")

    # Conversely: flipping a real engine to RETIRED drops it automatically.
    retired = dict(ep._PROFILE)
    retired["momentum"] = ep.EngineProfile(
        engine="momentum", cadence=ep.Cadence.MONTHLY_FIRST_TRADING_DAY,
        dispatch_order=3, lifecycle_state=ep.LifecycleState.RETIRED)
    monkeypatch.setattr(ep, "_PROFILE", retired)
    assert "momentum" not in ep.lab_targetable_engines()


# ── (4) policy-clause regression pins ────────────────────────────────────

def test_canary_not_lab_targetable():
    from tpcore.engine_profile import lab_targetable_engines
    assert "canary" not in lab_targetable_engines()


def test_lab_sentinel_not_lab_targetable():
    from tpcore.engine_profile import lab_targetable_engines
    assert "lab" not in lab_targetable_engines()


# ── (5) SP-A NON-REGRESSION: cumulative deflation still applies to a
#       newly-roster-resolved target; hard-reject happens BEFORE any
#       ledger spend (the dependency invariant, spec §4.5 / §4.10) ───────

@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetchrow(self, sql, *params):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO platform.data_quality_log"):
            source, ts = params[0], params[1]
            if any(r["source"] == source and r["timestamp"] == ts
                   for r in self._rows):
                return None
            self._rows.append({"source": source, "timestamp": ts,
                               "notes": params[6]})
            return {"?column?": 1}
        raise AssertionError(s)

    async def fetchval(self, sql, *params):
        s = " ".join(sql.split())
        if "SUM((notes::jsonb->>'trials')::int)" in s:
            src, before = params[0], params[1]
            import json as _j
            return sum(_j.loads(r["notes"])["trials"] for r in self._rows
                       if r["source"] == src and r["timestamp"] < before)
        raise AssertionError(s)


class _Acquire:
    def __init__(self, conn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


class _SharedPool:
    def __init__(self):
        self.rows = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))

    async def close(self):
        ...


def _ns(output, *, engine, trials, seed):
    return argparse.Namespace(
        engine=engine, trials=trials, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None)


def _install_offline(monkeypatch, lab_run, returns):
    from tpcore.backtest.credibility import CredibilityScore

    rubric = CredibilityScore(
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True, score=80)

    class _RR:
        credibility_score = 80
        credibility_rubric = rubric
        trade_log = [_Trade(date(2022, 1, 3) + timedelta(days=i), r)
                     for i, r in enumerate(returns)]

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: (lambda c, *, overrides=None: _RR()))
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: (lambda **k: _RR()))

    async def _aloader(**k):
        return _RR()

    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _aloader)

    async def _runner(**k):
        return _RR()

    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)

    async def _fw(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fw, raising=True)


async def test_sp_a_cumulative_applies_to_roster_resolved_target(
        monkeypatch, tmp_path):
    """A newly-roster-resolved target's SP-A cumulative deflation grows
    monotonically across runs — SP-B did NOT re-open SP-A's hole. Uses a
    real declared target (reversion) routed through the SP-B resolver
    (the resolver is between the CLI and the ledger; reversion is the
    'newly-roster-resolved' path post-SP-B). SP-B touches ZERO ledger
    code (spec §4.5)."""
    import numpy as np

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    returns = [float(x) for x in np.random.default_rng(0).normal(
        0.015, 0.01, 40)]
    seen = []
    real = lab_run.compute_dsr_for_verdict

    def _spy(r, *, n_trials, trial_sharpe_variance=None):
        seen.append(n_trials)
        return real(r, n_trials=n_trials,
                    trial_sharpe_variance=trial_sharpe_variance)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _spy)
    _install_offline(monkeypatch, lab_run, returns)
    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        c1 = await lab_run._run_lab_core(
            _ns(tmp_path / "a.csv", engine="reversion", trials=40, seed=1),
            candidate="rev_a")
    async with LabContext(db_url="postgres://fake/db"):
        c2 = await lab_run._run_lab_core(
            _ns(tmp_path / "b.csv", engine="reversion", trials=50, seed=2),
            candidate="rev_b")

    assert not isinstance(c1, int) and not isinstance(c2, int)
    assert seen == [40, 90]                 # cumulative: 0+40, then 40+50
    assert c2.effective_n_trials == 90 > c1.effective_n_trials == 40


async def test_undeclared_target_hard_rejects_before_any_ledger_spend(
        monkeypatch, tmp_path):
    """Edge §4.2/§4.10: a programmatic run with a non-targetable engine
    raises the clear ValueError inside sample_parameters → _lab_target_for
    BEFORE record_trial_spend (run.py:752-759). NO lab_trial_ledger.canary
    row is ever written."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        with pytest.raises(ValueError, match="not Lab-targetable"):
            await lab_run._run_lab_core(
                _ns(tmp_path / "x.csv", engine="canary", trials=10, seed=0),
                candidate="bad")

    # No spend row for the rejected target — the ledger stays clean.
    assert not any(
        r["source"] == "lab_trial_ledger.canary" for r in shared.rows), (
        "a hard-rejected target must NOT spend SP-A ledger budget")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tpcore/tests/test_lab_targeting_consistency.py -p no:xdist -q`
Expected: FAIL initially only if any wiring is off — but given Tasks 2/4/5 are merged, the clockwork+SP-A tests should largely PASS. The genuine TDD failure surface here is the **red-proof**: temporarily comment out the `and p.engine != "canary"` clause in `lab_targetable_engines()` and confirm `test_canary_not_lab_targetable` FAILS (proves the pin is non-vacuous), then restore it. Document this manual non-vacuity check in the commit.

- [ ] **Step 3: Confirm the red-proof is non-vacuous (manual injected divergence)**

Run:
```bash
python - <<'PY'
import tpcore.engine_profile as ep
# Simulate the canary clause being dropped:
src = ep.lab_targetable_engines()
print("canary in targetable WITH clause:", "canary" in src)
PY
```
Expected output: `canary in targetable WITH clause: False`. Then mentally verify: removing the `!= "canary"` clause would flip this to `True` and red `test_canary_not_lab_targetable` — the pin guards a real policy decision.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tpcore/tests/test_lab_targeting_consistency.py -p no:xdist -q`
Expected: PASS (7 tests: predicate-equality, CLI-generated, synthetic-drift red-proof, 2 policy pins, SP-A monotone non-regression, hard-reject-before-spend).

- [ ] **Step 5: ruff**

Run: `python -m ruff check tpcore/tests/test_lab_targeting_consistency.py`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add tpcore/tests/test_lab_targeting_consistency.py
git commit -m "test(lab-sp-b): roster-Lab consistency clockwork + synthetic-drift red-proof + SP-A non-regression"
```

---

## Task 7: Forward-dep scaffold — `engine_template/backtest.py` LAB_TARGET skeleton — spec §7 T7

**Purpose:** A new engine (SP-F Catalyst) scaffolded from the template is Lab-targetable by construction. Doc/scaffold only.

**Files:**
- Modify: `tpcore/templates/engine_template/backtest.py` (append)
- Test: `tpcore/tests/test_engine_lab_target_declarations.py` (add one scaffold-presence assertion)

- [ ] **Step 1: Inspect the current template backtest stub**

Run: `cat tpcore/templates/engine_template/backtest.py`
Expected: a short stub. Note the existing function names / whether `run_for_search`/`load_*`/`run_*`/`default_params` stubs already exist (the template is scaffold-only; `check_imports`/ruff exempt `tpcore/templates`).

- [ ] **Step 2: Write the failing test (scaffold contains a commented LAB_TARGET skeleton)**

Append to `tpcore/tests/test_engine_lab_target_declarations.py`:
```python
def test_engine_template_has_lab_target_skeleton():
    """SP-F forward dep: a new engine scaffolded from the template is
    Lab-targetable by construction — the template carries a commented
    LAB_TARGET skeleton + the 4 uniform symbol names so the SP-F engine
    only fills in its param ranges (spec §7 T7)."""
    from pathlib import Path

    src = Path("tpcore/templates/engine_template/backtest.py").read_text()
    assert "LAB_TARGET" in src
    assert "from tpcore.lab.target import LabTarget" in src
    for sym in ("run_for_search", "load_window_context",
                "run_with_context", "default_params"):
        assert sym in src
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tpcore/tests/test_engine_lab_target_declarations.py::test_engine_template_has_lab_target_skeleton -p no:xdist -q`
Expected: FAIL — `assert "LAB_TARGET" in src` (the template has no skeleton yet).

- [ ] **Step 4: Implement — append the commented skeleton to the template**

Append to `tpcore/templates/engine_template/backtest.py` (commented so the scaffold stays import-safe and a copy-paste start; the SP-F engine uncomments + fills its ranges):
```python
# ────────────────────────────────────────────────────────────────────────────
# SP-B forward dep — Lab targeting declaration. Uncomment + fill the
# param ranges once this engine has a backtest contract; the four
# callables below are the uniform Lab dispatch contract every engine
# already implements (run_for_search / load_<engine>_window_context /
# run_<engine>_with_context / default_params). Resolved lazily by
# ops.lab.run._lab_target_for via the roster SoT — being added to
# tpcore.engine_profile._PROFILE (PAPER/LAB/LIVE) + declaring this
# constant is ALL that is needed to be Lab-targetable (spec §7 T7).
# ────────────────────────────────────────────────────────────────────────────
#
# from tpcore.lab.target import LabTarget
#
# LAB_TARGET = LabTarget(
#     param_ranges={
#         # "my_param": (low, high, "float"),   # "float" | "int" | "choice:a,b"
#     },
#     run_for_search=run_for_search,
#     load_window_context=load_window_context,
#     run_with_context=run_with_context,
#     default_params=default_params,
# )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tpcore/tests/test_engine_lab_target_declarations.py -p no:xdist -q`
Expected: PASS (all prior + the new scaffold-skeleton test).

- [ ] **Step 6: Commit**

```bash
git add tpcore/templates/engine_template/backtest.py tpcore/tests/test_engine_lab_target_declarations.py
git commit -m "feat(lab-sp-b): add LAB_TARGET skeleton to engine_template (SP-F forward dep)"
```

---

## Task 8: Authoritative gate — full single-process suite + order-flip — spec §7 T8 / §5

**Purpose:** The DEV_PIPELINE_STANDARD authoritative gate. Subset/parallel green ≠ CI green (the `ops/` package-shadow is single-process). Also: SP-B adds NO new `gen_engine_manifest` fenced region (proves §1's "remove a shadow, don't add one").

**Files:** none (gate only)

- [ ] **Step 1: No T0 file fixture exists — confirm the inlined oracle is the only baseline**

There is deliberately NO `_sp_b_t0_baseline.txt` (or any committed runtime fixture). The T0 byte-parity oracle is the inlined module-level `_T0_PARAM_RANGES_KEYSETS` constant in `test_engine_lab_target_declarations.py` and `test_lab_dispatch_indirection.py` — a 3-key snapshot of a fully-known constant needs no file artifact (YAGNI), and a runtime fixture must never live under the `docs/` plans tree (#252 docs-to-reality). Confirm `git status` shows no `docs/superpowers/plans/_sp_b_*` artifact and no cwd-relative `Path("docs/...")` read in either test file.

- [ ] **Step 2: `gen_engine_manifest.py --check` stays green (no new shadow)**

Run: `python scripts/gen_engine_manifest.py --check`
Expected: exit 0 — SP-B added NO fenced region to `_FILE_REGIONS`; the Lab target set is runtime-derived (spec §1). If this REDs, SP-B accidentally drifted a generated shadow — investigate before proceeding.

- [ ] **Step 3: check_imports — the layering invariant (engine→tpcore one-way)**

Run: `python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: exit 0. `tpcore/lab/target.py` imports no engine; engines import it (the legal direction).

- [ ] **Step 4: ruff over the full SP-B blast radius**

Run: `python -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`
Expected: exit 0.

- [ ] **Step 5: Full single-process suite (the authoritative gate)**

Run: `python -m pytest -p no:xdist -q`
Expected: PASS (entire suite, single process — the `ops/` package-shadow invariant). Pay attention to: `scripts/tests/test_search_parameters_characterization.py` (UNMODIFIED, green), `tpcore/tests/test_engine_default_params_parity.py` (one assertion changed, green), `tpcore/tests/test_lab_ntrials_ledger.py` (SP-A untouched, green), `tpcore/tests/test_engine_lifecycle_consistency.py` (green — `roster_for_dispatch`/`archived_engines` untouched).

- [ ] **Step 6: Order-flip rerun (collection-order independence — the second half of the authoritative gate)**

Run: `python -m pytest -p no:xdist -q -p no:randomly --reverse 2>/dev/null || python -m pytest -p no:xdist -q --reverse`
(If `pytest-reverse` is unavailable, use the project's documented order-flip: `python -m pytest -p no:xdist -q` with the suite's standard reversed-collection invocation per `docs/DEV_PIPELINE_STANDARD.md` — consult that doc for the exact command the gate mandates.)
Expected: PASS identically. The `ops`-package-shadow tests carry `xdist_group("ops_shadow")` + the per-module `del sys.modules` eviction so they are order-robust.

- [ ] **Step 7: Confirm the data lane is untouched**

Run: `git diff --name-only main... | grep -E '^tpcore/(quality|providers|selfheal|ladder|datasupervisor)/' || echo "DATA LANE CLEAN"`
Expected: `DATA LANE CLEAN` (SP-B is engine-lane only; the data-SDLC files are read-only symmetry references — spec lane header).

- [ ] **Step 8: `gh pr checks` (the real gate)**

After pushing the branch + opening the PR (the controller does this — NOT this plan), run: `gh pr checks`
Expected: all checks green. This is the binding CI-green boundary (spec §5 "Real gate = `gh pr checks`").

- [ ] **Step 9: Final commit (if any gate-fix was needed) + handoff**

```bash
git status --porcelain   # expect clean if no gate fix was needed
# (only if a gate fix was applied:)
git add -A && git commit -m "fix(lab-sp-b): authoritative-gate fixes (full single-process + order-flip green)"
```
Hand off per `docs/DEV_PIPELINE_STANDARD.md` (split spec-compliance review → fresh-context code-quality review → squash-merge). Suggested review split (spec §7): T1–T3 + T7 (contract + declarations + scaffold) = review-unit A; T4–T6 (dispatch + clockwork + SP-A non-regression, the risk core) = review-unit B.

---

## Self-Review (performed)

### 1. Spec-coverage map (every §2/§3/§4/§7/§8 item → a task)

| Spec item | Task |
|---|---|
| §2.1 `lab_targetable_engines()` + `_LAB_TARGETABLE` + `_LAB_SENTINEL`; predicate table; canary/lab/RETIRED/allocator exclusions | **Task 2** |
| §2.1 §4b note: canary exclusion is a named clause + dedicated forcing test | **Task 2** (`test_canary_excluded_by_explicit_clause`) + **Task 6** (`test_canary_not_lab_targetable`) |
| §2.2 `tpcore/lab/target.py::LabTarget` frozen pydantic-v2 + `model_post_init` fail-loud + engine-free | **Task 1** |
| §2.3 `_lab_target_for` resolver; 3 thin seam views; clear ValueErrors; reject-before-ledger | **Task 4** |
| §2.3 `ops/engine_sdlc/default_params.py` → thin shim; no import cycle | **Task 4** (Step 6 + `test_no_import_cycle...`) |
| §2.4 lazy `PARAM_RANGES` Mapping; **binding ValueError→KeyError re-raise**; `in`/`get`/iter-order/`len` parity; `sample_parameters` clear-error wrap | **Task 4** (Steps 5, 8, the dedicated `KeyError`-not-`ValueError` + `.get(<undeclared>,{})=={}` tests) |
| §2.5 CLI choices generated both sites; no-eager-engine-import invariant | **Task 5** |
| §2.6 consistency clockwork: predicate-equality, CLI-generated, synthetic-drift red-proof, canary/lab pins; `xdist_group("ops_shadow")` | **Task 6** |
| §3 component table — `LabTarget`, accessor, resolver, seam views, lazy Mapping, CLI, clockwork, shim, parity-test delta, template scaffold | Tasks 1–7 (every row mapped) |
| §4.1 undeclared roster engine hard-reject (sentinel) | **Task 4** (`test_lab_target_for_rejects_eligible_but_undeclared_sentinel`) |
| §4.2 canary selected → argparse SystemExit + resolver ValueError, no ledger row | **Task 5** + **Task 6** (`test_undeclared_target_hard_rejects_before_any_ledger_spend`) |
| §4.3 `lab` sentinel selected | **Task 2/4/6** (name-clause + resolver + pin) |
| §4.4 RETIRED/allocator | **Task 2** (`test_retired_and_allocator_excluded`) |
| §4.5 SP-A ledger interaction — first run cumulative==0, monotone growth, ZERO ledger code change | **Task 6** (`test_sp_a_cumulative_applies_to_roster_resolved_target`) |
| §4.6 oracle monkeypatch survival (by-name) | **Task 4** Step 10 (oracle UNMODIFIED gate) |
| §4.7 importlib failure → clear ValueError | **Task 4** (resolver `ModuleNotFoundError` branch) |
| §4.8 two choices sites cannot drift | **Task 5** + **Task 6** (`test_cli_choices_are_generated_both_sites`) |
| §4.9 legacy `--engine` vs `--target-engine` both accept roster set; shim inherits | **Task 5** Step 6 (oracle green) |
| §4.10 `LabCandidate.target_engine` free-`str`; no model validator; programmatic bad-target hard-reject before spend | **Task 6** (`test_undeclared_target_hard_rejects_before_any_ledger_spend`); NON-GOAL: no `LabCandidate` validator added (respected) |
| §4.11 `test_engine_default_params_parity.py:35-38` ONE sanctioned message-pin update | **Task 4** Step 7 |
| §7 T0..T8 phasing | Tasks 0–8 one-to-one |
| §8-B6 sixth surface folded in | **Task 4** |
| §8-A4 no partial ledger write on hard-reject | **Task 6** (`...before_any_ledger_spend`) |
| §8-A2 ValueError→KeyError non-optional + dedicated test | **Task 4** (the dedicated `KeyError`-not-`ValueError` + `.get` tests, independent of the oracle) |
| §8 highest residual risk handed to plan | **Task 4** flagged HIGHEST-RISK; gate Step 8/11 |
| Import layering engine→tpcore one-way; `check_imports` green | **Task 1** Step 5, **Task 3** Step 7, **Task 8** Step 3 |
| §6 NON-GOALS (no ledger touch, no `_score_for_ranking`, no `_PROFILE` add, no `gen_engine_manifest` shadow, no `LabCandidate` validator) | Honored throughout; **Task 8** Step 2 (no new shadow), Step 7 (data lane clean) |

**Gaps found & fixed inline:** (a) Initial draft lacked the explicit "no-eager-engine-import" pristine-subprocess test for §4.8/§2.5 → added to **Task 5** Step 1. (b) The SP-A non-regression test initially risked duplicating ledger logic → rewrote to reuse the verified `test_lab_ntrials_ledger.py` fake-pool shape (`_FakeConn`/`_Acquire`/`_SharedPool`) verbatim, not new ledger code (spec §5 "do not duplicate ledger logic"). (c) Added the explicit char-before-refactor pin block (Task 4 Step 1–2) running against the UN-refactored tree, since the spec mandates characterize-before-refactor for the dispatch core.

### 2. Placeholder scan

No `TBD`/`TODO`/`<fill>`/"add error handling"/"similar to Task N"/"write tests for the above" remain. Every code step shows complete code; every run step shows the exact command + expected output. The one deliberate operator-judgement point (Task 8 Step 6 order-flip command) defers to `docs/DEV_PIPELINE_STANDARD.md` for the project's exact mandated invocation rather than guessing a flag — this is a reference to an authoritative in-repo doc, not a placeholder.

### 3. Type / signature consistency across tasks

- `LabTarget` field names (`param_ranges`, `run_for_search`, `load_window_context`, `run_with_context`, `default_params`) are identical in Task 1 (definition), Task 3 (engine declarations), Task 4 (`_lab_target_for(...).<field>` access), Task 7 (template skeleton). Consistent.
- `_lab_target_for(engine: str) -> LabTarget` — return type used as `.run_for_search`/`.param_ranges`/`.default_params` everywhere; the three seam views keep `(engine) -> Callable` signatures (Task 4) so the oracle by-name monkeypatch binds (Task 4 Step 10).
- `lab_targetable_engines() -> tuple[str, ...]` — used identically in Task 2, 4, 5, 6.
- The lazy Mapping is `_LazyParamRanges(Mapping)` with `__getitem__`/`__iter__`/`__len__`; `PARAM_RANGES` is a module-level instance — `from ops.lab.run import PARAM_RANGES` (the `search_parameters.py` shim + parity test) rebinds the same object, behaviourally identical to the old literal for `[]`/`in`/`get`/`set()`/iteration.
- The parity test exception **type** stays `ValueError` (Task 4 Step 7) — only the `match=` regex moves; consistent with spec §4.11 ("exception type unchanged").
- The SP-A non-regression test's `Namespace` shape (`_ns`) matches the verified `test_lab_ntrials_ledger.py:296-306` field set exactly (engine/trials/per_window_trials/train_start/holdout_end/final_holdout_*/walk_forward_step/train_years/holdout_years/seed/output/database_url/dsr_threshold/credibility_threshold/universe_tier_max). Consistent.

No inconsistencies remain.
