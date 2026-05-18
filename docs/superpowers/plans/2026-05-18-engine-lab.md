# Engine SDLC SP2 — The Lab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An isolated shadow/candidate backtest harness, runnable concurrently with live dispatch, ENFORCED zero live side-effects, scored against the same DSR/credibility gate, emitting a two-exit graduation dossier (recommendation only — SP3 applies it).

**Architecture:** Split package per H-S2-1 — `tpcore/lab/` = engine-FREE isolation primitives + frozen contract models (layering-clean); `ops/lab/` = the engine-importing `LabRun` + `python -m ops.lab` CLI (ops/ is exempt from the `check_imports` tpcore∌engine scanner). Read-only enforced *inside* `tpcore.db.build_asyncpg_pool` (H-S2-2). Lab-namespaced credibility source (H-S2-3). Characterization tests are the oracle (H-S2-4, written first).

**Tech Stack:** Python 3.11, asyncpg, pydantic v2, structlog, pytest (`asyncio_mode="auto"`). venv `/Users/michael/short-term-trading-engine/.venv/bin/python`; `ruff` on PATH. Worktree `/Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab` (branch `worktree-engine-lab`, base `310ea6e` post-SP1).

**Spec:** `docs/superpowers/specs/2026-05-18-engine-lab-design.md` (§12 hardening H-S2-1..6 are BINDING; T0-T10 decomposition).

**Lane discipline:** ENGINE lane only. NEVER edit data-SDLC files (`tpcore/providers.py`, `tpcore/ladder/`, `ops/weekly_digest.py`, `ops/data_repair_service.py`, `tpcore/selfheal|feeds|ingestion|datasupervisor`, `scripts/run_data_operations.sh`); `tpcore/parity/` is shape-only READ-ONLY reference. Never local-merge into shared main. CI-exact: `ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`; `python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore` (args unchanged).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `scripts/tests/test_search_parameters_characterization.py` | The behavior-preservation ORACLE (search has zero tests) | Create (T1) |
| `tpcore/db.py` | `read_only` kwarg → `default_transaction_read_only` | Modify (T2) |
| `tpcore/lab/__init__.py`, `context.py`, `models.py` | engine-FREE isolation primitives + frozen contract | Create (T3) |
| `tpcore/risk/governor.py`, `tpcore/aar/writer.py`, `tpcore/order_management/base_order_manager.py`, `tpcore/alpaca/broker_adapter.py`, `tpcore/logging/db_handler.py` | additive `assert_not_in_lab()` guard | Modify (T4, 1 line each) |
| `ops/lab/__init__.py`, `run.py`, `registry.py`, `dossier.py`, `__main__.py` | engine-importing LabRun + CLI | Create (T5,T6,T7,T8,T10) |
| `scripts/search_parameters.py` | thin delegating shim over `ops.lab.run` | Modify (T5) |
| `tpcore/engine_profile.py` | LAB sentinel `_PROFILE` entry | Modify (T7, 1 entry) |
| `tpcore/tests/test_engine_lifecycle_consistency.py` | `test_lab_sentinel_is_not_wired` leg | Modify (T7) |
| `tpcore/tests/test_lab_isolation.py` | binding isolation test (collected path, H-S2-6) | Create (T9) |
| `pyproject.toml` | add `ops/lab/tests` to testpaths if used | Modify (T10 if needed) |

---

## Task 0: Layering-home decision (no code — recorded in the plan)

Per H-S2-1: `tpcore/lab/` may contain ONLY engine-free code (imports `tpcore.*`/pydantic/stdlib). The engine-importing `LabRun` lives in `ops/lab/` (`ops/` is a real package with `ops/__init__.py`, ruff-covered, NOT in the `check_imports` scan args — verified). Entrypoint = `python -m ops.lab`. This is recorded; no code. Every subsequent task obeys this split. T10 re-runs `check_imports … tpcore` to prove it.

- [ ] **Step 1:** Confirm the split is internalized; proceed to T1. (No commit — decision only, already in spec §12.)

---

## Task 1: Characterization tests — the oracle (H-S2-4)

`scripts/search_parameters.py` has ZERO tests. Pin the pure units + an `amain` smoke BEFORE any extract.

**Files:** Create `scripts/tests/test_search_parameters_characterization.py` (in `scripts/tests` — already in pyproject testpaths).

- [ ] **Step 1: Write the characterization tests.** Header mirrors the sibling `scripts/tests/` collision-guard pattern:

```python
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import search_parameters as sp  # noqa: E402


def test_build_walk_windows_non_overlapping_train():
    w = sp.build_walk_windows(train_start=date(2018, 1, 1),
                              holdout_end=date(2023, 12, 31),
                              step_days=365, train_years=3, holdout_years=1)
    assert len(w) >= 1
    for win in w:
        assert win.train_start < win.train_end <= win.holdout_start < win.holdout_end
    # advancing by step_days
    if len(w) > 1:
        assert (w[1].train_start - w[0].train_start).days == 365


def test_sample_parameters_is_seed_deterministic():
    a = sp.sample_parameters("reversion", 20, seed=7)
    b = sp.sample_parameters("reversion", 20, seed=7)
    c = sp.sample_parameters("reversion", 20, seed=8)
    assert a == b
    assert a != c
    for combo in a:
        assert set(combo) == set(sp.PARAM_RANGES["reversion"])


def test_compute_dsr_for_verdict_bounds_and_monotone():
    import numpy as np
    rng = np.random.default_rng(0)
    flat = [0.0] * 60
    strong = list(rng.normal(0.02, 0.01, 60))
    d_flat = sp.compute_dsr_for_verdict(flat, n_trials=200)
    d_strong = sp.compute_dsr_for_verdict(strong, n_trials=200)
    assert 0.0 <= d_flat <= 1.0 and 0.0 <= d_strong <= 1.0
    assert d_strong >= d_flat
    # more trials ⇒ deflated (lower) DSR for the same returns
    assert sp.compute_dsr_for_verdict(strong, n_trials=2000) <= d_strong + 1e-9


def test_norm_inv_known_quantiles():
    assert abs(sp._norm_inv(0.5)) < 1e-6
    assert abs(sp._norm_inv(0.975) - 1.959963985) < 1e-3


def test_period_returns_and_slice_metrics_from_trades():
    trades = [
        {"entry_date": date(2024, 1, 2), "pnl_pct": 0.03},
        {"entry_date": date(2024, 1, 2), "pnl_pct": -0.01},
        {"entry_date": date(2024, 2, 1), "pnl_pct": 0.02},
    ]
    rets = sp.period_returns_from_trades(trades)
    assert len(rets) == 2  # grouped by entry_date period
    m = sp.compute_slice_metrics_from_trades(trades, span_days=60)
    assert m.n_trades == 3
    assert isinstance(m.sharpe, float) and isinstance(m.profit_factor, float)


def test_rank_candidates_groups_and_sorts():
    from search_parameters import SliceMetrics, TrialResult
    def tr(tid, params, sharpe):
        return TrialResult(trial_id=tid, window_label="w", parameters=params,
                           holdout=SliceMetrics(n_trades=10, sharpe=sharpe,
                                                profit_factor=1.5, max_drawdown=-0.1,
                                                win_rate=0.5),
                           full_credibility_score=70, error=None)
    p1 = {"z_threshold": 3.0}; p2 = {"z_threshold": 2.5}
    ranked = sp.rank_candidates([tr(0, p1, 0.5), tr(1, p1, 1.5), tr(2, p2, 0.2)])
    assert ranked[0][0] == p1  # higher mean score first
    assert ranked[0][2] == 2   # n_windows for p1


def test_write_results_csv_roundtrip(tmp_path):
    from search_parameters import SliceMetrics, TrialResult
    t = TrialResult(trial_id=0, window_label="w1", parameters={"z_threshold": 3.0},
                    holdout=SliceMetrics(n_trades=5, sharpe=1.0, profit_factor=2.0,
                                         max_drawdown=-0.05, win_rate=0.6),
                    full_credibility_score=72, error=None)
    out = tmp_path / "r.csv"
    sp.write_results_csv(out, [t])
    text = out.read_text()
    assert "trial_id" in text and "parameters_json" in text and "0.6" in text


async def test_amain_smoke_survived_verdict(monkeypatch, tmp_path):
    """amain end-to-end with a stubbed context-runner: asserts the
    SURVIVED verdict path + the write_credibility_score call args
    (O2: successive candidates don't leak overrides — the stub records
    each overrides dict it receives)."""
    from search_parameters import SliceMetrics
    seen_overrides = []

    class _FakeRubric:
        score = 80
    class _FakeRunResult:
        credibility_score = 80
        credibility_rubric = _FakeRubric()
        trades = [{"entry_date": date(2024, 6, 3), "pnl_pct": 0.02} for _ in range(8)]

    def _fake_ctx_runner(context, *, overrides=None, trade_log_path=None):
        seen_overrides.append(dict(overrides or {}))
        return _FakeRunResult()
    async def _fake_ctx_loader(*a, **k): return object()
    async def _fake_runner(*a, **k): return _FakeRunResult()

    monkeypatch.setattr(sp, "_context_runner_for", lambda e: _fake_ctx_runner)
    monkeypatch.setattr(sp, "_context_loader_for", lambda e: _fake_ctx_loader)
    monkeypatch.setattr(sp, "_runner_for", lambda e: _fake_runner)
    monkeypatch.setattr(sp, "_resolve_universe", lambda *a, **k: ("AAA", "BBB"),
                        raising=False)
    persisted = {}
    async def _fake_write(pool, *, engine_name, score, timestamp=None):
        persisted["engine_name"] = engine_name
        persisted["score"] = score
        return True
    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write, raising=True)

    class _NS:
        engine = "reversion"; trials = 4; per_window_trials = 2
        train_start = date(2022, 1, 1); holdout_end = date(2023, 12, 31)
        final_holdout_start = date(2024, 1, 1); final_holdout_end = date(2024, 12, 31)
        walk_forward_step = 365; train_years = 1; holdout_years = 1
        seed = 0; output = str(tmp_path / "o.csv"); database_url = "postgres://x/y"
        dsr_threshold = 0.0; credibility_threshold = 0; universe_tier_max = None
    rc = await sp.amain(_NS())
    assert rc == 0  # SURVIVED (thresholds set permissive)
    assert persisted["engine_name"] == "reversion"  # CURRENT behavior (pre-H-S2-3)
    assert len(seen_overrides) >= 2
    assert all(isinstance(o, dict) for o in seen_overrides)
```

(If a stubbed name differs from the real `search_parameters.py` symbol — e.g. universe resolution helper, or `amain` builds its own pool — adjust the monkeypatch target to the REAL symbol/seam by reading the file; keep every asserted invariant: pure-unit determinism, the SURVIVED rc=0 path, the `write_credibility_score(engine_name="reversion")` *current* arg, ≥2 distinct override dicts. The `engine_name=="reversion"` assertion captures CURRENT behavior; T6 changes it to `lab.<candidate>` and updates THIS test in the same commit — it is the oracle that proves T6's change is the *only* behavior delta.)

- [ ] **Step 2: Run, expect PASS against the un-refactored script** — `cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_search_parameters_characterization.py -q`. If a pure-unit test fails, the test's understanding of the real signature is wrong → fix the TEST to match the real `search_parameters.py` (this is characterization — capture what IS, not what should be). All must pass before any extract.

- [ ] **Step 3: ruff + commit**
```bash
ruff check scripts/tests/test_search_parameters_characterization.py
git add scripts/tests/test_search_parameters_characterization.py
git commit -m "$(cat <<'EOF'
test(lab): characterization oracle for search_parameters (SDLC SP2 T1, H-S2-4)

search_parameters.py had ZERO tests. Pin the pure units
(build_walk_windows/sample_parameters-seeded/dsr/norm_inv/period+slice
metrics/rank/csv) + an amain SURVIVED smoke with a stubbed context
runner (records per-candidate overrides — O2 no-leak). This is the
behavior-preservation oracle the T5 extract is gated on.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `tpcore.db.build_asyncpg_pool` read-only extension (H-S2-2)

**Files:** Modify `tpcore/db.py`; Test `tpcore/tests/test_db_read_only.py` (create — `tpcore/tests` is in testpaths).

- [ ] **Step 1: Write the failing test.**
```python
import asyncpg
import pytest

from tpcore.db import build_asyncpg_pool


@pytest.mark.skipif(__import__("os").environ.get("DATABASE_URL") is None,
                    reason="needs a DB")
async def test_read_only_pool_rejects_writes():
    import os
    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"],
                                    read_only=True, max_size=1)
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")  # reads OK
            with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
                await conn.execute(
                    "CREATE TEMP TABLE _lab_probe(x int); "
                    "INSERT INTO _lab_probe VALUES (1)")
    finally:
        await pool.close()


def test_build_asyncpg_pool_has_read_only_kwarg():
    import inspect
    sig = inspect.signature(build_asyncpg_pool)
    assert "read_only" in sig.parameters
    assert sig.parameters["read_only"].default is False
```

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_db_read_only.py -q` → FAIL (`read_only` kwarg missing).

- [ ] **Step 3: Implement.** In `tpcore/db.py`, the current function is exactly:
```python
async def build_asyncpg_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 4,
    timeout: float = 10.0,
) -> asyncpg.Pool:
    ...
    import asyncpg

    return await asyncpg.create_pool(
        dsn=normalize_database_url(database_url),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
    )
```
Add a `read_only: bool = False` keyword param; when `read_only` OR the `_LAB_ACTIVE` contextvar is set, pass `server_settings={"default_transaction_read_only": "on"}`:
```python
async def build_asyncpg_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 4,
    timeout: float = 10.0,
    read_only: bool = False,
) -> asyncpg.Pool:
    """... (keep existing docstring; add:)
    read_only=True (or an active tpcore.lab _LAB_ACTIVE context) builds a
    pool whose every connection runs with default_transaction_read_only=on
    — any write raises asyncpg ReadOnlySQLTransactionError server-side
    (the Lab isolation floor, SDLC SP2 H-S2-2)."""
    import asyncpg

    from tpcore.lab.context import lab_is_active  # local import: avoid cycle

    server_settings: dict[str, str] = {}
    if read_only or lab_is_active():
        server_settings["default_transaction_read_only"] = "on"
    kwargs: dict = dict(
        dsn=normalize_database_url(database_url),
        min_size=min_size, max_size=max_size, timeout=timeout,
    )
    if server_settings:
        kwargs["server_settings"] = server_settings
    return await asyncpg.create_pool(**kwargs)
```
(`tpcore.lab.context` is engine-free — created in T3; `lab_is_active()` is a 1-line contextvar read. The local import inside the function avoids an import-time cycle and keeps `tpcore/db.py` import-light. If T3 isn't merged yet in your task order, the import will fail — so T3's `tpcore/lab/context.py` MUST exist before this step's code runs; reorder: do T3 Step-3 `context.py` creation before T2 Step-3, OR guard with `try: from tpcore.lab.context import lab_is_active except ImportError: lab_is_active = lambda: False`. Use the try/except guard form — it makes T2 independently landable and is a deliberate, documented resilience, not a placeholder.)

- [ ] **Step 4: Run, expect PASS** — the kwarg test passes; the DB test passes if `DATABASE_URL` is set (skips otherwise — acceptable, T9 exercises it end-to-end). Then **FULL suite** (tpcore change — CLAUDE.md "never modify tpcore without checking all engines"): `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -3` → green (no regression; `read_only=False` default preserves every existing caller).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/db.py tpcore/tests/test_db_read_only.py
git add tpcore/db.py tpcore/tests/test_db_read_only.py
git commit -m "$(cat <<'EOF'
feat(db): build_asyncpg_pool read_only kwarg (SDLC SP2 T2, H-S2-2)

read_only=True (or an active _LAB_ACTIVE context) → server_settings
default_transaction_read_only=on; any write raises asyncpg
ReadOnlySQLTransactionError server-side. Engines build their own pool
from db_url internally, so the floor must live in the builder, not a
wrapper. Default False — every existing caller byte-equivalent;
full suite green (tpcore change).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `tpcore/lab/` engine-free isolation primitives + contract (H-S2-1)

**Files:** Create `tpcore/lab/__init__.py`, `tpcore/lab/context.py`, `tpcore/lab/models.py`; Test `tpcore/tests/test_lab_context.py`.

- [ ] **Step 1: Write failing tests** (`tpcore/tests/test_lab_context.py`):
```python
import pytest

from tpcore.lab.context import (
    LabIsolationViolation, LabContext, assert_not_in_lab, lab_is_active)
from tpcore.lab.models import LabCandidate, LabResult, ParamDelta


def test_lab_is_active_false_by_default():
    assert lab_is_active() is False
    assert_not_in_lab()  # no raise outside a Lab run


async def test_lab_context_sets_and_clears_active():
    assert lab_is_active() is False
    async with LabContext(db_url="postgres://x/y", build_pools=False) as lc:
        assert lab_is_active() is True
        with pytest.raises(LabIsolationViolation):
            assert_not_in_lab()
    assert lab_is_active() is False  # cleared on exit (even on exception)


async def test_lab_context_clears_active_on_exception():
    with pytest.raises(RuntimeError):
        async with LabContext(db_url="postgres://x/y", build_pools=False):
            assert lab_is_active() is True
            raise RuntimeError("boom")
    assert lab_is_active() is False


def test_models_are_frozen_pydantic_v2():
    c = LabCandidate(name="exp1", target_engine="reversion",
                     param_overrides={"z_threshold": 3.2},
                     intent="fold_existing", notes="n")
    with pytest.raises(Exception):
        c.name = "x"  # frozen
    r = LabResult(candidate="exp1", target_engine="reversion",
                  intent="fold_existing", verdict="SURVIVED", dsr=0.97,
                  credibility_score=72, credibility_rubric={},
                  held_metrics={"sharpe": 1.1, "profit_factor": 1.6,
                                "max_drawdown": -0.08, "n_trades": 12,
                                "win_rate": 0.55},
                  winning_params={"z_threshold": 3.2},
                  param_diff=[ParamDelta(name="z_threshold", current=3.0,
                                         winning=3.2)],
                  recommended_exit="fold_existing", ranked_alternatives=[],
                  walk_windows=[], n_trials=200, seed=0,
                  generated_at="2026-05-18T00:00:00Z")
    assert r.verdict == "SURVIVED" and r.recommended_exit == "fold_existing"
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: tpcore.lab`).

- [ ] **Step 3: Implement.** `tpcore/lab/__init__.py`:
```python
"""Engine SDLC SP2 — The Lab: engine-FREE isolation primitives + the
frozen SP2→SP3 contract. This package imports ONLY tpcore.*/pydantic/
stdlib (the tpcore∌engine layering invariant — check_imports scans
tpcore). The engine-importing LabRun lives in `ops/lab/` (H-S2-1)."""
```
`tpcore/lab/context.py`:
```python
from __future__ import annotations

import contextvars
from typing import Any

_LAB_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_LAB_ACTIVE", default=False)


class LabIsolationViolation(RuntimeError):
    """A live side-effect class was constructed inside an active Lab run."""


def lab_is_active() -> bool:
    return _LAB_ACTIVE.get()


def assert_not_in_lab() -> None:
    """Guard installed at every live-side-effect boundary (T4).
    Raises if a Lab run is active — the fail-closed reentrancy layer."""
    if _LAB_ACTIVE.get():
        raise LabIsolationViolation(
            "live side-effect path reached inside an active Lab run "
            "(SDLC SP2 isolation contract)")


class LabContext:
    """Async CM: marks the Lab active (so build_asyncpg_pool goes
    read-only + the reentrancy guards fire) and provides the single
    allowlisted RW credibility pool. build_pools=False is for unit
    tests that only need the contextvar semantics."""

    def __init__(self, *, db_url: str, build_pools: bool = True,
                 max_size: int = 2) -> None:
        self._db_url = db_url
        self._build_pools = build_pools
        self._max_size = max_size
        self._token: contextvars.Token | None = None
        self.read_pool: Any | None = None
        self.credibility_pool: Any | None = None

    async def __aenter__(self) -> LabContext:
        self._token = _LAB_ACTIVE.set(True)
        if self._build_pools:
            from tpcore.db import build_asyncpg_pool
            self.read_pool = await build_asyncpg_pool(
                self._db_url, read_only=True,
                min_size=1, max_size=self._max_size)
            # the ONE allowlisted RW handle — credibility append only.
            self.credibility_pool = await build_asyncpg_pool(
                self._db_url, read_only=False, min_size=1, max_size=1)
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self.read_pool is not None:
                await self.read_pool.close()
            if self.credibility_pool is not None:
                await self.credibility_pool.close()
        finally:
            if self._token is not None:
                _LAB_ACTIVE.reset(self._token)
```
(`build_asyncpg_pool` is imported locally inside `__aenter__` — keeps `tpcore/lab/context.py` import-light and breaks the `db.py`↔`context.py` cycle; `db.py`'s `lab_is_active` import is likewise local/guarded per T2.)
`tpcore/lab/models.py`:
```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class LabCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    target_engine: str
    param_overrides: dict[str, Any]
    intent: Literal["promote_new", "fold_existing"]
    notes: str = ""


class ParamDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    current: Any
    winning: Any


class LabResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate: str
    target_engine: str
    intent: Literal["promote_new", "fold_existing"]
    verdict: Literal["SURVIVED", "FAILED"]
    dsr: float
    credibility_score: int
    credibility_rubric: dict[str, Any]
    held_metrics: dict[str, Any]
    winning_params: dict[str, Any]
    param_diff: list[ParamDelta]
    recommended_exit: Literal["promote_new", "fold_existing", "none"]
    ranked_alternatives: list[dict[str, Any]]
    walk_windows: list[Any]
    n_trials: int
    seed: int
    generated_at: datetime
```

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_lab_context.py -q` → all pass. Confirm layering: `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports tpcore` → `ok` (tpcore/lab imports only tpcore/pydantic/stdlib).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/lab/ tpcore/tests/test_lab_context.py
git add tpcore/lab/ tpcore/tests/test_lab_context.py
git commit -m "$(cat <<'EOF'
feat(lab): engine-free isolation primitives + frozen contract (SDLC SP2 T3, H-S2-1)

tpcore/lab/: _LAB_ACTIVE contextvar, LabContext (read-only read_pool +
single RW credibility_pool), assert_not_in_lab/LabIsolationViolation,
frozen LabCandidate/ParamDelta/LabResult (the SP2→SP3 contract).
Imports only tpcore/pydantic/stdlib — check_imports clean.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Reentrancy guards at the 5 exact boundaries (H-S2-5)

**Files:** Modify `tpcore/risk/governor.py`, `tpcore/aar/writer.py`, `tpcore/order_management/base_order_manager.py`, `tpcore/alpaca/broker_adapter.py`, `tpcore/logging/db_handler.py` (1 line each). Test `tpcore/tests/test_lab_isolation_guards.py`.

- [ ] **Step 1: Write failing tests:**
```python
import pytest

from tpcore.lab.context import LabContext, LabIsolationViolation


async def test_live_constructors_fail_closed_in_lab():
    async with LabContext(db_url="postgres://x/y", build_pools=False):
        from tpcore.aar.writer import AARWriter
        with pytest.raises(LabIsolationViolation):
            AARWriter(None)
        from tpcore.alpaca.broker_adapter import AlpacaPaperBrokerAdapter
        with pytest.raises(LabIsolationViolation):
            AlpacaPaperBrokerAdapter()


def test_live_constructors_ok_outside_lab():
    from tpcore.aar.writer import AARWriter
    AARWriter(None)  # no raise outside a Lab run
```

- [ ] **Step 2: Run, expect FAIL** (no guard yet — `AARWriter(None)` inside LabContext does not raise).

- [ ] **Step 3: Implement — add the guard as the FIRST line of each exact boundary:**
  - `tpcore/risk/governor.py` `RiskGovernor.__init__` (the one at the `RiskGovernor` class, signature `(self, state_store, broker, limits=None, platform_capital=Decimal("0"), pool=None)`) — NOT `InMemoryRiskStateStore.__init__`. First body line: `from tpcore.lab.context import assert_not_in_lab; assert_not_in_lab()`.
  - `tpcore/aar/writer.py` `AARWriter.__init__(self, db_pool=None)` — first body line same guard.
  - `tpcore/order_management/base_order_manager.py` `BaseOrderManager.__init__` — first body line same guard.
  - `tpcore/alpaca/broker_adapter.py` `AlpacaPaperBrokerAdapter.__init__` — first body line same guard.
  - `tpcore/logging/db_handler.py` `DBLogHandler.startup` (the async STARTUP-row method, NOT `__init__`) — first line `assert_not_in_lab()` (import at module top is fine here since db_handler already imports tpcore).
  Use a module-top `from tpcore.lab.context import assert_not_in_lab` where it doesn't create a cycle (governor/writer/base_order_manager/broker_adapter/db_handler do not import tpcore.lab today; tpcore.lab.context imports nothing from them → no cycle; prefer module-top import, fall back to function-local only if a cycle is proven).

- [ ] **Step 4: Run, expect PASS** — guard tests pass. Then **FULL suite** (5 tpcore edits, additive): `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -3` → green (the guards are inert outside a Lab run; verified C2 that no engine backtest path constructs these). If a pre-existing test constructs one of these inside an (unexpected) active Lab context it would fail — investigate; do NOT weaken the guard (it's the safety contract).

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/risk/governor.py tpcore/aar/writer.py tpcore/order_management/base_order_manager.py tpcore/alpaca/broker_adapter.py tpcore/logging/db_handler.py tpcore/tests/test_lab_isolation_guards.py
git add tpcore/risk/governor.py tpcore/aar/writer.py tpcore/order_management/base_order_manager.py tpcore/alpaca/broker_adapter.py tpcore/logging/db_handler.py tpcore/tests/test_lab_isolation_guards.py
git commit -m "$(cat <<'EOF'
feat(lab): fail-closed reentrancy guards at the 5 live boundaries (SDLC SP2 T4, H-S2-5)

assert_not_in_lab() at RiskGovernor.__init__ (NOT InMemoryRiskStateStore),
AARWriter.__init__, BaseOrderManager.__init__, AlpacaPaperBrokerAdapter
.__init__, DBLogHandler.startup. Additive + inert outside a Lab run;
full suite green (no engine backtest path constructs these — C2).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Extract `LabRun` → `ops/lab/run.py`; script delegates (H-S2-1, D-SP2-2)

**Files:** Create `ops/lab/__init__.py`, `ops/lab/run.py`; Modify `scripts/search_parameters.py` (→ thin shim). Oracle: T1 characterization tests stay green UNCHANGED.

- [ ] **Step 1: Confirm the oracle is green** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_search_parameters_characterization.py -q` → all pass (baseline).

- [ ] **Step 2: Implement the extract.** Create `ops/lab/__init__.py` (`"""Engine SDLC SP2 — The Lab: engine-importing LabRun + CLI. ops/ is exempt from the check_imports tpcore∌engine scan (H-S2-1)."""`). Move the orchestration bodies (`build_walk_windows`, `WalkWindow`, `sample_parameters`, `SliceMetrics`, `compute_slice_metrics_from_trades`, `period_returns_from_trades`, `compute_dsr_for_verdict`, `_norm_inv`, `rank_candidates`, `_score_for_ranking`, `write_results_csv`, `TrialResult`, `_evaluate_candidate_with_context`, `_runner_for`, `_context_loader_for`, `_context_runner_for`, `PARAM_RANGES`, and the `amain` body) verbatim into `ops/lab/run.py`. `scripts/search_parameters.py` becomes a thin shim that re-exports the same public names and delegates `amain`/`main`/`_parse_args` to `ops.lab.run` so its existing CLI + exit codes are byte-identical:
```python
"""Thin compatibility shim — the walk-forward Lab engine now lives in
ops.lab.run (SDLC SP2 T5). This module preserves the historical
`python scripts/search_parameters.py` CLI + every public symbol the
characterization oracle pins; all logic delegates to ops.lab.run."""
from ops.lab.run import (  # noqa: F401
    PARAM_RANGES, SliceMetrics, TrialResult, WalkWindow, _context_loader_for,
    _context_runner_for, _evaluate_candidate_with_context, _norm_inv,
    _runner_for, _score_for_ranking, amain, build_walk_windows,
    compute_dsr_for_verdict, compute_slice_metrics_from_trades, main,
    period_returns_from_trades, rank_candidates, sample_parameters,
    write_results_csv,
)

if __name__ == "__main__":
    main()
```
(Re-export the EXACT public + underscore names the T1 oracle imports — `sp._norm_inv`, `sp._runner_for`, `sp._context_runner_for`, `sp.PARAM_RANGES`, etc. — so `import search_parameters as sp` keeps resolving them. The T1 test does `sys.path.insert(0, scripts/); import search_parameters` — that still works via the shim. `ops/lab/run.py` imports the engine backtests exactly as the original did inside `_runner_for`/`_context_*_for` — legal in `ops/`, illegal in `tpcore/`.)

- [ ] **Step 3: Run the oracle UNCHANGED** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_search_parameters_characterization.py -q` → ALL pass with ZERO edits to the test file (proves the extract is behavior-preserving). Also `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore` → `ok` (the engine imports moved to `ops/lab/run.py`, NOT tpcore — H-S2-1 proven).

- [ ] **Step 4: ruff + commit**
```bash
ruff check ops/lab/ scripts/search_parameters.py
git add ops/lab/__init__.py ops/lab/run.py scripts/search_parameters.py
git commit -m "$(cat <<'EOF'
refactor(lab): extract LabRun → ops/lab/run.py; script delegates (SDLC SP2 T5, H-S2-1)

The walk-forward engine moves to ops/lab/run.py (ops/ is exempt from
the check_imports tpcore∌engine scan; tpcore/lab/ could NOT host it).
scripts/search_parameters.py → thin re-export shim; the T1
characterization oracle passes UNCHANGED (behavior-preserving).
check_imports green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Lab-namespaced credibility (H-S2-3 — live-safety)

**Files:** Modify `ops/lab/run.py` (the `write_credibility_score` call site) + `scripts/tests/test_search_parameters_characterization.py` (the one `engine_name` assertion — the oracle proving this is the ONLY behavior delta). Test `tpcore/tests/test_lab_no_gate_poison.py`.

- [ ] **Step 1: Write the failing test:**
```python
import pytest
from tpcore.backtest.credibility import CREDIBILITY_SOURCE_PREFIX


def test_lab_credibility_source_is_namespaced():
    # the Lab MUST persist under backtest_credibility.lab.<candidate>,
    # never backtest_credibility.<live_engine> (would poison
    # graduation_ready for the live engine).
    from ops.lab.run import _lab_credibility_engine_name
    assert _lab_credibility_engine_name("reversion", "exp1") == "lab.exp1"
    src = f"{CREDIBILITY_SOURCE_PREFIX}.{_lab_credibility_engine_name('reversion','exp1')}"
    assert src == "backtest_credibility.lab.exp1"
    assert "backtest_credibility.reversion" != src
```

- [ ] **Step 2: Run, expect FAIL** (`_lab_credibility_engine_name` missing).

- [ ] **Step 3: Implement.** In `ops/lab/run.py` add:
```python
def _lab_credibility_engine_name(target_engine: str, candidate: str) -> str:
    """Lab credibility is namespaced `lab.<candidate>` so
    graduation_ready(pool, <target_engine>) can NEVER read an
    experimental score (live-safety, H-S2-3). `target_engine` is
    accepted for signature symmetry/future use; intentionally unused."""
    return f"lab.{candidate}"
```
Change the `amain` `write_credibility_score(persist_pool, engine_name=args.engine, score=...)` call to use a candidate-aware name. Since `amain` today is keyed by `args.engine`, introduce a `candidate` (default `args.engine` for the legacy CLI path so the *script* behavior is unchanged for the existing search CLI, but the Lab path passes a real candidate). Precisely: add an optional `candidate: str | None = None` to the `amain`/`LabRun` seam; when `candidate` is set (the Lab path) persist under `_lab_credibility_engine_name(args.engine, candidate)`; when `None` (legacy `python scripts/search_parameters.py` CLI) **keep `engine_name=args.engine`** (the historical search-CLI contract is NOT a Lab run and must stay byte-identical — it is a manual operator search, not the isolated Lab). Update the T1 oracle's `test_amain_smoke_survived_verdict`: it passes no `candidate` → still asserts `persisted["engine_name"] == "reversion"` (legacy path unchanged). Add a NEW oracle case `test_amain_lab_path_namespaces_credibility` that passes `candidate="exp1"` and asserts `persisted["engine_name"] == "lab.exp1"`. This makes the behavior delta explicit and oracle-pinned.

- [ ] **Step 4: Run** — `tpcore/tests/test_lab_no_gate_poison.py` + the updated/added characterization cases pass; full characterization file green; `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_no_gate_poison.py -q`.

- [ ] **Step 5: ruff + commit**
```bash
ruff check ops/lab/run.py scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_no_gate_poison.py
git add ops/lab/run.py scripts/tests/test_search_parameters_characterization.py tpcore/tests/test_lab_no_gate_poison.py
git commit -m "$(cat <<'EOF'
fix(lab): Lab-namespaced credibility source (SDLC SP2 T6, H-S2-3)

Lab persists under backtest_credibility.lab.<candidate>, never
backtest_credibility.<live_engine> — graduation_ready(pool,
<live_engine>) cannot be poisoned by an experiment. The legacy
search-CLI path (no candidate) stays byte-identical; the Lab path is
oracle-pinned to lab.<candidate>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Two-tier registry — LAB sentinel + consistency leg (D-SP2-4, H-S2-6)

**Files:** Modify `tpcore/engine_profile.py` (one `_PROFILE` entry), `tpcore/tests/test_engine_lifecycle_consistency.py` (one new leg). (`LabCandidate` already shipped in T3 `tpcore/lab/models.py`.)

- [ ] **Step 1: Write the failing consistency leg.** Append to `tpcore/tests/test_engine_lifecycle_consistency.py`:
```python
def test_lab_sentinel_is_not_wired():
    """The durable LAB sentinel proves LifecycleState.LAB is a real
    exercised state, but is NOT a runnable engine: absent from
    dispatch/allocator, no top-level package, and LAB is the ONLY
    non-{PAPER,LIVE,RETIRED} state (closes the half-state gap
    symmetric to the RETIRED leg)."""
    from tpcore.engine_profile import (
        LifecycleState, _PROFILE, allocator_eligible_engines,
        roster_for_dispatch)
    lab = [n for n, p in _PROFILE.items()
           if p.lifecycle_state is LifecycleState.LAB]
    assert lab == ["lab"], f"expected exactly one LAB sentinel, got {lab}"
    assert "lab" not in roster_for_dispatch()
    assert "lab" not in allocator_eligible_engines()
    assert not (REPO / "lab").is_dir()  # not a top-level package
    states = {p.lifecycle_state for p in _PROFILE.values()}
    assert states <= {LifecycleState.PAPER, LifecycleState.LIVE,
                      LifecycleState.RETIRED, LifecycleState.LAB}
```
(`REPO` is already defined in that test file's header — confirm the name; reuse it.)

- [ ] **Step 2: Run, expect FAIL** (`lab == []` — no sentinel yet).

- [ ] **Step 3: Implement — add the LAB sentinel `_PROFILE` entry** (after the `sigma` RETIRED entry):
```python
    # SP2 Lab sentinel: proves LifecycleState.LAB is a real exercised
    # state. NOT a runnable engine — no package/scheduler; excluded from
    # roster/allocator by _DISPATCHABLE; ephemeral experiments live in
    # ops/lab.registry (D-SP2-4 two-tier). dispatch_order=50 reserved
    # (gap between live ≤5 and retired 99), unique among non-RETIRED.
    "lab":       EngineProfile(engine="lab", cadence=Cadence.DAILY,
                               dispatch_order=50, lifecycle_state=LifecycleState.LAB),
```

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -q` → ALL legs green (the new one + the 6 existing — esp. `test_dispatch_order_invariant_is_the_frozen_literal` still `("reversion","vector","momentum","sentinel","canary")` since LAB ∉ `_DISPATCHABLE`; `test_no_half_state` still passes — `dispatch_order=50` unique among non-RETIRED). Full suite green.

- [ ] **Step 5: ruff + commit**
```bash
ruff check tpcore/engine_profile.py tpcore/tests/test_engine_lifecycle_consistency.py
git add tpcore/engine_profile.py tpcore/tests/test_engine_lifecycle_consistency.py
git commit -m "$(cat <<'EOF'
feat(lab): two-tier LAB registry — _PROFILE sentinel + consistency leg (SDLC SP2 T7, D-SP2-4)

One durable LAB sentinel in _PROFILE (dispatch_order=50, never
dispatched/allocated — proves LifecycleState.LAB is a real exercised
state) + the ephemeral LabCandidate overlay (shipped T3). New
test_lab_sentinel_is_not_wired leg closes the LAB half-state gap
symmetric to RETIRED. SP1 roster literal-pin + all 6 prior legs green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `ops/lab/dossier.py` — two-exit graduation dossier (D-SP2-7, O4)

**Files:** Create `ops/lab/dossier.py`; Test `scripts/tests/test_lab_dossier.py` (scripts/tests is collected; it imports `ops.lab` — legal).

- [ ] **Step 1: Write failing tests:**
```python
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.lab.dossier import dossier_path, render_lab_dossier, write_lab_dossier
from tpcore.lab.models import LabResult, ParamDelta

_R = LabResult(candidate="exp1", target_engine="reversion",
               intent="fold_existing", verdict="SURVIVED", dsr=0.97,
               credibility_score=72, credibility_rubric={"score": 72},
               held_metrics={"sharpe": 1.1, "profit_factor": 1.6,
                             "max_drawdown": -0.08, "n_trades": 12,
                             "win_rate": 0.55},
               winning_params={"z_threshold": 3.2},
               param_diff=[ParamDelta(name="z_threshold", current=3.0,
                                      winning=3.2)],
               recommended_exit="fold_existing", ranked_alternatives=[],
               walk_windows=[], n_trials=200, seed=7,
               generated_at=datetime(2026, 5, 18, tzinfo=UTC))


def test_render_is_deterministic_and_actionable():
    a = render_lab_dossier(_R)
    b = render_lab_dossier(_R)
    assert a == b
    assert "SURVIVED" in a and "fold_existing" in a
    assert "z_threshold" in a and "3.0" in a and "3.2" in a  # the diff


def test_path_scheme_and_idempotent_write(tmp_path, monkeypatch):
    monkeypatch.setattr("ops.lab.dossier.LAB_DIR", tmp_path)
    p1 = write_lab_dossier(_R)
    p2 = write_lab_dossier(_R)
    assert p1 == p2  # same candidate+verdict+seed ⇒ same path
    assert p1.name == "2026-05-18-exp1-SURVIVED-seed7.md"  # O4 discriminator
    assert p1.read_text() == render_lab_dossier(_R)
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: ops.lab.dossier`).

- [ ] **Step 3: Implement `ops/lab/dossier.py`** (structural twin of `tpcore/forensics/dossier.py` — render → deterministic path → write; O4 seed discriminator):
```python
from __future__ import annotations

from pathlib import Path

from tpcore.lab.models import LabResult

LAB_DIR = Path(__file__).resolve().parents[2] / "docs" / "lab"


def render_lab_dossier(r: LabResult) -> str:
    diff = "\n".join(
        f"- `{d.name}`: {d.current} → **{d.winning}**" for d in r.param_diff
    ) or "- (no param diff)"
    alts = "\n".join(f"- {a}" for a in r.ranked_alternatives) or "- (none)"
    return f"""# Lab Dossier — {r.candidate} → {r.target_engine} [{r.verdict}]

**Intent:** {r.intent}  **Recommended exit:** {r.recommended_exit}
**Generated:** {r.generated_at.isoformat()}  **Seed:** {r.seed}  **Trials:** {r.n_trials}

## 1. Verdict
- DSR: {r.dsr:.4f}  (gate ≥ 0.95)
- Credibility: {r.credibility_score}  (gate ≥ 60)
- Held metrics: {r.held_metrics}

## 2. Winning parameters vs current engine defaults
{diff}

## 3. Ranked alternatives
{alts}

## 4. Next step (SP3 — NOT applied by the Lab)
{_next_step(r)}

## 5. Credibility rubric
{r.credibility_rubric}
"""


def _next_step(r: LabResult) -> str:
    if r.recommended_exit == "none":
        return "- Verdict FAILED — iterate; nothing to graduate."
    if r.recommended_exit == "fold_existing":
        return (f"- Fold the §2 param diff into `{r.target_engine}` "
                f"(SP3 Engine Change Request → re-gate). Lab does not apply it.")
    return ("- Promote to a new engine via tpcore/templates/engine_template/ "
            "+ engine_readiness (SP3). Lab does not scaffold it.")


def dossier_path(r: LabResult) -> Path:
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    day = r.generated_at.strftime("%Y-%m-%d")
    return LAB_DIR / f"{day}-{r.candidate}-{r.verdict}-seed{r.seed}.md"


def write_lab_dossier(r: LabResult) -> Path:
    p = dossier_path(r)
    p.write_text(render_lab_dossier(r))
    return p
```

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_lab_dossier.py -q`.

- [ ] **Step 5: ruff + commit**
```bash
ruff check ops/lab/dossier.py scripts/tests/test_lab_dossier.py
git add ops/lab/dossier.py scripts/tests/test_lab_dossier.py
git commit -m "$(cat <<'EOF'
feat(lab): two-exit graduation dossier (SDLC SP2 T8, D-SP2-7/O4)

ops/lab/dossier.py — forensics-pattern twin: render→deterministic
path→write, idempotent, seed discriminator (O4). Recommends
promote_new vs fold_existing with the exact param diff; explicitly
states SP3 (not the Lab) applies it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Binding isolation test in a collected path (H-S2-6)

**Files:** Create `tpcore/tests/test_lab_isolation.py` (in `tpcore/tests` — already in pyproject testpaths; NOT `tpcore/lab/tests` which is uncollected).

- [ ] **Step 1: Write the binding test.** It composes the shipped pieces; DB-gated (skips without `DATABASE_URL`, fully exercised in CI which has one):
```python
import os
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DATABASE_URL") is None,
                                reason="Lab isolation test needs a DB")


async def _rowcount(pool, table, where=""):
    async with pool.acquire() as c:
        return await c.fetchval(f"SELECT count(*) FROM platform.{table} {where}")


async def test_lab_run_zero_live_side_effects():
    """A real LabRun yields ZERO row-delta on the live-write tables and
    persists exactly one row under the lab-namespaced source, ZERO
    under the live engine's source (H-S2-3/6, the make-or-break)."""
    from tpcore.db import build_asyncpg_pool
    from ops.lab.run import LabRun  # the entrypoint (T10 wires the CLI)
    url = os.environ["DATABASE_URL"]
    audit = await build_asyncpg_pool(url, max_size=1)
    try:
        before = {
            t: await _rowcount(audit, t) for t in
            ("risk_state", "open_orders", "aar_events")}
        startup_before = await _rowcount(
            audit, "application_log", "WHERE event_type='STARTUP'")
        rev_before = await _rowcount(
            audit, "data_quality_log",
            "WHERE source='backtest_credibility.reversion'")

        await LabRun(candidate="iso_probe", target_engine="reversion",
                     param_overrides={}, intent="fold_existing",
                     db_url=url, trials=2, per_window_trials=1,
                     universe=("AAPL", "MSFT")).execute()

        for t, b in before.items():
            assert await _rowcount(audit, t) == b, f"Lab wrote {t}"
        assert await _rowcount(
            audit, "application_log",
            "WHERE event_type='STARTUP'") == startup_before
        assert await _rowcount(
            audit, "data_quality_log",
            "WHERE source='backtest_credibility.reversion'") == rev_before
        lab_rows = await _rowcount(
            audit, "data_quality_log",
            "WHERE source='backtest_credibility.lab.iso_probe'")
        assert lab_rows >= 1
    finally:
        await audit.close()


async def test_read_pool_rejects_write_and_guards_fire():
    import asyncpg
    from tpcore.lab.context import LabContext, LabIsolationViolation
    url = os.environ["DATABASE_URL"]
    async with LabContext(db_url=url) as lc:
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            async with lc.read_pool.acquire() as c:
                await c.execute("CREATE TEMP TABLE _p(x int); INSERT INTO _p VALUES(1)")
        from tpcore.risk.governor import RiskGovernor
        with pytest.raises(LabIsolationViolation):
            RiskGovernor(None, None)  # guard fires before arg use
```
(`LabRun(...).execute()` is the T10 entrypoint shape — confirm/align the exact constructor/method names to what T5/T10 actually expose; keep the asserted invariant: zero live-write deltas, one lab-source row, zero live-source row, read-pool write rejected, guard fires. If the real `LabRun` API differs, align the call — NOT the assertions.)

- [ ] **Step 2: Run** — locally skips if no `DATABASE_URL`; with one (or in CI) all pass. Document the local run result.

- [ ] **Step 3: ruff + commit**
```bash
ruff check tpcore/tests/test_lab_isolation.py
git add tpcore/tests/test_lab_isolation.py
git commit -m "$(cat <<'EOF'
test(lab): binding zero-live-side-effects isolation test (SDLC SP2 T9, H-S2-6)

In tpcore/tests (a COLLECTED testpath — not the uncollected
tpcore/lab/tests). Real LabRun ⇒ zero row-delta on risk_state/
open_orders/aar_events/STARTUP, one row under lab.<candidate>, zero
under the live engine source; read-pool INSERT → ReadOnlySQLTransaction
Error; live constructor inside LabContext → LabIsolationViolation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `python -m ops.lab` CLI + full gate + finish

**Files:** Create `ops/lab/__main__.py` + (if `LabRun` class not already in T5) finalize `ops/lab/run.py`'s `LabRun`/`execute()`; verification only otherwise.

- [ ] **Step 1: Implement `ops/lab/__main__.py`** — argparse `--candidate --target-engine --intent {promote_new,fold_existing} --param-overrides JSON --db-url --trials --per-window-trials [--universe ...]`; builds a `LabCandidate`, runs `LabRun(...).execute()` inside a `LabContext`, writes the dossier via `write_lab_dossier`, prints the dossier path + verdict, exit 0 on SURVIVED else 1. Mirror the `ops.weekly_digest`/`ops.engine_ladder` `_amain`/`main`/`__main__` shape (no-DB → explicit rc, never silent). Add a small test `scripts/tests/test_lab_cli_entrypoint.py` asserting `python -m ops.lab` has `__main__`/argparse/`main` and a no-DSN explicit non-zero (the canary `-m`-no-op lesson).

- [ ] **Step 2: Run the new CLI test + the full suite**
`/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_lab_cli_entrypoint.py -q`
`/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -3` → full repo suite green (all SP2 tests + the T1 oracle + every pre-existing test, incl. `test_engine_lifecycle_consistency.py` 7 legs).

- [ ] **Step 3: CI-exact + lane gate** (verbatim tails):
```bash
ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
BASE=$(git merge-base HEAD origin/main); git diff --name-only $BASE..HEAD | grep -E "tpcore/providers\.py|tpcore/ladder/|ops/weekly_digest\.py|ops/data_repair_service\.py|tpcore/(selfheal|feeds|ingestion|datasupervisor)/|scripts/run_data_operations\.sh|tpcore/parity/" && echo "LANE VIOLATION" || echo "lane-clean"
```
Expected: `All checks passed!`; `ok: no forbidden imports found` (proves H-S2-1 — engine imports are in `ops/lab/`, not `tpcore/lab/`); `lane-clean`. If `check_imports` flags a `tpcore/lab/*` engine import, H-S2-1 is violated — move that code to `ops/lab/`, never weaken the scanner.

- [ ] **Step 4: commit + finish**
```bash
ruff check ops/lab/__main__.py scripts/tests/test_lab_cli_entrypoint.py
git add ops/lab/__main__.py scripts/tests/test_lab_cli_entrypoint.py ops/lab/run.py
git commit -m "$(cat <<'EOF'
feat(lab): python -m ops.lab CLI + SP2 gate (SDLC SP2 T10)

On-demand Lab entrypoint (LabContext → LabRun → write_lab_dossier),
never in dispatch/daemon; no-DSN → explicit non-zero. Full suite +
CI-exact ruff + check_imports green (H-S2-1 proven: engine imports in
ops/lab/, tpcore/lab/ engine-free); lane-clean.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```
Then use **superpowers:finishing-a-development-branch**: push `worktree-engine-lab`, open a PR, fetch origin/main + resolve conflicts combining intents (data session may have touched shared files — keep BOTH), integrated full suite green, merge when CI green (squash, no `--delete-branch`; delete the remote branch separately), clean the worktree. Do NOT local-merge into the shared checkout.

---

## Self-Review

**1. Spec coverage:** §3 the 5 deliverables → T3 (LabContext+models), T5 (LabRun), T7 (two-tier registry), T8 (dossier), T10 (CLI); §4 enforced 3-layer isolation → T2 (L1 read-only-in-builder), T3 (L2 credibility pool + context), T4 (L3 reentrancy guards), T9 (binding zero-side-effect test); §5 LAB lifecycle + half-state-gap fix → T7; §6 concurrency-with-live → T3 (pool sizing) + T10 (on-demand, never in dispatch); §7 LabResult + dual persistence + recommendation-not-application → T3 (frozen LabResult), T6 (lab-namespaced credibility), T8 (dossier `_next_step` states SP3 applies it); §8 D-SP2-1..9 each mapped; §12 H-S2-1 (T0 split + T5 ops/lab + T10 check_imports proof), H-S2-2 (T2), H-S2-3 (T6), H-S2-4 (T1 first), H-S2-5 (T4 exact points), H-S2-6 (T9 collected path); §10 out-of-scope honored (no LAB→PAPER transition/scaffold/constant-patch — dossier explicitly defers to SP3). No gaps.

**2. Placeholder scan:** every step has literal code + exact command + expected result. The few "align the stub/seam to the real symbol if it differs, keep the asserted invariant" notes are explicit verify-against-reality contingencies (the recon supplies the verbatim signatures) — the established style, not deferred work. T0 is a recorded no-code decision (the layering split) — not a placeholder; it gates every later task.

**3. Type/name consistency:** `LabContext`/`lab_is_active`/`assert_not_in_lab`/`LabIsolationViolation` (T3) consumed by T2 (`db.py` import), T4 (guards), T9; `LabCandidate`/`LabResult`/`ParamDelta` frozen pydantic-v2 (T3) consumed by T8/T9/T10; `_lab_credibility_engine_name`→`lab.<candidate>` (T6) pinned by `test_lab_no_gate_poison` + the T1 oracle; the LAB `_PROFILE` sentinel `dispatch_order=50` + `test_lab_sentinel_is_not_wired` (T7) consistent with SP1's `roster_for_dispatch()` literal-pin (LAB ∉ `_DISPATCHABLE`); `ops/lab/run.py::LabRun(...).execute()` referenced consistently T9/T10 (with the explicit "align to the real API, not the assertions" note). The H-S2-1 split (tpcore/lab engine-free; ops/lab engine-importing) is enforced by the T10 `check_imports` gate. No mismatches.
