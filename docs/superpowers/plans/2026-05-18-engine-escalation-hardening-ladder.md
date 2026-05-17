# Engine-Lane Escalation & Hardening Ladder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the silent-best-effort gap — make every engine escalation (DA-1 infra + DA-2 behavioral, incl. DA-2 escalate-only) carry a recorded disposition, clockwork-enforced (R2 build-break) and loudly surfaced (R3), engine-native.

**Architecture:** New `ops/engine_ladder.py` (ops-layer): `EngineEscalationDisposition` enum + `DISPOSITION_POLICIES` registry + `escalation_drift()` (R2) + `list`/`disposition` CLI (R3) + `__main__`. One behavior-preserving `INFRA_FAILURE_CLASSES` extract in `ops/engine_supervisor.py` made the enforced SoT (clockwork-pinned, incl. `_classify`). `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (R4, doc-only). Symmetry-references the data ladder shape; touches NO data-lane file.

**Tech Stack:** Python 3.11, asyncpg, structlog, Pydantic v2, pytest (`asyncio_mode="auto"`). venv `/Users/michael/short-term-trading-engine/.venv/bin/python`; `ruff` on PATH.

**Lane / scope discipline:** Touches ONLY `ops/engine_ladder.py` (new), `ops/engine_supervisor.py` (the one constant extract), `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (new), `CLAUDE.md` (one bullet, `git add -p`-guarded vs the concurrent data session), `scripts/tests/test_engine_ladder.py` (new), `scripts/tests/test_engine_supervisor.py` (the `_classify` SoT-pin test only). Does NOT touch `tpcore/ladder/`, `ops/weekly_digest.py`, `ops/aar_autotune.py` logic, the data lane, DA-1/DA-2 detection/clear logic, or alpha engines. `ops/engine_ladder.py` imports `ops.engine_supervisor`+`ops.aar_autotune`+`tpcore` (ops→ops, ops→tpcore allowed). CI-exact: `ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/` (ops/ already covered — no ci.yml change) + `python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore` (engine_ladder is ops, not an engine pkg — no arg change).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `ops/engine_supervisor.py` | DA-1 supervisor | Add `INFRA_FAILURE_CLASSES` constant; `_auto_clear` refs it (byte-identical) |
| `ops/engine_ladder.py` | The ladder: enum+registry+drift, list, disposition, CLI | Create |
| `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` | Canonical doc (R1-R5 + workflow) | Create |
| `CLAUDE.md` | One engine-lane-escalation-contract bullet | Modify (guarded) |
| `scripts/tests/test_engine_supervisor.py` | `_classify` SoT-pin test | Add 1 test |
| `scripts/tests/test_engine_ladder.py` | drift/list/disposition/CLI tests | Create |

---

## Task 1: `INFRA_FAILURE_CLASSES` constant — enforced SoT (the only DA-1 touch)

`ops/engine_supervisor.py` `_auto_clear` has the infra set as an inline tuple (lines ~152-154); `_classify` returns the same five strings as separate inline literals. They are two parallel un-enforced literals. Extract a constant, point `_auto_clear` at it byte-identically, and add a clockwork test pinning `_classify`'s emittable set to it (so a new `_classify` class fails CI).

**Files:**
- Modify: `ops/engine_supervisor.py`
- Test: `scripts/tests/test_engine_supervisor.py`

- [ ] **Step 1: Write the failing SoT-pin test**

Add to `scripts/tests/test_engine_supervisor.py` (the file already has the ops-collision guard + `_rows_conn` + `import ops.engine_supervisor as es`-style header — match its existing import alias; here written `es`).

The pin is a **GENUINE AST-introspection clockwork**, NOT a hand-maintained detector list. `test_classify_emittable_set_is_pinned_to_constant` reads `_classify`'s real source via `inspect.getsource`, `ast.parse`/`ast.walk`s it, and collects every string literal that is the first element of a `return "<cls>", <bool>` tuple. That emitted set MUST equal `set(es.INFRA_FAILURE_CLASSES)`. Because it parses the actual function, adding a 6th `_detect_*` whose `_classify` arm does `return "new_cls", x` makes `emitted` include `"new_cls"` ∉ the constant → this test fails RED until `INFRA_FAILURE_CLASSES` (and, via the engine-ladder drift test, a `DispositionPolicy`) is updated — closing the most common add-a-class path with no per-detector parametrize to forget. A non-vacuous behavior smoke (`test_classify_known_detectors_yield_their_class`) additionally asserts each of the 5 known detectors, True in isolation, drives `_classify` to its own class; and `test_infra_failure_classes_is_the_five_da1_classes` keeps the frozenset-equality anchor:

```python
def test_classify_emittable_set_is_pinned_to_constant():
    src = inspect.getsource(es._classify)
    tree = ast.parse(textwrap.dedent(src))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            first = node.value.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                emitted.add(first.value)
    assert emitted == set(es.INFRA_FAILURE_CLASSES)


def test_infra_failure_classes_is_the_five_da1_classes():
    assert es.INFRA_FAILURE_CLASSES == frozenset({
        "crashed_startup", "scheduler_crash", "data_request_timeout",
        "data_repair_escalated", "missed_cycle"})
```

Add `import ast`, `import inspect`, `import textwrap` to the test file's stdlib import block (ruff-ordered, with the other top stdlib imports). This is a pure test change — zero DA-1 production-logic delta.

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-ladder && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -k "infra_failure_classes or classify_emittable" -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'INFRA_FAILURE_CLASSES'`.

- [ ] **Step 3: Add the constant + point `_auto_clear` at it (byte-identical)**

In `ops/engine_supervisor.py`, near the other module constants (beside `_MAX_REINVOKE` etc., before `_emit`), add:

```python
# The DA-1 infra failure-class SoT (the engine-ladder R2 clockwork
# pins _classify's emittable set + the disposition registry to this).
INFRA_FAILURE_CLASSES: frozenset[str] = frozenset({
    "crashed_startup", "scheduler_crash", "data_request_timeout",
    "data_repair_escalated", "missed_cycle"})
```

In `_auto_clear`, replace ONLY the inline tuple membership check. The current code is:

```python
    if hold.failure_class not in (
            "crashed_startup", "scheduler_crash", "data_request_timeout",
            "data_repair_escalated", "missed_cycle"):
        return
```

Replace with (semantically byte-identical — same set, same `not in`):

```python
    if hold.failure_class not in INFRA_FAILURE_CLASSES:
        return
```

Do NOT change `_classify`'s `return "<str>", <bool>` lines gratuitously (the SoT-pin test AST-parses them from the real `_classify` source; they already all ∈ the constant — the test enforces the emitted set stays == `INFRA_FAILURE_CLASSES`, so any new/changed return-class arm must be matched by a constant update). No other change.

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: PASS — the 2 new tests pass AND every pre-existing DA-1 supervisor test still passes (the constant substitution is set-identical; DA-1 suite is the equivalence oracle). If any pre-existing test fails, the extract changed behavior — fix the extract, not the test.

- [ ] **Step 5: ruff + commit**

```bash
ruff check ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git add ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git commit -m "$(cat <<'EOF'
refactor(engine_supervisor): INFRA_FAILURE_CLASSES enforced SoT

Extract the DA-1 infra-class set to a module constant; _auto_clear
references it (set-identical, behavior-preserving — DA-1 suite is the
oracle). New clockwork test pins _classify's emittable set to the
constant so a new DA-1 class fails CI until the engine ladder's
disposition registry is updated.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Self-review: `git diff HEAD~1 -- ops/engine_supervisor.py` shows ONLY the constant added + the one `not in (...)`→`not in INFRA_FAILURE_CLASSES` line; `_classify`/`_verify_cleared`/emitters byte-unchanged; ruff clean; full supervisor suite green.

---

## Task 2: `ops/engine_ladder.py` — disposition enum + policy registry + clockwork drift (R2)

**Files:**
- Create: `ops/engine_ladder.py`
- Test: `scripts/tests/test_engine_ladder.py`

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_engine_ladder.py`:

```python
import contextlib  # noqa: F401  (used by later tasks' fake pools)
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import aar_autotune as at  # noqa: E402
from ops import engine_ladder as el  # noqa: E402
from ops import engine_supervisor as es  # noqa: E402


def test_disposition_enum_is_converted_structural_removed():
    vals = {d.value for d in el.EngineEscalationDisposition}
    assert vals == {"converted", "structural", "removed"}
    assert "auto_converted" not in vals  # no engine auto-conversion actor


def test_known_classes_derived_from_real_constants():
    assert el.KNOWN_ESCALATION_CLASSES == (
        es.INFRA_FAILURE_CLASSES | {at._BEHAVIORAL})


def test_every_known_class_has_a_policy():
    for cls in el.KNOWN_ESCALATION_CLASSES:
        p = el.policy_for(cls)
        assert p is not None
        assert isinstance(p.default, el.EngineEscalationDisposition)
        assert p.rationale.strip()


def test_data_repair_escalated_default_is_structural_not_removed():
    p = el.policy_for("data_repair_escalated")
    assert p.default is el.EngineEscalationDisposition.STRUCTURAL


def test_escalation_drift_empty_in_lockstep():
    assert el.escalation_drift() == (set(), set())


def test_escalation_drift_reports_missing_for_uncovered_class():
    """Non-tautology proof (mirrors tpcore data ladder
    test_no_drift_full_class_set_covered): a class in the derived
    KNOWN with no policy ⇒ escalation_drift().missing non-empty ⇒
    build would break."""
    missing, extra = el._drift_for(
        known=el.KNOWN_ESCALATION_CLASSES | {"_synthetic_probe"},
        policies=el.DISPOSITION_POLICIES)
    assert "_synthetic_probe" in missing
    assert extra == set()


def test_policy_for_unknown_is_none():
    assert el.policy_for("not_a_class") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_ladder'`.

- [ ] **Step 3: Implement the registry half of `ops/engine_ladder.py`**

Create `ops/engine_ladder.py` with the imports, enum, model, registry, and drift (the CLI/list/disposition land in Tasks 3-5; this step is the R2 core):

```python
"""Engine-Lane Escalation & Hardening Ladder (sub-project after canary).

Closes the silent-best-effort gap: DA-1 (engine_supervisor) and DA-2
(aar_autotune) emit ENGINE_ESCALATED with ZERO consumers. This module
makes every engine escalation CLASS carry a recorded disposition
(clockwork-enforced — a new class fails CI: R2) and every
undispositioned INSTANCE past grace surface via `python -m
ops.engine_ladder list` with a `disposition` verb (R3). Engine-native;
symmetry-references the data-lane ladder (tpcore/ladder + weekly_digest)
but touches NO data-lane file. The escalated engine is already
ENGINE_HELD by DA-1/DA-2 (R1) — no extra automatic trade consequence.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from ops.aar_autotune import _BEHAVIORAL
from ops.engine_supervisor import INFRA_FAILURE_CLASSES
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

import enum


class EngineEscalationDisposition(enum.StrEnum):
    """Every engine escalation terminates in exactly one of these.
    No AUTO_CONVERTED: the engine lane has no auto-conversion actor."""

    CONVERTED = "converted"
    STRUCTURAL = "structural"
    REMOVED = "removed"


class DispositionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    class_name: str
    default: EngineEscalationDisposition
    rationale: str


_D = EngineEscalationDisposition

DISPOSITION_POLICIES: dict[str, DispositionPolicy] = {
    "crashed_startup": DispositionPolicy(
        class_name="crashed_startup", default=_D.STRUCTURAL,
        rationale="DA-1 bounded re-invoke exhausted; persistence ⇒ a "
                  "structural scheduler/runtime fix."),
    "scheduler_crash": DispositionPolicy(
        class_name="scheduler_crash", default=_D.STRUCTURAL,
        rationale="non-zero scheduler exit survived self-heal ⇒ a "
                  "code/runtime defect to fix structurally."),
    "data_request_timeout": DispositionPolicy(
        class_name="data_request_timeout", default=_D.STRUCTURAL,
        rationale="data lane never answered in-window ⇒ the structural "
                  "fix is typically in the DATA LANE's request "
                  "fulfillment/timeout, NOT this engine; disposition "
                  "records the operator confirmed cross-lane ownership."),
    "data_repair_escalated": DispositionPolicy(
        class_name="data_repair_escalated", default=_D.STRUCTURAL,
        rationale="the DATA-LANE escalation owns the fix; this engine "
                  "is HELD (not removed) and auto-clears on "
                  "DATA_REPAIR_COMPLETE green via DA-1 _auto_clear; "
                  "escalate to REMOVED only if the source is "
                  "permanently retired."),
    "missed_cycle": DispositionPolicy(
        class_name="missed_cycle", default=_D.STRUCTURAL,
        rationale="engine silently failed to start over N cycles ⇒ a "
                  "structural scheduling/dispatch fix."),
    _BEHAVIORAL: DispositionPolicy(
        class_name=_BEHAVIORAL, default=_D.STRUCTURAL,
        rationale="DA-2 loss_cluster≥5 / drawdown ⇒ edge-decay; a "
                  "structural strategy review, or REMOVED if the edge "
                  "is gone (snap-out via the Engine SDLC)."),
}

KNOWN_ESCALATION_CLASSES: frozenset[str] = (
    INFRA_FAILURE_CLASSES | {_BEHAVIORAL})


def _drift_for(*, known: set[str] | frozenset[str],
               policies: dict[str, DispositionPolicy]
               ) -> tuple[set[str], set[str]]:
    have = set(policies)
    known_s = set(known)
    return known_s - have, have - known_s


def escalation_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) of the DERIVED KNOWN set vs DISPOSITION_POLICIES.
    No args (mirrors tpcore.ladder.disposition.disposition_drift). Both
    empty == lockstep. A new DA-1/DA-2 class grows KNOWN (via the pinned
    constants) ⇒ missing non-empty ⇒ the clockwork test fails the build
    until a policy is recorded — the R2 tooth."""
    return _drift_for(known=KNOWN_ESCALATION_CLASSES,
                      policies=DISPOSITION_POLICIES)


def policy_for(class_name: str) -> DispositionPolicy | None:
    return DISPOSITION_POLICIES.get(class_name)
```

(`import enum` is placed after the module docstring's other imports intentionally near `EngineEscalationDisposition`; if ruff E402-flags it, move `import enum` into the top stdlib import block — keep `enum.StrEnum`.)

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: ruff + commit**

```bash
ruff check ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git add ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git commit -m "$(cat <<'EOF'
feat(engine_ladder): disposition registry + clockwork drift (R2)

EngineEscalationDisposition(converted|structural|removed), per-class
DISPOSITION_POLICIES (data_repair_escalated→STRUCTURAL per expert),
KNOWN derived from engine_supervisor.INFRA_FAILURE_CLASSES |
{aar_autotune._BEHAVIORAL}, escalation_drift() no-tautology drift
(symmetry-ref tpcore.ladder.disposition). New DA-1/DA-2 class ⇒
build breaks until a policy is recorded.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `list` — undispositioned-instance digest (R3, both shapes)

Two shapes: **held-class** (paired `ENGINE_HELD`; closed by later `ENGINE_CLEARED` or a disposition) and **escalate-only** (no `ENGINE_HELD`, no possible `ENGINE_CLEARED`; closed by a disposition OR all its payload `triggers` fingerprints resolved/absent from `forensics_triggers`). Both: not dispositioned, older than grace.

**Files:**
- Modify: `ops/engine_ladder.py`
- Test: `scripts/tests/test_engine_ladder.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_ladder.py`:

```python
import json as _json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=10)   # past 7d grace
FRESH = NOW - timedelta(days=1)  # within grace


def _conn(esc_rows, open_fps):
    """esc_rows: list of dicts the candidate-SQL returns; open_fps:
    set of fingerprints still open in forensics_triggers."""
    class _C:
        async def fetch(self, sql, *a):
            if "forensics_triggers" in sql:
                return [{"fp": fp} for fp in open_fps]
            return esc_rows
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield _C()
    return _P()


def _row(hold_id, *, engine="reversion", failure_class="crashed_startup",
         reason="x", recorded_at=OLD, has_held=True, triggers=None):
    return {"hold_id": hold_id, "engine": engine,
            "failure_class": failure_class, "reason": reason,
            "recorded_at": recorded_at, "has_held": has_held,
            "triggers": _json.dumps(triggers or [])}


async def test_list_includes_past_grace_held_open():
    pool = _conn([_row("h1")], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["h1"]
    assert out[0]["shape"] == "held"


async def test_list_excludes_within_grace():
    pool = _conn([_row("h2", recorded_at=FRESH)], set())
    assert await el.list_undispositioned(pool, now=NOW, grace_days=7) == []


async def test_list_escalate_only_included_when_fps_still_open():
    pool = _conn([_row("e1", failure_class="behavioral", has_held=False,
                        triggers=["fp-a", "fp-b"])], {"fp-b"})
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["e1"]
    assert out[0]["shape"] == "escalate-only"


async def test_list_escalate_only_excluded_when_all_fps_resolved():
    # all fingerprints resolved/absent from forensics_triggers →
    # auto-closed even though past grace (the auto-close disjunct).
    pool = _conn([_row("e2", failure_class="behavioral", has_held=False,
                        triggers=["fp-x"])], set())
    assert await el.list_undispositioned(pool, now=NOW, grace_days=7) == []


async def test_list_carries_policy_default():
    pool = _conn([_row("h3", failure_class="data_repair_escalated")], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert out[0]["policy_default"] == "structural"
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -k list_ -q`
Expected: FAIL — `AttributeError: module 'ops.engine_ladder' has no attribute 'list_undispositioned'`.

- [ ] **Step 3: Implement `list_undispositioned` + the candidate SQL**

Append to `ops/engine_ladder.py`:

```python
_GRACE_DAYS = int(os.environ.get("ENGINE_LADDER_GRACE_DAYS", "7"))

# Candidate ENGINE_ESCALATED rows: not later-CLEARED for held-class,
# not DISPOSITIONED, with a has_held flag (paired ENGINE_HELD on the
# SAME hold_id) so the caller distinguishes the two shapes. Escalate-
# only auto-close (trigger fingerprints resolved) is applied in Python
# against forensics_triggers (mirrors aar_autotune._maybe_clear_*).
_CANDIDATE_SQL = """
    SELECT e.data->>'hold_id'        AS hold_id,
           e.engine                  AS engine,
           e.data->>'failure_class'  AS failure_class,
           e.data->>'reason'         AS reason,
           e.recorded_at             AS recorded_at,
           (e.data->'triggers')      AS triggers,
           EXISTS (SELECT 1 FROM platform.application_log h
                   WHERE h.event_type = 'ENGINE_HELD'
                     AND (h.data->>'hold_id') = (e.data->>'hold_id'))
                                     AS has_held
    FROM platform.application_log e
    WHERE e.event_type = 'ENGINE_ESCALATED'
      AND (e.data->>'hold_id') IS NOT NULL
      AND e.recorded_at < $1
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log d
        WHERE d.event_type = 'ENGINE_ESCALATION_DISPOSITIONED'
          AND (d.data->>'hold_id') = (e.data->>'hold_id'))
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log c
        WHERE c.event_type = 'ENGINE_CLEARED'
          AND (c.data->>'hold_id') = (e.data->>'hold_id')
          AND c.recorded_at > e.recorded_at)
    ORDER BY e.recorded_at
"""

_OPEN_FP_SQL = """
    SELECT payload->>'fingerprint' AS fp
    FROM platform.forensics_triggers
    WHERE resolved_at IS NULL AND payload->>'fingerprint' = ANY($1::text[])
"""


def _triggers_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [str(x) for x in raw] if isinstance(raw, list) else []


async def list_undispositioned(pool, *, now=None, grace_days: int | None = None
                               ) -> list[dict]:
    """Open-undispositioned engine escalations (held + escalate-only).
    Read-only, grace-windowed."""
    from datetime import UTC, datetime, timedelta
    now = now or datetime.now(UTC)
    grace = grace_days if grace_days is not None else _GRACE_DAYS
    cutoff = now - timedelta(days=grace)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CANDIDATE_SQL, cutoff)
    out: list[dict] = []
    for r in rows:
        hold_id = r["hold_id"]
        has_held = bool(r["has_held"])
        if has_held:
            shape = "held"
        else:
            shape = "escalate-only"
            fps = _triggers_list(r["triggers"])
            if fps:
                async with pool.acquire() as conn:
                    open_rows = await conn.fetch(_OPEN_FP_SQL, fps)
                if not open_rows:
                    continue  # all fps resolved → auto-closed
            # no fps recorded → cannot auto-close; remains open
        pol = policy_for(r["failure_class"])
        out.append({
            "hold_id": hold_id, "engine": r["engine"],
            "failure_class": r["failure_class"], "reason": r["reason"],
            "recorded_at": r["recorded_at"], "shape": shape,
            "policy_default": (pol.default.value if pol else None),
            "policy_rationale": (pol.rationale if pol else None),
        })
    return out
```

(The test's `_conn.fetch` branches on `"forensics_triggers" in sql` → returns `[{"fp":..}]`; `list_undispositioned` only checks truthiness of `open_rows`, so the `fp` column name is not asserted — keep the SQL alias `fp` for clarity.)

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -q`
Expected: PASS (12 passed: 7 + 5). If a test's fake-pool SQL routing mismatches the real SQL fragments, align the test's `_conn` branch to the real fragments (keep the asserted behavior — held included past-grace, within-grace excluded, escalate-only included iff fps open, excluded iff all resolved, policy_default surfaced).

- [ ] **Step 5: ruff + commit**

```bash
ruff check ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git add ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git commit -m "$(cat <<'EOF'
feat(engine_ladder): list_undispositioned — held + escalate-only (R3)

Held-class: ENGINE_ESCALATED w/ paired ENGINE_HELD, no later
ENGINE_CLEARED, no DISPOSITIONED, past grace. Escalate-only (no
ENGINE_HELD): same minus the impossible CLEARED, plus auto-close when
all payload trigger fingerprints are resolved/absent from
forensics_triggers (mirrors aar_autotune re-eval). Surfaces the
class's policy default+rationale.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `disposition` verb — event-sourced terminal (R3)

**Files:**
- Modify: `ops/engine_ladder.py`
- Test: `scripts/tests/test_engine_ladder.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_ladder.py`:

```python
def _rec_pool(open_hold_ids):
    """fetch returns a 1-row marker iff hold_id is an open escalation;
    execute records the disposition INSERT."""
    class _C:
        def __init__(self):
            self.inserts = []
        async def fetch(self, sql, *a):
            if "ENGINE_ESCALATED" in sql:
                hid = a[1] if len(a) > 1 else a[0]
                return [{"hold_id": hid}] if hid in open_hold_ids else []
            return []
        async def execute(self, sql, *a):
            self.inserts.append((sql, a))
    c = _C()
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield c
    p = _P()
    p._c = c
    return p


async def test_disposition_emits_locked_event_for_valid_verb():
    pool = _rec_pool({"h9"})
    rc = await el.disposition(pool, "h9", "Structural", "the note")
    assert rc == 0
    ins = [a for s, a in pool._c.inserts
           if "INSERT INTO platform.application_log" in s]
    assert len(ins) == 1
    payload = _json.loads(ins[0][-1])
    assert payload == {"schema": 1, "hold_id": "h9",
                       "disposition": "structural", "note": "the note"}
    # event_type arg (3rd positional after engine, run_id)
    assert ins[0][2] == "ENGINE_ESCALATION_DISPOSITIONED"


async def test_disposition_rejects_unknown_verb_no_write():
    pool = _rec_pool({"h9"})
    rc = await el.disposition(pool, "h9", "bogus", "")
    assert rc != 0
    assert not any("INSERT" in s for s, _ in pool._c.inserts)


async def test_disposition_rejects_unknown_or_not_open_hold_no_write():
    pool = _rec_pool(set())  # h9 not open
    rc = await el.disposition(pool, "h9", "structural", "")
    assert rc != 0
    assert not any("INSERT" in s for s, _ in pool._c.inserts)


async def test_disposition_accepts_escalate_only_hold_id():
    # escalate-only hold_id is "open" per the candidate query (no
    # current_hold gating) → disposition must succeed.
    pool = _rec_pool({"e1"})
    rc = await el.disposition(pool, "e1", "converted", "fixed it")
    assert rc == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -k disposition -q`
Expected: FAIL — no `disposition` attr.

- [ ] **Step 3: Implement `disposition`**

Append to `ops/engine_ladder.py`:

```python
_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""

_DISPOSITIONED_EVENT = "ENGINE_ESCALATION_DISPOSITIONED"

_IS_OPEN_SQL = """
    SELECT e.data->>'hold_id' AS hold_id, e.engine AS engine
    FROM platform.application_log e
    WHERE e.event_type = 'ENGINE_ESCALATED'
      AND (e.data->>'hold_id') = $1
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log d
        WHERE d.event_type = 'ENGINE_ESCALATION_DISPOSITIONED'
          AND (d.data->>'hold_id') = $1)
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log c
        WHERE c.event_type = 'ENGINE_CLEARED'
          AND (c.data->>'hold_id') = $1
          AND c.recorded_at > e.recorded_at)
    LIMIT 1
"""


async def disposition(pool, hold_id: str, verb: str, note: str) -> int:
    """Record an operator disposition for an open engine escalation.
    Accepts BOTH held and escalate-only hold_ids (validity is the
    open-escalation predicate, NOT current_hold). 0 ok; non-zero +
    NO write on a bad verb or an unknown/not-open hold_id."""
    try:
        disp = EngineEscalationDisposition(verb.strip().lower())
    except ValueError:
        logger.error("engine_ladder.bad_verb", verb=verb)
        return 2
    async with pool.acquire() as conn:
        row = await conn.fetch(_IS_OPEN_SQL, hold_id)
    if not row:
        logger.error("engine_ladder.unknown_or_not_open", hold_id=hold_id)
        return 2
    engine = row[0]["engine"] if isinstance(row[0], dict) else "engine"
    payload = {"schema": 1, "hold_id": hold_id,
               "disposition": disp.value, "note": note}
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, engine, uuid.uuid4(), _DISPOSITIONED_EVENT,
            "INFO", f"escalation {hold_id} dispositioned: {disp.value}",
            json.dumps(payload, default=str))
    logger.info("engine_ladder.dispositioned", hold_id=hold_id,
                disposition=disp.value)
    return 0
```

(The test's `_rec_pool.fetch` returns `[{"hold_id": hid}]` — no `engine` key — so `row[0]["engine"]` would KeyError; use `row[0].get("engine", "engine")` if rows are dicts. Adjust: `engine = (row[0].get("engine") if isinstance(row[0], dict) else None) or "engine"`. Use that exact safe form in the implementation so the unit test's minimal fake row works AND a real asyncpg Record (which has `engine`) works.)

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -q`
Expected: PASS (16 passed). If the `_IS_OPEN_SQL` arg position the test inspects differs, align the test's `fetch` arg-handling to the real call (keep: valid verb+open→one INSERT with the locked payload; bad verb / not-open→rc!=0 + no INSERT; escalate-only hold_id accepted).

- [ ] **Step 5: ruff + commit**

```bash
ruff check ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git add ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git commit -m "$(cat <<'EOF'
feat(engine_ladder): disposition verb — event-sourced terminal (R3)

ENGINE_ESCALATION_DISPOSITIONED {schema:1,hold_id,disposition,note}
via the locked application_log INSERT. Validity = the open-escalation
predicate (accepts escalate-only hold_ids; NOT gated on current_hold).
Bad verb / unknown-or-not-open hold_id → non-zero, NO write.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `__main__` CLI — `list` | `disposition` (mirrors weekly_digest tail)

**Files:**
- Modify: `ops/engine_ladder.py`
- Test: `scripts/tests/test_engine_ladder.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_ladder.py`:

```python
def test_module_has_main_entrypoint():
    src = (REPO_ROOT / "ops" / "engine_ladder.py").read_text()
    assert 'if __name__ == "__main__":' in src
    assert "argparse" in src
    assert "def main()" in src


async def test_amain_list_runs_dbless_clean(monkeypatch):
    # No DSN → clean non-zero, no crash (canary -m-no-op lesson: the
    # entrypoint must actually execute, not silently exit 0).
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_IPV4", raising=False)
    rc = await el._amain(["list"])
    assert rc == 1  # explicit "no DSN" failure, not a silent 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -k "main_entrypoint or amain_list" -q`
Expected: FAIL — no `_amain`/`main`/`__main__`.

- [ ] **Step 3: Implement the CLI tail (mirror `weekly_digest`'s `_amain`/`main`)**

Append to `ops/engine_ladder.py`:

```python
def _fmt(rows: list[dict]) -> str:
    if not rows:
        return ("UNDISPOSITIONED ENGINE-LANE ESCALATIONS (0) — "
                "rung-3: each MUST be converted | structural | removed")
    lines = [f"UNDISPOSITIONED ENGINE-LANE ESCALATIONS ({len(rows)}) — "
             "rung-3: each MUST be converted | structural | removed"]
    for r in rows:
        lines.append(
            f"  [{r['shape']}] {r['engine']}/{r['failure_class']} "
            f"hold_id={r['hold_id']} since={r['recorded_at']} "
            f"reason={r['reason']} "
            f"→ policy={r['policy_default']} ({r['policy_rationale']})")
    return "\n".join(lines)


async def _amain(argv: list[str]) -> int:
    dsn = (os.environ.get("DATABASE_URL")
           or os.environ.get("DATABASE_URL_IPV4"))
    if not dsn:
        logger.error("engine_ladder.no_dsn")
        return 1
    p = argparse.ArgumentParser(prog="python -m ops.engine_ladder")
    sub = p.add_subparsers(dest="cmd")
    pl = sub.add_parser("list")
    pl.add_argument("--grace-days", type=int, default=None)
    pd = sub.add_parser("disposition")
    pd.add_argument("hold_id")
    pd.add_argument("verb")
    pd.add_argument("note", nargs="*", default=[])
    args = p.parse_args(argv or ["list"])
    pool = await build_asyncpg_pool(dsn)
    try:
        if args.cmd in (None, "list"):
            rows = await list_undispositioned(
                pool, grace_days=getattr(args, "grace_days", None))
            print(_fmt(rows))
            return 0
        if args.cmd == "disposition":
            return await disposition(pool, args.hold_id, args.verb,
                                     " ".join(args.note))
        p.print_usage(sys.stderr)
        return 2
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover — CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_ladder.py -q`
Expected: PASS (18 passed). Then prove non-no-op: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ops.engine_ladder list 2>&1 | tail -2` — must print the `engine_ladder.no_dsn` error (or, if a DSN is set, the digest header) and exit non-zero/zero accordingly — NOT silent.

- [ ] **Step 5: ruff + check_imports + commit**

```bash
ruff check ops/engine_ladder.py scripts/tests/test_engine_ladder.py
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
git add ops/engine_ladder.py scripts/tests/test_engine_ladder.py
git commit -m "$(cat <<'EOF'
feat(engine_ladder): __main__ CLI — list | disposition

argparse subcommands mirroring ops.weekly_digest's tail; `list`
prints the rung-3 digest, `disposition` records the terminal. -m
invocable (no-DSN → explicit rc=1, never a silent 0 — canary lesson).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Canonical doc (R4) + CLAUDE.md bullet

**Files:**
- Create: `docs/ENGINE_ESCALATION_HARDENING_LADDER.md`
- Modify: `CLAUDE.md` (one bullet, guarded)

- [ ] **Step 1: Write the doc**

Create `docs/ENGINE_ESCALATION_HARDENING_LADDER.md`:

```markdown
# Engine-Lane Escalation & Hardening Ladder

**Principle:** every engine escalation terminates in exactly one of
`converted` (a new bounded deterministic capability — e.g. a new DA-1
detector/healer or DA-2 trigger class), `structural` (a structural fix
to DA-1/DA-2/engine_profile logic or config), or `removed` (the engine
de-escalated from live capital — archived / kill-switched /
graduation-gated out). **Never silent best-effort; never an indefinite
hold without a recorded disposition.** Engine-native; symmetry-
references the data-lane ladder (`docs/ESCALATION_HARDENING_LADDER.md`)
— same shape, NOT a clone, lane-separate.

## The rungs

- **R1 — fail-closed (exists):** DA-1 (`ops/engine_supervisor`) and
  DA-2 (`ops/aar_autotune`) emit `ENGINE_ESCALATED` (+`ENGINE_HELD`
  for held-class) when a bounded agent can't resolve a failure; a held
  engine is gated off by `tpcore.engine_profile.should_fire`. DA-2
  *escalate-only* (noise: outlier_loss / short loss_cluster) emits
  `ENGINE_ESCALATED` with NO hold — the engine keeps trading by
  design.
- **R2 — clockwork forcing-function:** `ops/engine_ladder.py`
  `DISPOSITION_POLICIES` covers every class in
  `KNOWN_ESCALATION_CLASSES` (derived from
  `engine_supervisor.INFRA_FAILURE_CLASSES |
  {aar_autotune._BEHAVIORAL}`, with `_classify` pinned to the
  constant). `escalation_drift()` ⇒ a new DA-1/DA-2 class **fails the
  build** until a policy is recorded.
- **R3 — surface + disposition:** `python -m ops.engine_ladder list`
  shows undispositioned instances past a 7-day grace
  (`ENGINE_LADDER_GRACE_DAYS`). Held-class closes on `ENGINE_CLEARED`
  or a disposition; **escalate-only** closes on a disposition OR all
  its payload `triggers` fingerprints resolved/absent from
  `forensics_triggers` (so the "every escalation terminates" claim is
  literally true for the no-hold case). `python -m ops.engine_ladder
  disposition <hold_id> <converted|structural|removed> [note]` records
  an event-sourced `ENGINE_ESCALATION_DISPOSITIONED`.
- **R4 — structural removal levers (existing, no new code):** the
  `removed` disposition is physically realized via
  `RiskGovernor.emergency_kill`/kill-switch, the DSR/credibility
  graduation gate, or `archive/<engine>/EULOGY.md` + the Engine SDLC
  snap-out.
- **R5 — LLM/agentic triage:** OUT of scope (Epic E).

## Operator workflow

`python -m ops.engine_ladder list` → triage the Sprint Dossier / logs
→ apply the fix (or remove the engine) → `python -m ops.engine_ladder
disposition <hold_id> <converted|structural|removed> "<note>"`.

## Disposition vocabulary

`converted` · `structural` · `removed` (no `auto_converted` — the
engine lane has no auto-conversion actor; R3 surfaces, it does not
auto-apply fixes).
```

- [ ] **Step 2: Add the CLAUDE.md bullet (guarded vs the concurrent data session)**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-ladder && git diff --stat CLAUDE.md` — if it shows unrelated working-tree changes you did NOT make (a parallel session), use `git add -p CLAUDE.md` to stage ONLY your bullet hunk in Step 4. Read `CLAUDE.md`, find the existing data-lane-escalation-contract bullet (the one mentioning `docs/ESCALATION_HARDENING_LADDER.md`), and add immediately after it a parallel one-line bullet:

```
- **Engine-lane escalation contract (2026-05-18):** `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` is the canonical engine-lane contract — every engine escalation class carries a disposition (`converted`/`structural`/`removed`), clockwork-enforced via `ops/engine_ladder.escalation_drift()` (a new DA-1/DA-2 class fails the build until a policy is recorded); undispositioned instances past the 7-day grace surface via `python -m ops.engine_ladder list`; escalate-only (no-hold) escalations close on disposition or trigger-fingerprint resolution. Engine lane only; symmetry-references the data-lane ladder, not a clone.
```

- [ ] **Step 3: Verify docs**

Run: `test -f docs/ENGINE_ESCALATION_HARDENING_LADDER.md && grep -q "Engine-lane escalation contract" CLAUDE.md && echo OK`
Expected: `OK`.

- [ ] **Step 4: Commit (stage ONLY our hunks)**

```bash
git add docs/ENGINE_ESCALATION_HARDENING_LADDER.md
git add -p CLAUDE.md   # interactively stage ONLY the new engine-lane bullet hunk; if non-interactive isn't possible and CLAUDE.md has ONLY your change, `git add CLAUDE.md`
git commit -m "$(cat <<'EOF'
docs(engine_ladder): canonical Engine Escalation & Hardening Ladder

R1-R5 + disposition vocabulary + operator workflow; symmetry-references
the data-lane ladder, engine-native, lane-separate. + a parallel
CLAUDE.md engine-lane-escalation-contract bullet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If the data session has uncommitted CLAUDE.md changes in this shared file, `git add -p` MUST select only the new bullet; never stage their hunks. Report if encountered.

---

## Task 7: Full-suite + CI gate + finish

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-ladder && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -4`
Expected: PASS (entire suite green incl. `scripts/tests/test_engine_ladder.py` + the supervisor SoT-pin tests; DA-1 supervisor suite green = the extract oracle).

- [ ] **Step 2: CI-exact lint + import-layering + lane scope**

Run: `ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/ && /Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `All checks passed!` + `ok: no forbidden imports found`. Then assert lane discipline: `BASE=$(git merge-base HEAD origin/main); git diff --name-only $BASE..HEAD | grep -E "tpcore/ladder/|ops/weekly_digest|ops/data_repair_service|ops/cutover_agent|ops/aar_autotune\.py|tpcore/(selfheal|feeds|ingestion)"` → MUST be empty (no data-lane/DA-2-logic file changed; only `ops/engine_supervisor.py`'s constant extract is an allowed DA-1 touch). `grep -nE "^(from|import) (tpcore\.ladder|ops\.weekly_digest)" ops/engine_ladder.py` → empty (no data-lane import).

- [ ] **Step 3: Finish the branch**

Use **superpowers:finishing-a-development-branch**. Per the standing pattern: push the worktree branch, open a PR, fetch origin/main and resolve conflicts to combine intents (the data session is iterating its own ladder + CLAUDE.md concurrently — do NOT clobber their hunks; CLAUDE.md merge resolves by keeping BOTH the data-lane and engine-lane bullets, as in DA-1's CLAUDE.md merge), integrated full suite green, merge when CI green, clean the worktree. Do NOT local-merge into the shared checkout.

---

## Self-Review

**1. Spec coverage:** §2 mandate (R2 build-break + R3 surfacing, no extra trade teeth) → T2 (drift) + T3/T4/T5 (surface+verb+CLI). §3 registry/enum/drift/`INFRA_FAILURE_CLASSES` SoT pin incl. `_classify` → T1 (constant + `_classify` SoT-pin test) + T2 (registry/drift, expert-corrected `data_repair_escalated→STRUCTURAL`, no-tautology drift via `_drift_for`). §4 two shapes (held + escalate-only auto-close on resolved fingerprints; disposition accepts escalate-only, not gated on current_hold) → T3 (`list_undispositioned` both shapes) + T4 (`disposition` accepts escalate-only). §5 R4 doc + CLAUDE.md bullet → T6. §6 lane discipline (only DA-1 constant-extract touch; no data-lane/weekly_digest/tpcore-ladder) → T1 scope + T7 Step 2 assertion. §7 determinism/bounded → T3/T4 (grace-windowed reads, one emit). §8 testing list → T1-T5 each TDD incl. the non-tautology drift proof + the escalate-only-all-resolved-excluded-before-grace test + the `__main__`-no-op regression test. §9 scope / §10 decisions (D-EL-1..9) → covered. No gaps.

**2. Placeholder scan:** No "TBD/handle errors/similar to Task N". Every code step is complete literal code; every command has an expected result. The few "align the fake-pool SQL routing / arg position to the real call if it differs" notes are explicit bounded verify-against-reality contingencies with the invariant pinned + "keep the asserted behavior" — the accepted style from C/DA-1/DA-2/canary plans, not deferred work. `import enum` placement + the `row[0].get("engine","engine")` safe-form are called out explicitly with the exact fix.

**3. Type/name consistency:** `INFRA_FAILURE_CLASSES: frozenset[str]` (T1) ← imported by `engine_ladder` (T2) + the supervisor SoT-pin test (T1). `EngineEscalationDisposition`/`DispositionPolicy`/`DISPOSITION_POLICIES`/`KNOWN_ESCALATION_CLASSES`/`escalation_drift`/`_drift_for`/`policy_for` consistent T2↔tests. `list_undispositioned(pool,*,now,grace_days)` consistent T3 def↔tests↔T5 CLI. `disposition(pool,hold_id,verb,note)->int` consistent T4 def↔tests↔T5 CLI. `_amain`/`main`/`__main__` consistent T5↔tests. Event `ENGINE_ESCALATION_DISPOSITIONED` + payload `{schema:1,hold_id,disposition,note}` consistent T4 impl↔test↔doc↔CLAUDE bullet. `_BEHAVIORAL` imported from `ops.aar_autotune`, `INFRA_FAILURE_CLASSES` from `ops.engine_supervisor` — ops→ops, layering-safe. No mismatches.
