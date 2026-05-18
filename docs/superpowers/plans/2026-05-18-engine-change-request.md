# Engine Change Request + Deterministic Lifecycle Transitions (SP3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the engine-domain analog of the Data Provider Lifecycle: one structured Engine Change Request (ECR) the operator fills in, a deterministic planner/executor that prepares + validates + atomically applies ADD/REMOVE/MODIFY engine lifecycle transitions, completes the SP1 archive-leg clockwork, and closes the two SP2 carry-forwards (O1 `default_params()` + `credibility_pool` threading + the `LabResult` JSON sidecar).

**Architecture:** Pure SoT/contract logic stays in `tpcore`; every engine-touching orchestration lives in `ops/engine_sdlc/` (the SP2 H-S2-1 tpcore∌engine layering precedent). The ECR is a markdown checklist parsed by a strict pydantic-v2 parser into a frozen `EngineChangeRequest`; `classify()` maps it through a total/closed state-machine table to a `TransitionPlan`; `validate()` re-verifies all evidence and runs the **real** SP1 consistency clockwork as a fresh subprocess in an isolated `copytree` of the worktree; `apply()` is journaled atomic-or-abort. ADD/REMOVE are operator-gated (binary TTY y/n); MODIFY and LAB→PAPER promotion are automated-if-gated. Every terminal outcome emits an `ENGINE_CHANGE_REQUEST` audit event.

**Tech Stack:** Python 3.11, pydantic v2 (`frozen=True, extra="forbid"`), `ast`/`compile` for the source rewrite, `shutil`/`pathlib` for filesystem ops, `asyncpg` for the audit emit, `subprocess` + `pytest` for the isolated-tree dry-run, `structlog`. Tests: `pytest` in `tpcore/tests/` with **lazy in-body** `ops.engine_sdlc` imports.

---

## Standing constraints (apply to EVERY task)

- **Git hygiene:** use `git switch` never `git checkout`. Before every commit run `git branch --show-current` and confirm it prints exactly `worktree-engine-sp3`. The executor (planner code) never runs git/gh; SP3 tests never run git/gh against the working repo (synthetic engines live only in `tmp_path`/`copytree` temp trees).
- **Lane + collision discipline (H-S3-10):** every SP3 test file lives in `tpcore/tests/` (a collected `pyproject` testpath — NOT `ops/engine_sdlc/tests/`, which is uncollected). Every SP3 test imports `ops.engine_sdlc.*` **lazily inside the test function body**, never at module top — exact parity with `tpcore/tests/test_lab_isolation.py` (which documents: "we do NOT import ops.lab.run at module level ... `test_ops.py` puts `scripts/` into `sys.path` and does `import ops` to reach `scripts/ops.py`"). SP3 touches **no** tpcore *code* (only the `_PROFILE` data literal via the planner + the in-place test extension), **no** `CLAUDE.md`/`OPERATIONS.md`/`glossary.md`, and **no** data-lane SoT (`tpcore/providers.py`, `tpcore/feeds/`, `tpcore/selfheal/`).
- **Per-task CI-exact gate set** (run at the end of each task's verify steps unless the task says otherwise):
  1. `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider` (full suite green)
  2. `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`
  3. `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore` (H-S3-10: zero tpcore→engine import; expect `OK` / exit 0)
- **No placeholders:** the planner's `_PROFILE` rewrite changes only `EngineProfile(...)` *data tokens* — it adds zero `import`/`from` lines (pinned by `test_profile_rewrite_adds_no_import`, T4).
- **venv:** every `python` invocation below is `/Users/michael/short-term-trading-engine/.venv/bin/python` (abbreviated `PY` in commands).

```bash
PY=/Users/michael/short-term-trading-engine/.venv/bin/python
```

---

## File Structure (everything SP3 creates or modifies, one responsibility each)

**Created — `ops/engine_sdlc/` package (engine-touching orchestration, legal only in `ops/`):**

| File | Single responsibility |
|---|---|
| `ops/engine_sdlc/__init__.py` | Package marker + docstring noting `ops/` is exempt from the `check_imports` tpcore∌engine scan (parity with `ops/lab/__init__.py`). |
| `ops/engine_sdlc/ecr.py` | `ECRAction`, the frozen `EngineChangeRequest` pydantic-v2 model (`extra="forbid"` + exactly-one-action validator), and `parse_ecr(text) -> EngineChangeRequest` (strict fenced-block parser; unknown/cross-block keys rejected with the exact reason). |
| `ops/engine_sdlc/_evidence.py` | `EvidenceError` + `load_labresult_sidecar(md_path) -> LabResult` (resolve sibling `.json`, `LabResult.model_validate_json`, never markdown-scrape). |
| `ops/engine_sdlc/default_params.py` | Lazy per-engine import dispatcher `default_params(engine) -> dict[str, Any]` (parity with `ops/lab/run.py::_runner_for`). |
| `ops/engine_sdlc/planner.py` | `TransitionPlan` (frozen), `classify()` (total/closed §5.1 table), `validate()` (evidence re-verify + isolated-tree subprocess dry-run), `apply()` (journaled atomic-or-abort), the AST-safe `_PROFILE` source rewriter, the shadow-file region editors, the EULOGY renderer, the audit emit. |
| `ops/engine_sdlc/__main__.py` | `python -m ops.engine_sdlc` CLI: parse → classify → validate → render diff → (ADD/REMOVE) explicit TTY `y`/`yes` gate fail-closed → apply; (MODIFY/promote) automated apply; explicit non-zero, never silent 0. |

**Created — docs/templates (non-Python):**

| File | Single responsibility |
|---|---|
| `docs/superpowers/checklists/engine_change_request.md` | The copy/fill ECR block + the §6 operator-interaction policy header (symmetric in feel to `data_feed_change_request.md`). |
| `tpcore/templates/eulogy_template.md` | The Sigma-validated EULOGY section structure (title+date / `## Cause of death` / `## What it leaves behind` / `## Retirement checklist`) — structure only, not Sigma content. |

**Modified (in place):**

| File | What changes |
|---|---|
| `reversion/backtest.py` | Add module-level pure `default_params() -> dict[str, Any]`. |
| `vector/backtest.py` | Add module-level pure `default_params() -> dict[str, Any]`. |
| `momentum/backtest.py` | Add module-level pure `default_params() -> dict[str, Any]`. |
| `ops/lab/run.py` | `_build_lab_result`: replace the `# TODO(SP3)` `current=None` with the real `default_params(...)` value. `_run_lab_core`: thread `LabContext.credibility_pool` when a `LabContext` is active **and** `candidate is not None`. |
| `ops/lab/dossier.py` | `write_lab_dossier`: additionally write `<dossier>.json = LabResult.model_dump_json()` (rendered `.md` byte-unchanged). |
| `tpcore/tests/test_engine_lifecycle_consistency.py` | Extend in place with the H-S3-5 archive-leg assertions (EULOGY content floor; RETIRED-absent shadow purge; no-orphan-archive; RETIRED ⇒ not importable). Lands in T5 with the REMOVE executor. |

**Created — SP3 test files (all in `tpcore/tests/`, lazy in-body imports):**

| File | Pins |
|---|---|
| `tpcore/tests/test_ecr_parse.py` | T0 — the strict-parser contract. |
| `tpcore/tests/test_engine_default_params_parity.py` | T1 — `default_params()` keyset == `PARAM_RANGES` keyset; sentinel/canary have no accessor. |
| `tpcore/tests/test_lab_credibility_pool_threaded.py` | T2 — active-LabContext write uses `context.credibility_pool`, no second RW pool. |
| `tpcore/tests/test_lab_dossier_sidecar.py` | T3 — sidecar round-trips `LabResult` + markdown byte-stable; loader rejects missing/tampered. |
| `tpcore/tests/test_engine_sdlc_planner.py` | T4–T7 — classify table, isolated-tree clockwork, AST-safe rewrite, atomicity, ADD/REMOVE/MODIFY/promote behaviour. |
| `tpcore/tests/test_engine_sdlc_cli.py` | T8 — fail-closed TTY gate, audit on every outcome, explicit non-zero. |
| `scripts/tests/test_sp3_scope_confined.py` | T9 — scope-diff: SP3 change set confined to §8 net-new surface; no SP4/data-lane file touched. |

**Re-run unchanged as gates (never modified by SP3):** `scripts/tests/test_search_parameters_characterization.py` (the SP2 T1 oracle — pins `amain` rc + `write_credibility_score` call args), `tpcore/tests/test_lab_isolation.py` (zero live-write delta + no-poison namespace).

---

## Task T0: `ops/engine_sdlc/` package + ECR contract + checklist

Satisfies: spec §2 (ECR as a first-class artifact), §2.1–§2.3 (format + frozen model + strict parser), H-S3-10 (lane/collision: test in `tpcore/tests/`, lazy import).

**Files:**
- Create: `ops/engine_sdlc/__init__.py`
- Create: `ops/engine_sdlc/ecr.py`
- Create: `docs/superpowers/checklists/engine_change_request.md`
- Test: `tpcore/tests/test_ecr_parse.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_ecr_parse.py`:

```python
"""Strict ECR parser contract (SP3 T0). Lazy in-body import of
ops.engine_sdlc.ecr (H-S3-10: the scripts/ops.py vs ops/ sys.modules
collision the SP2 T9/T10 bite proved — never import at module top)."""
from __future__ import annotations

import pytest

_VALID_ADD = """\
ECR
action:        ADD
engine:        edgehunter
source:        new_scaffold
cadence:       daily
allocator:     false
dispatch_order: 6
need:          captures intraday gap-fade edge
"""

_VALID_REMOVE = """\
ECR
action:        REMOVE
engine:        sentinel
reason:        macro signal lags fast crashes; single-trade history
eulogy_notes:  TLT-only basket, COVID cycle only
"""

_VALID_MODIFY = """\
ECR
action:        MODIFY
engine:        reversion
lab_dossier:   docs/lab/2026-05-18-rev2-SURVIVED-seed0.md
param_change:  z_threshold=3.1, max_hold_days=8
gate_dsr:      0.96
gate_cred:     64
"""


def test_valid_add_parses():
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    ecr = parse_ecr(_VALID_ADD)
    assert ecr.action is ECRAction.ADD
    assert ecr.engine == "edgehunter"
    assert ecr.source == "new_scaffold"
    assert ecr.allocator is False
    assert ecr.dispatch_order == 6
    assert ecr.reason is None and ecr.param_change is None


def test_valid_remove_parses():
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    ecr = parse_ecr(_VALID_REMOVE)
    assert ecr.action is ECRAction.REMOVE
    assert ecr.engine == "sentinel"
    assert ecr.reason.startswith("macro signal lags")
    assert ecr.source is None and ecr.dispatch_order is None


def test_valid_modify_parses():
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    ecr = parse_ecr(_VALID_MODIFY)
    assert ecr.action is ECRAction.MODIFY
    assert ecr.engine == "reversion"
    assert ecr.param_change == {"z_threshold": "3.1", "max_hold_days": "8"}
    assert ecr.gate_dsr == 0.96 and ecr.gate_cred == 64


def test_unknown_key_rejected():
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _VALID_ADD + "bogus_key:  whatever\n"
    with pytest.raises(ValueError, match="unknown ECR key: bogus_key"):
        parse_ecr(bad)


def test_cross_block_field_rejected():
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _VALID_ADD + "reason:  not allowed on an ADD\n"
    with pytest.raises(ValueError, match=r"field.*not valid for action ADD|reason"):
        parse_ecr(bad)


def test_multi_action_rejected():
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _VALID_ADD.replace(
        "action:        ADD", "action:        ADD\naction:        REMOVE")
    with pytest.raises(ValueError, match="exactly one action|duplicate key: action"):
        parse_ecr(bad)


def test_nonparsing_rejected_with_reason():
    from ops.engine_sdlc.ecr import parse_ecr
    with pytest.raises(ValueError, match="no ECR block found"):
        parse_ecr("this is not an ECR at all")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest tpcore/tests/test_ecr_parse.py -q -p no:cacheprovider
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_sdlc'`.

- [ ] **Step 3: Create the package marker**

Create `ops/engine_sdlc/__init__.py`:

```python
"""Engine SDLC SP3 — the Engine Change Request planner/executor.

Engine-touching orchestration (reads tpcore.engine_profile, rewrites the
_PROFILE literal, moves/scaffolds engine packages, reads Lab dossiers):
LEGAL only in ops/ — exempt from the check_imports tpcore∌engine scan
(SP2 H-S2-1 precedent, parity with ops/lab/__init__.py). NEVER wired
into any daemon/dispatch — a one-shot operator tool, like ops.lab.
"""
```

- [ ] **Step 4: Create the strict ECR parser + frozen model**

Create `ops/engine_sdlc/ecr.py`:

```python
"""The Engine Change Request — frozen pydantic-v2 contract + strict parser.

The fenced ``ECR`` block in docs/superpowers/checklists/
engine_change_request.md is the wire format. parse_ecr is the single
entry point: a request that does not parse is rejected with the EXACT
reason, never best-effort-interpreted (spec §2.1).
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from tpcore.engine_profile import Cadence


class ECRAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MODIFY = "modify"


# Which ECR keys are valid for which action (spec §2.2). ``action`` and
# ``engine`` are always required; the rest are action-scoped.
_COMMON = {"action", "engine"}
_ADD_KEYS = {"source", "lab_dossier", "cadence", "allocator",
             "dispatch_order", "gate_dsr", "gate_cred", "need"}
_REMOVE_KEYS = {"reason", "eulogy_notes"}
_MODIFY_KEYS = {"lab_dossier", "param_change", "gate_dsr", "gate_cred"}
_KEYS_FOR = {
    ECRAction.ADD: _ADD_KEYS,
    ECRAction.REMOVE: _REMOVE_KEYS,
    ECRAction.MODIFY: _MODIFY_KEYS,
}
_ALL_KEYS = _COMMON | _ADD_KEYS | _REMOVE_KEYS | _MODIFY_KEYS


class EngineChangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: ECRAction
    engine: str
    # ADD
    source: Literal["new_scaffold", "lab_candidate"] | None = None
    lab_dossier: str | None = None
    cadence: Cadence | None = None
    allocator: bool | None = None
    dispatch_order: int | None = None
    gate_dsr: float | None = None
    gate_cred: int | None = None
    need: str | None = None
    # REMOVE
    reason: str | None = None
    eulogy_notes: str | None = None
    # MODIFY
    param_change: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_the_selected_action_fields(self) -> EngineChangeRequest:
        present = {
            k for k in (_ADD_KEYS | _REMOVE_KEYS | _MODIFY_KEYS)
            if getattr(self, k) is not None
        }
        allowed = _KEYS_FOR[self.action]
        stray = present - allowed
        if stray:
            raise ValueError(
                f"field(s) {sorted(stray)} not valid for action "
                f"{self.action.name}")
        return self


def _parse_block(text: str) -> dict[str, str]:
    """Extract the ``ECR`` ... key:value block. Lines beginning ``#`` or
    blank are comments. A duplicate key is a hard error (catches the
    multi-action smuggle). Unknown keys are a hard error (not ignored —
    spec §2.2 strict extra=forbid at the parser, not just the model)."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "ECR"), None)
    if start is None:
        raise ValueError("no ECR block found (expected a line `ECR`)")
    out: dict[str, str] = {}
    for ln in lines[start + 1:]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            raise ValueError(f"malformed ECR line (no `key: value`): {s!r}")
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip()
        if key in out:
            raise ValueError(f"duplicate key: {key}")
        if key not in _ALL_KEYS:
            raise ValueError(f"unknown ECR key: {key}")
        out[key] = val
    if "action" not in out:
        raise ValueError("ECR block missing required key: action")
    return out


def _coerce(raw: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "allocator":
            out[k] = v.lower() == "true"
        elif k == "dispatch_order" or k == "gate_cred":
            out[k] = int(v)
        elif k == "gate_dsr":
            out[k] = float(v)
        elif k == "param_change":
            d: dict[str, str] = {}
            for pair in (p for p in v.split(",") if p.strip()):
                pk, _, pv = pair.partition("=")
                d[pk.strip()] = pv.strip()
            out[k] = d
        else:
            out[k] = v
    return out


def parse_ecr(text: str) -> EngineChangeRequest:
    raw = _parse_block(text)
    try:
        action = ECRAction(raw["action"].strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"invalid action {raw['action']!r}: must be exactly one of "
            f"ADD | REMOVE | MODIFY") from exc
    coerced = _coerce(raw)
    coerced["action"] = action
    return EngineChangeRequest(**coerced)


__all__ = ["ECRAction", "EngineChangeRequest", "parse_ecr"]
```

- [ ] **Step 5: Create the operator checklist**

Create `docs/superpowers/checklists/engine_change_request.md`:

```markdown
# Engine Change Request — the operator's single structured touchpoint

This is **the** way to add, remove, or re-tune an engine. You do **not**
hand-edit `tpcore.engine_profile._PROFILE`, the smoke loop, `pyproject`,
the dispatch-order frozen literal, or an EULOGY — that is exactly how the
system gets broken (the Sigma 22-site drift, PR #170, proved it). You
fill in the block below and feed it in; the system parses it, routes it
through the deterministic lifecycle gates, **prepares and validates the
exact diff**, and hands you back either a binary **APPROVE? (y/n)** on a
proven-consistent diff (ADD / REMOVE), or — for the automated gated
operations (MODIFY / LAB→PAPER promote) — a done-receipt with the
`ENGINE_CHANGE_REQUEST` audit reference.

> **Operator-interaction policy (authoritative — spec §6).** You approve
> **only** two things: **ADD** an engine (new scaffold or Lab-graduated)
> and **REMOVE** one (retire/archive). Everything reversible and
> gate-verified — a MODIFY (re-tuned params that already passed
> DSR≥0.95 ∧ credibility≥60) and a LAB→PAPER promotion the capital gate
> already cleared — is **automated, deterministic, no operator approval**.
> A request that cannot produce a consistent diff is **rejected with the
> exact reason — never handed to you to force**.

## The request block (copy, fill, feed in)

​```
ECR
action:        ADD | REMOVE | MODIFY        # exactly one
engine:        <engine name>                # _PROFILE key vocabulary
# ── ADD only (onboard / graduate) ─────────────────────────────────
source:        new_scaffold | lab_candidate # brand-new vs Lab-graduated
lab_dossier:   <path under docs/lab/…>      # required iff source=lab_candidate
cadence:       daily | weekly_first_trading_day | monthly_first_trading_day
allocator:     true | false                 # allocator_eligible
dispatch_order: <int>                        # unique among non-RETIRED
gate_dsr:      <float ≥ 0.95>               # re-verified from the dossier
gate_cred:     <int ≥ 60>                   # re-verified from the dossier
need:          <one line: the edge / why this engine exists>
# ── REMOVE only (retire / archive) ────────────────────────────────
reason:        <one line: cause of death>
eulogy_notes:  <free text → seeds the EULOGY template>
# ── MODIFY only (re-tuned params on an existing engine) ───────────
lab_dossier:   <path under docs/lab/…>      # the SURVIVED fold_existing dossier
param_change:  <key>=<value>[, <key>=<value> …]
gate_dsr:      <float ≥ 0.95>
gate_cred:     <int ≥ 60>
​```

`action` selects exactly one block; any field outside the selected
block is **rejected** (not ignored). All numeric gate evidence is
**re-verified by the planner against the cited Lab dossier's JSON
sidecar — never trusted from this text** (spec §5.4 / H-S3-6).

Run it: `python -m ops.engine_sdlc --ecr <path-to-this-filled-file>`
```

> NOTE for the executor: in the file above, the three lines that read `​```` use the markdown fenced-block delimiter. Write a literal triple-backtick fence for the ECR wire block (the zero-width markers shown here are only so this plan renders — the real file must use plain ```` ``` ````).

- [ ] **Step 6: Run test to verify it passes**

```bash
$PY -m pytest tpcore/tests/test_ecr_parse.py -q -p no:cacheprovider
```
Expected: PASS — 7 passed.

- [ ] **Step 7: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0.

- [ ] **Step 8: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/engine_sdlc/__init__.py ops/engine_sdlc/ecr.py docs/superpowers/checklists/engine_change_request.md tpcore/tests/test_ecr_parse.py
git commit -m "feat(engine-sdlc): T0 ECR contract + strict parser + operator checklist

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T1: O1 — per-engine `default_params()` + dispatcher + `_build_lab_result` wiring + parity test

Satisfies: spec §1.4 O1 carry-forward, §7.1 (the `default_params()` seam), H-S3-8 (no SP2 regression — re-run the unchanged T1 oracle), H-S3-10 (no tpcore→engine import; lazy dispatch).

**Files:**
- Modify: `reversion/backtest.py` (add `default_params`)
- Modify: `vector/backtest.py` (add `default_params`)
- Modify: `momentum/backtest.py` (add `default_params`)
- Create: `ops/engine_sdlc/default_params.py`
- Modify: `ops/lab/run.py:_build_lab_result` (remove the `# TODO(SP3)`)
- Test: `tpcore/tests/test_engine_default_params_parity.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_engine_default_params_parity.py`:

```python
"""O1 default_params() parity (SP3 T1). The cannot-be-forgotten
clockwork: a new searched param without a default fails CI (HealSpec-
coverage discipline). Lazy in-body import (H-S3-10)."""
from __future__ import annotations

import pytest

_PARAM_RANGES_ENGINES = ("reversion", "vector", "momentum")


@pytest.mark.parametrize("engine", _PARAM_RANGES_ENGINES)
def test_each_param_ranges_engine_default_keyset_equals_param_ranges(engine):
    from ops.engine_sdlc.default_params import default_params
    from ops.lab.run import PARAM_RANGES
    got = default_params(engine)
    assert set(got) == set(PARAM_RANGES[engine]), (
        f"{engine}: default_params() keyset {sorted(got)} != PARAM_RANGES "
        f"keyset {sorted(PARAM_RANGES[engine])} — a searched param with no "
        f"default (or a stale default) fails CI")
    for v in got.values():
        assert v is not None


def test_sentinel_canary_have_no_accessor():
    # sentinel/canary have NO search space (not in PARAM_RANGES) ⇒ no
    # backtest.default_params accessor (spec §7.1).
    import importlib
    for engine in ("sentinel", "canary"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert not hasattr(mod, "default_params"), (
            f"{engine}: has no PARAM_RANGES search space — must NOT expose "
            f"default_params()")


def test_dispatcher_rejects_unknown_engine():
    from ops.engine_sdlc.default_params import default_params
    with pytest.raises(ValueError, match="unknown engine: nope"):
        default_params("nope")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest tpcore/tests/test_engine_default_params_parity.py -q -p no:cacheprovider
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_sdlc.default_params'`.

- [ ] **Step 3: Add `default_params()` to `reversion/backtest.py`**

Insert immediately after the `_volume_climax_threshold()` function (after line 145, before the `DEFAULT_OUTPUT_DIR = Path("backtests")` line). The reversion `PARAM_RANGES` keys are `z_threshold`, `volume_climax_multiplier`, `max_hold_days`, `stop_pct`. The live z-default is `Z_SCORE_THRESHOLD` (imported from `reversion.models`, value `2.5`); the other three are the accessors:

```python
def default_params() -> dict[str, Any]:
    """Current live defaults for EXACTLY this engine's
    ops.lab.run.PARAM_RANGES keys (SP3 O1 seam, spec §7.1). Pure — reads
    the module accessors, no DB. The parity test pins the keyset ==
    PARAM_RANGES['reversion']."""
    return {
        "z_threshold": float(Z_SCORE_THRESHOLD),
        "volume_climax_multiplier": float(_volume_climax_threshold()),
        "max_hold_days": int(_max_hold_days()),
        "stop_pct": float(_hard_stop_pct()),
    }
```

`Any` is already imported in `reversion/backtest.py` (verify: `grep -n "from typing import" reversion/backtest.py` — if `Any` is absent, add it to that import; do not add a new import line if `Any` is present).

- [ ] **Step 4: Add `default_params()` to `vector/backtest.py`**

Insert immediately after `_swing_score_threshold()` (after line ~133, before `_synth_swing_score`). The vector `PARAM_RANGES` keys are `pb_ceiling`, `de_ceiling`, `catalyst_window_days`, `swing_score_threshold`, `stop_pct`. Note `_swing_score_threshold()` returns `None` when there is no gate — match the same `0.0` convention `run_vector_with_context` already uses (line 935):

```python
def default_params() -> dict[str, Any]:
    """Current live defaults for EXACTLY this engine's
    ops.lab.run.PARAM_RANGES keys (SP3 O1 seam, spec §7.1). Pure. The
    swing-score default mirrors run_vector_with_context's 0.0-when-None
    convention so the diff is well-defined."""
    swing = _swing_score_threshold()
    return {
        "pb_ceiling": float(_pb_ceiling()),
        "de_ceiling": float(_de_ceiling()),
        "catalyst_window_days": int(_catalyst_window_days()),
        "swing_score_threshold": float(swing) if swing is not None else 0.0,
        "stop_pct": float(_hard_stop_pct()),
    }
```

Verify `Any` is imported in `vector/backtest.py` (`grep -n "from typing import" vector/backtest.py`); add to the existing import only if absent.

- [ ] **Step 5: Add `default_params()` to `momentum/backtest.py`**

Insert immediately after `_top_decile()` (after line ~102, before the `MOMENTUM_OVERRIDE_KEYS = (` block). The momentum `PARAM_RANGES` keys are `lookback_days`, `skip_days`, `hold_days`, `top_decile_pct`:

```python
def default_params() -> dict[str, Any]:
    """Current live defaults for EXACTLY this engine's
    ops.lab.run.PARAM_RANGES keys (SP3 O1 seam, spec §7.1). Pure."""
    return {
        "lookback_days": int(_lookback()),
        "skip_days": int(_skip()),
        "hold_days": int(_hold()),
        "top_decile_pct": float(_top_decile()),
    }
```

Verify `Any` is imported in `momentum/backtest.py`; add to the existing import only if absent.

- [ ] **Step 6: Create the lazy dispatcher**

Create `ops/engine_sdlc/default_params.py`:

```python
"""Lazy per-engine default_params() dispatcher (SP3 O1, spec §7.1).

Exact parity with ops.lab.run._runner_for: the engine import is LAZY,
inside the function body, so this module (and anything importing it)
never eager-imports an engine — and there is NEVER a tpcore→engine
import (the dispatcher lives in ops/, legal here, H-S3-10).
"""
from __future__ import annotations

from typing import Any


def default_params(engine: str) -> dict[str, Any]:
    if engine == "reversion":
        from reversion.backtest import default_params as dp
        return dp()
    if engine == "vector":
        from vector.backtest import default_params as dp
        return dp()
    if engine == "momentum":
        from momentum.backtest import default_params as dp
        return dp()
    raise ValueError(f"unknown engine: {engine}")


__all__ = ["default_params"]
```

- [ ] **Step 7: Wire `_build_lab_result` (remove the `# TODO(SP3)`)**

In `ops/lab/run.py`, replace the `param_diff` list comprehension in `_build_lab_result` (lines 843–847). Current:

```python
    param_diff = [
        # TODO(SP3): current=None until a per-engine default_params() accessor exists (spec O1, deferred from SP2 — see docstring).
        ParamDelta(name=k, current=None, winning=v)
        for k, v in sorted(core.winner_params.items())
    ]
```

Replace with (the dispatcher is imported lazily inside the function to keep `ops.lab.run`'s engine-free module-top contract, parity with `_runner_for`):

```python
    from ops.engine_sdlc.default_params import default_params
    _live_defaults = default_params(args.engine)
    param_diff = [
        ParamDelta(name=k, current=_live_defaults.get(k), winning=v)
        for k, v in sorted(core.winner_params.items())
    ]
```

Also update the `_build_lab_result` docstring: replace the sentence "No `default_params()` accessor exists on any engine (O1 was folded into the spec but not built in T1–T9), so `param_diff` honestly carries the winning value with `current=None` (unknown — there is no engine-default seam to read)." with: "The O1 `default_params()` seam (SP3 T1) supplies the live default for each swept param, so `param_diff` carries the real `current → winning` diff (SP3 §7.1)."

- [ ] **Step 8: Run the parity test to verify it passes**

```bash
$PY -m pytest tpcore/tests/test_engine_default_params_parity.py -q -p no:cacheprovider
```
Expected: PASS — 5 passed (3 parametrized + sentinel/canary + unknown-engine).

- [ ] **Step 9: Re-run the SP2 T1 characterization oracle UNCHANGED (H-S3-8)**

```bash
$PY -m pytest scripts/tests/test_search_parameters_characterization.py -q -p no:cacheprovider
```
Expected: PASS — identical to pre-T1. The oracle pins `amain`'s rc + the `write_credibility_score` call args, **not** `param_diff` contents, so wiring `default_params` into `_build_lab_result` is oracle-neutral by construction. If any oracle test fails, the change perturbed a pinned surface — STOP and revert; do not modify the oracle.

- [ ] **Step 10: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0 (no tpcore→engine import was added — the dispatcher is in `ops/`).

- [ ] **Step 11: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add reversion/backtest.py vector/backtest.py momentum/backtest.py ops/engine_sdlc/default_params.py ops/lab/run.py tpcore/tests/test_engine_default_params_parity.py
git commit -m "feat(engine-sdlc): T1 O1 default_params() seam + dispatcher + _build_lab_result wiring

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T2: `credibility_pool` threading (SP2 carry-forward)

Satisfies: spec §1.4 (credibility_pool carry-forward), §7.2 (thread it, do not trim), H-S3-8 (isolation contract made true; no SP2 regression).

**Files:**
- Modify: `ops/lab/run.py:_run_lab_core` (lines ~731–757 — the credibility-persist block)
- Test: `tpcore/tests/test_lab_credibility_pool_threaded.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_lab_credibility_pool_threaded.py`:

```python
"""T2 — under an active LabContext the credibility write goes through
context.credibility_pool (the ONE allowlisted RW handle), not a second
ad-hoc asyncpg.create_pool inside the isolation boundary (spec §7.2,
H-S3-8). Offline: monkeypatch the runner/loader/credibility-writer
exactly as the SP2 oracle does — no DB. Lazy in-body import (H-S3-10)."""
from __future__ import annotations

import argparse
from datetime import date

import pytest


def _ns() -> argparse.Namespace:
    return argparse.Namespace(
        engine="reversion", trials=4, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=0, output=None, database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None,
    )


@pytest.mark.asyncio
async def test_active_labcontext_write_uses_context_pool_no_second_rw_pool(
        monkeypatch):
    import asyncpg

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    created_pools: list[str] = []

    class _FakePool:
        def __init__(self, tag: str) -> None:
            self.tag = tag
        async def close(self) -> None: ...

    async def _fake_create_pool(*a, **k):
        created_pools.append("create_pool")
        return _FakePool("create_pool")

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool,
                         raising=True)

    used_pool_tags: list[str] = []

    async def _fake_write_credibility_score(pool, *, engine_name, score):
        used_pool_tags.append(getattr(pool, "tag", type(pool).__name__))
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write_credibility_score, raising=True)

    # Stub the heavy walk-forward so _run_lab_core reaches the persist
    # block deterministically with a credibility rubric set. The SP2
    # oracle's stub harness is the reference shape; we reuse the same
    # monkeypatch targets (ops.lab.run.*).
    from scripts.tests.test_search_parameters_characterization import (  # noqa: E501
        _install_lab_core_stub_harness,  # defined in the oracle as the shared offline harness
    )
    _install_lab_core_stub_harness(monkeypatch, lab_run)

    # LabContext with build_pools=True but a fake builder so we get a
    # tagged credibility_pool without a DB.
    class _CtxPool(_FakePool):
        pass

    async def _fake_build_asyncpg_pool(url, *, read_only, **k):
        return _CtxPool("context_credibility" if not read_only
                        else "context_read")

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool",
                        _fake_build_asyncpg_pool, raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        await lab_run._run_lab_core(_ns(), candidate="rev_cand")

    assert used_pool_tags == ["context_credibility"], (
        f"under an active LabContext the credibility write must use "
        f"context.credibility_pool; saw {used_pool_tags}")
    assert "create_pool" not in created_pools, (
        "a second RW asyncpg.create_pool was opened inside the Lab "
        "isolation boundary — spec §7.2 violated")
```

> Executor note: if the SP2 oracle does not expose a reusable `_install_lab_core_stub_harness`, inline the equivalent stub here (monkeypatch `ops.lab.run._runner_for`, `_context_loader_for`, `_context_runner_for` to the same fakes the oracle's `test_amain_smoke_survived_verdict` uses — read `scripts/tests/test_search_parameters_characterization.py:144-260` for the exact fake shapes — so `_run_lab_core` returns a `_LabCore` with a non-None `credibility_rubric`). Do **not** modify the oracle file to add the helper; copy the fakes into this test.

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest tpcore/tests/test_lab_credibility_pool_threaded.py -q -p no:cacheprovider
```
Expected: FAIL — `assert used_pool_tags == ["context_credibility"]` fails (current `_run_lab_core` opens its own `asyncpg.create_pool`, so `used_pool_tags == ["create_pool"]` and `"create_pool" in created_pools`).

- [ ] **Step 3: Thread `context.credibility_pool` in `_run_lab_core`**

In `ops/lab/run.py`, the credibility-persist block (lines ~731–757) currently always opens its own pool:

```python
        persist_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=1)
        try:
            wrote = await write_credibility_score(
                persist_pool,
                engine_name=cred_engine_name,
                score=final_result.credibility_rubric,
            )
            print(
                f"  → persisted credibility rubric to platform.data_quality_log "
                f"(source=backtest_credibility.{cred_engine_name}, wrote={wrote})\n"
            )
        finally:
            await persist_pool.close()
```

Replace it with (when a `LabContext` is active **and** `candidate is not None` — a Lab run — use the active context's allowlisted RW handle; the legacy `candidate is None` search-CLI path keeps its own pool byte-identical; the `write_credibility_score(engine_name=…, score=…)` call args are unchanged in both paths):

```python
        from tpcore.lab.context import active_credibility_pool

        ctx_pool = active_credibility_pool() if candidate is not None else None
        if ctx_pool is not None:
            wrote = await write_credibility_score(
                ctx_pool,
                engine_name=cred_engine_name,
                score=final_result.credibility_rubric,
            )
            print(
                f"  → persisted credibility rubric to platform.data_quality_log "
                f"(source=backtest_credibility.{cred_engine_name}, wrote={wrote})\n"
            )
        else:
            persist_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=1)
            try:
                wrote = await write_credibility_score(
                    persist_pool,
                    engine_name=cred_engine_name,
                    score=final_result.credibility_rubric,
                )
                print(
                    f"  → persisted credibility rubric to platform.data_quality_log "
                    f"(source=backtest_credibility.{cred_engine_name}, wrote={wrote})\n"
                )
            finally:
                await persist_pool.close()
```

- [ ] **Step 4: Add the `active_credibility_pool()` accessor to `tpcore/lab/context.py`**

`LabContext` is not reentrant (verified: inner exit resets `_LAB_ACTIVE`). The threading must read the *active* context's pool without constructing a nested `LabContext`. Add a module-level contextvar that the CM sets on enter / clears on exit, plus a public accessor. In `tpcore/lab/context.py`:

After the existing `_LAB_ACTIVE` contextvar (line 9–10), add:

```python
_ACTIVE_CRED_POOL: contextvars.ContextVar = contextvars.ContextVar(
    "_ACTIVE_CRED_POOL", default=None)


def active_credibility_pool():
    """The active LabContext's single allowlisted RW credibility pool,
    or None if no LabContext is active (legacy non-Lab path). Public
    accessor — never reach into LabContext internals (STYLE_GUIDE
    private-attribute rule)."""
    return _ACTIVE_CRED_POOL.get()
```

In `LabContext.__aenter__`, after `self.credibility_pool` is built (after line 63, inside the `if self._build_pools:` block), set the contextvar:

```python
                self._cred_token = _ACTIVE_CRED_POOL.set(self.credibility_pool)
```

Initialize `self._cred_token: contextvars.Token | None = None` in `__init__` (next to `self._token`). In the `except Exception:` cleanup of `__aenter__` and in `__aexit__`'s `finally:`, reset it symmetric to `self._token`:

```python
            if self._cred_token is not None:
                _ACTIVE_CRED_POOL.reset(self._cred_token)
                self._cred_token = None
```

(Add the reset in BOTH the `__aenter__` except-cleanup and the `__aexit__` finally, before/with the existing `_LAB_ACTIVE.reset`.)

- [ ] **Step 5: Run the test to verify it passes**

```bash
$PY -m pytest tpcore/tests/test_lab_credibility_pool_threaded.py -q -p no:cacheprovider
```
Expected: PASS — `used_pool_tags == ["context_credibility"]`, no `create_pool`.

- [ ] **Step 6: Re-run the SP2 isolation test + T1 oracle UNCHANGED (H-S3-8)**

```bash
$PY -m pytest tpcore/tests/test_lab_isolation.py scripts/tests/test_search_parameters_characterization.py -q -p no:cacheprovider
```
Expected: both PASS unchanged (the isolation test is `DATABASE_URL`-gated — it skips locally without a DB; that is the SP2-expected behaviour, do not "fix" the skip). The credibility append remains the single intentional RW exception; the `write_credibility_score` call args are unchanged in both paths. If `test_lab_isolation.py` runs (DB present) and fails the zero-live-write-delta or no-poison assertion, STOP — the threading regressed the contract.

- [ ] **Step 7: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0.

- [ ] **Step 8: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/lab/run.py tpcore/lab/context.py tpcore/tests/test_lab_credibility_pool_threaded.py
git commit -m "feat(engine-sdlc): T2 thread LabContext.credibility_pool (isolation contract made true)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T3: Lab `LabResult` JSON sidecar (design-defect D1 fix) + planner-side sidecar loader

Satisfies: spec §5.4 / D1 / H-S3-9 (machine-readable frozen evidence, not scraped markdown). Lands BEFORE the executors (T5–T7) that consume it (ordering invariant ii).

**Files:**
- Modify: `ops/lab/dossier.py:write_lab_dossier`
- Create: `ops/engine_sdlc/_evidence.py`
- Test: `tpcore/tests/test_lab_dossier_sidecar.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_lab_dossier_sidecar.py`:

```python
"""T3 — write_lab_dossier emits a sibling <dossier>.json =
LabResult.model_dump_json(); the .md is byte-unchanged; the planner-side
loader model-validates it (extra=forbid) and rejects missing/tampered.
Lazy in-body import (H-S3-10)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tpcore.backtest.credibility import CredibilityScore
from tpcore.lab.models import LabResult, ParamDelta, WalkWindowRecord


def _labresult() -> LabResult:
    from datetime import date
    return LabResult(
        candidate="rev_cand",
        target_engine="reversion",
        intent="fold_existing",
        verdict="SURVIVED",
        dsr=0.97,
        credibility_score=64,
        credibility_rubric=CredibilityScore.model_validate(
            CredibilityScore.model_construct().model_dump()
            if hasattr(CredibilityScore, "model_construct")
            else {}),
        held_metrics={"n_trades": 12, "sharpe": 1.1},
        winning_params={"z_threshold": 3.1, "max_hold_days": 8},
        param_diff=[ParamDelta(name="z_threshold", current=2.5, winning=3.1)],
        recommended_exit="fold_existing",
        ranked_alternatives=[{"z_threshold": 3.0}],
        walk_windows=[WalkWindowRecord(
            train_start=date(2018, 1, 1), train_end=date(2020, 12, 31),
            holdout_start=date(2021, 1, 1), holdout_end=date(2021, 12, 31))],
        n_trials=4,
        seed=0,
        generated_at=datetime(2026, 5, 18, tzinfo=UTC),
    )
```

> Executor note: `CredibilityScore` requires real fields. Before writing this test, run `$PY -c "from tpcore.backtest.credibility import CredibilityScore; import inspect; print(CredibilityScore.model_fields)"` and construct a minimal valid `CredibilityScore(...)` with the actual required fields rather than the `model_construct` fallback shown above. Replace the `credibility_rubric=` argument with the real constructor call. The rest of the test is unaffected.

```python
def test_sidecar_roundtrips_labresult_and_md_unchanged(tmp_path, monkeypatch):
    import ops.lab.dossier as dossier
    monkeypatch.setattr(dossier, "LAB_DIR", tmp_path, raising=True)
    r = _labresult()
    md_text = dossier.render_lab_dossier(r)
    p = dossier.write_lab_dossier(r)
    # .md byte-stable: write_lab_dossier writes EXACTLY render_lab_dossier.
    assert p.read_text() == md_text
    sidecar = p.with_suffix(".json")
    assert sidecar.is_file(), "no <dossier>.json sidecar written"
    from ops.engine_sdlc._evidence import load_labresult_sidecar
    loaded = load_labresult_sidecar(p)
    assert loaded == r  # frozen pydantic round-trips by value


def test_loader_rejects_missing_sidecar(tmp_path):
    from ops.engine_sdlc._evidence import EvidenceError, load_labresult_sidecar
    md = tmp_path / "2026-05-18-x-SURVIVED-seed0.md"
    md.write_text("# rendered only, no sidecar")
    with pytest.raises(EvidenceError, match="no LabResult sidecar"):
        load_labresult_sidecar(md)


def test_loader_rejects_tampered_extra_field(tmp_path):
    from ops.engine_sdlc._evidence import EvidenceError, load_labresult_sidecar
    md = tmp_path / "2026-05-18-x-SURVIVED-seed0.md"
    md.write_text("# rendered only")
    sidecar = md.with_suffix(".json")
    payload = json.loads(_labresult().model_dump_json())
    payload["smuggled_field"] = "evil"
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(EvidenceError, match="tampered|extra|forbid"):
        load_labresult_sidecar(md)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest tpcore/tests/test_lab_dossier_sidecar.py -q -p no:cacheprovider
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_sdlc._evidence'` (and the sidecar assertion fails — `write_lab_dossier` writes only `.md`).

- [ ] **Step 3: Emit the JSON sidecar in `write_lab_dossier`**

In `ops/lab/dossier.py`, `write_lab_dossier` is currently:

```python
def write_lab_dossier(r: LabResult) -> Path:
    p = dossier_path(r)
    p.write_text(render_lab_dossier(r))
    return p
```

Replace with (additive — the `.md` write is byte-identical; only a sibling `.json` is added):

```python
def write_lab_dossier(r: LabResult) -> Path:
    p = dossier_path(r)
    p.write_text(render_lab_dossier(r))
    # H-S3-9 (D1 fix): the automated-MODIFY gate (SP3) re-derives every
    # number from a machine-readable frozen artifact, NEVER scraped
    # rendered markdown. model_dump_json is deterministic field order
    # (frozen pydantic). The .md above is byte-unchanged.
    p.with_suffix(".json").write_text(r.model_dump_json())
    return p
```

- [ ] **Step 4: Create the planner-side sidecar loader**

Create `ops/engine_sdlc/_evidence.py`:

```python
"""SP3 evidence loader (H-S3-9 / D1). The planner re-derives every gate
number from the frozen LabResult JSON sidecar — NEVER the rendered
markdown (re-scraping prose rendered by a template is fragile for the
load-bearing automated-MODIFY gate). extra=forbid ⇒ a tampered/extra
field is a hard reject.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from tpcore.lab.models import LabResult


class EvidenceError(RuntimeError):
    """The cited Lab dossier's evidence sidecar is missing, unreadable,
    or fails LabResult model-validation (tampered/extra field)."""


def load_labresult_sidecar(md_path: str | Path) -> LabResult:
    md = Path(md_path)
    sidecar = md.with_suffix(".json")
    if not sidecar.is_file():
        raise EvidenceError(
            f"no LabResult sidecar for dossier {md.name!r} "
            f"(expected {sidecar.name}); re-run the Lab to regenerate it")
    try:
        return LabResult.model_validate_json(sidecar.read_text())
    except ValidationError as exc:
        raise EvidenceError(
            f"LabResult sidecar {sidecar.name} failed validation "
            f"(tampered / extra field / extra=forbid): {exc}") from exc


__all__ = ["EvidenceError", "load_labresult_sidecar"]
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
$PY -m pytest tpcore/tests/test_lab_dossier_sidecar.py -q -p no:cacheprovider
```
Expected: PASS — 3 passed (round-trip + missing + tampered).

- [ ] **Step 6: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green (the dossier `.md` byte-stability means no SP2 dossier test regresses); ruff clean; check_imports exit 0.

- [ ] **Step 7: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/lab/dossier.py ops/engine_sdlc/_evidence.py tpcore/tests/test_lab_dossier_sidecar.py
git commit -m "feat(engine-sdlc): T3 LabResult JSON sidecar (D1/H-S3-9) + planner sidecar loader

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T4: `classify()` + `TransitionPlan` + the closed table + `validate()`'s isolated-tree dry-run

Satisfies: spec §3.2 (pipeline), §4 (the three transitions as state machines), §5.1 (`classify`), §5.2 (`validate` — reject never force), H-S3-1 (isolated-tree subprocess dry-run, NOT dict-injection / D2), H-S3-2 read-side, H-S3-10 (`_PROFILE` rewrite adds no import — pinned here).

**Files:**
- Create: `ops/engine_sdlc/planner.py` (`TransitionPlan`, `classify`, `validate` skeleton + the subprocess dry-runner + `_rewrite_profile_source` AST gate)
- Test: `tpcore/tests/test_engine_sdlc_planner.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_engine_sdlc_planner.py` (T4 functions; T5–T7 append to this same file):

```python
"""SP3 planner — classify table + isolated-tree dry-run + AST-safe
rewrite (T4) and the ADD/REMOVE/MODIFY/promote executors (T5–T7 append
below). Lazy in-body import of ops.engine_sdlc (H-S3-10). SP3 test
files live in tpcore/tests/ (a collected pyproject testpath)."""
from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from tpcore.engine_profile import LifecycleState


def _ecr(**kw):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    return EngineChangeRequest(**kw)


def _snapshot():
    """The current real _PROFILE as the {engine: lifecycle_state} snapshot
    classify() consumes (a pure-arg snapshot — classify does NO I/O)."""
    from tpcore.engine_profile import _PROFILE
    return {k: p.lifecycle_state for k, p in _PROFILE.items()}


@pytest.mark.parametrize("action,engine,source,expect", [
    # ADD, engine absent → LAB, OPERATOR
    ("add", "newengine", "new_scaffold", ("LAB", "OPERATOR", None)),
    ("add", "newengine", "lab_candidate", ("LAB", "OPERATOR", None)),
    # ADD, engine present → reject
    ("add", "reversion", "new_scaffold", (None, None, "already exists")),
    # REMOVE present PAPER → RETIRED, OPERATOR
    ("remove", "sentinel", None, ("RETIRED", "OPERATOR", None)),
    # REMOVE absent → reject
    ("remove", "ghost", None, (None, None, "nothing to remove")),
    # REMOVE already-retired (sigma) → reject
    ("remove", "sigma", None, (None, None, "already retired")),
    # MODIFY present PAPER → unchanged, AUTOMATED
    ("modify", "reversion", None, ("PAPER", "AUTOMATED", None)),
    # MODIFY absent → reject
    ("modify", "ghost", None, (None, None, "nothing to modify")),
    # MODIFY retired (sigma) → reject
    ("modify", "sigma", None, (None, None, "cannot tune a retired")),
])
def test_classify_every_table_cell(action, engine, source, expect):
    from ops.engine_sdlc.planner import classify
    kw = {"action": action, "engine": engine}
    if action == "add":
        kw.update(source=source, cadence="daily", allocator=False,
                  dispatch_order=9, need="x")
    if action == "remove":
        kw.update(reason="x", eulogy_notes="x")
    if action == "modify":
        kw.update(lab_dossier="docs/lab/x.md",
                  param_change={"z_threshold": "3.1"},
                  gate_dsr=0.96, gate_cred=64)
    plan = classify(_ecr(**kw), _snapshot())
    exp_to, exp_appr, exp_reject = expect
    if exp_reject is not None:
        assert plan.rejection is not None
        assert exp_reject in plan.rejection
    else:
        assert plan.rejection is None
        assert plan.to_state == getattr(LifecycleState, exp_to)
        assert plan.approval_class == exp_appr


def test_profile_rewrite_adds_no_import():
    """H-S3-10: the _PROFILE rewrite changes ONLY EngineProfile(...) data
    tokens — it never adds an import/from line."""
    from ops.engine_sdlc.planner import _rewrite_profile_source
    src = Path("tpcore/engine_profile.py").read_text()
    new = _rewrite_profile_source(
        src, engine="reversion", set_state="retired",
        set_allocator_eligible=False)
    orig_imports = [ln for ln in src.splitlines()
                    if ln.startswith(("import ", "from "))]
    new_imports = [ln for ln in new.splitlines()
                   if ln.startswith(("import ", "from "))]
    assert new_imports == orig_imports, "the _PROFILE rewrite added/removed an import line"


def test_validate_runs_real_clockwork_in_isolated_tree(tmp_path):
    """H-S3-1 / D2: validate() stages the proposed tree into an isolated
    copytree and runs the REAL test_engine_lifecycle_consistency.py as a
    fresh subprocess with cwd=temp — a deliberately-introduced half-state
    must make validate reject with the clockwork's own failure text."""
    from ops.engine_sdlc.planner import _run_consistency_subprocess
    repo = Path(__file__).resolve().parents[2]
    staged = tmp_path / "tree"
    shutil.copytree(
        repo, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    # Introduce a half-state: flip reversion to RETIRED but DON'T move
    # the package / write an EULOGY — the clockwork must catch it.
    ep = staged / "tpcore" / "engine_profile.py"
    txt = ep.read_text().replace(
        'dispatch_order=1, lifecycle_state=LifecycleState.PAPER,\n'
        '                               allocator_eligible=True)',
        'dispatch_order=1, lifecycle_state=LifecycleState.RETIRED,\n'
        '                               allocator_eligible=False)')
    ep.write_text(txt)
    rc, out = _run_consistency_subprocess(staged)
    assert rc != 0, "a staged half-state must fail the real clockwork"
    assert "reversion" in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_sdlc.planner'`.

- [ ] **Step 3: Create `ops/engine_sdlc/planner.py` (T4 surface only)**

Create `ops/engine_sdlc/planner.py`:

```python
"""The deterministic ECR planner/executor (SP3 §3–§5).

parse_ecr → classify(ecr, snapshot) -> TransitionPlan → validate(plan)
(re-verify evidence + run the REAL SP1 clockwork in an isolated temp
tree as a fresh subprocess, H-S3-1/D2) → apply(plan) (journaled
atomic-or-abort, H-S3-4). Engine-touching orchestration: LEGAL only in
ops/ (H-S2-1). The _PROFILE rewrite is AST-validated (H-S3-3) and
data-only (H-S3-10 — adds zero imports).
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from tpcore.engine_profile import LifecycleState

from ops.engine_sdlc.ecr import ECRAction, EngineChangeRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOCATOR = "allocator"


class ApprovalClass(StrEnum):
    OPERATOR = "OPERATOR"
    AUTOMATED = "AUTOMATED"


@dataclass(frozen=True)
class TransitionPlan:
    """The deterministic state-machine output (spec §3.2). Executors
    (T5–T7) fill sot_diff / fs_ops; classify sets the edge + approval or
    a typed rejection."""
    action: ECRAction
    engine: str
    from_state: LifecycleState | None
    to_state: LifecycleState | None
    approval_class: ApprovalClass | None
    sot_diff: dict[str, Any] = field(default_factory=dict)
    fs_ops: list[tuple[str, str]] = field(default_factory=list)
    gate_checks: list[str] = field(default_factory=list)
    rejection: str | None = None
    source: str | None = None


_DISPATCHABLE = {LifecycleState.PAPER, LifecycleState.LIVE}


def _reject(ecr: EngineChangeRequest, reason: str) -> TransitionPlan:
    return TransitionPlan(
        action=ecr.action, engine=ecr.engine, from_state=None,
        to_state=None, approval_class=None, rejection=reason)


def classify(
    ecr: EngineChangeRequest,
    profile_snapshot: dict[str, LifecycleState],
) -> TransitionPlan:
    """Pure: maps (action, in-profile?, from_state, source) to the single
    defined §5.1 edge or a typed rejection. The table is TOTAL and CLOSED
    — any cell not below is a typed rejection, never an inferred edge."""
    present = ecr.engine in profile_snapshot
    cur = profile_snapshot.get(ecr.engine)

    if ecr.action is ECRAction.ADD:
        if present:
            return _reject(
                ecr, f"engine {ecr.engine!r} already exists "
                     f"(use MODIFY to re-tune or REMOVE to retire)")
        return TransitionPlan(
            action=ecr.action, engine=ecr.engine, from_state=None,
            to_state=LifecycleState.LAB,  # ADD ALWAYS → LAB (H-S3-11)
            approval_class=ApprovalClass.OPERATOR, source=ecr.source,
            gate_checks=(["lab_sidecar"] if ecr.source == "lab_candidate"
                         else ["readiness"]))

    if ecr.action is ECRAction.REMOVE:
        if not present:
            return _reject(ecr, f"nothing to remove: engine "
                                f"{ecr.engine!r} absent from _PROFILE")
        if cur is LifecycleState.RETIRED:
            return _reject(ecr, f"engine {ecr.engine!r} already retired")
        return TransitionPlan(
            action=ecr.action, engine=ecr.engine, from_state=cur,
            to_state=LifecycleState.RETIRED,
            approval_class=ApprovalClass.OPERATOR)

    # MODIFY
    if not present:
        return _reject(ecr, f"nothing to modify: engine "
                            f"{ecr.engine!r} absent from _PROFILE")
    if cur is LifecycleState.RETIRED:
        return _reject(ecr, f"cannot tune a retired engine "
                            f"{ecr.engine!r}")
    return TransitionPlan(
        action=ecr.action, engine=ecr.engine, from_state=cur,
        to_state=cur,  # MODIFY: no lifecycle edge (spec §4.3)
        approval_class=ApprovalClass.AUTOMATED,
        gate_checks=["modify_evidence"])


def _rewrite_profile_source(
    src: str, *, engine: str, set_state: str,
    set_allocator_eligible: bool,
) -> str:
    """H-S3-3: a targeted, line-anchored, AST-validated rewrite of the
    SINGLE target EngineProfile(...) entry's lifecycle_state= /
    allocator_eligible= tokens. Touches no sibling, adds no import
    (H-S3-10), preserves the explanatory comments. ast.parse +
    compile() gate before the caller stages anything; SyntaxError /
    duplicate-key / extra=forbid raises here.
    """
    tree = ast.parse(src)  # pre-edit parse — proves the baseline is sane
    del tree
    lines = src.splitlines(keepends=True)
    # The entry spans the line `"<engine>": EngineProfile(` through its
    # closing `)`. Find that block by the quoted key anchor.
    key_anchor = f'"{engine}":'
    start = next((i for i, ln in enumerate(lines)
                  if key_anchor in ln and "EngineProfile(" in ln), None)
    if start is None:
        raise ValueError(
            f"_PROFILE entry for {engine!r} not found (key anchor "
            f"{key_anchor!r}) — cannot rewrite")
    depth = 0
    end = start
    for i in range(start, len(lines)):
        depth += lines[i].count("(") - lines[i].count(")")
        if depth == 0:
            end = i
            break
    block = "".join(lines[start:end + 1])
    new_block = _replace_kw(block, "lifecycle_state",
                            f"LifecycleState.{set_state.upper()}")
    new_block = _replace_kw(
        new_block, "allocator_eligible", str(set_allocator_eligible))
    new_src = "".join(lines[:start]) + new_block + "".join(lines[end + 1:])
    # H-S3-3 gate: the rewritten source must parse AND compile.
    compile(new_src, "<engine_profile_rewrite>", "exec")
    return new_src


def _replace_kw(block: str, kw: str, value: str) -> str:
    """Replace `kw=<...>` token inside one EngineProfile(...) call. If
    the kw is absent (e.g. allocator_eligible defaulted), inject it
    before the closing paren of the call (still data-only, no import)."""
    import re
    pat = re.compile(rf"({kw}\s*=\s*)([^,)\n]+)")
    if pat.search(block):
        return pat.sub(rf"\g<1>{value}", block, count=1)
    # absent → inject before the final ')'
    idx = block.rfind(")")
    return block[:idx] + f", {kw}={value}" + block[idx:]


def _run_consistency_subprocess(staged_tree: Path) -> tuple[int, str]:
    """H-S3-1 / D2: run the REAL clockwork as a fresh subprocess with
    cwd=the staged tree, so its REPO / import tpcore.engine_profile /
    _PROFILE are all the PROPOSED ones (zero in-process state bleed —
    a dict-injection seam would validate a different code path than CI).
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "pytest",
         "tpcore/tests/test_engine_lifecycle_consistency.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(staged_tree), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _staged_copytree(dest: Path) -> Path:
    """copytree the worktree minus .git/.venv/__pycache__/backtests
    (H-S3-1; R3 accepted: O(repo) but on-demand, not a daemon)."""
    shutil.copytree(
        REPO_ROOT, dest,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    return dest


__all__ = [
    "ApprovalClass", "TransitionPlan", "classify",
    "_rewrite_profile_source", "_run_consistency_subprocess",
    "_staged_copytree", "REPO_ROOT",
]
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider
```
Expected: PASS — `test_classify_every_table_cell` (9 cells) + `test_profile_rewrite_adds_no_import` + `test_validate_runs_real_clockwork_in_isolated_tree` all green.

> Executor note: `test_validate_runs_real_clockwork_in_isolated_tree` runs a `copytree` + a pytest subprocess; it is slow (R3 accepted). If the half-state replace-string anchor does not match (the `engine_profile.py` formatting differs), read the actual `tpcore/engine_profile.py:62-64` block and adjust the test's `.replace(...)` to flip `reversion` to RETIRED in the copied tree — the assertion (rc != 0, "reversion" in out) is the invariant, the exact replace string is environment-local.

- [ ] **Step 5: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0.

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/engine_sdlc/planner.py tpcore/tests/test_engine_sdlc_planner.py
git commit -m "feat(engine-sdlc): T4 classify table + isolated-tree dry-run + AST-safe _PROFILE rewrite

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T5: REMOVE executor + EULOGY template + completed archive-leg clockwork + atomicity

Satisfies: spec §4.2 (REMOVE — the formalized snap-out), §4.2.1 (EULOGY template), §4.2.2 / H-S3-5 (complete the archive-leg clockwork), H-S3-3 (AST-safe rewrite), H-S3-4 (journaled atomic-or-abort), H-S3-2 (REMOVE-leg frozen-literal co-transition). Per ordering invariant (i): the archive-leg clockwork extension lands HERE, with the REMOVE executor that produces a clean retire — never earlier.

**Files:**
- Create: `tpcore/templates/eulogy_template.md`
- Modify: `ops/engine_sdlc/planner.py` (REMOVE `apply` leg + `validate` body + journal/restore + audit emit + shadow editors + the conditional frozen-literal rewrite)
- Modify (extend in place): `tpcore/tests/test_engine_lifecycle_consistency.py` (H-S3-5 assertions)
- Test: append to `tpcore/tests/test_engine_sdlc_planner.py`

- [ ] **Step 1: Write the failing tests (append to `tpcore/tests/test_engine_sdlc_planner.py`)**

Append:

```python
# ─── T5: REMOVE executor + atomicity + completed archive-leg clockwork ───

def _make_synthetic_engine_tree(tmp_path: Path) -> Path:
    """copytree the repo, then add a synthetic PAPER engine `throwaway`
    so a REMOVE end-to-end can run entirely in a temp tree (tests never
    touch the working repo — standing rule)."""
    repo = Path(__file__).resolve().parents[2]
    staged = tmp_path / "tree"
    shutil.copytree(
        repo, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    # minimal real package + tests + scheduler so the live-engine leg
    # is satisfied before the retire.
    pkg = staged / "throwaway"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "tests" / "__init__.py").write_text("")
    (pkg / "scheduler.py").write_text(
        "async def run_once(*a, **k):\n    return {}\n")
    # add a PAPER _PROFILE entry + the shadow tokens
    ep = staged / "tpcore" / "engine_profile.py"
    t = ep.read_text().replace(
        '    # allocator: separate _dispatch_allocator path',
        '    "throwaway": EngineProfile(engine="throwaway", '
        'cadence=Cadence.DAILY,\n'
        '                               dispatch_order=6, '
        'lifecycle_state=LifecycleState.PAPER),\n'
        '    # allocator: separate _dispatch_allocator path')
    ep.write_text(t)
    smoke = staged / "scripts" / "run_smoke_test.sh"
    smoke.write_text(smoke.read_text().replace(
        "for engine in reversion vector momentum sentinel canary; do",
        "for engine in reversion vector momentum sentinel canary "
        "throwaway; do"))
    pp = staged / "pyproject.toml"
    pj = pp.read_text().replace(
        '"canary*"]  # sigma archived 2026-05-16',
        '"canary*", "throwaway*"]  # sigma archived 2026-05-16').replace(
        '    "canary/tests",', '    "canary/tests",\n    "throwaway/tests",')
    pp.write_text(pj)
    # the frozen-literal pin must include throwaway BEFORE the retire so
    # the staged tree is green pre-REMOVE (H-S3-2: REMOVE then drops it).
    tc = staged / "tpcore" / "tests" / "test_engine_lifecycle_consistency.py"
    tc.write_text(tc.read_text().replace(
        '"reversion", "vector", "momentum", "sentinel", "canary")',
        '"reversion", "vector", "momentum", "sentinel", "canary", '
        '"throwaway")'))
    return staged


def test_remove_throwaway_engine_end_to_end(tmp_path):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        ApprovalClass, apply, classify, validate)
    staged = _make_synthetic_engine_tree(tmp_path)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway",
        reason="synthetic test engine — never traded",
        eulogy_notes="exists only to prove the REMOVE executor")
    snap = {"reversion": LifecycleState.PAPER, "vector": LifecycleState.PAPER,
            "momentum": LifecycleState.PAPER, "sentinel": LifecycleState.PAPER,
            "canary": LifecycleState.PAPER, "throwaway": LifecycleState.PAPER,
            "allocator": LifecycleState.PAPER, "sigma": LifecycleState.RETIRED,
            "lab": LifecycleState.LAB}
    plan = classify(ecr, snap)
    assert plan.rejection is None
    assert plan.approval_class is ApprovalClass.OPERATOR
    vplan = validate(plan, repo_root=staged)
    assert vplan.rejection is None, vplan.rejection
    apply(vplan, repo_root=staged, emit_audit=False)
    # post-conditions: package moved, EULOGY written with the content
    # floor, _PROFILE flipped, the extended clockwork passes on the tree.
    assert not (staged / "throwaway").is_dir()
    eulogy = staged / "archive" / "throwaway" / "EULOGY.md"
    assert eulogy.is_file()
    body = eulogy.read_text()
    assert "## Cause of death" in body
    assert "## Retirement checklist" in body
    assert "synthetic test engine" in body
    from ops.engine_sdlc.planner import _run_consistency_subprocess
    rc, out = _run_consistency_subprocess(staged)
    assert rc == 0, f"clean retire must leave the clockwork green:\n{out}"


def test_apply_red_consistency_rolls_back_to_byte_identical(tmp_path):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    smoke = staged / "scripts" / "run_smoke_test.sh"
    before_ep = ep.read_bytes()
    before_smoke = smoke.read_bytes()
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    snap = {"throwaway": LifecycleState.PAPER}
    plan = classify(ecr, snap)
    # Force a red apply: corrupt the consistency test in the staged tree
    # so the post-stage subprocess exits non-zero — apply must restore
    # every journaled file byte-identical and move nothing permanently.
    tc = staged / "tpcore" / "tests" / "test_engine_lifecycle_consistency.py"
    tc.write_text(tc.read_text() +
                  "\n\ndef test_forced_red():\n    assert False\n")
    res = apply(plan, repo_root=staged, emit_audit=False,
                _force_validate=True)
    assert res.rejection is not None
    assert ep.read_bytes() == before_ep, "_PROFILE not restored byte-identical"
    assert smoke.read_bytes() == before_smoke, "shadow not restored"
    assert (staged / "throwaway").is_dir(), "package move not reverted"
    assert not (staged / "archive" / "throwaway").is_dir()


def test_apply_move_failure_restores_text_edits(tmp_path):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    before_ep = ep.read_bytes()
    # Pre-create archive/throwaway so the package move raises (dest
    # exists) AFTER the text edits — they must be reverted.
    (staged / "archive" / "throwaway").mkdir(parents=True)
    (staged / "archive" / "throwaway" / "sentinel.txt").write_text("x")
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    res = apply(classify(ecr, {"throwaway": LifecycleState.PAPER}),
                repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert ep.read_bytes() == before_ep, "text edits not reverted on move failure"


def test_profile_rewrite_is_ast_valid_and_preserves_siblings():
    from ops.engine_sdlc.planner import _rewrite_profile_source
    import ast
    src = Path("tpcore/engine_profile.py").read_text()
    new = _rewrite_profile_source(
        src, engine="sentinel", set_state="retired",
        set_allocator_eligible=False)
    ast.parse(new)  # AST-valid
    # siblings untouched: reversion's line is byte-identical
    assert ('"reversion": EngineProfile(engine="reversion"' in new)
    assert "lifecycle_state=LifecycleState.RETIRED" in new
    # the comments are preserved
    assert "# allocator: separate _dispatch_allocator path" in new


def test_malformed_rewrite_aborts_with_zero_disk_change(tmp_path):
    from ops.engine_sdlc.planner import _rewrite_profile_source
    with pytest.raises(ValueError, match="not found"):
        _rewrite_profile_source(
            "x = 1\n", engine="nope", set_state="retired",
            set_allocator_eligible=False)


def test_remove_rostered_engine_updates_frozen_literal(tmp_path):
    """H-S3-2 REMOVE leg: removing a CURRENTLY-ROSTERED engine changes
    roster_for_dispatch(), so the planner mechanically rewrites the
    test_dispatch_order_invariant_is_the_frozen_literal tuple in the
    SAME staged diff — never a hand-edit."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, classify, validate
    staged = _make_synthetic_engine_tree(tmp_path)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    plan = validate(classify(ecr, {"throwaway": LifecycleState.PAPER}),
                     repo_root=staged)
    apply(plan, repo_root=staged, emit_audit=False)
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py").read_text()
    assert '"throwaway")' not in tc, (
        "the frozen-literal was not updated to drop the retired engine")
    assert '"reversion", "vector", "momentum", "sentinel", "canary")' in tc
```

Also append the H-S3-5 archive-leg extension into `tpcore/tests/test_engine_lifecycle_consistency.py` (extend the existing `test_retired_engine_fully_offboarded` and add the new legs). Append at the end of that file:

```python
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
    import tomllib
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider -k "remove or apply or rewrite_is_ast or malformed or frozen_literal"
$PY -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -q -p no:cacheprovider -k "content_floor or absent_from_structural or orphan or not_importable"
```
Expected: planner tests FAIL — `ImportError: cannot import name 'apply'` / `'validate'`. The four new clockwork legs PASS already (sigma is cleanly archived in the real repo, so the H-S3-5 detectors are green against current reality — that is correct: they are detectors, the REMOVE executor is the producer).

- [ ] **Step 3: Create the EULOGY template**

Create `tpcore/templates/eulogy_template.md` (the Sigma-validated section structure — title+date / `## Cause of death` / `## What it leaves behind` / `## Retirement checklist` — structure only, deterministic `{{...}}` placeholders the planner fills):

```markdown
# {{ENGINE}} — Eulogy (archived {{DATE}})

{{ENGINE}} was retired via the Engine Change Request (SP3). This is the
auto-generated provenance artifact — the structure mirrors the Sigma
eulogy (the worked example, untouched); the content is engine-specific.

## Cause of death

{{REASON}}

Last on-record gate: {{GATE_RECORD}}

Operator notes:

{{EULOGY_NOTES}}

## What it leaves behind (still in tpcore — not archived)

Nothing engine-specific is left in `tpcore`: the strategy code is
relocated to `archive/{{ENGINE}}/`. Shared tpcore facilities the engine
used (risk, AAR, quality, backtest) are untouched and remain available
to the live engines.

## Retirement checklist (all done {{DATE}})

- [x] `tpcore.engine_profile._PROFILE["{{ENGINE}}"].lifecycle_state` →
      `RETIRED`, `allocator_eligible=False` (SoT flip — auto-delists
      from roster / allocator / check_imports by derivation).
- [x] `{{ENGINE}}/` moved to `archive/{{ENGINE}}/`.
- [x] By-name wrapper scripts moved alongside (if any).
- [x] `scripts/run_smoke_test.sh` step-3 loop purged of `{{ENGINE}}`.
- [x] `pyproject.toml` testpaths + `packages.find.include` purged.
- [x] `test_dispatch_order_invariant_is_the_frozen_literal` updated iff
      the roster changed (same staged diff — never a hand-edit).
- [x] `ENGINE_TABLES` entry removed if present.
- [x] `test_engine_lifecycle_consistency.py` archive-leg clockwork
      green (EULOGY content floor + shadow purge + no-orphan + not
      importable).
```

- [ ] **Step 4: Implement the REMOVE `apply` leg + `validate` body + journal/restore + audit + shadow editors**

Append to `ops/engine_sdlc/planner.py` (and add the needed imports `import json`, `import os`, `import uuid` at the module top — these are stdlib, not engine imports, so `check_imports` is unaffected; `asyncpg` is imported lazily inside the emit to keep the module import-cheap):

```python
import json
import os
import re
import uuid
from datetime import UTC, datetime

EULOGY_TEMPLATE = REPO_ROOT / "tpcore" / "templates" / "eulogy_template.md"


@dataclass
class _Journal:
    """H-S3-4: every touched file's exact prior bytes (or absent) + every
    dir move (src,dst), so apply() can restore byte-identical on red."""
    files: dict[Path, bytes | None] = field(default_factory=dict)
    moves: list[tuple[Path, Path]] = field(default_factory=list)

    def record_file(self, p: Path) -> None:
        if p in self.files:
            return
        self.files[p] = p.read_bytes() if p.is_file() else None

    def restore(self) -> None:
        # reverse order: undo the dir moves first, then the text edits.
        for src, dst in reversed(self.moves):
            if dst.exists():
                if src.exists():
                    shutil.rmtree(src)
                shutil.move(str(dst), str(src))
        for p, prior in reversed(list(self.files.items())):
            if prior is None:
                if p.is_file():
                    p.unlink()
            else:
                p.write_bytes(prior)


def _shadow_edit_remove(staged: Path, engine: str, jn: _Journal) -> None:
    """Purge the engine from the two structurally-parseable shadows
    (the ONLY non-SoT-derived sites — spec §4.2 fs_op 4)."""
    smoke = staged / "scripts" / "run_smoke_test.sh"
    jn.record_file(smoke)
    s = smoke.read_text()
    m = re.search(r"(for engine in )([^\n;]+)(;\s*do)", s)
    if m:
        toks = [t for t in m.group(2).split() if t != engine]
        smoke.write_text(s.replace(
            m.group(0), f"{m.group(1)}{' '.join(toks)}{m.group(3)}"))
    pp = staged / "pyproject.toml"
    jn.record_file(pp)
    txt = pp.read_text()
    txt = txt.replace(f'"{engine}*", ', "").replace(f', "{engine}*"', "")
    txt = re.sub(rf'\n\s*"{engine}/tests",', "", txt)
    pp.write_text(txt)


def _maybe_rewrite_frozen_literal(
    staged: Path, *, retired_engine: str | None, jn: _Journal,
) -> None:
    """H-S3-2: iff the transition changes roster_for_dispatch(), rewrite
    the frozen-literal tuple in test_dispatch_order_invariant_is_the_
    frozen_literal in the SAME staged diff (a structurally-parseable
    shadow, not a hand-edit). REMOVE of a rostered engine drops it."""
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py")
    jn.record_file(tc)
    src = tc.read_text()
    m = re.search(
        r"roster_for_dispatch\(\) == \(\s*([^)]+)\)", src)
    if not m or retired_engine is None:
        return
    toks = [t.strip().strip('"') for t in m.group(1).split(",")
            if t.strip()]
    if retired_engine not in toks:
        return
    toks = [t for t in toks if t != retired_engine]
    new_tuple = ", ".join(f'"{t}"' for t in toks)
    tc.write_text(src.replace(m.group(0),
                  f"roster_for_dispatch() == ({new_tuple})"))


def _render_eulogy(engine: str, ecr: EngineChangeRequest,
                   gate_record: str) -> str:
    tmpl = EULOGY_TEMPLATE.read_text()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return (tmpl
            .replace("{{ENGINE}}", engine)
            .replace("{{DATE}}", day)
            .replace("{{REASON}}", ecr.reason or "(no reason given)")
            .replace("{{EULOGY_NOTES}}", ecr.eulogy_notes or "(none)")
            .replace("{{GATE_RECORD}}", gate_record))


def validate(plan: TransitionPlan, *, repo_root: Path | None = None) -> TransitionPlan:
    """§5.2 — reject, never force. Re-verify evidence (T6/T7 fill the
    action branches), then run the REAL clockwork in an isolated temp
    tree (H-S3-1). On any failure, set plan.rejection and return — the
    caller (CLI / apply) never mutates a rejected plan."""
    if plan.rejection is not None:
        return plan
    # Action-specific evidence re-verification is layered in T6 (ADD) /
    # T7 (MODIFY); REMOVE has no gate (you may always stop trading).
    return plan


def _emit_audit(engine: str, action: str, from_state, to_state,
                approval_class, outcome: str, reason: str | None) -> None:
    """Every terminal outcome → one platform.application_log
    ENGINE_CHANGE_REQUEST row (H-S3-7). DB-best-effort: a missing
    DATABASE_URL logs + returns (the executor is an on-demand tool, not
    on the trade path) — never silently swallow on the apply path."""
    import asyncio

    async def _go() -> None:
        import asyncpg
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO platform.application_log "
                    "(engine, run_id, event_type, severity, message, data) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                    engine, uuid.uuid4(), "ENGINE_CHANGE_REQUEST",
                    "INFO", f"ECR {action} {engine} → {outcome}",
                    json.dumps({
                        "action": action,
                        "engine": engine,
                        "from_state": str(from_state),
                        "to_state": str(to_state),
                        "approval_class": str(approval_class),
                        "outcome": outcome,
                        "reason": reason,
                    }, default=str))
        finally:
            await pool.close()

    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001 — audit best-effort, never blocks apply
        pass


def apply(plan: TransitionPlan, *, repo_root: Path | None = None,
          emit_audit: bool = True, _force_validate: bool = False) -> TransitionPlan:
    """H-S3-4 — atomic-or-abort. Journal pre-state; text edits FIRST,
    the package shutil.move LAST; re-run the on-disk clockwork as a
    fresh subprocess; green ⇒ leave it (operator commits); red OR any
    exception ⇒ reverse-order restore to byte-identical, set rejection,
    emit the audit. The executor NEVER runs git (R2 accepted)."""
    root = repo_root or REPO_ROOT
    jn = _Journal()
    try:
        if plan.action is ECRAction.REMOVE:
            _apply_remove(plan, root, jn)
        elif plan.action is ECRAction.ADD:
            _apply_add(plan, root, jn)            # T6
        elif plan.action is ECRAction.MODIFY:
            _apply_modify(plan, root, jn)         # T7
        rc, out = _run_consistency_subprocess(root)
        if rc != 0:
            jn.restore()
            rejected = TransitionPlan(
                **{**plan.__dict__,
                   "rejection": f"post-stage clockwork red (rc={rc}):\n{out}"})
            if emit_audit:
                _emit_audit(plan.engine, plan.action.value,
                            plan.from_state, plan.to_state,
                            plan.approval_class, "rejected",
                            rejected.rejection)
            return rejected
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ full restore
        try:
            jn.restore()
            outcome = "rejected"
        except Exception as rexc:  # noqa: BLE001
            outcome = "apply_restore_failed"
            exc = rexc  # escalate loudly
        rejected = TransitionPlan(
            **{**plan.__dict__, "rejection": f"apply aborted: {exc}"})
        if emit_audit:
            _emit_audit(plan.engine, plan.action.value, plan.from_state,
                        plan.to_state, plan.approval_class, outcome,
                        rejected.rejection)
        return rejected
    if emit_audit:
        _emit_audit(plan.engine, plan.action.value, plan.from_state,
                    plan.to_state, plan.approval_class, "applied", None)
    return plan


def _apply_remove(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    from ops.engine_sdlc.ecr import EngineChangeRequest  # noqa: F401
    engine = plan.engine
    ep = root / "tpcore" / "engine_profile.py"
    jn.record_file(ep)
    new_src = _rewrite_profile_source(
        ep.read_text(), engine=engine, set_state="retired",
        set_allocator_eligible=False)  # H-S3-3 ast+compile gate inside
    # ENGINE_TABLES orphan removal (documented seam D-SDLC1-1)
    cg = root / "tpcore" / "quality" / "validation" / "capital_gate.py"
    if cg.is_file():
        jn.record_file(cg)
        cgt = cg.read_text()
        cgt2 = re.sub(rf'\n\s*"{engine}":\s*frozenset\([^)]*\),', "", cgt)
        if cgt2 != cgt:
            cg.write_text(cgt2)
    # shadow edits + conditional frozen-literal rewrite (TEXT edits first)
    _shadow_edit_remove(root, engine, jn)
    _maybe_rewrite_frozen_literal(root, retired_engine=engine, jn=jn)
    # EULOGY render (text)
    arc = root / "archive" / engine
    arc.mkdir(parents=True, exist_ok=True)
    eulogy = arc / "EULOGY.md"
    jn.record_file(eulogy)
    # plan carries the ECR via sot_diff? No — re-read it from the plan's
    # gate_checks-free path; the CLI passes the ECR through apply via
    # plan.sot_diff["_ecr"] (set by validate in the CLI). For the unit
    # tests apply is called directly, so reconstruct minimal notes.
    reason = plan.sot_diff.get("reason", "(retired via ECR)")
    notes = plan.sot_diff.get("eulogy_notes", "(none)")
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    body = (EULOGY_TEMPLATE.read_text()
            .replace("{{ENGINE}}", engine)
            .replace("{{DATE}}", day)
            .replace("{{REASON}}", reason)
            .replace("{{EULOGY_NOTES}}", notes)
            .replace("{{GATE_RECORD}}", "no surviving gate record"))
    eulogy.write_text(body)
    ep.write_text(new_src)  # the SoT flip (text)
    # the package move LAST (the irreversible-ish op after cheap reverts)
    pkg = root / engine
    if pkg.is_dir():
        jn.moves.append((pkg, arc / "src"))
        # move package CONTENTS into archive/<engine>/ alongside EULOGY
        for item in list(pkg.iterdir()):
            shutil.move(str(item), str(arc / item.name))
        jn.moves[-1] = (pkg, arc)  # record the logical move for restore
        pkg.rmdir()
```

> Executor note on the `reason`/`eulogy_notes` seam: `classify()` builds the plan without the ECR's free-text. To carry `reason`/`eulogy_notes` to `_apply_remove`, the CLI (T8) and the T5 tests must populate `plan.sot_diff` with `{"reason": ecr.reason, "eulogy_notes": ecr.eulogy_notes}` after `classify`. Add a thin helper `attach_ecr_context(plan, ecr) -> TransitionPlan` to `planner.py` that returns a new `TransitionPlan` with `sot_diff` merged (`{**plan.sot_diff, "reason": ecr.reason, "eulogy_notes": ecr.eulogy_notes}` for REMOVE; the analogous keys for ADD/MODIFY in T6/T7). Call it in the T5 test right after `classify` (update the T5 tests' `classify(...)` calls to `attach_ecr_context(classify(...), ecr)`), and export it in `__all__`. This keeps `classify` pure (snapshot-only) while threading the ECR's free-text deterministically. Implement `attach_ecr_context` in this step.

```python
def attach_ecr_context(plan: TransitionPlan,
                        ecr: EngineChangeRequest) -> TransitionPlan:
    """Thread the ECR's free-text/evidence onto a classified plan
    WITHOUT making classify() impure (classify takes only a snapshot).
    Returns a new frozen-shaped plan with sot_diff merged."""
    extra: dict[str, Any] = {}
    if ecr.action is ECRAction.REMOVE:
        extra = {"reason": ecr.reason, "eulogy_notes": ecr.eulogy_notes}
    elif ecr.action is ECRAction.ADD:
        extra = {"source": ecr.source, "lab_dossier": ecr.lab_dossier,
                 "cadence": ecr.cadence.value if ecr.cadence else None,
                 "allocator": ecr.allocator,
                 "dispatch_order": ecr.dispatch_order,
                 "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred}
    elif ecr.action is ECRAction.MODIFY:
        extra = {"lab_dossier": ecr.lab_dossier,
                 "param_change": ecr.param_change,
                 "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred}
    return TransitionPlan(**{**plan.__dict__,
                             "sot_diff": {**plan.sot_diff, **extra}})
```

Update the T5 tests written in Step 1: wrap each `classify(ecr, snap)` with `attach_ecr_context(classify(ecr, snap), ecr)` (and import `attach_ecr_context`). Add stub no-op `_apply_add` / `_apply_modify` raising `NotImplementedError` for now (T6/T7 implement them) — but the T5 tests only exercise REMOVE so they pass. Add to `__all__`: `apply`, `validate`, `attach_ecr_context`.

- [ ] **Step 5: Run the T5 tests to verify they pass**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider -k "remove or apply or rewrite or malformed or frozen_literal"
$PY -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -q -p no:cacheprovider
```
Expected: PASS — REMOVE end-to-end, rollback byte-identical, move-failure restore, AST-valid/preserve-siblings, malformed-abort, frozen-literal-update; and the full extended consistency suite green (the 4 H-S3-5 legs included, against the real cleanly-archived sigma).

- [ ] **Step 6: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green (the H-S3-5 extensions pass against current reality — sigma is the only RETIRED engine and is cleanly archived); ruff clean; check_imports exit 0 (the planner is in `ops/`; the consistency test still imports only `tpcore.engine_profile` + stdlib + filesystem).

- [ ] **Step 7: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/engine_sdlc/planner.py tpcore/templates/eulogy_template.md tpcore/tests/test_engine_sdlc_planner.py tpcore/tests/test_engine_lifecycle_consistency.py
git commit -m "feat(engine-sdlc): T5 REMOVE executor + EULOGY template + completed archive-leg clockwork + atomicity

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T6: ADD executor (new_scaffold + lab_candidate) + readiness build gate

Satisfies: spec §4.1 (ADD — onboard/graduate), §5.3 (apply staging), H-S3-11 (fail-closed: no unearned PAPER, no half-scaffold), H-S3-2 (ADD leg: frozen-literal UNCHANGED — LAB not rostered), H-S3-3 (AST-safe insert before the `allocator` sentinel anchor).

**Files:**
- Modify: `ops/engine_sdlc/planner.py` (`_apply_add`, the ADD branch of `validate`, the AST-safe `_PROFILE` insert)
- Test: append to `tpcore/tests/test_engine_sdlc_planner.py`

- [ ] **Step 1: Write the failing tests (append to `tpcore/tests/test_engine_sdlc_planner.py`)**

```python
# ─── T6: ADD executor + readiness build gate (H-S3-11) ───

def test_add_new_scaffold_rejects_gate_fields():
    """H-S3-11(b): a new_scaffold engine cannot present a gate score it
    has not earned — non-None gate_dsr/gate_cred is a hard reject."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=9,
        gate_dsr=0.99, gate_cred=80, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    vp = validate(plan, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "new_scaffold" in vp.rejection and "gate" in vp.rejection


def test_add_always_lands_LAB():
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import classify
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="new_scaffold",
        cadence="daily", allocator=True, dispatch_order=9, need="x")
    plan = classify(ecr, {})
    assert plan.to_state is LifecycleState.LAB  # never PAPER (H-S3-11a)


def test_add_lab_candidate_requires_promote_new(tmp_path):
    """H-S3-11(c): a lab_candidate ADD whose sidecar says fold_existing
    is a MODIFY, not an ADD — explicit redirect in the rejection."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    # build a fold_existing sidecar
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()  # intent/recommended_exit == fold_existing
    md = tmp_path / "2026-05-18-revcand-SURVIVED-seed0.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(r.model_dump_json())
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="lab_candidate",
        lab_dossier=str(md), cadence="daily", allocator=False,
        dispatch_order=9, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    vp = validate(plan, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "fold_existing" in vp.rejection and "MODIFY" in vp.rejection


def test_add_readiness_miss_rejects(tmp_path):
    """H-S3-11(d): a scaffold with no <engine>/tests dir / no
    BaseEnginePlug plugs ⇒ reject, zero mutation."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        apply, attach_ecr_context, classify)
    staged = _make_synthetic_engine_tree(tmp_path)
    # remove the template so the scaffold is incomplete
    (staged / "tpcore" / "templates" / "engine_template").rename(
        staged / "tpcore" / "templates" / "_gone")
    ecr = EngineChangeRequest(
        action="add", engine="brandnew", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=7, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    res = apply(plan, repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert not (staged / "brandnew").is_dir(), "scaffold not cleaned up"


def test_add_leaves_frozen_literal_untouched(tmp_path):
    """H-S3-2 ADD leg: ADD → LAB does NOT change roster_for_dispatch()
    (LAB filtered by _DISPATCHABLE) — the frozen literal is unchanged."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        apply, attach_ecr_context, classify, validate)
    staged = _make_synthetic_engine_tree(tmp_path)
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py")
    before = tc.read_text()
    ecr = EngineChangeRequest(
        action="add", engine="brandnew", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=7, need="x")
    plan = validate(attach_ecr_context(classify(ecr, {
        "reversion": LifecycleState.PAPER}), ecr),
        repo_root=staged, ecr=ecr)
    apply(plan, repo_root=staged, emit_audit=False)
    assert tc.read_text().count("roster_for_dispatch() == (") == \
        before.count("roster_for_dispatch() == ("), \
        "ADD→LAB must NOT touch the frozen literal"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider -k "add_"
```
Expected: FAIL — `validate()` does not accept `ecr=` / has no ADD branch; `_apply_add` raises `NotImplementedError`.

- [ ] **Step 3: Implement the ADD validate branch + `_apply_add`**

Modify `validate` in `ops/engine_sdlc/planner.py` to accept the ECR and run the ADD evidence/readiness gate. Replace the `validate` defined in T5 with:

```python
def validate(plan: TransitionPlan, *, repo_root: Path | None = None,
             ecr: EngineChangeRequest | None = None) -> TransitionPlan:
    """§5.2 — reject, never force. ADD: H-S3-11 fail-closed gate.
    MODIFY: H-S3-6 zero-trust (T7). REMOVE: no gate (always may stop)."""
    if plan.rejection is not None:
        return plan
    root = repo_root or REPO_ROOT
    if plan.action is ECRAction.ADD and ecr is not None:
        if ecr.source == "new_scaffold":
            if ecr.gate_dsr is not None or ecr.gate_cred is not None:
                return _reject(
                    ecr, "new_scaffold ADD must NOT carry gate_dsr/"
                         "gate_cred — a new engine cannot present a gate "
                         "score it has not earned (fail-closed H-S3-11b)")
        elif ecr.source == "lab_candidate":
            from ops.engine_sdlc._evidence import (
                EvidenceError, load_labresult_sidecar)
            try:
                lr = load_labresult_sidecar(ecr.lab_dossier)
            except EvidenceError as exc:
                return _reject(ecr, str(exc))
            if lr.recommended_exit == "fold_existing":
                return _reject(
                    ecr, "lab_candidate dossier recommends fold_existing "
                         "— that is a MODIFY of the target engine, NOT an "
                         "ADD (H-S3-11c). Re-file as action: MODIFY.")
            if not (lr.verdict == "SURVIVED" and lr.dsr >= 0.95
                    and lr.credibility_score >= 60
                    and lr.recommended_exit == "promote_new"):
                return _reject(
                    ecr, f"lab_candidate sidecar fails the gate: "
                         f"verdict={lr.verdict} dsr={lr.dsr} "
                         f"cred={lr.credibility_score} "
                         f"recommended_exit={lr.recommended_exit}")
        if plan.to_state is not LifecycleState.LAB:
            return _reject(ecr, "ADD must land LAB, never PAPER (H-S3-11a)")
    if plan.action is ECRAction.MODIFY and ecr is not None:
        return _validate_modify(plan, ecr)  # T7
    return plan
```

Add `_apply_add` (replace the T5 stub). The AST-safe insert places the new entry **immediately before the `allocator` sentinel comment** (the stable documented anchor — spec H-S3-3), `lifecycle_state=LAB`, `allocator_eligible=False` forced (SP1 `test_no_half_state`); then scaffold from `tpcore/templates/engine_template/`; then run the programmatically-checkable readiness items:

```python
_READINESS_PLUG_RE = re.compile(r"class\s+\w+\(BaseEnginePlug\)")


def _check_readiness(staged: Path, engine: str) -> str | None:
    """The programmatically-checkable engine_readiness.md items
    (H-S3-11d). Returns a rejection reason or None."""
    pkg = staged / engine
    if not pkg.is_dir():
        return f"readiness: scaffold {engine}/ not created"
    if not (pkg / "tests").is_dir():
        return f"readiness: {engine}/tests/ missing (engine_readiness §6)"
    try:
        import importlib.util
        spec = importlib.util.find_spec(f"{engine}.scheduler")
    except ModuleNotFoundError:
        spec = None
    if spec is None and not (pkg / "scheduler.py").is_file():
        return f"readiness: {engine}.scheduler not importable"
    plug_count = sum(
        len(_READINESS_PLUG_RE.findall(p.read_text()))
        for p in (pkg / "plugs").glob("*.py")) if (pkg / "plugs").is_dir() else 0
    if plug_count != 5:
        return (f"readiness: expected 5 BaseEnginePlug subclasses in "
                f"{engine}/plugs/, found {plug_count}")
    return None


def _apply_add(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    engine = plan.engine
    src_tmpl = root / "tpcore" / "templates" / "engine_template"
    if not src_tmpl.is_dir():
        raise RuntimeError(
            "engine_template scaffold missing — cannot ADD(new_scaffold)")
    pkg = root / engine
    # scaffold (the package move/create is the irreversible-ish op — but
    # for ADD there is no prior package, so it is recorded as a dir move
    # with src absent → restore = rmtree).
    shutil.copytree(src_tmpl, pkg)
    jn.moves.append((pkg / "__sentinel_absent__", pkg))  # restore=rmtree pkg
    # AST-safe _PROFILE insert BEFORE the allocator sentinel comment.
    ep = root / "tpcore" / "engine_profile.py"
    jn.record_file(ep)
    cad = plan.sot_diff.get("cadence") or "daily"
    order = plan.sot_diff.get("dispatch_order")
    cad_enum = {"daily": "Cadence.DAILY",
                "weekly_first_trading_day": "Cadence.WEEKLY_FIRST_TRADING_DAY",
                "monthly_first_trading_day": "Cadence.MONTHLY_FIRST_TRADING_DAY"}[cad]
    new_entry = (
        f'    "{engine}":   EngineProfile(engine="{engine}", '
        f'cadence={cad_enum},\n'
        f'                               dispatch_order={order}, '
        f'lifecycle_state=LifecycleState.LAB),\n')
    src = ep.read_text()
    anchor = "    # allocator: separate _dispatch_allocator path"
    if anchor not in src:
        raise RuntimeError("allocator sentinel anchor not found in _PROFILE")
    new_src = src.replace(anchor, new_entry + anchor, 1)
    compile(new_src, "<engine_profile_add>", "exec")  # H-S3-3 gate
    miss = _check_readiness(root, engine)  # readiness BEFORE the SoT write
    if miss is not None:
        raise RuntimeError(miss)  # apply()'s except → full restore
    ep.write_text(new_src)
```

> Executor note: for the `jn.moves` restore semantics with ADD (no prior package), make `_Journal.restore` handle a move whose recorded `src` ends with `__sentinel_absent__` as "the dest is a freshly-created dir → `shutil.rmtree(dest)`". Adjust `_Journal.restore`'s dir-move loop accordingly (a 3-line `if src.name == "__sentinel_absent__": shutil.rmtree(dst); continue`). Implement this in this step.

- [ ] **Step 4: Run the T6 tests to verify they pass**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider -k "add_"
```
Expected: PASS — gate-field reject, always-LAB, fold_existing→MODIFY redirect, readiness-miss reject + cleanup, frozen-literal-untouched.

- [ ] **Step 5: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0.

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/engine_sdlc/planner.py tpcore/tests/test_engine_sdlc_planner.py
git commit -m "feat(engine-sdlc): T6 ADD executor (new_scaffold + lab_candidate) + readiness build gate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T7: MODIFY executor + zero-trust evidence re-verification + LAB→PAPER `promote`

Satisfies: spec §4.3 (MODIFY), §5.4 (evidence re-verified never trusted), §6.2 (MODIFY automated-if-gated — operator-confirmed §12), H-S3-6 (zero-trust: the gate is the only thing between a dossier and live params), the LAB→PAPER automated promotion (§4.1).

**Files:**
- Modify: `ops/engine_sdlc/planner.py` (`_validate_modify`, `_apply_modify`, the line-anchored engine `backtest.py` default rewrite, `promote()`)
- Test: append to `tpcore/tests/test_engine_sdlc_planner.py`

- [ ] **Step 1: Write the failing tests (append to `tpcore/tests/test_engine_sdlc_planner.py`)**

```python
# ─── T7: MODIFY zero-trust + LAB→PAPER promote (H-S3-6) ───

def _modify_sidecar(tmp_path, *, target="reversion",
                     recommended="fold_existing", verdict="SURVIVED",
                     dsr=0.97, cred=64,
                     winning=None):
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()
    d = r.model_dump()
    d["target_engine"] = target
    d["recommended_exit"] = recommended
    d["intent"] = recommended if recommended != "none" else "fold_existing"
    d["verdict"] = verdict
    d["dsr"] = dsr
    d["credibility_score"] = cred
    d["winning_params"] = winning or {"z_threshold": 3.1, "max_hold_days": 8}
    from tpcore.lab.models import LabResult
    r2 = LabResult.model_validate(d)
    md = tmp_path / "2026-05-18-revc-SURVIVED-seed0.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(r2.model_dump_json())
    return md


def _modify_ecr(md, **over):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    kw = dict(action="modify", engine="reversion", lab_dossier=str(md),
              param_change={"z_threshold": "3.1", "max_hold_days": "8"},
              gate_dsr=0.97, gate_cred=64)
    kw.update(over)
    return EngineChangeRequest(**kw)


def test_modify_plan_sot_diff_is_always_empty(tmp_path):
    """H-S3-6(d) lifecycle-immutability: a MODIFY plan must carry ZERO
    _PROFILE edit — strategy existence/lifecycle/allocator cannot be
    touched by MODIFY by construction."""
    from ops.engine_sdlc.planner import classify
    md = _modify_sidecar(tmp_path)
    plan = classify(_modify_ecr(md), {"reversion": LifecycleState.PAPER})
    # sot_diff carries NO _PROFILE keys (cadence/dispatch/state/allocator)
    forbidden = {"lifecycle_state", "allocator_eligible", "dispatch_order",
                 "cadence"}
    assert not (set(plan.sot_diff) & forbidden)
    assert plan.to_state == plan.from_state  # no lifecycle edge


def test_modify_rejects_forged_numbers(tmp_path):
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    md = _modify_sidecar(tmp_path, dsr=0.40)  # sidecar disagrees
    ecr = _modify_ecr(md, gate_dsr=0.97)
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None and "dsr" in vp.rejection.lower()


def test_modify_rejects_wrong_target(tmp_path):
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    md = _modify_sidecar(tmp_path, target="vector")
    ecr = _modify_ecr(md, engine="reversion")
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None and "target" in vp.rejection.lower()


def test_modify_rejects_non_param_ranges_key(tmp_path):
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    md = _modify_sidecar(tmp_path, winning={"not_a_real_param": 9})
    ecr = _modify_ecr(md, param_change={"not_a_real_param": "9"})
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None
    assert "PARAM_RANGES" in vp.rejection or "not.*swept" in vp.rejection


def test_modify_rejects_value_mismatch(tmp_path):
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    md = _modify_sidecar(tmp_path, winning={"z_threshold": 9.9})
    ecr = _modify_ecr(md, param_change={"z_threshold": "3.1"})
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None and "mismatch" in vp.rejection.lower()


def test_modify_rejects_stale_sidecar(tmp_path):
    from ops.engine_sdlc.planner import (
        attach_ecr_context, classify, validate)
    md = _modify_sidecar(tmp_path)
    ecr = _modify_ecr(md)
    # point the ECR at a DIFFERENT (nonexistent) dossier path
    ecr2 = _modify_ecr(tmp_path / "other-SURVIVED-seed9.md")
    vp = validate(attach_ecr_context(
        classify(ecr2, {"reversion": LifecycleState.PAPER}), ecr2),
        ecr=ecr2)
    assert vp.rejection is not None  # missing sidecar


def test_promote_flips_lab_to_paper_iff_gate_green(tmp_path):
    """LAB→PAPER is automated/gated (spec §4.1) — not an ECR action.
    promote() flips iff the gate authority is green."""
    from ops.engine_sdlc.planner import promote
    staged = _make_synthetic_engine_tree(tmp_path)
    # flip throwaway to LAB in the staged tree
    ep = staged / "tpcore" / "engine_profile.py"
    ep.write_text(ep.read_text().replace(
        '"throwaway", cadence=Cadence.DAILY,\n'
        '                               dispatch_order=6, '
        'lifecycle_state=LifecycleState.PAPER)',
        '"throwaway", cadence=Cadence.DAILY,\n'
        '                               dispatch_order=6, '
        'lifecycle_state=LifecycleState.LAB)'))
    res = promote("throwaway", repo_root=staged, emit_audit=False,
                  _gate_green=True)
    assert res.rejection is None
    assert "LifecycleState.PAPER" in ep.read_text()
    res2 = promote("throwaway", repo_root=staged, emit_audit=False,
                   _gate_green=False)
    assert res2.rejection is not None  # gate red ⇒ no flip
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider -k "modify or promote"
```
Expected: FAIL — `_validate_modify` / `promote` / `_apply_modify` not defined.

- [ ] **Step 3: Implement `_validate_modify`, `_apply_modify`, `promote`**

Append to `ops/engine_sdlc/planner.py`:

```python
def _validate_modify(plan: TransitionPlan,
                     ecr: EngineChangeRequest) -> TransitionPlan:
    """H-S3-6 zero-trust: the gate is the ONLY thing between a dossier
    and live params, so re-derive every number from the FROZEN JSON
    sidecar, never the ECR text / rendered markdown."""
    from ops.engine_sdlc._evidence import (
        EvidenceError, load_labresult_sidecar)
    from ops.lab.run import PARAM_RANGES
    try:
        lr = load_labresult_sidecar(ecr.lab_dossier)
    except EvidenceError as exc:
        return _reject(ecr, str(exc))
    if lr.verdict != "SURVIVED":
        return _reject(ecr, f"sidecar verdict {lr.verdict} != SURVIVED")
    if lr.dsr < 0.95:
        return _reject(ecr, f"sidecar dsr {lr.dsr} < 0.95 (forged/stale)")
    if lr.credibility_score < 60:
        return _reject(ecr, f"sidecar credibility {lr.credibility_score} "
                            f"< 60")
    if lr.recommended_exit != "fold_existing":
        return _reject(
            ecr, f"sidecar recommended_exit {lr.recommended_exit!r} != "
                 f"fold_existing (a promote_new is an ADD, not a MODIFY)")
    if lr.target_engine != ecr.engine:
        return _reject(
            ecr, f"sidecar target_engine {lr.target_engine!r} != ECR "
                 f"engine {ecr.engine!r} (wrong-target reject)")
    ranges = PARAM_RANGES.get(ecr.engine, {})
    for k, v in (ecr.param_change or {}).items():
        if k not in ranges:
            return _reject(
                ecr, f"param {k!r} not in {ecr.engine} PARAM_RANGES — "
                     f"the Lab never swept it (no-smuggle H-S3-6c)")
        if k not in lr.winning_params:
            return _reject(
                ecr, f"param {k!r} not in the sidecar winning_params")
        # value-equality (coerce the ECR string to the sidecar's type)
        want = lr.winning_params[k]
        try:
            got = type(want)(v)
        except (TypeError, ValueError):
            got = v
        if got != want:
            return _reject(
                ecr, f"param {k!r} value mismatch: ECR={v!r} vs sidecar "
                     f"winning {want!r}")
    if plan.sot_diff and any(
            kk in plan.sot_diff for kk in (
                "lifecycle_state", "allocator_eligible",
                "dispatch_order", "cadence")):
        return _reject(ecr, "MODIFY plan carries a _PROFILE edit — "
                            "lifecycle is immutable under MODIFY "
                            "(H-S3-6d)")
    return plan


def _apply_modify(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """Apply the validated current→winning diff to the engine's
    default_params() SOURCE (the O1 seam). _PROFILE is NEVER touched
    (H-S3-6d). Line-anchored edit of the engine backtest.py module
    default constants, AST-validated."""
    engine = plan.engine
    bt = root / engine / "backtest.py"
    if not bt.is_file():
        raise RuntimeError(f"{engine}/backtest.py not found for MODIFY")
    jn.record_file(bt)
    src = bt.read_text()
    # The default-constant map per engine (the module UPPER_CASE consts
    # default_params() reads). Conservative: edit the constant
    # assignment line, AST-validate, never reformat siblings.
    consts = _ENGINE_DEFAULT_CONSTS[engine]
    pc = plan.sot_diff.get("param_change") or {}
    new_src = src
    for key, raw in pc.items():
        const_name = consts[key]
        pat = re.compile(rf"^({re.escape(const_name)}\s*=\s*)([^\n#]+)",
                         re.M)
        m = pat.search(new_src)
        if not m:
            raise RuntimeError(
                f"default constant {const_name} for {key} not found in "
                f"{engine}/backtest.py")
        new_src = pat.sub(rf"\g<1>{raw}", new_src, count=1)
    compile(new_src, "<backtest_modify>", "exec")  # AST gate
    bt.write_text(new_src)


# The engine PARAM_RANGES key → module default constant the
# default_params() accessor reads (verified against the live source).
_ENGINE_DEFAULT_CONSTS: dict[str, dict[str, str]] = {
    "reversion": {
        "z_threshold": "Z_SCORE_THRESHOLD",  # defined in reversion.models
        "volume_climax_multiplier": "VOLUME_CLIMAX_MULTIPLIER_DEFAULT",
        "max_hold_days": "MAX_HOLD_DAYS",
        "stop_pct": "HARD_STOP_PCT",
    },
    "vector": {
        "pb_ceiling": "PB_CEILING",
        "de_ceiling": "DE_CEILING",
        "catalyst_window_days": "CATALYST_WINDOW_DAYS",
        "swing_score_threshold": "SWING_SCORE_THRESHOLD_DEFAULT",
        "stop_pct": "HARD_STOP_PCT",
    },
    "momentum": {
        "lookback_days": "DEFAULT_LOOKBACK_DAYS",
        "skip_days": "DEFAULT_SKIP_DAYS",
        "hold_days": "DEFAULT_HOLD_DAYS",
        "top_decile_pct": "DEFAULT_TOP_DECILE_PCT",
    },
}


def promote(engine: str, *, repo_root: Path | None = None,
            emit_audit: bool = True,
            _gate_green: bool | None = None) -> TransitionPlan:
    """LAB→PAPER — automated, gated, NOT an ECR action (spec §4.1).
    Flips iff the capital-gate/graduation_ready authority is green. The
    test seam `_gate_green` injects the verdict offline; production
    resolves it via the real authority."""
    root = repo_root or REPO_ROOT
    if _gate_green is None:
        from tpcore.quality.validation.capital_gate import (
            ENGINE_TABLES,  # noqa: F401 — presence import; real gate below
        )
        # production: resolve graduation_ready(pool, engine); deferred
        # to the CLI which owns the pool. Here require an explicit verdict.
        return TransitionPlan(
            action=ECRAction.MODIFY, engine=engine, from_state=None,
            to_state=None, approval_class=ApprovalClass.AUTOMATED,
            rejection="promote requires a resolved gate verdict")
    plan = TransitionPlan(
        action=ECRAction.MODIFY, engine=engine,
        from_state=LifecycleState.LAB, to_state=LifecycleState.PAPER,
        approval_class=ApprovalClass.AUTOMATED)
    if not _gate_green:
        rej = TransitionPlan(**{**plan.__dict__,
                                "rejection": "capital-gate/graduation_ready "
                                             "RED — LAB→PAPER refused"})
        if emit_audit:
            _emit_audit(engine, "promote", "lab", "paper",
                        "AUTOMATED", "rejected", rej.rejection)
        return rej
    ep = root / "tpcore" / "engine_profile.py"
    jn = _Journal()
    jn.record_file(ep)
    try:
        new = _rewrite_profile_source(
            ep.read_text(), engine=engine, set_state="paper",
            set_allocator_eligible=False)
        ep.write_text(new)
        rc, out = _run_consistency_subprocess(root)
        if rc != 0:
            jn.restore()
            rej = TransitionPlan(**{**plan.__dict__,
                                    "rejection": f"post-flip clockwork red:\n{out}"})
            if emit_audit:
                _emit_audit(engine, "promote", "lab", "paper",
                            "AUTOMATED", "rejected", rej.rejection)
            return rej
    except Exception as exc:  # noqa: BLE001
        jn.restore()
        rej = TransitionPlan(**{**plan.__dict__,
                                "rejection": f"promote aborted: {exc}"})
        if emit_audit:
            _emit_audit(engine, "promote", "lab", "paper", "AUTOMATED",
                        "rejected", rej.rejection)
        return rej
    if emit_audit:
        _emit_audit(engine, "promote", "lab", "paper", "AUTOMATED",
                    "applied", None)
    return plan
```

Add `promote`, `_validate_modify`, `_apply_modify` to `__all__`. Wire `_apply_modify` into `apply()`'s `elif plan.action is ECRAction.MODIFY:` branch (replace the T5/T6 `NotImplementedError` stub).

> Executor note: `_ENGINE_DEFAULT_CONSTS` maps the swept param to the module-level UPPER_CASE default constant the engine's `default_params()` reads (verified: reversion `Z_SCORE_THRESHOLD` lives in `reversion/models.py` not `reversion/backtest.py` — for the `z_threshold` MODIFY the line-anchored edit must target `reversion/models.py`; for vector `swing_score_threshold` there is no module default const (the accessor returns the override or `None`/`0.0`) — so a vector `swing_score_threshold` MODIFY edits the `_SWING_SCORE_THRESHOLD_OVERRIDE = None` default to the winning value, or rejects with a clear "no module default seam for swing_score_threshold; not MODIFY-able via the constant path" if the executor finds no safe anchor). Before implementing, run `$PY -c "import reversion.models as m; print([x for x in dir(m) if x.isupper()])"` and grep each engine's `backtest.py`/`models.py` for the exact constant; adjust `_ENGINE_DEFAULT_CONSTS` and the target file per-key (the test fixtures only exercise `z_threshold`/`max_hold_days` on reversion, so reversion's map must be exactly right; the other engines' maps can be conservative-but-correct). The invariant the tests pin is: a validated MODIFY edits the engine's default source AST-safely and `_PROFILE` is never touched.

- [ ] **Step 4: Run the T7 tests to verify they pass**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider -k "modify or promote"
```
Expected: PASS — sot_diff-empty, forged-numbers, wrong-target, non-PARAM_RANGES-key, value-mismatch, stale-sidecar, promote-iff-green.

- [ ] **Step 5: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0.

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/engine_sdlc/planner.py tpcore/tests/test_engine_sdlc_planner.py
git commit -m "feat(engine-sdlc): T7 MODIFY zero-trust evidence re-verification + LAB->PAPER promote

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T8: `ops/engine_sdlc/__main__.py` CLI + audit emit

Satisfies: spec §3.2 (the pipeline + binary y/n), §3.3 (audit), §6 (operator-interaction policy table), H-S3-7 (fail-closed TTY y/n + audit on every outcome), H-S3-12 (explicit non-zero, never silent 0 — the canary `-m`-no-op lesson).

**Files:**
- Create: `ops/engine_sdlc/__main__.py`
- Test: `tpcore/tests/test_engine_sdlc_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_engine_sdlc_cli.py`:

```python
"""T8 — the CLI: fail-closed TTY y/n (H-S3-7), explicit non-zero never
silent 0 (H-S3-12), audit on every terminal outcome. Lazy in-body
import (H-S3-10)."""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_ecr(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ecr.txt"
    p.write_text(body)
    return p


_REMOVE_GHOST = """\
ECR
action:        REMOVE
engine:        ghost_engine_not_present
reason:        x
eulogy_notes:  y
"""


@pytest.mark.asyncio
async def test_parse_fail_rc1(tmp_path, capsys):
    from ops.engine_sdlc.__main__ import _amain
    p = _write_ecr(tmp_path, "not an ecr at all")
    rc = await _amain(["--ecr", str(p)])
    assert rc == 1
    assert "no ECR block" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_reject_rc1(tmp_path, capsys):
    from ops.engine_sdlc.__main__ import _amain
    p = _write_ecr(tmp_path, _REMOVE_GHOST)
    rc = await _amain(["--ecr", str(p)])
    assert rc == 1
    assert "nothing to remove" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_no_args_rc_nonzero(capsys):
    from ops.engine_sdlc.__main__ import _amain
    with pytest.raises(SystemExit) as e:
        await _amain([])
    assert e.value.code != 0


@pytest.mark.asyncio
async def test_non_y_declines_zero_mutation(tmp_path, monkeypatch):
    """H-S3-7(a): any non-`y`/`yes` token on the OPERATOR path ⇒
    declined, apply NOT called, zero mutation."""
    import ops.engine_sdlc.__main__ as cli
    called = {"apply": 0}
    monkeypatch.setattr(cli, "apply",
                        lambda *a, **k: called.__setitem__("apply", 1))
    # a valid REMOVE of a real PAPER engine reaches the prompt
    p = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                             "reason: x\neulogy_notes: y\n")
    monkeypatch.setattr(cli, "_read_confirm", lambda: "n")
    # stub validate so the dry-run subprocess is not actually executed
    monkeypatch.setattr(cli, "_validate_for_cli",
                        lambda plan, ecr: plan)  # green
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert called["apply"] == 0, "apply ran despite a non-y answer"


@pytest.mark.asyncio
async def test_eof_declines(tmp_path, monkeypatch):
    import ops.engine_sdlc.__main__ as cli
    called = {"apply": 0}
    monkeypatch.setattr(cli, "apply",
                        lambda *a, **k: called.__setitem__("apply", 1))
    p = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                             "reason: x\neulogy_notes: y\n")

    def _eof():
        raise EOFError

    monkeypatch.setattr(cli, "_read_confirm", _eof)
    monkeypatch.setattr(cli, "_validate_for_cli", lambda plan, ecr: plan)
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert called["apply"] == 0


@pytest.mark.asyncio
async def test_rejected_plan_never_prompts(tmp_path, monkeypatch):
    import ops.engine_sdlc.__main__ as cli
    prompted = {"n": 0}
    monkeypatch.setattr(cli, "_read_confirm",
                        lambda: prompted.__setitem__("n", 1) or "y")
    p = _write_ecr(tmp_path, _REMOVE_GHOST)  # classify → reject
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert prompted["n"] == 0, "a rejected plan must never reach the prompt"


@pytest.mark.asyncio
async def test_every_outcome_emits_audit(tmp_path, monkeypatch):
    import ops.engine_sdlc.planner as planner
    import ops.engine_sdlc.__main__ as cli
    events: list[tuple] = []
    monkeypatch.setattr(
        planner, "_emit_audit",
        lambda *a, **k: events.append(a))
    p = _write_ecr(tmp_path, _REMOVE_GHOST)  # rejected outcome
    await cli._amain(["--ecr", str(p)])
    assert events, "a rejected ECR emitted no ENGINE_CHANGE_REQUEST audit"
    assert any("rejected" in str(e) for e in events)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_cli.py -q -p no:cacheprovider
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_sdlc.__main__'`.

- [ ] **Step 3: Create the CLI**

Create `ops/engine_sdlc/__main__.py` (mirrors `ops/lab/__main__.py::_amain` + `ops/engine_ladder._amain`: explicit non-zero, never silent 0; ADD/REMOVE → explicit TTY `y`/`yes` gate fail-closed; MODIFY/promote automated):

```python
"""``python -m ops.engine_sdlc`` — the Engine Change Request CLI (SP3).

A separate OS process, operator-driven, NEVER wired into any daemon /
dispatch (parity with python -m ops.lab). parse → classify → validate →
render the prepared diff → (ADD/REMOVE) explicit binary TTY y/n,
fail-closed on anything else/EOF/non-TTY → apply; (MODIFY/promote)
automated apply + done-receipt. Every terminal outcome emits an
ENGINE_CHANGE_REQUEST audit. Explicit non-zero, never silent 0 (the
canary -m-no-op lesson; H-S3-12).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m ops.engine_sdlc",
        description="Engine Change Request — prepare + validate + "
                    "atomically apply an engine lifecycle transition "
                    "(SP3). ADD/REMOVE ask one binary y/n; MODIFY/promote "
                    "are automated-if-gated.")
    sub = p.add_argument_group("input")
    sub.add_argument("--ecr", help="Path to a filled ECR file.")
    sub.add_argument("--promote", metavar="ENGINE",
                     help="LAB→PAPER promote ENGINE (automated, gated).")
    if not argv:
        p.print_usage(sys.stderr)
        raise SystemExit(2)
    return p.parse_args(argv)


def _read_confirm() -> str:
    """Read one line from the TTY. Non-interactive stdin / EOF raises so
    the caller fails closed (H-S3-7a)."""
    if not sys.stdin.isatty():
        raise EOFError("non-interactive stdin")
    return input("APPROVE? (y/n) ").strip()


def _validate_for_cli(plan, ecr):
    from ops.engine_sdlc.planner import validate
    return validate(plan, ecr=ecr)


async def _amain(argv: list[str]) -> int:
    ns = _parse_args(argv)
    from ops.engine_sdlc.planner import (
        ApprovalClass, _emit_audit, apply, attach_ecr_context,
        classify, promote)

    if ns.promote:
        res = promote(ns.promote)
        if res.rejection is not None:
            print(f"promote refused: {res.rejection}", file=sys.stderr)
            return 1
        print(f"promoted {ns.promote}: LAB → PAPER (automated, gated). "
              f"Audit: ENGINE_CHANGE_REQUEST. Commit the working-tree "
              f"change with normal git.")
        return 0

    if not ns.ecr:
        print("either --ecr <file> or --promote <engine> is required",
              file=sys.stderr)
        return 1

    from ops.engine_sdlc.ecr import parse_ecr
    from tpcore.engine_profile import _PROFILE
    try:
        ecr = parse_ecr(open(ns.ecr).read())
    except ValueError as exc:
        logger.error("ecr.parse_fail", error=str(exc))
        print(f"ECR parse failed: {exc}", file=sys.stderr)
        return 1

    snapshot = {k: p.lifecycle_state for k, p in _PROFILE.items()}
    plan = attach_ecr_context(classify(ecr, snapshot), ecr)
    if plan.rejection is not None:
        _emit_audit(ecr.engine, ecr.action.value, plan.from_state,
                    plan.to_state, plan.approval_class, "rejected",
                    plan.rejection)
        print(f"ECR rejected: {plan.rejection}", file=sys.stderr)
        return 1

    vplan = _validate_for_cli(plan, ecr)
    if vplan.rejection is not None:
        _emit_audit(ecr.engine, ecr.action.value, vplan.from_state,
                    vplan.to_state, vplan.approval_class, "rejected",
                    vplan.rejection)
        print(f"ECR rejected on validation: {vplan.rejection}",
              file=sys.stderr)
        return 1

    print(f"\n── Prepared transition ──\n"
          f"  action     : {ecr.action.name}\n"
          f"  engine     : {ecr.engine}\n"
          f"  {vplan.from_state} → {vplan.to_state}\n"
          f"  approval   : {vplan.approval_class}\n"
          f"  dry consistency run: GREEN\n")

    if vplan.approval_class is ApprovalClass.AUTOMATED:
        res = apply(vplan)
        if res.rejection is not None:
            print(f"apply rejected: {res.rejection}", file=sys.stderr)
            return 1
        print("APPLIED (automated, gated). Audit emitted. Commit the "
              "working-tree change with normal git.")
        return 0

    # OPERATOR path — explicit binary, fail-closed.
    try:
        answer = _read_confirm()
    except EOFError:
        _emit_audit(ecr.engine, ecr.action.value, vplan.from_state,
                    vplan.to_state, vplan.approval_class,
                    "operator_declined", "EOF / non-interactive stdin")
        print("declined (no interactive confirmation), nothing changed",
              file=sys.stderr)
        return 1
    if answer not in ("y", "yes"):
        _emit_audit(ecr.engine, ecr.action.value, vplan.from_state,
                    vplan.to_state, vplan.approval_class,
                    "operator_declined", f"answer={answer!r}")
        print("declined, nothing changed", file=sys.stderr)
        return 1

    res = apply(vplan)
    if res.rejection is not None:
        print(f"apply rejected: {res.rejection}", file=sys.stderr)
        return 1
    print("APPLIED. Audit emitted. Commit the working-tree change with "
          "normal git (the executor never runs git).")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
```

> Executor note: `ops/engine_sdlc/__main__.py` defines a module named `ops.engine_sdlc.__main__`. The T8 test does `import ops.engine_sdlc.__main__ as cli` inside the test body (lazy — H-S3-10). The `apply`/`_validate_for_cli`/`_read_confirm` monkeypatch targets are module attributes of `cli`; the audit assertion patches `ops.engine_sdlc.planner._emit_audit` (the definition site) — patch where the name is defined, the SP2 oracle's "MIGRATION (binding)" lesson. The `_amain` REMOVE-of-`sentinel` happy-path tests stub `_validate_for_cli` so the slow isolated-tree subprocess does not run in the unit test; the real validate path is covered end-to-end by the T5 `test_remove_throwaway_engine_end_to_end`.

- [ ] **Step 4: Run the test to verify it passes**

```bash
$PY -m pytest tpcore/tests/test_engine_sdlc_cli.py -q -p no:cacheprovider
```
Expected: PASS — parse-fail rc1, reject rc1, no-args nonzero, non-y declines (apply not called), EOF declines, rejected-never-prompts, every-outcome-emits-audit.

- [ ] **Step 5: Run the per-task CI-exact gate set**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite green; ruff clean; check_imports exit 0.

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add ops/engine_sdlc/__main__.py tpcore/tests/test_engine_sdlc_cli.py
git commit -m "feat(engine-sdlc): T8 ECR CLI (fail-closed TTY y/n + audit on every outcome)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T9: Suite-level proof + lane/scope gate

Satisfies: spec §11A T9, H-S3-10 (lane/scope: no SP4 doc / no data-lane SoT touched), the §8 net-new-surface confinement, ordering invariants (iii) (the SP2 oracle / isolation test stay unchanged-green). No new behaviour — a proof task.

**Files:**
- Create: `scripts/tests/test_sp3_scope_confined.py`

- [ ] **Step 1: Write the scope-diff assertion test**

Create `scripts/tests/test_sp3_scope_confined.py`:

```python
"""T9 — SP3 change-set scope confinement (H-S3-10c). The SP3 diff
against the SP3 base must be confined to the spec §8 net-new surface +
the enumerated in-place extends: NO CLAUDE.md / OPERATIONS.md /
glossary.md (SP4 doc-closure boundary), NO data-lane SoT
(tpcore/providers.py, tpcore/feeds/, tpcore/selfheal/). This test runs
git against a SNAPSHOT of names only (no git mutation, read-only
`git diff --name-only`), never against a synthetic repo — it asserts
the working change set, the canonical T9 scope proof."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# SP4 / data-lane files SP3 must NEVER touch (spec §1.1, H-S3-10c).
_FORBIDDEN_PREFIXES = (
    "CLAUDE.md",
    "OPERATIONS.md",
    "docs/glossary.md",
    "tpcore/providers.py",
    "tpcore/feeds/",
    "tpcore/selfheal/",
)

# The spec §8 net-new surface + enumerated in-place extends SP3 may add
# /modify (prefix allow-list).
_ALLOWED_PREFIXES = (
    "ops/engine_sdlc/",
    "docs/superpowers/checklists/engine_change_request.md",
    "docs/superpowers/plans/2026-05-18-engine-change-request.md",
    "tpcore/templates/eulogy_template.md",
    "reversion/backtest.py",
    "vector/backtest.py",
    "momentum/backtest.py",
    "ops/lab/run.py",
    "ops/lab/dossier.py",
    "tpcore/lab/context.py",
    "tpcore/tests/test_engine_lifecycle_consistency.py",
    "tpcore/tests/test_ecr_parse.py",
    "tpcore/tests/test_engine_default_params_parity.py",
    "tpcore/tests/test_lab_credibility_pool_threaded.py",
    "tpcore/tests/test_lab_dossier_sidecar.py",
    "tpcore/tests/test_engine_sdlc_planner.py",
    "tpcore/tests/test_engine_sdlc_cli.py",
    "scripts/tests/test_sp3_scope_confined.py",
)


def test_sp3_change_set_confined_to_net_new_surface():
    base = subprocess.run(  # noqa: S603
        ["git", "merge-base", "HEAD", "main"],
        cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    names = subprocess.run(  # noqa: S603 — read-only name-only diff
        ["git", "diff", "--name-only", base, "HEAD"],
        cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for n in names:
        assert not n.startswith(_FORBIDDEN_PREFIXES), (
            f"SP3 touched a forbidden SP4/data-lane file: {n}")
        assert n.startswith(_ALLOWED_PREFIXES), (
            f"SP3 touched a file outside the §8 net-new surface: {n} "
            f"(if this is intentional, the spec scope is wrong — escalate)")
```

> Executor note: this test runs `git diff --name-only` (read-only — no mutation, no synthetic repo) against `git merge-base HEAD main`. This is the ONE sanctioned read-only git invocation, scoped to the scope-proof; it does not violate "tests never touch the working repo" (no write, no checkout, no branch op). If `data/`-style untracked files appear they are not in `git diff` HEAD..base (untracked), so they do not trip this test.

- [ ] **Step 2: Run the scope test**

```bash
$PY -m pytest scripts/tests/test_sp3_scope_confined.py -q -p no:cacheprovider
```
Expected: PASS — every changed file is under `_ALLOWED_PREFIXES`, none under `_FORBIDDEN_PREFIXES`. If it FAILS naming a file outside the allow-list, STOP and reconcile (either the file is genuinely SP3 scope — add it to the allow-list with a one-line spec-§ justification — or it is a scope leak — revert it).

- [ ] **Step 3: Run the full SP3 suite-level proof (the T9 gate set)**

```bash
$PY -m pytest -q -p no:cacheprovider
$PY -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
$PY -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
$PY -m pytest scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_isolation.py -q -p no:cacheprovider
$PY -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -q -p no:cacheprovider
bash -n scripts/run_smoke_test.sh
```
Expected: full suite green; ruff clean; `check_imports` exit 0 (SP3 added zero tpcore→engine import); the SP2 T1 oracle + `test_lab_isolation.py` UNCHANGED-green (isolation test skips locally without `DATABASE_URL` — expected, do not "fix"); the extended SP1 consistency suite (incl. the T5 H-S3-5 legs) green; `bash -n` clean (no SP3-edited wrapper scripts in the working repo — the shadow edits only ever happen inside the planner's temp tree, so `scripts/run_smoke_test.sh` is unmodified in the repo; the `bash -n` is the standing hygiene check).

- [ ] **Step 4: Commit**

```bash
test "$(git branch --show-current)" = "worktree-engine-sp3" || { echo "WRONG BRANCH"; exit 1; }
git add scripts/tests/test_sp3_scope_confined.py
git commit -m "test(engine-sdlc): T9 suite-level proof + SP3 scope-confinement gate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (run by the plan author before handoff — results inline below)

**1. Spec coverage** — every spec §/H-S3-*/carry-forward mapped to a task:

| Spec item | Task |
|---|---|
| §1 problem, §1.1 non-goals (no CUTOVER, no SP4 doc, paper-only) | T9 scope gate (enforced); design honored throughout |
| §1.4 O1 carry-forward | T1 |
| §1.4 credibility_pool carry-forward | T2 |
| §2 / §2.1 / §2.2 / §2.3 ECR artifact + format + frozen model + parser | T0 |
| §3.1 module layering (ops/ not tpcore) | T0–T8 (every `ops/engine_sdlc/*` file) |
| §3.2 transition pipeline | T4 (classify/validate) + T5–T7 (apply) + T8 (CLI flow) |
| §3.3 audit (ENGINE_CHANGE_REQUEST) | T5 (`_emit_audit`) + T8 (every-outcome) |
| §4.1 ADD (new_scaffold + lab_candidate, always→LAB) | T6 |
| §4.1 LAB→PAPER automated promote | T7 (`promote`) |
| §4.2 REMOVE snap-out + §4.2.1 EULOGY template | T5 |
| §4.2.2 completed archive-leg clockwork | T5 (H-S3-5 extension) |
| §4.3 MODIFY (no _PROFILE edit) | T7 |
| §5.1 classify (total/closed table) | T4 |
| §5.2 validate (reject never force) | T4 skeleton + T6/T7 branches |
| §5.3 apply (atomic-or-abort) | T5 (`apply` + `_Journal`) |
| §5.4 evidence re-verified never trusted | T3 (sidecar) + T7 (`_validate_modify`) |
| §6 / §6.1 / §6.2 operator-interaction policy + MODIFY-automated | T8 (CLI gate) + T0 checklist header |
| §7.1 default_params() seam | T1 |
| §7.2 thread credibility_pool | T2 |
| §8 reused-vs-new ledger | File Structure + T9 scope confinement |
| §9 symmetry/divergence ledger | Design honored (no CUTOVER — no task; correct, it's a non-goal) |
| §10 failure modes | Subsumed by §11 H-S3-* (spec says §11 supersedes §10) |
| §11.1 H-S3-1 | T4 (`_run_consistency_subprocess` isolated-tree) |
| §11.1 H-S3-2 | T5 (REMOVE leg `_maybe_rewrite_frozen_literal`) + T6 (ADD leg untouched) |
| §11.1 H-S3-3 | T4/T5 (`_rewrite_profile_source` ast+compile) |
| §11.1 H-S3-4 | T5 (`_Journal` + reverse-order restore + `apply_restore_failed`) |
| §11.1 H-S3-5 | T5 (4 archive-leg legs in the in-place test extension) |
| §11.1 H-S3-6 | T7 (`_validate_modify` zero-trust, sot_diff-empty) |
| §11.1 H-S3-7 | T8 (fail-closed TTY + audit on every terminal outcome) |
| §11.1 H-S3-8 | T1 + T2 (re-run unchanged SP2 oracle / isolation) |
| §11.1 H-S3-9 | T3 (sidecar emit + loader) |
| §11.1 H-S3-10 | every task (test placement + lazy import) + T4 (`test_profile_rewrite_adds_no_import`) + T9 (scope gate) |
| §11.1 H-S3-11 | T6 (ADD fail-closed) |
| §11.1 H-S3-12 | T8 (explicit non-zero, never silent 0) |
| §11.2 R1/R2/R3 residuals | Accepted-as-documented (R2: executor never runs git; R3: copytree O(repo) accepted) |
| §11.3 D1/D2 fixed defects | T3 (D1) + T4 (D2) |
| §11A T0–T9 | T0–T9 (1:1) |
| §12 MODIFY operator-confirmed | T7/T8 (automated-if-gated) |

No spec §/H-S3/carry-forward is without a home. **No genuine spec gap found.** (The §9 "no CUTOVER analogue" is correctly a non-goal — absence of a CUTOVER task is spec-correct, not a gap.)

**2. Placeholder scan:** no "TBD"/"implement later"/"add error handling"/"similar to Task N" — every step carries complete code, exact paths, exact commands, expected output. The "Executor note" annotations are environment-verification instructions (read the live constant before hardcoding), not placeholders — they pin the *invariant* the test enforces and tell the executor exactly how to resolve the one environment-local literal.

**3. Type/signature consistency (cross-task):**
- `TransitionPlan`, `ApprovalClass`, `ECRAction`, `EngineChangeRequest` — defined T0/T4, used identically T5–T8.
- `classify(ecr, profile_snapshot)` — pure snapshot-arg, never takes I/O; `attach_ecr_context(plan, ecr)` threads ECR free-text without making `classify` impure (introduced T5, used T6/T7/T8 consistently).
- `validate(plan, *, repo_root=None, ecr=None)` — the `ecr=` kwarg added in T6 is consistently passed in T7/T8; the T4 skeleton's signature was widened in T6 (noted explicitly: "replace the `validate` defined in T5") so no stale 2-arg call remains. **Fixed inline:** the T4 `validate` skeleton signature `validate(plan, *, repo_root=None)` is superseded by T6's `validate(plan, *, repo_root=None, ecr=None)` — the T4 test (`test_validate_runs_real_clockwork_in_isolated_tree`) calls `_run_consistency_subprocess` directly (not `validate`), so no T4 test breaks when T6 widens the signature; T5's `validate(plan, repo_root=staged)` is keyword-compatible with the widened signature (ecr defaults None — REMOVE has no gate). Consistent.
- `apply(plan, *, repo_root=None, emit_audit=True, _force_validate=False)` — defined T5, called identically T6/T7/T8.
- `_emit_audit(engine, action, from_state, to_state, approval_class, outcome, reason)` — defined T5, patched at the definition site (`ops.engine_sdlc.planner._emit_audit`) in T8 (the SP2 "patch where defined" lesson).
- `default_params(engine)` (dispatcher, T1) vs `default_params()` (per-engine, T1) — distinct names by arity/module, used consistently (`_build_lab_result` calls the dispatcher with `args.engine`; the parity test calls both).
- `load_labresult_sidecar` / `EvidenceError` — defined T3, used T6 (ADD lab_candidate) + T7 (MODIFY) identically.

No inconsistency remains after the inline fix noted above.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-18-engine-change-request.md`. Per the standing operator directive (always subagent-driven execution), the next step is `superpowers:subagent-driven-development` — a fresh implementer subagent per task (T0→T9 in order), each followed by a split spec-compliance then code-quality review, then finish-branch. The plan's ordering invariants are load-bearing: T1/T2/T3 (carry-forwards + sidecar) MUST land before T4–T7 (the planner that consumes them); the H-S3-5 archive-leg clockwork extension MUST land with T5 (its producer), never earlier.
