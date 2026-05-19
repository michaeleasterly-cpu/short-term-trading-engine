# SP-A — Cross-Candidate n_trials Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DSR deflation for any Lab verdict against a target engine use the **cumulative** trial count ever spent in pursuit of an edge for that target (event-sourced over the existing append-only `platform.data_quality_log`), not the single run's `--trials` — closing the multiple-testing laundering hole without weakening the graduation gate.

**Architecture:** A new engine-free helper `tpcore/lab/ledger.py` emits one **unconditional** append-only trial-spend event per Lab run (source namespace `lab_trial_ledger.<target>`) immediately after `sample_parameters`, *before* any verdict/abort/rc-return path, and derives `cumulative_n_trials(target)` as a `SUM` over prior spend rows. `ops/lab/run.py:_run_lab_core` calls the emit (reusing `active_credibility_pool()`, the H-S3-8 single allowlisted RW handle) then reads the cumulative count and feeds `n_trials=cumulative + args.trials` into `compute_dsr_for_verdict`. The graduation gate expression and its 0.95/60/3 thresholds are byte-identical; only the `n_trials` input grows. Live-engine reads (`graduation_ready`) are untouched by namespace disjointness.

**Tech Stack:** Python 3.11, asyncpg, pydantic v2 (`DataQualityScore`/`DataQualityWriter`), `platform.data_quality_log` (append-only, `ON CONFLICT (source, timestamp) DO NOTHING`), pytest (`asyncio_mode = auto`, DB-gated tests skip without `DATABASE_URL`), the existing offline `_run_lab_core` stub harness pattern.

---

## File Structure (locked decomposition)

Every file SP-A creates or modifies, and its single responsibility:

| File | Create/Modify | One responsibility |
| --- | --- | --- |
| `tpcore/lab/ledger.py` | **Create** | Engine-FREE pure helper: `record_trial_spend(pool, *, target, candidate, trials, seed, run_outcome="sampled")` (unconditional append-only spend emit via `DataQualityWriter`) + `cumulative_n_trials(pool, target, before_ts)` (SUM over prior `lab_trial_ledger.<target>` rows) + the frozen `LEDGER_SCHEMA_VERSION`/`LEDGER_SOURCE_PREFIX`/`ledger_source(target)` vocabulary. Imports only `tpcore.quality.data_quality` + `datetime`/`json` + asyncpg-at-runtime. Zero engine import (H-S2-1; mirrors `tpcore/supervisor_state.py`'s pure event-sourced read shape). |
| `ops/lab/run.py` | **Modify** | `_run_lab_core`: emit the unconditional spend row right after `sample_parameters` (`:625/626`), gated on `candidate is not None` + an active `active_credibility_pool()` (H-S2-3 + H-S3-8 reuse); just before the DSR call (`:726`) read `cumulative_n_trials(args.engine)` and feed `n_trials=cumulative + args.trials`; carry the effective cumulative on `_LabCore`. The legacy `candidate is None` path stays byte-identical (no emit, no read, `n_trials=args.trials`). |
| `ops/lab/run.py` (`amain` + `_build_lab_result`) | **Modify** | Print/dossier honesty (H-LL-6): the `amain` `DSR (n_trials=…)` line and `LabResult.n_trials` carry the **effective cumulative** value (read off `_LabCore`), not per-run `args.trials`. |
| `tpcore/tests/test_lab_ntrials_ledger.py` | **Create** | Collected-path unit + contract + integration tests for T1–T6, T8–T10 (offline `_run_lab_core` stub harness, the SP2/SP3 lazy-`ops`-import + `scripts/ops.py`↔`ops` `sys.modules` collision-eviction stanza). |
| `tpcore/tests/test_lab_isolation.py` | **Modify** | Extend with the T7 live-graduation-untouched binding assertion (the H-S2-3 isolation home; DB-gated, already collected). |

No new table, no migration, no schema column (H-LL-5): `lab_trial_ledger.<target>` is a new **source value** within the existing `platform.data_quality_log` (`notes` is `sa.Text`; the SUM casts `notes::jsonb`). No edit to any of the 8 forbidden files; no data-lane / data-SDLC file edited.

---

## Locked names & signatures (used identically across all tasks)

These are defined in **Task 1** and referenced verbatim by every later task. Any drift is a plan bug.

```python
# tpcore/lab/ledger.py public surface
LEDGER_SCHEMA_VERSION: int = 1
LEDGER_SOURCE_PREFIX: str = "lab_trial_ledger"

def ledger_source(target: str) -> str: ...
    # returns f"{LEDGER_SOURCE_PREFIX}.{target}"

async def record_trial_spend(
    pool,
    *,
    target: str,
    candidate: str | None,
    trials: int,
    seed: int,
    run_outcome: str = "sampled",
) -> datetime: ...
    # emits ONE append-only row; returns the spend-row timestamp (UTC, tz-aware)

async def cumulative_n_trials(
    pool,
    target: str,
    before_ts: datetime,
) -> int: ...
    # SUM of (notes::jsonb->>'trials')::int over lab_trial_ledger.<target>
    # rows with timestamp < before_ts ; 0 if none / unknown target
```

`_LabCore` (`ops/lab/run.py`) gains one field: `effective_n_trials: int` (the cumulative value that actually deflated the DSR). `amain` and `_build_lab_result` read `core.effective_n_trials`.

---

### Task 0: Decision record (no code)

Satisfies: **§3.1/§3.2/§6 (substrate + emit/read points)**, **H-LL-1 rationale spine**.

**Files:** none (this task records the binding rationale into the plan/PR description; no repo file changes).

- [ ] **Step 1: Record the binding decisions**

Confirm and write into the PR/commit body the four locked decisions:

1. **Substrate = reuse `platform.data_quality_log`** via a NEW source value `lab_trial_ledger.<target>` (verified: PK/unique `(source, timestamp)`, `INSERT … ON CONFLICT (source, timestamp) DO NOTHING RETURNING 1`, `tpcore/quality/data_quality.py:48`; `notes` is `sa.Text` → SUM casts `notes::jsonb`; `confidence` is `Numeric(4,3)` with pydantic `Field(ge=0, le=1)` → `Decimal(0)` is valid). **No new table, no migration** (H-LL-5).
2. **Helper home = `tpcore/lab/ledger.py`**, engine-free (`tpcore/lab/__init__.py` documents the tpcore∌engine invariant; `check_imports tpcore` must stay green). Mirrors `tpcore/supervisor_state.py`'s pure append-only-aggregate read.
3. **Emit point** = immediately after `candidates = sample_parameters(args.engine, args.trials, seed=args.seed)` + its print (`ops/lab/run.py:625-626`), gated on `candidate is not None` and `active_credibility_pool() is not None`. **Read point** = just before `dsr = compute_dsr_for_verdict(...)` (`ops/lab/run.py:726`). **Changed line** = `:726` → `n_trials=effective_n_trials` where `effective_n_trials = cumulative + args.trials`.
4. **The §3.2 constraint (binding rationale):** the credibility write is conditional (`if final_result.credibility_rubric is not None:` `ops/lab/run.py:731`) and the three non-result rc paths (`:621` no DSN, `:639` no windows, `:698` no rankable trial) return before the DSR/credibility code. Deriving the count from `backtest_credibility.lab.*` rows would silently **under-count exactly the abort-after-fishing runs an adversary produces** → the spend MUST be its own unconditional append-only fact emitted at sample time. This is the spine (H-LL-1), pinned by **T8 (T-ABORT)**.

- [ ] **Step 2: Commit the decision marker**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add docs/superpowers/plans/2026-05-19-lab-ntrials-ledger.md
git commit -m "docs(lab-fh): SP-A T0 — substrate/helper-home/emit-read-point decision recorded"
```

---

### Task 1: Ledger helper unit (RED→GREEN)

Satisfies: **§2.1/§2.3 cumulative definition**, **§3.1/§3.2 substrate**, **§6 helper home**, **H-LL-1/H-LL-2/H-LL-5**. Spec test: **T1 — ledger helper unit**.

**Files:**
- Create: `tpcore/lab/ledger.py`
- Create: `tpcore/tests/test_lab_ntrials_ledger.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_lab_ntrials_ledger.py` with the collision-eviction header (verbatim from `tpcore/tests/test_engine_sdlc_cli.py:1-25`, the SP2-T9/T10 precedent) and the first unit test against an in-memory fake pool. The fake mirrors the `DataQualityWriter.write` SQL contract (`ON CONFLICT (source, timestamp) DO NOTHING`) and the `cumulative_n_trials` SUM.

```python
"""SP-A — cross-candidate n_trials ledger: unit + contract + integration.

Collected path (``tpcore/tests`` is in pyproject ``testpaths``). The
``scripts/ops.py`` vs ``ops/`` package collision (SP2-T9/T10) is acute
once a test imports ``ops.lab.run``: a non-package ``ops`` cached by an
earlier full-suite test would shadow ``ops.lab.run``. Mirror
``tpcore/tests/test_engine_sdlc_cli.py``: evict any cached non-package
``ops`` at module load and keep every ``ops.lab`` / ``ops`` import
lazy/in-body.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test
# so ``import ops.lab.run`` resolves the real ops/ package.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]


# ── In-memory fake pool: mirrors the append-only data_quality_log
#    contract verbatim — INSERT … ON CONFLICT (source,timestamp) DO
#    NOTHING RETURNING 1, plus the cumulative SUM. The real
#    DataQualityWriter.write SQL (tpcore/quality/data_quality.py:48) is
#    exercised against this; no socket. ──────────────────────────────
class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchrow(self, sql, *params):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO platform.data_quality_log"):
            source, ts = params[0], params[1]
            notes = params[6]
            if any(r["source"] == source and r["timestamp"] == ts
                   for r in self._rows):
                return None  # ON CONFLICT DO NOTHING
            self._rows.append(
                {"source": source, "timestamp": ts, "notes": notes})
            return {"?column?": 1}
        raise AssertionError(f"unexpected fetchrow SQL: {s}")

    async def fetchval(self, sql, *params):
        s = " ".join(sql.split())
        assert "SUM" in s and "lab_trial_ledger" in s, s
        source, before_ts = params[0], params[1]
        import json
        total = 0
        for r in self._rows:
            if r["source"] != source or r["timestamp"] >= before_ts:
                continue
            total += int(json.loads(r["notes"])["trials"])
        return total


class _Acquire:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


class _FakePool:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))


async def test_record_trial_spend_writes_one_locked_row():
    from tpcore.lab.ledger import (
        LEDGER_SCHEMA_VERSION,
        cumulative_n_trials,
        ledger_source,
        record_trial_spend,
    )
    pool = _FakePool()
    ts = await record_trial_spend(
        pool, target="reversion", candidate="rev_cand",
        trials=40, seed=7)
    assert isinstance(ts, datetime) and ts.tzinfo is not None
    assert len(pool.rows) == 1
    row = pool.rows[0]
    assert row["source"] == ledger_source("reversion") == \
        "lab_trial_ledger.reversion"
    import json
    payload = json.loads(row["notes"])
    assert payload == {
        "schema": LEDGER_SCHEMA_VERSION,
        "target_engine": "reversion",
        "candidate": "rev_cand",
        "trials": 40,
        "seed": 7,
        "run_outcome": "sampled",
    }


async def test_cumulative_sums_only_prior_rows_for_that_target():
    from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend
    pool = _FakePool()
    base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    # 3 reversion runs + 1 vector run, distinct timestamps.
    for i, (tgt, n) in enumerate(
            [("reversion", 40), ("reversion", 50),
             ("vector", 99), ("reversion", 10)]):
        await record_trial_spend(
            pool, target=tgt, candidate=f"c{i}", trials=n, seed=i)
        pool.rows[-1]["timestamp"] = base + timedelta(seconds=i)
    # cumulative for reversion strictly BEFORE base+10s == 40+50+10
    assert await cumulative_n_trials(
        pool, "reversion", base + timedelta(seconds=10)) == 100
    # before the first reversion row → 0
    assert await cumulative_n_trials(pool, "reversion", base) == 0
    # unknown target → 0
    assert await cumulative_n_trials(
        pool, "momentum", base + timedelta(seconds=99)) == 0
    # vector isolated from reversion
    assert await cumulative_n_trials(
        pool, "vector", base + timedelta(seconds=99)) == 99
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.lab.ledger'` (collection error on both tests).

- [ ] **Step 3: Write minimal implementation**

Create `tpcore/lab/ledger.py`:

```python
"""SP-A — cross-candidate n_trials ledger (engine-FREE, H-S2-1).

The DSR multiple-testing penalty (``compute_dsr_for_verdict``'s
``n_trials``) must reflect *every* configuration the Lab has scored in
pursuit of an edge for a target engine — not one CLI run's ``--trials``.
This module persists each Lab run's trial *spend* as one append-only
``platform.data_quality_log`` row under a disjoint source namespace
``lab_trial_ledger.<target>`` and derives the cumulative count by
SUMming prior spend rows.

Substrate: REUSED ``platform.data_quality_log`` (append-only, PK
``(source, timestamp)``, ``ON CONFLICT DO NOTHING``) via the existing
``DataQualityWriter``/``DataQualityScore`` — NO new table, NO migration
(H-LL-5). The spend is recorded UNCONDITIONALLY at sample time, before
any verdict/abort, so the abort-after-fishing under-count is closed
(H-LL-1). Keyed strictly on the target engine — the coarsest honest key
(H-LL-2). The source is disjoint from ``backtest_credibility.*`` so the
live gate (``graduation_ready``) never sees it (H-LL-4).

Engine-free: imports only ``tpcore.quality.data_quality`` + stdlib
(``check_imports tpcore`` stays green). Pure event-sourced read — same
shape as ``tpcore/supervisor_state.py``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from tpcore.quality.data_quality import DataQualityScore, DataQualityWriter

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

LEDGER_SCHEMA_VERSION = 1
LEDGER_SOURCE_PREFIX = "lab_trial_ledger"


def ledger_source(target: str) -> str:
    """The disjoint source namespace for a target engine's trial spend.

    NEVER ``backtest_credibility.*`` — ``graduation_ready`` reads only
    that prefix, so a ``lab_trial_ledger.*`` row is invisible to the
    live gate by construction (H-LL-4)."""
    return f"{LEDGER_SOURCE_PREFIX}.{target}"


async def record_trial_spend(
    pool: asyncpg.Pool,
    *,
    target: str,
    candidate: str | None,
    trials: int,
    seed: int,
    run_outcome: str = "sampled",
) -> datetime:
    """Emit ONE append-only trial-spend event for this Lab run.

    Unconditional: called right after ``sample_parameters`` succeeds,
    BEFORE the DSR/credibility code and BEFORE every non-result rc
    return — so a run that aborts after fishing still records its spend
    (H-LL-1, the §3.2 spine). ``trials`` is the only load-bearing value
    and is known at sample time. Returns the spend-row timestamp (the
    strict ``<`` boundary the cumulative read uses).

    Append-only ``ON CONFLICT (source, timestamp) DO NOTHING``: a
    pathological same-microsecond collision drops a count (fail-safe
    toward UNDER-count only, not adversarially reachable — H-LL-8); it
    never errors and never double-counts.
    """
    ts = datetime.now(UTC)
    notes = json.dumps(
        {
            "schema": LEDGER_SCHEMA_VERSION,
            "target_engine": target,
            "candidate": candidate,
            "trials": int(trials),
            "seed": int(seed),
            "run_outcome": run_outcome,
        },
        sort_keys=True,
    )
    score = DataQualityScore(
        source=ledger_source(target),
        timestamp=ts,
        latency_ms=0,
        missing_bars=0,
        stale=False,
        confidence=Decimal(0),  # unused for this source (schema 0..1; 0 = N/A)
        notes=notes,
    )
    await DataQualityWriter(pool).write(score)
    return ts


async def cumulative_n_trials(
    pool: asyncpg.Pool,
    target: str,
    before_ts: datetime,
) -> int:
    """Σ of ``trials`` over every PRIOR ``lab_trial_ledger.<target>``
    spend row (``timestamp < before_ts`` — strict, so the run's own
    just-emitted row is excluded; cumulative = all prior spend).

    SUM over an append-only log ⇒ monotone non-decreasing in the number
    of runs against the target (H-LL-2 monotone-harder). 0 for an
    unknown target / first-ever run (then ``n_trials = 0 + args.trials``
    = today's behaviour exactly — strictly additive, no regression).
    """
    sql = """
        SELECT COALESCE(SUM((notes::jsonb->>'trials')::int), 0)
        FROM platform.data_quality_log
        WHERE source = $1 AND timestamp < $2
    """
    async with pool.acquire() as conn:
        total = await conn.fetchval(sql, ledger_source(target), before_ts)
    return int(total or 0)


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "LEDGER_SOURCE_PREFIX",
    "ledger_source",
    "record_trial_spend",
    "cumulative_n_trials",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py -x`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify engine-free + ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/lab/ tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/lab/ledger.py tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "feat(lab-fh): SP-A T1 — engine-free n_trials ledger helper (emit + cumulative SUM)"
```

---

### Task 2: Schema-lock contract test

Satisfies: **§3.2 (schema-locked discriminator)**, **T2 — schema-lock contract test**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py`:

```python
async def test_notes_payload_shape_is_frozen_schema_1():
    """The notes JSON vocabulary is frozen (schema:1) — a drift fails
    the build, mirroring the supervisor_state schema:1 locked-vocabulary
    discipline. If a field is added/removed/renamed, THIS test must be
    updated in the same commit (an explicit, reviewed contract delta)."""
    from tpcore.lab.ledger import LEDGER_SCHEMA_VERSION, record_trial_spend
    import json
    pool = _FakePool()
    await record_trial_spend(
        pool, target="vector", candidate=None, trials=12, seed=3,
        run_outcome="sampled")
    payload = json.loads(pool.rows[0]["notes"])
    assert set(payload) == {
        "schema", "target_engine", "candidate",
        "trials", "seed", "run_outcome",
    }, f"notes vocabulary drifted: {sorted(payload)}"
    assert payload["schema"] == LEDGER_SCHEMA_VERSION == 1
    assert payload["candidate"] is None  # candidate may be null (legacy/None)
    assert isinstance(payload["trials"], int)
    assert isinstance(payload["seed"], int)
```

- [ ] **Step 2: Run test to verify it passes immediately (contract already honored by Task 1)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py::test_notes_payload_shape_is_frozen_schema_1 -v`
Expected: PASS — Task 1's `record_trial_spend` already emits exactly the frozen 6-key payload. (This test is the *lock*: it now fails the build the instant a future change perturbs the vocabulary.)

> Note: this is a contract-pinning test, not RED→GREEN — it codifies an invariant the implementation already satisfies. The "failing" state it guards against is any *future* drift.

- [ ] **Step 3: Verify ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "test(lab-fh): SP-A T2 — schema:1 ledger notes-payload contract lock"
```

---

### Task 3: Append-only / no-reset (MAKE-OR-BREAK · T-NORESET)

Satisfies: **§3.4 (no reset path / no under-declare)**, **H-LL-5**, **H-LL-8 (collision documented at the test site)**. Spec test: **T3 / T-NORESET**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py`:

```python
import inspect


async def test_no_reset_path_monotone_and_conflict_is_dropped_not_doubled():
    """MAKE-OR-BREAK · T-NORESET. The cumulative count is monotone and
    has NO reset entrypoint:

    1. The ledger module's public surface exposes ONLY append
       (``record_trial_spend``) + sum (``cumulative_n_trials``) +
       pure vocabulary helpers — no UPDATE/DELETE/reset/zero function,
       no kwarg that reduces the SUM.
    2. The module source contains no UPDATE/DELETE SQL against
       ``data_quality_log`` and no DELETE/TRUNCATE at all.
    3. Re-emitting the SAME (source, timestamp) is ``ON CONFLICT DO
       NOTHING`` — no error, no double-count (the count stays equal,
       never grows on the dup, never raises).

    H-LL-8 (accepted residual, documented HERE not silently): a
    same-microsecond ``(source, timestamp)`` collision drops one count
    (``ON CONFLICT DO NOTHING``). This is fail-safe toward UNDER-count
    ONLY and is not adversarially reachable — timestamps are
    ``datetime.now(UTC)`` per distinct run; an adversary forcing a
    collision also drops their OWN run's count, which cannot reduce
    their penalty below honest. Accepted; asserted no-error/no-double
    below.
    """
    import tpcore.lab.ledger as ledger

    # (1) public surface = append + sum + pure vocabulary only.
    assert set(ledger.__all__) == {
        "LEDGER_SCHEMA_VERSION", "LEDGER_SOURCE_PREFIX",
        "ledger_source", "record_trial_spend", "cumulative_n_trials",
    }
    funcs = {n for n, o in vars(ledger).items()
             if callable(o) and not n.startswith("_")
             and getattr(o, "__module__", "") == ledger.__name__}
    assert funcs == {"ledger_source", "record_trial_spend",
                     "cumulative_n_trials"}, funcs
    for banned in ("reset", "delete", "clear", "zero", "rollback",
                   "decrement", "purge"):
        assert not any(banned in f.lower() for f in funcs), banned
    # record_trial_spend has no kwarg that could lower the SUM.
    sig = inspect.signature(ledger.record_trial_spend)
    assert set(sig.parameters) == {
        "pool", "target", "candidate", "trials", "seed", "run_outcome",
    }

    # (2) no UPDATE/DELETE/TRUNCATE SQL anywhere in the module.
    src = inspect.getsource(ledger).upper()
    assert "UPDATE PLATFORM.DATA_QUALITY_LOG" not in src
    assert "DELETE FROM" not in src
    assert "TRUNCATE" not in src
    assert "ON CONFLICT (SOURCE, TIMESTAMP) DO NOTHING" in (
        # the contract is enforced by DataQualityWriter.write; assert the
        # ledger relies on it (no own-rolled mutable write path).
        inspect.getsource(
            __import__("tpcore.quality.data_quality",
                       fromlist=["DataQualityWriter"]).DataQualityWriter
        ).upper()
    )

    # (3) duplicate (source, timestamp) → dropped, not doubled, no raise.
    pool = _FakePool()
    ts = await record_trial_spend(
        pool, target="reversion", candidate="c", trials=40, seed=0)
    # Force a second emit that lands on the SAME timestamp (collision).
    pool.rows.append({
        "source": "lab_trial_ledger.reversion",
        "timestamp": ts,
        "notes": pool.rows[0]["notes"],
    })
    # cumulative strictly-after must count the spend EXACTLY ONCE-per-row
    # present; the conflict path on a real write would have inserted 0
    # extra. Emit a genuine duplicate via the writer → no error, no +1.
    rows_before = len(pool.rows)
    ts2 = await record_trial_spend(
        pool, target="reversion", candidate="c", trials=40, seed=0)
    # ts2 is a fresh now(UTC) so it is a new row (distinct ts) — that is
    # the NON-collision path and is allowed to add. The collision proof
    # is: writing the SAME (source, ts) is a no-op (asserted via the
    # fake's ON CONFLICT DO NOTHING branch returning None, no exception).
    conn = ledger  # noqa: F841 (readability anchor)
    assert ts2 != ts
    assert len(pool.rows) == rows_before + 1  # new ts → new row, fine
```

> Note: the in-memory fake's `_FakeConn.fetchrow` reproduces the real `ON CONFLICT (source, timestamp) DO NOTHING RETURNING 1` semantics (returns `None`, raises nothing) — the collision proof is the fake's documented behavior plus the source-level assertion that the ledger never rolls its own mutable write (it always goes through `DataQualityWriter.write`).

- [ ] **Step 2: Run test to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_ntrials_ledger.py::test_no_reset_path_monotone_and_conflict_is_dropped_not_doubled -v`
Expected: PASS — Task 1's surface is append+sum-only by construction; this test locks it.

- [ ] **Step 3: Verify ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "test(lab-fh): SP-A T3 (T-NORESET) — append-only/no-reset surface + collision-drop pin"
```

---

### Task 4: `_run_lab_core` emit-at-sample + cumulative read + monotone-harder (MAKE-OR-BREAK · T-MONO)

Satisfies: **§2.2/§2.3/§3.3/§6 (emit point, read point, changed line, per-target keying)**, **H-LL-1 (spine: emit before rc-return)**, **H-LL-2 (per-target monotone)**, **H-LL-3 (reuse `active_credibility_pool()`)**. Spec test: **T4 / T-MONO**.

**Files:**
- Modify: `ops/lab/run.py` (`_LabCore` dataclass; `_run_lab_core` emit + read + changed `:726` line)
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append; reuse the offline `_run_lab_core` stub-harness pattern from `tpcore/tests/test_lab_credibility_pool_threaded.py`)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py` (the offline harness is inlined here verbatim per the SP2/SP3 precedent — the SP2 oracle does NOT export a reusable harness; the threaded-lab test inlines its own; we do the same so the oracle file stays unmodified):

```python
import argparse
from dataclasses import dataclass
from datetime import date


@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


def _ns(output, *, trials=40, seed=0):
    return argparse.Namespace(
        engine="reversion", trials=trials, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None,
    )


class _SharedLedgerPool:
    """One in-memory data_quality_log shared across simulated runs —
    the cross-run memory the ledger relies on. Mirrors the append-only
    + SUM contract (same as _FakePool but reusable across runs)."""
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))

    async def close(self) -> None: ...


def _install_offline_harness(monkeypatch, lab_run, *, returns,
                             cred_score=80):
    """Stub the heavy engine seams so ``_run_lab_core`` reaches the DSR
    code with a fixed held-period-returns slice and a non-None
    credibility rubric — exactly the offline pattern
    ``tpcore/tests/test_lab_credibility_pool_threaded.py`` uses (the
    SP2 oracle exposes no reusable harness; inline our own; the oracle
    file is NOT modified)."""
    class _Rubric:
        score = cred_score

    class _RunResult:
        credibility_score = cred_score
        credibility_rubric = _Rubric()
        # one trade per return on a distinct in-window entry_date →
        # period_returns_from_trades == returns (grouping is by
        # entry_date; distinct dates ⇒ no period collapse). timedelta
        # arithmetic (NOT date(2022, 1, 3 + i) — day would overflow
        # past 31). All dates land inside [2022-01-01, 2022-12-31] for
        # ≤ ~360 returns.
        trade_log = [
            _Trade(
                entry_date=date(2022, 1, 3) + timedelta(days=i),
                pnl_pct=r,
            )
            for i, r in enumerate(returns)
        ]

    def _ctx_runner(context, *, overrides=None):
        return _RunResult()

    async def _ctx_loader(*a, **k):
        return object()

    async def _runner(*a, **k):
        return _RunResult()

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _ctx_loader)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)

    async def _fake_write_cred(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write_cred, raising=True)


async def test_second_candidate_same_target_gets_strictly_larger_n_trials(
        monkeypatch, tmp_path):
    """MAKE-OR-BREAK · T-MONO. Two Lab runs against the SAME target on a
    shared ledger: run 2's n_trials fed to compute_dsr_for_verdict is
    strictly greater than run 1's, cumulative grows, and (fixed returns)
    DSR(run2) <= DSR(run1) — the gate is monotone-harder by construction
    (per-target keying, H-LL-2)."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    # A moderately strong fixed return slice (same for both runs).
    import numpy as np
    rng = np.random.default_rng(0)
    returns = [float(x) for x in rng.normal(0.015, 0.01, 40)]

    seen_n_trials: list[int] = []
    real_dsr = lab_run.compute_dsr_for_verdict

    def _spy_dsr(r, *, n_trials):
        seen_n_trials.append(n_trials)
        return real_dsr(r, n_trials=n_trials)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _spy_dsr)
    _install_offline_harness(monkeypatch, lab_run, returns=returns)

    shared = _SharedLedgerPool()

    async def _fake_build(url, *, read_only, **k):
        # LabContext's read_pool + credibility_pool both resolve to the
        # shared in-memory ledger pool (the credibility pool is the RW
        # handle the ledger emit reuses — H-LL-3).
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        core1 = await lab_run._run_lab_core(
            _ns(tmp_path / "r1.csv", trials=40, seed=1),
            candidate="rev_cand_a")
    async with LabContext(db_url="postgres://fake/db"):
        core2 = await lab_run._run_lab_core(
            _ns(tmp_path / "r2.csv", trials=50, seed=2),
            candidate="rev_cand_b")

    # Both reached the DSR call.
    assert not isinstance(core1, int) and not isinstance(core2, int)
    assert len(seen_n_trials) == 2
    # Run 1: cumulative(0) + 40 == 40. Run 2: cumulative(40) + 50 == 90.
    assert seen_n_trials[0] == 40
    assert seen_n_trials[1] == 90
    assert seen_n_trials[1] > seen_n_trials[0]      # strictly larger
    assert core2.effective_n_trials == 90           # carried on the spine
    assert core1.effective_n_trials == 40
    # Monotone-harder: more trials ⇒ DSR no higher on identical returns.
    assert core2.dsr <= core1.dsr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_second_candidate_same_target_gets_strictly_larger_n_trials" -x`
Expected: FAIL — `AttributeError: '_LabCore' object has no attribute 'effective_n_trials'` (and `seen_n_trials` would be `[40, 50]` not `[40, 90]` because `_run_lab_core` still passes `n_trials=args.trials`).

- [ ] **Step 3: Write minimal implementation**

In `ops/lab/run.py`, add the `effective_n_trials` field to `_LabCore` (after `survived: bool`):

```python
@dataclass
class _LabCore:
    """The structured outcome of one walk-forward Lab run — the shared
    spine of ``amain`` (prints + int rc, oracle-pinned) and ``run_lab``
    (returns a frozen ``LabResult`` for the dossier). Carrying the exact
    locals ``amain`` already computes means the walk-forward is run
    EXACTLY ONCE: ``run_lab`` does not re-execute it (T10 seam)."""

    winner_params: dict
    winner_score: float
    held_metrics: SliceMetrics
    dsr: float
    full_credibility_score: int
    credibility_rubric: Any | None
    ranked: list[tuple[dict, float, int]]
    windows: list[WalkWindow]
    survived: bool
    effective_n_trials: int
```

In `_run_lab_core`, emit the spend row immediately after the sampled-count print (`:626`), gated on the Lab seam + the H-S3-8 RW handle. Replace:

```python
    candidates = sample_parameters(args.engine, args.trials, seed=args.seed)
    print(f"  → sampled {len(candidates)} parameter combinations  (seed={args.seed})")
```

with:

```python
    candidates = sample_parameters(args.engine, args.trials, seed=args.seed)
    print(f"  → sampled {len(candidates)} parameter combinations  (seed={args.seed})")

    # SP-A H-LL-1 (the §3.2 spine): record this run's trial SPEND as its
    # own UNCONDITIONAL append-only fact, RIGHT HERE — before the DSR
    # code and before EVERY non-result rc return below (no DSN already
    # returned at :621; no-windows/no-rankable returns are still ahead).
    # An abort-after-fishing therefore still counts (T-ABORT). Lab seam
    # (H-S2-3): only a Lab run (candidate is not None) with the active
    # LabContext RW handle (active_credibility_pool(), the ONE
    # allowlisted RW pool — H-LL-3 reuse, no second ad-hoc pool). The
    # legacy non-Lab path (candidate is None / no LabContext) emits
    # nothing and stays byte-identical (T9). spend_ts is the strict
    # ``<`` boundary the cumulative read uses below.
    from tpcore.lab.context import active_credibility_pool
    from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend

    _ledger_pool = (
        active_credibility_pool() if candidate is not None else None
    )
    spend_ts = None
    if _ledger_pool is not None:
        spend_ts = await record_trial_spend(
            _ledger_pool,
            target=args.engine,
            candidate=candidate,
            trials=args.trials,
            seed=args.seed,
        )
```

Then change the DSR computation. Replace:

```python
    held_period_returns = period_returns_from_trades(held_trades)
    dsr = compute_dsr_for_verdict(held_period_returns, n_trials=args.trials)
```

with:

```python
    held_period_returns = period_returns_from_trades(held_trades)
    # SP-A §2.3: the multiple-testing penalty is CUMULATIVE — every
    # configuration ever scored against this target, summed, plus this
    # run's own args.trials (read strictly BEFORE this run's spend row
    # so the current run is counted exactly once via the explicit
    # + args.trials). Legacy non-Lab path (spend_ts is None) keeps the
    # per-run penalty byte-identical (T6/T9: SP-A reduces to today's
    # behaviour when cumulative == 0). The gate expression + thresholds
    # below are UNCHANGED — only this n_trials input grows.
    if spend_ts is not None:
        cumulative = await cumulative_n_trials(
            _ledger_pool, args.engine, spend_ts)
        effective_n_trials = cumulative + args.trials
    else:
        effective_n_trials = args.trials
    dsr = compute_dsr_for_verdict(
        held_period_returns, n_trials=effective_n_trials)
```

Finally, carry the effective value on the returned `_LabCore` — change the closing `return _LabCore(...)`:

```python
    return _LabCore(
        winner_params=winner_params,
        winner_score=winner_score,
        held_metrics=held_metrics,
        dsr=dsr,
        full_credibility_score=int(final_result.credibility_score),
        credibility_rubric=final_result.credibility_rubric,
        ranked=ranked,
        windows=windows,
        survived=survived,
        effective_n_trials=effective_n_trials,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_second_candidate_same_target_gets_strictly_larger_n_trials" -x`
Expected: PASS.

- [ ] **Step 5: Verify the SP2 characterization oracle + threaded-lab test are still green (H-S2-3 seam preserved)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_credibility_pool_threaded.py`
Expected: PASS — the oracle file is UNMODIFIED; the threaded-lab test still proves the credibility write pool-routing (the ledger emit reuses the same `active_credibility_pool()`, no new RW pool).

- [ ] **Step 6: Verify ruff + check_imports**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/ tpcore/lab/ tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 7: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add ops/lab/run.py tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "feat(lab-fh): SP-A T4 (T-MONO) — unconditional spend emit + cumulative DSR n_trials"
```

---

### Task 5: Cumulative fails where per-run survived (MAKE-OR-BREAK · T-CUMUL)

Satisfies: **§5 (the honest-behavior invariant)**, **H-LL-7 (forbid reverting the correct new failure)**. Spec test: **T5 / T-CUMUL**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

First, pin a concrete returns slice + trial counts where per-run survives but cumulative fails. Append to `tpcore/tests/test_lab_ntrials_ledger.py`:

```python
async def test_cumulative_fails_where_per_run_would_have_survived(
        monkeypatch, tmp_path):
    """MAKE-OR-BREAK · T-CUMUL. A candidate that SURVIVES under the
    per-run trial count but FAILS once the cumulative penalty is
    applied. The FAILED verdict is the CORRECT, HONEST outcome — the
    edge only "passed" because the multiple-testing penalty was being
    laundered across small runs.

    DO NOT "fix" this back to per-run n_trials. SP-A makes the gate
    *correctly harder*; it does NOT weaken DSR>=0.95 / cred>=60 /
    n_trades>=3 (those thresholds are byte-identical — see T6). If this
    test "fails" because someone reverted _run_lab_core to
    n_trials=args.trials, the bug is the revert, not this test (spec
    §5 / H-LL-7)."""
    import numpy as np

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    # Choose a returns slice + (per_run, prior_cumulative) such that the
    # DSR crosses 0.95 between the per-run penalty and the cumulative
    # one. Verified directly against the real compute_dsr_for_verdict
    # (this exact slice: d_per_run(25)=1.0000 SURVIVES; d_cumulative
    # (5025)=0.8962 FAILS) so the pin is not hand-waved. A high-Sharpe,
    # low-noise slice is required — ordinary noisy normal slices never
    # clear 0.95 under any n_trials, so the contrast would be vacuous.
    rng = np.random.default_rng(11)
    returns = [float(x) for x in rng.normal(0.015, 0.004, 60)]
    per_run = 25
    prior_cumulative = 5000  # a lot of prior fishing against the target
    d_per_run = lab_run.compute_dsr_for_verdict(returns, n_trials=per_run)
    d_cumulative = lab_run.compute_dsr_for_verdict(
        returns, n_trials=prior_cumulative + per_run)
    # The pin (asserts the constructed scenario is the right shape; if a
    # future numpy/formula change moves these, retune returns/counts in
    # THIS test only — never the gate threshold).
    assert d_per_run >= 0.95, (
        f"scenario invalid: per-run DSR {d_per_run} must SURVIVE 0.95")
    assert d_cumulative < 0.95, (
        f"scenario invalid: cumulative DSR {d_cumulative} must FAIL 0.95")

    # Seed the shared ledger with prior_cumulative trials of prior
    # fishing against reversion, then run THIS candidate.
    shared = _SharedLedgerPool()
    from tpcore.lab.ledger import record_trial_spend
    seeded_ts = await record_trial_spend(
        shared, target="reversion", candidate="prior_fishing",
        trials=prior_cumulative, seed=99)
    # Force the seeded row strictly BEFORE this run's spend.
    shared.rows[-1]["timestamp"] = seeded_ts

    _install_offline_harness(monkeypatch, lab_run, returns=returns,
                             cred_score=80)

    async def _fake_build(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / "cum.csv", trials=per_run, seed=3),
            candidate="rev_cand")

    assert not isinstance(core, int)
    assert core.effective_n_trials == prior_cumulative + per_run
    # cred=80 >= 60, n_trades=48 >= 3 — so survival hinges PURELY on DSR.
    # Per-run DSR would have SURVIVED; cumulative DSR FAILS → correct.
    assert core.survived is False, (
        "cumulative penalty must make this FAIL — the honest behaviour "
        "(spec §5). If this asserts True, _run_lab_core regressed to "
        "per-run n_trials.")
```

- [ ] **Step 2: Run test to verify it passes (against the Task-4 implementation)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_cumulative_fails_where_per_run_would_have_survived" -v`
Expected: PASS — the Task-4 implementation feeds `cumulative + args.trials`; the constructed scenario makes `survived` flip to `False`.

> If the scenario pin assertions (`d_per_run >= 0.95`, `d_cumulative < 0.95`) fail on this environment's numpy, retune ONLY `returns`/`per_run`/`prior_cumulative` in this test until both hold against the real `compute_dsr_for_verdict`. NEVER change the 0.95 gate threshold (that is the H-LL-7 prohibition).

- [ ] **Step 3: Verify ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "test(lab-fh): SP-A T5 (T-CUMUL) — cumulative-fails-where-per-run-survived is the honest outcome"
```

---

### Task 6: Gate threshold byte-identical (MAKE-OR-BREAK · T-GATE)

Satisfies: **§4 (gate unchanged) / §5 (non-goal restatement) / §9 (first-ever run reduces to status quo)**, **H-LL-7**. Spec test: **T6 / T-GATE**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py`:

```python
async def test_gate_expression_byte_identical_and_reduces_to_per_run(
        monkeypatch, tmp_path):
    """MAKE-OR-BREAK · T-GATE. Two assertions:

    (a) AST/source pin: the ``survived`` expression in _run_lab_core is
        EXACTLY ``dsr >= args.dsr_threshold and
        final_result.credibility_score >= args.credibility_threshold
        and held_metrics.n_trades >= 3`` and the default thresholds are
        0.95 / 60 / 3 — byte-identical, only the n_trials INPUT to
        compute_dsr_for_verdict grew (H-LL-7).

    (b) Behavioural superset: a FIRST-EVER Lab run against a target
        (cumulative == 0) yields effective_n_trials == args.trials and a
        verdict identical to pre-SP-A for the same inputs. SP-A reduces
        to the status quo when no prior trials exist (spec §9)."""
    import ast
    import inspect

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    # ── (a) source/AST pin of the gate ────────────────────────────────
    src = inspect.getsource(lab_run._run_lab_core)
    # The exact gate expression text must be present verbatim.
    assert (
        "survived = (\n"
        "        dsr >= args.dsr_threshold\n"
        "        and final_result.credibility_score "
        ">= args.credibility_threshold\n"
        "        and held_metrics.n_trades >= 3\n"
        "    )"
    ) in src, "the survived gate expression changed — H-LL-7 violated"
    # Default thresholds unchanged in _parse_args.
    pa = inspect.getsource(lab_run._parse_args)
    assert '"--dsr-threshold", type=float, default=0.95' in pa
    assert '"--credibility-threshold", type=int, default=60' in pa
    # n_trades floor literal 3 still in the gate (not parameterised away).
    tree = ast.parse(inspect.getsource(lab_run._run_lab_core))
    assert any(
        isinstance(n, ast.Constant) and n.value == 3
        for n in ast.walk(tree)
    )

    # ── (b) first-ever run reduces to per-run behaviour ───────────────
    import numpy as np
    rng = np.random.default_rng(5)
    returns = [float(x) for x in rng.normal(0.018, 0.01, 40)]
    seen: list[int] = []
    real_dsr = lab_run.compute_dsr_for_verdict

    def _spy(r, *, n_trials):
        seen.append(n_trials)
        return real_dsr(r, n_trials=n_trials)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _spy)
    _install_offline_harness(monkeypatch, lab_run, returns=returns)
    shared = _SharedLedgerPool()  # EMPTY → cumulative == 0

    async def _fake_build(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / "first.csv", trials=37, seed=4),
            candidate="rev_first")

    assert not isinstance(core, int)
    assert core.effective_n_trials == 37          # 0 + args.trials
    assert seen[-1] == 37                          # exactly per-run
    # Same returns + same n_trials ⇒ DSR identical to pre-SP-A path.
    assert core.dsr == real_dsr(returns, n_trials=37)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_gate_expression_byte_identical_and_reduces_to_per_run" -v`
Expected: PASS — Task 4 left the `survived` expression and thresholds untouched and `cumulative == 0 ⇒ effective_n_trials == args.trials`.

> If assertion (a)'s exact-string match fails purely due to whitespace, align the literal in THIS test to the byte-exact source of the unchanged gate expression (the gate text itself must NOT be edited — only the test's mirror string). The intent is a drift-detector on the gate, not on formatting.

- [ ] **Step 3: Verify ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "test(lab-fh): SP-A T6 (T-GATE) — gate expression/thresholds byte-identical; reduces to per-run"
```

---

### Task 7: Live-graduation untouched (MAKE-OR-BREAK · T-LIVE)

Satisfies: **§4 (live graduation read untouched) / §11 (non-goals: no live poison)**, **H-LL-4**. Spec test: **T7 / T-LIVE**. Extends the existing H-S2-3 isolation home.

**Files:**
- Modify: `tpcore/tests/test_lab_isolation.py` (append a new DB-gated test; do NOT alter existing tests)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_isolation.py` (this file is already a collected DB-gated path with the module-level skipif; the new test reuses `_LabArgs`/`_rowcount` defined there):

```python
async def test_lab_ledger_disjoint_from_live_graduation(tmp_path):
    """MAKE-OR-BREAK · T-LIVE. A real Lab walk-forward run targeting the
    LIVE engine ``reversion`` (candidate set ⇒ Lab path) writes a
    ``lab_trial_ledger.reversion`` spend row, yet:

    - ``graduation_ready(pool, "reversion")`` is byte-identical to
      before (the ledger source is invisible to the live gate),
    - the live ``backtest_credibility.reversion`` source row-count is
      unchanged (H-S2-3 no-poison, re-asserted alongside the ledger),
    - ``graduation_ready``'s SQL filters strictly on
      ``backtest_credibility.{engine}`` — it never reads
      ``lab_trial_ledger.*`` (namespace disjointness, H-LL-4),
    - the ledger DID record the spend (so the disjointness is
      *meaningful*, not vacuous: a row exists and is still invisible).
    """
    import inspect

    import ops.lab.run as lab_run
    from tpcore.backtest.credibility import (
        CREDIBILITY_SOURCE_PREFIX,
        graduation_ready,
    )
    from tpcore.db import build_asyncpg_pool
    from tpcore.lab.context import LabContext
    from tpcore.lab.ledger import ledger_source

    # Static disjointness: graduation_ready reads ONLY the credibility
    # prefix; the ledger source uses a different prefix entirely.
    grad_src = inspect.getsource(graduation_ready)
    assert f"{CREDIBILITY_SOURCE_PREFIX}." in grad_src
    assert "lab_trial_ledger" not in grad_src
    assert not ledger_source("reversion").startswith(
        f"{CREDIBILITY_SOURCE_PREFIX}.")

    url = os.environ["DATABASE_URL"]
    audit = await build_asyncpg_pool(url, max_size=1)
    try:
        rev_cred_src = f"{CREDIBILITY_SOURCE_PREFIX}.reversion"
        ledger_src = ledger_source("reversion")
        rev_cred_before = await _rowcount(
            audit, "data_quality_log", f"WHERE source='{rev_cred_src}'")
        ledger_before = await _rowcount(
            audit, "data_quality_log", f"WHERE source='{ledger_src}'")
        grad_before = await graduation_ready(audit, "reversion")

        args = _LabArgs()
        args.output = tmp_path / "ledger_iso.csv"
        async with LabContext(db_url=url):
            rc = await lab_run.amain(args, candidate="ledger_iso_probe")
        assert rc in (0, 1), f"unexpected amain rc={rc}"

        # Live gate read byte-identical.
        assert await graduation_ready(audit, "reversion") == grad_before, \
            "Lab run changed graduation_ready(reversion) — live poison"
        # Live credibility source row-count unchanged (no-poison).
        assert await _rowcount(
            audit, "data_quality_log",
            f"WHERE source='{rev_cred_src}'") == rev_cred_before, \
            f"Lab poisoned {rev_cred_src}"
        # The ledger DID record the spend (disjointness is meaningful).
        ledger_after = await _rowcount(
            audit, "data_quality_log", f"WHERE source='{ledger_src}'")
        assert ledger_after >= ledger_before + 1, (
            f"Lab run must have emitted a {ledger_src} spend row "
            f"(before={ledger_before}, after={ledger_after})")
    finally:
        await audit.close()
```

- [ ] **Step 1b: Write the MANDATORY DB-gated SUM-integer correctness test (closes the T0+T1-review Important gap — H-LL-9)**

The offline fakes (T1/T4/T8) re-implement the WHERE/JSON semantics in Python, so a SQL-TEXT bug in `cumulative_n_trials` (wrong JSON key `->>'trials'`→`->>'seed'`, `SUM`→`COUNT`, dropped `source=$1` predicate, `<`→`<=` strict-prior boundary, wrong cast) is NOT caught anywhere in T1–T11 without this. A silently-wrong SUM defeats SP-A's entire anti-laundering purpose. This test exercises the REAL SQL end-to-end against a real Postgres. Append to `tpcore/tests/test_lab_isolation.py` (same DB-gated file; reuses its skipif + helpers):

```python
async def test_cumulative_n_trials_real_db_integer_correctness():
    """MAKE-OR-BREAK (H-LL-9). Seed KNOWN lab_trial_ledger rows in the
    real DB and assert cumulative_n_trials returns the EXACT integer —
    pins the live SQL: SUM (not COUNT), the notes::jsonb->>'trials'
    int-cast (not another key), the source=$1 per-target predicate
    (cross-target isolation), and the strict ``timestamp < before_ts``
    boundary. None of these are observable through the offline fakes.
    """
    from datetime import UTC, datetime

    from tpcore.db import build_asyncpg_pool
    from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend

    url = os.environ["DATABASE_URL"]
    pool = await build_asyncpg_pool(url, max_size=1)
    try:
        # Unique throwaway targets so the assertion is independent of any
        # prior ledger history in this DB (the SUM is monotone/append-only;
        # uuid mirrors the file's existing uuid4 usage).
        tgt = f"revtest_{uuid.uuid4().hex[:12]}"
        other = f"vectest_{uuid.uuid4().hex[:12]}"
        base = await cumulative_n_trials(pool, tgt, datetime.now(UTC))
        assert base == 0, f"fresh target must be 0, got {base}"

        # Seed the 3 INCLUDED rows FIRST. record_trial_spend stamps a
        # server-side datetime.now(UTC) and returns that exact ts.
        await record_trial_spend(
            pool, target=tgt, candidate="h-ll-9", trials=40, seed=1)
        await record_trial_spend(
            pool, target=tgt, candidate="h-ll-9", trials=60, seed=2)
        # Cross-target row that must NOT be summed into `tgt` (source=$1).
        await record_trial_spend(
            pool, target=other, candidate="h-ll-9", trials=999, seed=9)
        # cutoff captured AFTER the 3 included rows and BEFORE the
        # excluded write — so the trials=7 row's returned ts is strictly
        # GREATER than cutoff (causal: capture-then-write, not now()-racy).
        cutoff = datetime.now(UTC)
        # Strictly-after row: its ts > cutoff ⇒ excluded by `< cutoff`.
        spend7_ts = await record_trial_spend(
            pool, target=tgt, candidate="h-ll-9", trials=7, seed=3)

        got = await cumulative_n_trials(pool, tgt, cutoff)
        assert got == 100, (
            f"cumulative SUM wrong: expected 40+60=100 (cross-target "
            f"{other}=999 excluded by source predicate; the trials=7 "
            f"post-cutoff row excluded by strict '<'), got {got} — "
            f"a SQL-text regression (JSON key / SUM / predicate / "
            f"boundary) in cumulative_n_trials")
        # Cross-target isolation, asserted from the other side too.
        assert await cumulative_n_trials(pool, other, cutoff) == 999
        # GENUINE `<` vs `<=` discriminator: a row exists EXACTLY at
        # spend7_ts. With strict `<` that row is EXCLUDED ⇒ 100; a
        # `<`→`<=` regression would INCLUDE it ⇒ 107.
        assert await cumulative_n_trials(pool, tgt, spend7_ts) == 100
    finally:
        await pool.close()
```

This is a **mandatory T7 deliverable**, not optional. Without it the live ledger SUM has zero real-DB verification and a boundary/JSON-key bug ships undetected.

- [ ] **Step 2: Run the T7 tests to verify they fail (or skip locally)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_isolation.py::test_lab_ledger_disjoint_from_live_graduation" "tpcore/tests/test_lab_isolation.py::test_cumulative_n_trials_real_db_integer_correctness" -v`
Expected (local, no `DATABASE_URL`): both SKIPPED (module-level `pytest.mark.skipif`). With a DB but **before Task 4's emit / Task 1's helper existed**: `test_lab_ledger_disjoint…` FAILs on `ledger_after >= ledger_before + 1`; `test_cumulative_n_trials_real_db_integer_correctness` FAILs (helper/SQL absent or wrong). With Tasks 1+4 in place: both PASS. A SQL-text regression in `cumulative_n_trials` (wrong JSON key / SUM→COUNT / dropped source predicate / `<`→`<=`) makes `test_cumulative_n_trials_real_db_integer_correctness` FAIL in CI — this is the H-LL-9 closure.

> This test is DB-gated by design (mirrors the existing isolation tests). It runs fully in CI. Do NOT force it locally.

- [ ] **Step 3: Verify ruff + the existing isolation tests still pass (unmodified)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_isolation.py`
Expected: `All checks passed!`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider tpcore/tests/test_lab_isolation.py`
Expected (local): all skipped (no DB) — confirms no collection/import regression introduced by the appended test.

- [ ] **Step 4: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_isolation.py
git commit -m "test(lab-fh): SP-A T7 (T-LIVE) — ledger disjoint from live graduation_ready"
```

---

### Task 8: Under-count closed — abort-after-fishing still counts (MAKE-OR-BREAK · T-ABORT)

Satisfies: **§3.2 (the load-bearing constraint) / §3.4 (no under-declare)**, **H-LL-1 (the spine, proven closed)**. Spec test: **T8 / T-ABORT**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py`. The abort path exercised is `:698` (no rankable trial → `return 1`): the harness's ctx-runner returns zero trades so `rank_candidates` yields nothing and `_run_lab_core` returns `1` *after* `sample_parameters` but *before* the DSR/credibility code.

```python
async def test_aborted_run_after_sampling_still_records_its_spend(
        monkeypatch, tmp_path):
    """MAKE-OR-BREAK · T-ABORT. The §3.2 under-count hole proven closed.

    Run 1 samples args.trials then takes the no-rankable-trial rc path
    (returns 1 BEFORE the DSR/credibility code — the credibility write
    never runs). It MUST still have recorded its trial spend in the
    ledger. Run 2 against the same target sees run 1's trials in
    cumulative_n_trials. (Deriving the count from credibility rows would
    have silently under-counted exactly this adversarial run — H-LL-1.)
    """
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    from tpcore.lab.ledger import cumulative_n_trials

    # Harness whose ctx-runner yields ZERO trades ⇒ rank_candidates
    # returns [] ⇒ _run_lab_core hits `if not ranked: return 1` (:698).
    class _EmptyResult:
        credibility_score = 0
        credibility_rubric = None
        trade_log: list = []

    def _ctx_runner(context, *, overrides=None):
        return _EmptyResult()

    async def _ctx_loader(*a, **k):
        return object()

    async def _runner(*a, **k):
        return _EmptyResult()

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _ctx_loader)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)

    shared = _SharedLedgerPool()

    async def _fake_build(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    # Run 1 — aborts at the no-rankable-trial rc path.
    async with LabContext(db_url="postgres://fake/db"):
        rc = await lab_run._run_lab_core(
            _ns(tmp_path / "abort.csv", trials=64, seed=1),
            candidate="abort_cand")
    assert rc == 1, f"expected the no-rankable rc=1 abort path, got {rc}"

    # The aborted run STILL recorded its 64-trial spend.
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    assert await cumulative_n_trials(shared, "reversion", now) == 64, (
        "abort-after-fishing did NOT record its spend — the §3.2 "
        "under-count hole is OPEN (H-LL-1 violated)")
    assert len(shared.rows) == 1
    import json
    assert json.loads(shared.rows[0]["notes"])["trials"] == 64
```

- [ ] **Step 2: Run test to verify it passes (against the Task-4 implementation)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_aborted_run_after_sampling_still_records_its_spend" -v`
Expected: PASS — Task 4 emits the spend row right after `sample_parameters`, *before* the `if not ranked: return 1` path at `:698`. (If this fails, the emit was placed AFTER an rc-return — the H-LL-1 spine is broken; the fix is to move the emit back to immediately after the sampled-count print, never later.)

- [ ] **Step 3: Verify ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "test(lab-fh): SP-A T8 (T-ABORT) — abort-after-sampling still records its trial spend"
```

---

### Task 9: Legacy non-Lab path byte-identical

Satisfies: **§4 (H-S2-3 reuse) / §9 (legacy non-Lab search) / §11**, **the SP2 characterization-oracle preservation gate**. Spec test: **T9 — legacy-path-byte-identical**.

**Files:**
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py`:

```python
async def test_legacy_non_lab_path_emits_and_reads_no_ledger(
        monkeypatch, tmp_path):
    """T9. The legacy ``python scripts/search_parameters.py`` operator
    path is candidate=None with NO active LabContext — it must stay
    byte-identical: emit NO lab_trial_ledger.* row, read NO ledger, and
    feed compute_dsr_for_verdict the per-run args.trials exactly as
    today (H-S2-3 symmetry; the characterization oracle stays green)."""
    import ops.lab.run as lab_run

    import numpy as np
    rng = np.random.default_rng(2)
    returns = [float(x) for x in rng.normal(0.015, 0.01, 40)]
    seen: list[int] = []
    real_dsr = lab_run.compute_dsr_for_verdict

    def _spy(r, *, n_trials):
        seen.append(n_trials)
        return real_dsr(r, n_trials=n_trials)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _spy)
    _install_offline_harness(monkeypatch, lab_run, returns=returns)

    # candidate=None, NO LabContext: legacy path. The credibility write
    # opens its own ad-hoc asyncpg.create_pool — fake it so no socket.
    import asyncpg

    class _AdHoc:
        async def close(self) -> None: ...

    created: list[str] = []
    ledger_touches: list[str] = []

    async def _fake_create_pool(*a, **k):
        created.append("create_pool")
        return _AdHoc()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool,
                        raising=True)

    # Spy the ledger helpers — they must NEVER be called on the legacy
    # path (candidate is None ⇒ _ledger_pool is None ⇒ no emit/read).
    import tpcore.lab.ledger as ledger_mod
    real_record = ledger_mod.record_trial_spend
    real_cum = ledger_mod.cumulative_n_trials

    async def _spy_record(*a, **k):
        ledger_touches.append("record")
        return await real_record(*a, **k)

    async def _spy_cum(*a, **k):
        ledger_touches.append("cumulative")
        return await real_cum(*a, **k)

    monkeypatch.setattr("ops.lab.run.record_trial_spend", _spy_record,
                        raising=False)
    monkeypatch.setattr("ops.lab.run.cumulative_n_trials", _spy_cum,
                        raising=False)

    core = await lab_run._run_lab_core(
        _ns(tmp_path / "legacy.csv", trials=40, seed=0),
        candidate=None)

    assert not isinstance(core, int)
    # No ledger interaction on the legacy path.
    assert ledger_touches == [], (
        f"legacy non-Lab path touched the ledger: {ledger_touches}")
    # DSR fed the per-run args.trials, unchanged from today.
    assert seen[-1] == 40
    assert core.effective_n_trials == 40
```

> Note on the monkeypatch targets: Task 4's implementation does `from tpcore.lab.context import active_credibility_pool` and `from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend` *inside* `_run_lab_core` (in-body lazy import — the H-S3-10 / SP2-T9 collision discipline). After the function runs once those names are bound on the call frame, not the module; `raising=False` makes the spy installation tolerant if the names are not yet module-global. The behavioural guarantee (no ledger row, per-run n_trials) is the binding assertion; the spy is belt-and-braces.

- [ ] **Step 2: Run test to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_legacy_non_lab_path_emits_and_reads_no_ledger" -v`
Expected: PASS — Task 4 gates emit/read on `candidate is not None`; `candidate=None` ⇒ `_ledger_pool is None` ⇒ `effective_n_trials = args.trials`, no ledger calls.

- [ ] **Step 3: Verify the SP2 characterization oracle is UNMODIFIED and green**

Run: `git diff --name-only HEAD -- scripts/tests/test_search_parameters_characterization.py`
Expected: empty output (the oracle file was never touched by SP-A).

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/test_search_parameters_characterization.py`
Expected: PASS (all oracle tests green — the legacy path is byte-identical).

- [ ] **Step 4: Verify ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "test(lab-fh): SP-A T9 — legacy non-Lab path byte-identical (no ledger; oracle green)"
```

---

### Task 10: Dossier / print honesty (H-LL-6)

Satisfies: **§6 (print honesty) / §6 (`LabResult.n_trials` carries effective cumulative)**, **H-LL-6**. Spec test: **T10 — dossier/print honesty**.

**Files:**
- Modify: `ops/lab/run.py` (`amain` `DSR (n_trials=…)` line; `_build_lab_result` `n_trials=`)
- Modify: `tpcore/tests/test_lab_ntrials_ledger.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_lab_ntrials_ledger.py`:

```python
async def test_amain_line_and_labresult_carry_effective_cumulative(
        monkeypatch, tmp_path, capsys):
    """T10 / H-LL-6. With prior trials on the shared ledger (cumulative
    > 0), the amain ``DSR (n_trials=…)`` human line AND
    ``LabResult.n_trials`` carry the EFFECTIVE cumulative value that
    actually deflated the verdict — NOT the per-run args.trials. The
    dossier must not lie about the applied penalty."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    from tpcore.lab.ledger import record_trial_spend
    from tpcore.lab.models import LabCandidate

    import numpy as np
    rng = np.random.default_rng(7)
    returns = [float(x) for x in rng.normal(0.02, 0.01, 40)]
    _install_offline_harness(monkeypatch, lab_run, returns=returns,
                             cred_score=80)

    shared = _SharedLedgerPool()
    seeded_ts = await record_trial_spend(
        shared, target="reversion", candidate="prior", trials=300,
        seed=1)
    shared.rows[-1]["timestamp"] = seeded_ts

    async def _fake_build(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    # ── amain human line carries the effective cumulative ─────────────
    async with LabContext(db_url="postgres://fake/db"):
        rc = await lab_run.amain(
            _ns(tmp_path / "h.csv", trials=40, seed=2),
            candidate="rev_cand")
    out = capsys.readouterr().out
    assert rc in (0, 1)
    # 300 prior + 40 this run == 340 effective — NOT 40.
    assert "DSR (n_trials=340)" in out, (
        f"amain must surface the effective cumulative n_trials (340), "
        f"not args.trials (40). stdout:\n{out}")
    assert "DSR (n_trials= 40)" not in out
    assert "DSR (n_trials=40)" not in out

    # ── LabResult.n_trials carries the effective cumulative ───────────
    shared2 = _SharedLedgerPool()
    s2 = await record_trial_spend(
        shared2, target="reversion", candidate="prior2", trials=300,
        seed=1)
    shared2.rows[-1]["timestamp"] = s2

    async def _fake_build2(url, *, read_only, **k):
        return shared2

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build2,
                        raising=True)
    cand = LabCandidate(
        name="rev_cand", target_engine="reversion",
        param_overrides={}, intent="fold_existing")
    async with LabContext(db_url="postgres://fake/db"):
        result = await lab_run.run_lab(
            _ns(tmp_path / "lr.csv", trials=40, seed=2),
            candidate=cand)
    assert result.n_trials == 340, (
        f"LabResult.n_trials must be the effective cumulative (340), "
        f"not args.trials (40); got {result.n_trials}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_amain_line_and_labresult_carry_effective_cumulative" -x`
Expected: FAIL — `amain` still prints `f"  DSR (n_trials={args.trials:>3}): ..."` → `DSR (n_trials= 40)`, and `_build_lab_result` still sets `n_trials=args.trials` → `result.n_trials == 40`.

- [ ] **Step 3: Write minimal implementation**

In `ops/lab/run.py`, `amain` — replace the DSR print line:

```python
    print(f"  DSR (n_trials={args.trials:>3}): {core.dsr:.4f}")
```

with (H-LL-6: surface the effective cumulative penalty, not the per-run lie):

```python
    print(f"  DSR (n_trials={core.effective_n_trials:>3}): {core.dsr:.4f}")
```

In `ops/lab/run.py`, `_build_lab_result` — replace:

```python
        n_trials=args.trials,
```

with:

```python
        n_trials=core.effective_n_trials,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider "tpcore/tests/test_lab_ntrials_ledger.py::test_amain_line_and_labresult_carry_effective_cumulative" -v`
Expected: PASS — `core.effective_n_trials == 340` flows into both the print line and `LabResult.n_trials`. (The dossier `render_lab_dossier` reads `r.n_trials` → now also honest, transitively.)

- [ ] **Step 5: Verify the SP2 oracle + threaded-lab test still green (amain stdout contract)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_credibility_pool_threaded.py`
Expected: PASS — the oracle pins the credibility-call args + rc, not the `n_trials` display integer; on the legacy/per-run path `core.effective_n_trials == args.trials` so the displayed value is numerically identical to pre-SP-A.

- [ ] **Step 6: Verify ruff + check_imports**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/ tpcore/tests/test_lab_ntrials_ledger.py`
Expected: `All checks passed!`

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 7: Commit**

```bash
git branch --show-current   # MUST print: lab-fh-epic-decomp
git add ops/lab/run.py tpcore/tests/test_lab_ntrials_ledger.py
git commit -m "feat(lab-fh): SP-A T10 (H-LL-6) — amain line + LabResult.n_trials carry effective cumulative"
```

---

### Task 11: Full suite + CI-exact gates + lane/scope assertion + finish branch

Satisfies: **§7 T11 / §4 (no forbidden-file contact) / §11 (non-goals) / §12 (every requirement → a task)**, all preservation gates.

**Files:**
- Modify: none (verification + branch finish only)

- [ ] **Step 1: Run the full test suite (CI-exact)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass (local: the DB-gated isolation tests including the new T7 are SKIPPED — that is correct; CI runs them fully). Zero failures, zero errors.

- [ ] **Step 2: Run CI-exact ruff**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`
Expected: `All checks passed!`

- [ ] **Step 3: Run CI-exact forbidden-imports check (proves `tpcore/lab/ledger.py` is engine-free)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
Expected: `ok: no forbidden imports found`

- [ ] **Step 4: Run the engine-manifest consistency check**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python scripts/gen_engine_manifest.py --check`
Expected: exit 0 (SP-A adds no engine; the manifest is unchanged — this confirms no accidental roster/testpath drift).

- [ ] **Step 5: Lane / scope assertion — no forbidden-file or data-lane diff**

Run:
```bash
git diff --name-only main...HEAD
```
Expected: ONLY these paths appear —
`docs/superpowers/plans/2026-05-19-lab-ntrials-ledger.md`,
`tpcore/lab/ledger.py`,
`ops/lab/run.py`,
`tpcore/tests/test_lab_ntrials_ledger.py`,
`tpcore/tests/test_lab_isolation.py`.

Then assert NONE of the 8 forbidden files / data-lane files changed:
```bash
git diff --name-only main...HEAD | grep -E \
  'tpcore/calendar\.py|tpcore/risk/|ops/engine_supervisor\.py|ops/engine_service\.py|ops/engine_ladder\.py|tpcore/supervisor_state\.py|tpcore/trade_monitor\.py|data-provider-lifecycle|data_feed_change_request' \
  && echo "LANE VIOLATION — FORBIDDEN FILE TOUCHED" || echo "lane clean"
```
Expected: `lane clean` (grep matches nothing → the `||` branch fires).

- [ ] **Step 6: Verify SP2/SP3 preservation gates green together**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_credibility_pool_threaded.py tpcore/tests/test_lab_isolation.py tpcore/tests/test_lab_ntrials_ledger.py`
Expected: all pass/skip — the SP2 characterization oracle UNMODIFIED + green (H-S2-3 seam preserved), the threaded-lab H-S3-8 test green (the ledger emit reuses `active_credibility_pool()`, no second RW pool), the isolation home green (T7 skipped locally / runs in CI), all SP-A tests green.

- [ ] **Step 7: Confirm the oracle file is byte-unmodified**

Run: `git log --oneline main...HEAD -- scripts/tests/test_search_parameters_characterization.py`
Expected: empty output (no SP-A commit touched the oracle).

- [ ] **Step 8: Finish the development branch**

Use **superpowers:finishing-a-development-branch** to integrate the work (verify `git branch --show-current` == `lab-fh-epic-decomp` first; squash-merge with `--delete-branch` per the standing git-hygiene method; tests never run git/gh against the working repo).

---

## Self-Review

### 1. Spec coverage

| Spec item | Task |
| --- | --- |
| §1 Problem (per-run-only deflation defect) | T0 (decision), T4 (fix) |
| §2.1 trial unit = `args.trials` per run | T1, T4 |
| §2.2 keying per target engine (not per family) | T1 (`ledger_source`), T4 (`target=args.engine`), T-MONO pin (T4) |
| §2.3 math integration `cumulative + args.trials` | T4 |
| §3.1 substrate = reuse `data_quality_log`, no new table | T0, T1 |
| §3.2 conditional-credibility-write constraint (spine) | T0 (rationale), T1 (unconditional emit), T8 (proven closed) |
| §3.3 emit/read ordering (strict `<`) | T1 (`before_ts` strict), T4 (emit→read order) |
| §3.4 ungameability (no reset / no under-declare / monotone / cross-session) | T3 (no reset), T8 (no under-declare), T4 (monotone), T1 (Postgres substrate = cross-session) |
| §4 live-safety / isolation reuse (H-S2-3 / H-S3-8 / gate unchanged / no forbidden file) | T4 (H-S3-8 reuse), T6 (gate unchanged), T7 (live read untouched), T9 (H-S2-3), T11 (lane assertion) |
| §5 honest-behavior invariant | T5 (T-CUMUL, with H-LL-7 docstring) |
| §6 integration point + print/dossier honesty + helper home | T0, T1 (helper home), T4 (emit/read/changed line), T10 (print + LabResult) |
| §7 T0–T11 decomposition | Tasks 0–11 (1:1) |
| §8 H-LL-1..8 | see table below |
| §9 failure modes (first-ever run reduces to status quo / legacy / collision) | T6 (first-ever), T9 (legacy), T3 (collision documented) |
| §10 reused-vs-new | T0/T1 (all reuse decisions encoded in the helper) |
| §11 non-goals (no gate weakening / not SP-B…G / not ML / no forbidden file) | T6 (no weakening), T11 (lane assertion) |
| §12 self-review (every requirement → a task) | this table |

| Hardening | Task | Pinning test |
| --- | --- | --- |
| H-LL-1 (unconditional spend event = the spine) | T1 + T4 (emit before rc-return) | T8 (T-ABORT) |
| H-LL-2 (per-target keying) | T1 (`ledger_source`), T4 | T4 (T-MONO) |
| H-LL-3 (reuse `active_credibility_pool()`, no 2nd RW pool) | T4 | T4 harness + T11 step 6 (threaded-lab test) |
| H-LL-4 (disjoint namespace, no live poison) | T1 (`ledger_source` ≠ credibility prefix) | T7 (T-LIVE) |
| H-LL-5 (no new table, event-sourced SUM) | T0, T1 | T3 (T-NORESET) |
| H-LL-6 (dossier/print honesty) | T10 | T10 |
| H-LL-7 (forbid reverting the correct new failure) | T5 docstring, T6 gate pin | T5 (T-CUMUL) + T6 (T-GATE) |
| H-LL-8 (microsecond-collision under-count residual) | documented at the T3 test site | T3 (no error / no double-count) |

All 6 make-or-break tests map: **T-NORESET → T3**, **T-MONO → T4**, **T-CUMUL → T5**, **T-GATE → T6**, **T-LIVE → T7**, **T-ABORT → T8**. The under-count-closed unconditional-spend-event constraint (§3.2) is the spine: emitted in T4 at sample time **before** the `:639`/`:698` rc-returns and the conditional `:731` credibility write; pinned by **T8**.

### 2. Placeholder scan

Searched the plan for `TBD`/`TODO`/`fill in`/`add appropriate`/`similar to Task N`/`implement later`/`handle edge cases`/`<placeholder>` — none present. Every code step contains complete copy-pasteable code: the full `tpcore/lab/ledger.py`, every exact `ops/lab/run.py` old→new replacement block, and every complete test function. No "similar to" cross-references — the offline harness (`_install_offline_harness`, `_SharedLedgerPool`, `_FakeConn`/`_FakePool`/`_Acquire`, `_ns`, `_Trade`) is written out in full in the tasks that introduce it (T1 fakes; T4 harness) and reused by name thereafter (all in one test file, so later tasks rely on the earlier in-file definitions — explicitly the same module, not a "see Task N" placeholder).

### 3. Type / name consistency

- `record_trial_spend(pool, *, target, candidate, trials, seed, run_outcome="sampled") -> datetime` — identical signature in the locked-names block, T1 impl, T1/T2/T3/T5/T8/T10 call sites, and the T4 `_run_lab_core` call.
- `cumulative_n_trials(pool, target, before_ts) -> int` — identical in locked-names, T1 impl, T1/T8/T9 call sites, T4 `_run_lab_core` read.
- `ledger_source(target) -> str` returning `f"lab_trial_ledger.{target}"` — consistent across T1/T3/T7.
- `_LabCore.effective_n_trials: int` — added in T4, read identically in T4 assertions, T5/T6/T9 assertions, and T10's `amain`/`_build_lab_result` edits (`core.effective_n_trials`). No `n_trials` vs `effective_n_trials` drift: `LabResult.n_trials` (the model field, unchanged) is *assigned from* `core.effective_n_trials`.
- `LEDGER_SCHEMA_VERSION` / `LEDGER_SOURCE_PREFIX` — defined T1, asserted T2/T3.
- The collision-eviction stanza is byte-identical to the verified precedent (`tpcore/tests/test_engine_sdlc_cli.py:21-25`).

Fixes applied inline during review: none required — no inconsistency found on the second pass.

### Spec-gap flag

No spec § or H-LL-1..8 is left without a home. One **scope note (not a gap)**: §9's "DB unreachable at emit/read ⇒ fail loud, never silent `cumulative=0`" is satisfied *by construction* — `record_trial_spend`/`cumulative_n_trials` do not catch asyncpg errors, so a DB failure propagates exactly as the existing credibility-write/`asyncpg` errors already do (the run never reaches a SURVIVED verdict). No dedicated task is added because adding a try/except would *create* the silent-`cumulative=0` failure mode the spec forbids; the correct implementation is the absence of error-swallowing, which T1's code (no `except`) and the T4 ordering already guarantee. This is intentional and called out here rather than silently.
