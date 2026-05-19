# SP-D — Pluggable Per-Engine Success Scoring + Richer Dossier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the Lab's Sharpe-only candidate ranking to a per-engine declared primary metric (`SHARPE` default = byte-identical; `MAXDD_REDUCTION` new; `ULCER`/`INVERSE_ETF_HOLD` reserved fail-loud) plus an objective-keyed dossier block, while proving the sacred graduation gate (DSR≥0.95 ∧ cred≥60 ∧ n_trades≥3) and its downstream ECR re-derivation stay byte-identical regardless of the chosen metric.

**Architecture:** Add an engine-free `LabPrimaryMetric` `StrEnum` + a defaulted `LabTarget.primary_metric` field in `tpcore/lab/target.py` (the SP-B engine-owned contract layer); a Lab-resident frozen `_RANKING_METRICS` dispatch table + parameterized `_score_for_ranking`/`rank_candidates` in `ops/lab/run.py`; a pure pre-spend `_resolve_ranking_metric` fence wired strictly before the SP-A `record_trial_spend` block; a defaulted `LabResult.primary_metric` for sidecar provenance + read-compat with pre-SP-D sidecars; an objective block in `ops/lab/dossier.py`. The gate functions and the downstream `planner._validate_modify`/`_evidence.py` re-derivation are fenced byte-unchanged by an AST/source-hash test; behaviour is proven by a non-tautological make-or-break runtime test across two full pipeline executions.

**Tech Stack:** Python 3.11, pydantic v2 (frozen, `extra="forbid"`), `enum.StrEnum`, numpy, pytest (asyncio auto-mode), ruff, `tpcore.scripts.check_imports`, `scripts/gen_engine_manifest.py`.

---

## File Structure (decomposition map)

| File | Responsibility | Created/Modified |
| --- | --- | --- |
| `tpcore/lab/target.py` | `LabPrimaryMetric` StrEnum + defaulted `LabTarget.primary_metric` field (engine-free vocabulary) | Modify |
| `tpcore/lab/models.py` | `LabResult.primary_metric` defaulted field (sidecar provenance + pre-SP-D read-compat) | Modify |
| `ops/lab/run.py` | `_RANKING_METRICS` frozen dict + `_unimplemented_metric` sentinel + parameterized `_score_for_ranking`/`rank_candidates` + `_resolve_ranking_metric` pre-spend fence + `_run_lab_core`/`_build_lab_result` wiring | Modify |
| `ops/lab/dossier.py` | Objective line in "## 1. Verdict" + new "## 2a. Objective-appropriate summary" block | Modify |
| `tpcore/tests/test_lab_sp_d_char_anchor.py` | §5.1 char-before-refactor golden of current Sharpe ranking | Create |
| `tpcore/tests/test_lab_sp_d_make_or_break.py` | §5.2 non-tautological gate-invariance proof (step 0 + ECR 4-tuple + adversarial probe through `_validate_modify`) | Create |
| `tpcore/tests/test_lab_sp_d_units.py` | §5.5 focused units: MAXDD_REDUCTION, NaN clamp, pre-spend reject (ledger-spy), dossier, pre-SP-D-sidecar regression | Create |
| `tpcore/tests/test_lab_primary_metric_consistency.py` | §5.4 metric-implementability clockwork | Create |
| `tpcore/tests/test_lab_sp_d_diff_fence.py` | §5.3 AST/source-hash diff-scope allow-list fence | Create |
| `pyproject.toml` | per-file SLF ignores for the new tests that name `ops.lab.run`/`tpcore.engine_profile` privates (mirrors SP-B precedent) | Modify |

**Tasks map onto spec §7 T0..T5.** Task 1 = the char-before-refactor anchor that MUST be pinned before any code change (proves "default=SHARPE byte-identical" rather than asserting it). Task 2 = T0 RED skeletons. Tasks 3–8 = T1..T5.

---

## Conventions every task obeys

- `from __future__ import annotations` at the top of every new module.
- pydantic v2; engine-free `tpcore` (no `ops`/engine import in `tpcore/lab/target.py` — only `enum` is added vs today).
- NEVER an inline `# noqa: SLF001`. A new test that must name an `ops.lab.run`-private or `tpcore.engine_profile`-private symbol gets a **scoped per-file ignore in `pyproject.toml`** (mirrors the SP-B `test_lab_dispatch_indirection.py` / `test_lab_targeting_consistency.py` precedent — see `pyproject.toml:153` and `:173`).
- Conventional-commit messages; commit at the end of every task (and the sub-commits noted).
- The characterization oracle `scripts/tests/test_search_parameters_characterization.py` is **byte-frozen** — it must stay byte-unmodified and green through every task (the defaulted-arg shape is chosen specifically so the no-arg `rank_candidates([...])` call in `test_rank_candidates_groups_and_sorts` is byte-identical).
- Run pytest single-process when an `ops/*.py` import path is exercised (CLAUDE.md ops-package shadow rule); the authoritative gate is the full single-process suite + order-flip + `gh pr checks`, NOT any subset.
- Co-Authored-By trailer on every commit:

```
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

### Task 1: Char-before-refactor anchor of the CURRENT Sharpe ranking (T0, pin BEFORE any code change)

Pins the exact current `_score_for_ranking`/`rank_candidates` Sharpe behaviour to a committed golden **before any production change** so "default=`SHARPE` is byte-identical" is *proven by a green golden*, not asserted. This is the TDD-RED-first anchor.

**Files:**
- Create: `tpcore/tests/test_lab_sp_d_char_anchor.py`
- Modify: `pyproject.toml:122-176` (add a per-file SLF ignore — the test names `ops.lab.run`-private `_score_for_ranking`)

- [ ] **Step 1: Write the char-anchor test against the CURRENT (un-changed) code**

The current signatures are `_score_for_ranking(metrics: SliceMetrics) -> float` (`ops/lab/run.py:466`) and `rank_candidates(trials) -> list[tuple[dict,float,int]]` (`ops/lab/run.py:479`). The golden is computed inline from the current closed-form expression so a refactor that changes it reds this test.

Create `tpcore/tests/test_lab_sp_d_char_anchor.py`:

```python
"""SP-D §5.1 — char-before-refactor anchor.

Pins the EXACT current Sharpe ranking BEFORE any SP-D code change so the
post-refactor defaulted (metric=SHARPE, no arg) path is provably
byte-identical, not merely asserted. The golden is the current closed
form character-for-character:
    n_trades < 3            -> -1.0
    else                    -> sharpe + 0.05 * log10(max(n_trades, 1))
plus the current rank_candidates grouping + descending mean-score sort.
This test must stay GREEN through every SP-D task with NO edit.
"""
from __future__ import annotations

import math

import pytest

import ops.lab.run as sp

pytestmark = pytest.mark.xdist_group("ops_shadow")


def _golden_score(n_trades: int, sharpe: float) -> float:
    if n_trades < 3:
        return -1.0
    return float(sharpe) + 0.05 * math.log10(max(n_trades, 1))


@pytest.mark.parametrize(
    "n_trades,sharpe",
    [
        (10, 0.5), (10, 1.5), (10, 0.2),   # the oracle's exact triple
        (2, 9.9),                          # thin -> -1.0 floor
        (3, 0.0),                          # boundary n_trades==3
        (250, 2.3),                        # high trade-count bonus arm
        (5, -0.4),                         # negative Sharpe
    ],
)
def test_score_for_ranking_matches_current_closed_form(n_trades, sharpe):
    m = sp.SliceMetrics(
        n_trades=n_trades, sharpe=sharpe, profit_factor=1.5,
        max_drawdown=-0.1, win_rate=0.5,
    )
    assert sp._score_for_ranking(m) == _golden_score(n_trades, sharpe)


def test_rank_candidates_current_grouping_and_sort_golden():
    def tr(tid, params, sharpe):
        return sp.TrialResult(
            trial_id=tid, window_label="w", parameters=params,
            holdout=sp.SliceMetrics(
                n_trades=10, sharpe=sharpe, profit_factor=1.5,
                max_drawdown=-0.1, win_rate=0.5),
            full_credibility_score=70, error=None,
        )

    p1 = {"z_threshold": 3.0}
    p2 = {"z_threshold": 2.5}
    ranked = sp.rank_candidates(
        [tr(0, p1, 0.5), tr(1, p1, 1.5), tr(2, p2, 0.2)]
    )
    # p1 mean score = mean(_golden(10,0.5), _golden(10,1.5))
    p1_mean = (_golden_score(10, 0.5) + _golden_score(10, 1.5)) / 2.0
    p2_mean = _golden_score(10, 0.2)
    assert ranked[0][0] == p1
    assert ranked[0][1] == pytest.approx(p1_mean)
    assert ranked[0][2] == 2
    assert ranked[1][0] == p2
    assert ranked[1][1] == pytest.approx(p2_mean)
    assert ranked[1][2] == 1
```

- [ ] **Step 2: Add the scoped SLF per-file ignore (no inline noqa, SP-B precedent)**

In `pyproject.toml`, inside `[tool.ruff.lint.per-file-ignores]` (the block ending at the `test_lab_targeting_consistency.py` entry around `:173`), append directly after that last `"tpcore/tests/test_lab_targeting_consistency.py" = ["SLF"]` line:

```toml
# SP-D §5.1 char-before-refactor anchor: pinning the CURRENT Sharpe
# ranking REQUIRES naming the `ops.lab.run`-private `_score_for_ranking`
# (that IS the test's purpose — it freezes the private scorer's closed
# form). Engine-lane-module-private (NOT tpcore-private) access in a
# char-oracle; the scoped per-file ignore is the correct form (mirrors
# the SP-B char/dispatch precedents above) — never an inline
# `# noqa: SLF001` (CLAUDE.md / STYLE_GUIDE).
"tpcore/tests/test_lab_sp_d_char_anchor.py" = ["SLF"]
```

- [ ] **Step 3: Run the anchor against the UNCHANGED tree — expect GREEN (it pins current behaviour)**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_char_anchor.py -p no:xdist -q`
Expected: **PASS** (8 params + 1 = all green). This is the deliberate exception to RED-first: the anchor pins *existing* behaviour, so it is green now and must STAY green post-refactor (Task 4 proves the byte-identity).

- [ ] **Step 4: Confirm the byte-frozen oracle is still green on the unmodified tree (baseline)**

Run: `python -m pytest scripts/tests/test_search_parameters_characterization.py -p no:xdist -q`
Expected: **PASS** (the pre-SP-D baseline; this exact command must still pass after Task 4 with the oracle byte-unmodified).

- [ ] **Step 5: ruff the new test + pyproject**

Run: `ruff check tpcore/tests/test_lab_sp_d_char_anchor.py pyproject.toml`
Expected: no output (clean).

- [ ] **Step 6: Commit**

```bash
git add tpcore/tests/test_lab_sp_d_char_anchor.py pyproject.toml
git commit -m "test(lab-sp-d): char-before-refactor anchor of current Sharpe ranking (T0)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: RED skeletons — make-or-break + pre-SP-D-sidecar regression fixture (T0)

Authors the §5.2 make-or-break test (including step-0 non-vacuity preconditions) and the §5.5 pre-SP-D-sidecar regression against the *intended* SP-D signatures so they RED now (symbols don't exist) and become the implementation's acceptance gate in Tasks 5/7.

**Files:**
- Create: `tpcore/tests/test_lab_sp_d_make_or_break.py`
- Create: `tpcore/tests/test_lab_sp_d_units.py` (only the pre-SP-D-sidecar regression test in this task; the rest of §5.5 lands in Task 7)
- Modify: `pyproject.toml` (per-file SLF ignores for both new test files — they name `ops.lab.run` privates)

- [ ] **Step 1: Write the make-or-break test (RED — SP-D symbols don't exist yet)**

The stub mirrors `tpcore/tests/test_lab_targeting_consistency.py` `_install_offline`/`_SharedPool`/`_FakeConn`/`_Trade` (verbatim pattern, `:181-280`) so the ledger-spy + offline harness is the established one. The candidate set is a single `choice:A,B,C` param so `sample_parameters` yields a fixed noise-free 3-set; each callable returns a deterministic trade-log keyed by the param dict. A=deep DD+higher Sharpe+survives, B=shallow DD+lower Sharpe+survives (orders provably invert), C=final-holdout `n_trades<3` (metric-blind fail lever) but with a finite *windowed* `holdout` score so C is a real `ranked` member.

Create `tpcore/tests/test_lab_sp_d_make_or_break.py`:

```python
"""SP-D §5.2 — the make-or-break: the ECR-relevant gate surface is
byte-identical regardless of the declared ranking metric (NON-tautological).

Step 0 asserts its own non-vacuity (ERRORs, never silently passes, if the
stub stops creating gate/ranking disagreement). Steps 2-5 run the WHOLE
_run_lab_core -> _build_lab_result pipeline twice (SHARPE vs
MAXDD_REDUCTION), assert the §0.2a ECR-re-derived 4-tuple
(verdict, dsr, credibility_score, winning_params) is byte-identical per
candidate while the WINNER differs, and drive an adversarial metric
through BOTH the in-core gate AND planner._validate_modify.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

import ops.lab.run as lab_run
from tpcore.lab.context import LabContext

pytestmark = pytest.mark.xdist_group("ops_shadow")


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


# §8-A15 satisfiable construction (corrected MAXDD_REDUCTION = the
# m.max_drawdown value itself; shallower=less-negative=larger=ranks-first
# under the descending reverse=True sort). Offline-proven:
#   WINDOWED (n=8 all): A sharpe=4.1666 mdd=-0.045 (SHARPE winner);
#   B sharpe=2.9704 mdd=0.000 (MAXDD winner); C sharpe=3.6178 mdd=-0.015
#   (wins neither). SHARPE A>C>B (A strictly max); MAXDD B>C>A (strict).
#   FINAL HOLDOUT: A,B n=8 survive; C n=2 (<3 ⇒ metric-blind gate FAIL).
_PROFILES = {
    "A": {"kind": "loss", "ret": 0.080, "loss": -0.045, "dd_trades": 8},
    "B": {"kind": "volpos", "hi": 0.040, "lo": 0.002, "dd_trades": 8},
    "C": {"kind": "loss", "ret": 0.020, "loss": -0.015, "dd_trades": 2},
}


def _trade_log(choice: str, *, final_holdout: bool) -> list[_Trade]:
    p = _PROFILES[choice]
    n = p["dd_trades"] if final_holdout else 8
    base = date(2022, 1, 3) if final_holdout else date(2019, 1, 3)
    log = []
    for i in range(n):
        if p["kind"] == "volpos":
            # Strictly positive ⇒ zero drawdown (shallowest); hi/lo
            # spread keeps Sharpe modest (not a near-constant blow-up).
            r = p["hi"] if i % 2 == 0 else p["lo"]
        else:
            r = p["ret"]
            if i == n // 2:  # one moderate loss ⇒ deep-ish drawdown
                r = p["loss"]
        log.append(_Trade(entry_date=base + timedelta(days=30 * i),
                           pnl_pct=r))
    return log


class _RR:
    def __init__(self, choice: str, *, final_holdout: bool):
        from tpcore.backtest.credibility import CredibilityScore
        self.credibility_score = 80
        self.credibility_rubric = CredibilityScore(
            lookahead_clean=True, survivorship_inclusive=True,
            pit_fundamentals=True, regime_coverage=True,
            out_of_sample_validated=True, monte_carlo_drawdown=True,
            score=80)
        self.trade_log = _trade_log(choice, final_holdout=final_holdout)


def _install_choice_stub(monkeypatch):
    """A LabTarget whose callables key a deterministic trade-log off the
    `choice` param. choice:A,B,C -> a fixed noise-free 3-set."""
    from tpcore.lab.target import LabTarget

    def _choice_of(overrides: dict | None) -> str:
        return (overrides or {}).get("choice", "A")

    async def _runner(*, db_url, start, end, overrides, universe):
        # The final held-back replay (runner) -> final_holdout=True.
        return _RR(_choice_of(overrides), final_holdout=True)

    async def _loader(*, db_url, start, end, universe):
        return object()

    def _ctx_runner(context, *, overrides=None):
        # Per-window evaluation -> final_holdout=False (a real ranked
        # member; C has 8 windowed trades so it is NOT n<3-floored here).
        return _RR(_choice_of(overrides), final_holdout=False)

    def _default_params() -> dict:
        return {"choice": "A"}

    tgt = LabTarget(
        param_ranges={"choice": (0, 1, "choice:A,B,C")},
        run_for_search=_runner,
        load_window_context=_loader,
        run_with_context=_ctx_runner,
        default_params=_default_params,
    )
    monkeypatch.setattr("ops.lab.run._lab_target_for", lambda e: tgt)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for", lambda e: _loader)
    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)

    async def _fw(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fw, raising=True)
    return tgt


def _ns(output, *, seed):
    return argparse.Namespace(
        engine="reversion", trials=3, per_window_trials=3,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.0, credibility_threshold=0,
        universe_tier_max=None)


def _candidate(name: str):
    from tpcore.lab.models import LabCandidate
    return LabCandidate(name=name, target_engine="reversion",
                        param_overrides={}, intent="fold_existing")


async def _run_once(monkeypatch, tmp_path, *, metric_name, seed):
    from tpcore.lab.target import LabPrimaryMetric
    tgt = _install_choice_stub(monkeypatch)
    monkeypatch.setattr(
        "ops.lab.run._lab_target_for",
        lambda e: tgt.model_copy(
            update={"primary_metric": LabPrimaryMetric(metric_name)}))
    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)
    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / f"{metric_name}.csv", seed=seed),
            candidate=f"exp_{metric_name}")
    assert not isinstance(core, int)
    lr = lab_run._build_lab_result(
        candidate=_candidate(f"exp-{metric_name}"), core=core,
        args=_ns(tmp_path / f"{metric_name}2.csv", seed=seed))
    return core, lr


async def test_step0_non_vacuity_preconditions(monkeypatch, tmp_path):
    """Step 0: the stub MUST create gate/ranking disagreement, else the
    proof is vacuous. ERROR (not silently pass) if any precondition fails.
    """
    from tpcore.lab.target import LabPrimaryMetric

    _install_choice_stub(monkeypatch)  # side-effect: stub install only

    def _tr(choice):
        return lab_run.TrialResult(
            trial_id=0, window_label="w", parameters={"choice": choice},
            holdout=lab_run.compute_slice_metrics_from_trades(
                _trade_log(choice, final_holdout=False), span_days=365),
            full_credibility_score=80, error=None)

    trials = [_tr("A"), _tr("B"), _tr("C")]
    sharpe_rank = lab_run.rank_candidates(trials, LabPrimaryMetric.SHARPE)
    maxdd_rank = lab_run.rank_candidates(
        trials, LabPrimaryMetric.MAXDD_REDUCTION)
    if sharpe_rank[0][0] != {"choice": "A"}:
        pytest.fail("VACUOUS: SHARPE winner != A — stub no longer creates "
                    "the intended ranking; proof would be meaningless")
    if maxdd_rank[0][0] != {"choice": "B"}:
        pytest.fail("VACUOUS: MAXDD_REDUCTION winner != B — orders no "
                    "longer invert; proof would be meaningless")
    if sharpe_rank[0][0] == maxdd_rank[0][0]:
        pytest.fail("VACUOUS: SHARPE and MAXDD winners coincide")
    # §8-A15: pin the *strict* disagreement so a future lever drift that
    # re-introduces a tie or collapses the order ERRORs loudly.
    sharpe_score = {tuple(sorted(p.items())): s for p, s, _ in sharpe_rank}
    maxdd_score = {tuple(sorted(p.items())): s for p, s, _ in maxdd_rank}
    a, b, c = (("choice", "A"),), (("choice", "B"),), (("choice", "C"),)
    if not (sharpe_score[a] > sharpe_score[b]
            and sharpe_score[a] > sharpe_score[c]):
        pytest.fail("VACUOUS: A's SHARPE score is not STRICTLY maximal")
    if not (maxdd_score[b] > maxdd_score[c] > maxdd_score[a]):
        pytest.fail("VACUOUS: corrected-MAXDD order is not STRICTLY B>C>A")
    # C's final-holdout replay must fail the gate via n_trades<3.
    held = lab_run.compute_slice_metrics_from_trades(
        _trade_log("C", final_holdout=True), span_days=365)
    if held.n_trades >= 3:
        pytest.fail("VACUOUS: C's final-holdout replay has n_trades>=3 — "
                    "the metric-blind fail lever is gone")
    # C's WINDOWED replay must still be a real ranked member (n>=3).
    c_windowed = lab_run.compute_slice_metrics_from_trades(
        _trade_log("C", final_holdout=False), span_days=365)
    if c_windowed.n_trades < 3:
        pytest.fail("VACUOUS: C's WINDOWED replay has n_trades<3 — C is "
                    "pre-killed by the ranking floor, not a real member")


def _ecr_tuple(lr) -> tuple[str, float, int, dict]:
    """The EXACT 4-tuple planner._validate_modify re-derives (§0.2a/A12):
    verdict, dsr, credibility_score, winning_params. The make-or-break
    invariant: byte-identical between the SHARPE and MAXDD runs for a
    FIXED candidate — the gate must not move when only the metric does."""
    return (lr.verdict, lr.dsr, lr.credibility_score, lr.winning_params)


async def test_make_or_break_gate_invariant_over_ecr_tuple(
        monkeypatch, tmp_path):
    """Steps 2-4 + §0.2a/A12: run the WHOLE pipeline twice. For a FIXED
    candidate the ECR-re-derived 4-tuple is BYTE-IDENTICAL between the
    two metric runs (the gate verdict does NOT move when only ranking
    changes — that IS the make-or-break); only the headline differs."""
    core_s, lr_s = await _run_once(
        monkeypatch, tmp_path, metric_name="sharpe", seed=1)
    core_m, lr_m = await _run_once(
        monkeypatch, tmp_path, metric_name="maxdd_reduction", seed=1)

    # Step 4 (pluggability anti-vacuity): the metric genuinely re-orders.
    assert lr_s.winning_params != lr_m.winning_params
    assert lr_s.winning_params == {"choice": "A"}
    assert lr_m.winning_params == {"choice": "B"}

    from tpcore.lab.target import LabPrimaryMetric

    async def _run_pinned(*, metric_name: str, pinned: str):
        tgt = _install_choice_stub(monkeypatch)
        monkeypatch.setattr(
            "ops.lab.run._lab_target_for",
            lambda e: tgt.model_copy(
                update={"primary_metric": LabPrimaryMetric(metric_name)}))
        real_rank = lab_run.rank_candidates

        def _pinned_rank(trials, metric=LabPrimaryMetric.SHARPE):
            ranked = real_rank(trials, metric)
            head = [r for r in ranked if r[0] == {"choice": pinned}]
            rest = [r for r in ranked if r[0] != {"choice": pinned}]
            return head + rest

        monkeypatch.setattr("ops.lab.run.rank_candidates", _pinned_rank)
        shared = _SharedPool()

        async def _fb(url, *, read_only, **k):
            return shared

        monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb,
                            raising=True)
        async with LabContext(db_url="postgres://fake/db"):
            core = await lab_run._run_lab_core(
                _ns(tmp_path / f"{metric_name}_{pinned}.csv", seed=1),
                candidate=f"exp_{metric_name}_{pinned}")
        assert not isinstance(core, int)
        return lab_run._build_lab_result(
            candidate=_candidate(f"exp-{metric_name}-{pinned}"), core=core,
            args=_ns(tmp_path / f"{metric_name}_{pinned}2.csv", seed=1))

    for pinned in ("A", "B", "C"):
        lr_sharpe = await _run_pinned(metric_name="sharpe", pinned=pinned)
        lr_maxdd = await _run_pinned(
            metric_name="maxdd_reduction", pinned=pinned)
        t_s, t_m = _ecr_tuple(lr_sharpe), _ecr_tuple(lr_maxdd)
        assert t_s[0] == t_m[0], f"{pinned}: verdict moved with metric"
        assert t_s[2] == t_m[2], f"{pinned}: credibility moved"
        assert t_s[3] == t_m[3], f"{pinned}: winning_params moved"
        assert not math.isnan(t_s[1]) and not math.isnan(t_m[1]), (
            f"{pinned}: dsr is NaN — must FAIL the invariant, not pass")
        assert t_s[1] == t_m[1], f"{pinned}: dsr moved with metric"

    assert _ecr_tuple(lr_s) != _ecr_tuple(lr_m)  # headlines differ


async def test_make_or_break_adversarial_through_both_gates(
        monkeypatch, tmp_path):
    """Step 5: an adversarial _RANKING_METRICS entry that maximizes the
    GATE-FAILING candidate C cannot launder it past the in-core gate OR
    the downstream planner._validate_modify (§0.2a)."""
    from tpcore.lab.target import LabPrimaryMetric

    tgt = _install_choice_stub(monkeypatch)
    monkeypatch.setattr(
        "ops.lab.run._lab_target_for",
        lambda e: tgt.model_copy(
            update={"primary_metric": LabPrimaryMetric.MAXDD_REDUCTION}))

    def _adversarial(m):
        # +1e9 for the C-shaped slice (n_trades small in final holdout but
        # finite windowed score), -1e9 otherwise. Keyed off win_rate so it
        # is metric-value adversarial without touching the gate.
        return 1e9 if m.n_trades < 5 else -1e9

    monkeypatch.setitem(
        lab_run._RANKING_METRICS, LabPrimaryMetric.MAXDD_REDUCTION,
        _adversarial)
    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)
    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / "adv.csv", seed=1), candidate="exp_adv")
    assert not isinstance(core, int)
    assert core.winner_params == {"choice": "C"}      # adversarial picked C
    assert core.survived is False                     # in-core gate rejects
    lr = lab_run._build_lab_result(
        candidate=_candidate("exp-adv"), core=core,
        args=_ns(tmp_path / "adv2.csv", seed=1))
    assert lr.verdict == "FAILED"
    assert lr.recommended_exit == "none"

    # Downstream: a synthetic ECR citing this sidecar must hard-reject on
    # lr.verdict != "SURVIVED" inside planner._validate_modify.
    from ops.engine_sdlc.planner import _validate_modify

    sidecar = tmp_path / "2026-05-20-exp-adv-FAILED-seed1.json"
    sidecar.write_text(lr.model_dump_json())

    class _ECR:
        engine = "reversion"
        lab_dossier = str(sidecar.with_suffix(".md"))
        param_change = {}

    class _Plan:
        sot_diff = None

    rejected = _validate_modify(_Plan(), _ECR())
    # _reject returns a plan whose status carries the rejection; the only
    # contract we pin is that it did NOT pass through unchanged.
    assert rejected is not _Plan  # a rejection object, not the input plan
```

> Note for the implementer: `_validate_modify`'s `_reject` return shape is whatever `ops/engine_sdlc/planner.py::_reject` produces (it is NOT the input plan object); the assertion only pins "not accepted unchanged". If `_validate_modify`'s happy path returns the *same* `plan` object, the rejection path returns a distinct `_reject(...)` object — assert `rejected is not the plan instance`. Adjust the final assert to `assert rejected is not plan_instance` if you bind the plan to a variable; the intent is "the FAILED sidecar is hard-rejected, not accepted".

> **Plan correction (T2 impl, 2026-05-20, controller-adjudicated — SP-B/T1 SLF-baseline/plan-defect precedent):** two verbatim-code-block defects in this Task-2 section were corrected during implementation to keep the plan truthful:
> 1. **Final assert was trivially-true (vacuous).** The literal block had `assert rejected is not _Plan` — `_Plan` is the *class*, so a `TransitionPlan` instance is never identical to it and the assertion is always-true (it pins nothing). The implementer Note immediately below it already sanctions the fix: bind `plan_instance = _Plan()` and assert `rejected is not plan_instance`. Applied as written. (Not the T2 RED lever — the test REDs earlier on the absent `LabPrimaryMetric` import — but the assertion is now non-vacuous for when the symbol lands in T5/T7.)
> 2. **`F841` ruff violation in `test_step0_non_vacuity_preconditions`.** The block bound `tgt = _install_choice_stub(monkeypatch)` but never used `tgt` (step-0 uses only the call's monkeypatch side-effects, unlike `_run_once`/the adversarial test which use the return). `**/tests/**` ignores only `E741`/`E702` — `F841` is enforced. Changed to `_install_choice_stub(monkeypatch)  # side-effect: stub install only` (no inline noqa; intent unchanged).
>
> **Plan correction addendum (T2 systematic-debugging pass, 2026-05-20 — three substantive defects, RE-VERIFIED by direct computation against `ops/lab/run.py` before fixing; spec §8-A15):**
> 3. **(SPEC ROOT CAUSE) `MAXDD_REDUCTION` sign inverted.** The spec specified `lambda m: -float(m.max_drawdown)` ("higher=shallower=better"). RE-VERIFIED: `compute_slice_metrics_from_trades` (`run.py:370`) is `((equity-peak)/peak).min()` with `equity ≤ peak` ⇒ `max_drawdown ≤ 0` always; under `rank_candidates`' descending `reverse=True` sort (`run.py:494`, "higher=better" per `_score_for_ranking` docstring `run.py:469`), `-max_drawdown` gives the DEEPER drawdown (`-(-0.18)=+0.18 > -(-0.01)=+0.01`) the larger score ⇒ would rank the WORSE candidate first (a live-money-adjacent ranking defect). **Corrected the merged spec** (§2.2 mapping+prose, §5.2 claim, §5.5 unit form, new §8-A15 hardening line, §8 self-review contradiction-check-3) to `lambda m: float(m.max_drawdown)` (the value itself; `-abs(...)` rejected as it hides the load-bearing ≤0 invariant). Plan Task-4 `_RANKING_METRICS` impl + `test_score_maxdd_reduction_*` unit assertions corrected to match (was `pytest.approx(-0.30 * -1)`/`approx(0.05)` → `approx(-0.30)`/`approx(-0.05)`).
> 4. **(CRITICAL) the make-or-break construction was unsatisfiable under the corrected mapping.** RE-VERIFIED by direct computation against the real metric math: at the old levers (A `sharpe_lever=0.030`+a `−0.18` loss, B `0.012`+a `−0.01` loss, C `n=2`) the single deep loss tanks A's windowed Sharpe (0.143) far BELOW B's near-constant series (3.365) ⇒ `sharpe_rank[0]=B≠A`; and C's all-positive windowed slice has `max_drawdown=0.0` ⇒ wins the corrected MAXDD ranking, so `maxdd_rank[0]=C≠B`. `test_step0_non_vacuity_preconditions` could never go green. **Re-tuned to a provably-satisfiable construction** (`kind="loss"` A: `ret=0.080`+one `−0.045`; `kind="volpos"` B: alternating `0.040`/`0.002`, strictly positive ⇒ zero drawdown + modest Sharpe; C: `ret=0.020`+`−0.015`, final-holdout `dd_trades=2`). Offline-proven against the exact `_trade_log`+`rank_candidates` math: WINDOWED SHARPE A=4.1666 > C=3.6178 > B=2.9704 (A strictly max); corrected MAXDD B=0.0 > C=−0.015 > A=−0.045 (strict B>C>A); FINAL-HOLDOUT A,B n=8 survive, C n=2<3 fails the sacred gate (metric-blind). The Task-2 embedded `_PROFILES`/`_trade_log` updated to match. `test_step0_non_vacuity_preconditions` **strengthened**: it now pins the STRICT Sharpe-max and STRICT B>C>A drawdown order (not just the winners) plus C's windowed-`n≥3` real-member precondition, so a future lever drift that collapses the disagreement ERRORs loudly instead of silently passing on Timsort luck.
> 6. **(PROVENANCE, doc-only) T2 §8-A15/test-comment C-Sharpe provenance number corrected 2.2459→3.6178, order A>B>C→A>C>B; no logic/assertion change.**
> 5. **(HIGH) `test_make_or_break_gate_invariant_over_ecr_tuple` did not assert the §0.2a/A12 invariant.** It only asserted `winning_params !=` and `isinstance(dsr, float)` (which passes for NaN) — it never pinned the core make-or-break: the ECR-re-derived 4-tuple `(verdict, dsr, credibility_score, winning_params)` must be byte-identical between the SHARPE run and the MAXDD run for a FIXED candidate. **Rewritten** to drive each of {A,B,C} as the pinned winner (via a `rank_candidates` monkeypatch that lifts the chosen candidate to `ranked[0]`) through the full `_build_lab_result` gate path under BOTH metrics and assert exact equality of verdict/credibility_score/winning_params, an explicit-and-loud NaN rejection, and exact `dsr` equality (tolerance 0.0 justified: nothing metric-dependent feeds `compute_dsr_for_verdict` — the replay is metric-invariant by construction, so it is provably bit-identical, not merely ≤1 ULP). The adversarial sibling `test_make_or_break_adversarial_through_both_gates` was assessed structurally sound and preserved verbatim (the re-tuning does not change C's windowed `n=8`, so its `m.n_trades < 5` predicate behaves exactly as before — no adjustment required).
> 7. **(CODE-QUALITY) T2 adversarial test hardened to pin the verdict-gate reject predicate, not just any rejection — code-quality review.**

- [ ] **Step 2: Write ONLY the pre-SP-D-sidecar regression test (§5.5 forcing regression) into `test_lab_sp_d_units.py`**

Inline-fixture the EXACT key set of the verified-real `docs/lab/2026-05-18-exp1-SURVIVED-seed7.json` (NO `primary_metric` key) — copied into the test, NOT read from the live tree. RED now (`LabResult.primary_metric` doesn't exist; today this JSON validates fine, so the test as written asserting `lr.primary_metric == SHARPE` fails on the missing attribute).

Create `tpcore/tests/test_lab_sp_d_units.py`:

```python
"""SP-D §5.5 — focused units. This file grows in Task 7; Task 2 lands
ONLY the pre-SP-D-sidecar forcing regression (RED until §2.4's defaulted
LabResult.primary_metric exists)."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

# The EXACT key set of the verified-real pre-SP-D sidecar
# docs/lab/2026-05-18-exp1-SURVIVED-seed7.json (NO `primary_metric` key).
# Inlined deliberately — NOT read from the live docs/ tree.
_PRE_SP_D_SIDECAR = {
    "candidate": "exp1",
    "target_engine": "reversion",
    "intent": "fold_existing",
    "verdict": "SURVIVED",
    "dsr": 0.97,
    "credibility_score": 72,
    "credibility_rubric": {
        "lookahead_clean": True, "survivorship_inclusive": True,
        "pit_fundamentals": True, "regime_coverage": True,
        "out_of_sample_validated": True, "monte_carlo_drawdown": True,
        "sensitivity_surface_flat": False,
        "monte_carlo_sequence_passed": False,
        "dsr_above_pass_threshold": False,
        "backtest_length_above_minbtl": False,
        "pbo_passes": False, "trades_per_param_passes": False,
        "score": 72, "notes": None,
    },
    "held_metrics": {
        "sharpe": 1.1, "profit_factor": 1.6, "max_drawdown": -0.08,
        "n_trades": 12, "win_rate": 0.55,
    },
    "winning_params": {"z_threshold": 3.2},
    "param_diff": [{"name": "z_threshold", "current": 3.0,
                    "winning": 3.2}],
    "recommended_exit": "fold_existing",
    "ranked_alternatives": [],
    "walk_windows": [],
    "n_trials": 200,
    "seed": 7,
    "generated_at": "2026-05-18T00:00:00Z",
}


def test_pre_sp_d_sidecar_validates_and_defaults_to_sharpe():
    """A pre-SP-D sidecar with NO `primary_metric` key still
    model_validates and resolves to SHARPE (the default). REDs if the
    field is ever made required (§2.4 / §8-A11)."""
    from tpcore.lab.models import LabResult
    from tpcore.lab.target import LabPrimaryMetric

    lr = LabResult.model_validate_json(json.dumps(_PRE_SP_D_SIDECAR))
    assert lr.primary_metric == LabPrimaryMetric.SHARPE


def test_pre_sp_d_sidecar_still_accepted_by_evidence_loader(tmp_path):
    """load_labresult_sidecar over the pre-SP-D shape still ACCEPTs (no
    EvidenceError) — the live SP3 MODIFY-ECR gate is not regressed."""
    from ops.engine_sdlc._evidence import load_labresult_sidecar

    md = tmp_path / "2026-05-18-exp1-SURVIVED-seed7.md"
    md.write_text("# stub dossier\n")
    md.with_suffix(".json").write_text(json.dumps(_PRE_SP_D_SIDECAR))
    lr = load_labresult_sidecar(md)
    assert lr.verdict == "SURVIVED"
    assert lr.dsr == 0.97
    assert lr.credibility_score == 72
```

- [ ] **Step 3: Add scoped SLF per-file ignores for both new test files**

Append to `pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` after the Task-1 entry:

```toml
# SP-D §5.2 make-or-break + §5.5 units: proving gate-invariance + the
# pre-SP-D-sidecar regression REQUIRES naming `ops.lab.run`-private
# symbols (`_run_lab_core`, `_build_lab_result`, `_RANKING_METRICS`,
# `_lab_target_for`, `_score_for_ranking`, `rank_candidates`,
# `compute_slice_metrics_from_trades`, `period_returns_from_trades`,
# `compute_dsr_for_verdict`) — that IS the tests' purpose. Engine-lane-
# module-private (NOT tpcore-private) access; the scoped per-file ignore
# is the correct form (SP-B precedent) — never an inline `# noqa: SLF001`.
"tpcore/tests/test_lab_sp_d_make_or_break.py" = ["SLF"]
"tpcore/tests/test_lab_sp_d_units.py" = ["SLF"]
```

- [ ] **Step 4: Run both new tests — expect RED (SP-D symbols absent)**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_make_or_break.py tpcore/tests/test_lab_sp_d_units.py -p no:xdist -q`
Expected: **FAIL** — `ImportError: cannot import name 'LabPrimaryMetric' from 'tpcore.lab.target'` (make-or-break) and `AttributeError: 'LabResult' object has no attribute 'primary_metric'` / import error (units). Confirms the skeletons are RED against the unbuilt feature.

- [ ] **Step 5: ruff clean**

Run: `ruff check tpcore/tests/test_lab_sp_d_make_or_break.py tpcore/tests/test_lab_sp_d_units.py pyproject.toml`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add tpcore/tests/test_lab_sp_d_make_or_break.py tpcore/tests/test_lab_sp_d_units.py pyproject.toml
git commit -m "test(lab-sp-d): RED make-or-break + pre-SP-D-sidecar regression skeletons (T0)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: tpcore contract — `LabPrimaryMetric` enum + defaulted `LabTarget.primary_metric` (T1)

Adds the engine-free vocabulary + the defaulted optional field. `model_post_init` gains NO new logic (the enum type already constrains; implementability is validated Lab-side at resolve, §2.1). reversion/vector/momentum `LAB_TARGET` declarations are **NOT edited** (A11 rationale — a defaulted field needs no edit there).

> **Plan correction (T3 contract hardened, 2026-05-20, code-quality review):** T3 contract hardened — exhaustive-vocabulary pin (`test_vocabulary_is_exactly_pinned`) + gate-separation doc clause on `primary_metric`; the Step-1 test block and Step-3 field comment above reflect the shipped code.

**Files:**
- Modify: `tpcore/lab/target.py:17-85`
- Test: `tpcore/tests/test_lab_target.py` (existing — must stay green; frozen/extra-forbid unchanged) + a new focused assertion file `tpcore/tests/test_lab_primary_metric.py`

- [ ] **Step 1: Write the failing tpcore-contract test**

Create `tpcore/tests/test_lab_primary_metric.py`:

```python
"""SP-D §2.1 — the engine-free LabPrimaryMetric vocabulary + the
defaulted LabTarget.primary_metric field. tpcore stays engine-free
(only `enum` added vs today)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _callables():
    async def _afn(*a, **k):
        return None

    def _sfn(*a, **k):
        return None

    def _dp() -> dict:
        return {}

    return _afn, _sfn, _dp


def test_enum_members_and_str_values():
    from tpcore.lab.target import LabPrimaryMetric

    assert LabPrimaryMetric.SHARPE == "sharpe"
    assert LabPrimaryMetric.MAXDD_REDUCTION == "maxdd_reduction"
    assert LabPrimaryMetric.ULCER == "ulcer"
    assert LabPrimaryMetric.INVERSE_ETF_HOLD == "inverse_etf_hold"
    # StrEnum -> serializes as a plain string for the dossier JSON.
    assert isinstance(LabPrimaryMetric.SHARPE.value, str)


def test_vocabulary_is_exactly_pinned():
    """Persisted-value contract: ``LabPrimaryMetric`` member NAMES and
    string VALUES are written into ``LabResult`` JSON sidecars and
    compared in the make-or-break. This asserts the enum is EXACTLY
    these four (name, value) pairs — no more, no fewer, none renamed.
    Any add/rename/remove is a deliberate persisted-state migration and
    MUST be a conscious edit to this set, never an incidental enum
    change; this test reds the build until that edit is made.
    """
    from tpcore.lab.target import LabPrimaryMetric

    assert {(m.name, m.value) for m in LabPrimaryMetric} == {
        ("SHARPE", "sharpe"),
        ("MAXDD_REDUCTION", "maxdd_reduction"),
        ("ULCER", "ulcer"),
        ("INVERSE_ETF_HOLD", "inverse_etf_hold"),
    }


def test_labtarget_primary_metric_defaults_to_sharpe():
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp)
    assert t.primary_metric == LabPrimaryMetric.SHARPE


def test_labtarget_accepts_explicit_metric():
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp,
                  primary_metric=LabPrimaryMetric.MAXDD_REDUCTION)
    assert t.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION


def test_labtarget_rejects_unknown_metric_string():
    """extra='forbid' + closed StrEnum -> a misspelled metric is a
    pydantic ValidationError at declaration (§8-A8, fail-loud, never a
    silent Sharpe fallback)."""
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    with pytest.raises(ValidationError):
        LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp,
                  primary_metric="shrpe")


def test_target_module_still_engine_free():
    """tpcore/lab/target.py imports only pydantic + stdlib (now incl.
    `enum`) — no engine, no ops edge."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("tpcore/lab/target.py").read_text())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    for bad in ("reversion", "vector", "momentum", "sentinel",
                "canary", "ops"):
        assert bad not in mods
    assert mods <= {"__future__", "collections", "typing", "pydantic",
                    "enum"}
```

- [ ] **Step 2: Run the test — expect RED**

Run: `python -m pytest tpcore/tests/test_lab_primary_metric.py -p no:xdist -q`
Expected: **FAIL** — `ImportError: cannot import name 'LabPrimaryMetric' from 'tpcore.lab.target'`.

- [ ] **Step 3: Add the enum + field (minimal)**

Edit `tpcore/lab/target.py`. Change the import block (`:17-22`) and add the enum + field. Replace lines `17-22`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class LabPrimaryMetric(StrEnum):
    """SP-D — the engine-FREE Lab ranking-objective vocabulary.

    The engine names its single pre-registered ranking objective here;
    the metric->scalar IMPLEMENTATION is Lab-resident
    (`ops/lab/run.py::_RANKING_METRICS`) because it reads `SliceMetrics`,
    a Lab dataclass (spec §1.1). SHARPE is the default ⇒ an engine that
    does not declare it gets today's behaviour byte-identically.
    ULCER/INVERSE_ETF_HOLD are RESERVED vocabulary: declared so the enum
    is forward-complete, but unimplemented (fail-loud at resolve, §4.3) —
    SP-E owns Sentinel's exact bar.
    """

    SHARPE = "sharpe"
    MAXDD_REDUCTION = "maxdd_reduction"
    ULCER = "ulcer"
    INVERSE_ETF_HOLD = "inverse_etf_hold"
```

Then add the field to `LabTarget` immediately after the `default_params` field (`:44`), so the field block reads:

```python
    param_ranges: dict[str, tuple]
    run_for_search: Callable[..., Awaitable[Any]]
    load_window_context: Callable[..., Awaitable[Any]]
    run_with_context: Callable[..., Any]
    default_params: Callable[[], dict[str, Any]]
    # SP-D: the engine's single declared ranking objective. Optional +
    # defaulted ⇒ reversion/vector/momentum (which omit it) are
    # byte-identical (Sharpe). model_post_init needs NO new logic — the
    # StrEnum type already constrains; implementability is validated
    # Lab-side at resolve (spec §2.1, §4.3). This selects the candidate
    # RANKING objective ONLY and NEVER affects the DSR/credibility
    # graduation gate — the SP-D sacred-gate separation is absolute.
    primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE
```

Update `__all__` (`:84`):

```python
__all__ = ["LabPrimaryMetric", "LabTarget"]
```

- [ ] **Step 4: Run the test — expect GREEN**

Run: `python -m pytest tpcore/tests/test_lab_primary_metric.py tpcore/tests/test_lab_target.py -p no:xdist -q`
Expected: **PASS** (new file all green; the pre-existing `test_lab_target.py` frozen/extra-forbid suite still green — a defaulted optional field does not break `extra="forbid"` nor the malformed-param-ranges cases).

- [ ] **Step 5: Prove tpcore stayed engine-free**

Run: `python -m tpcore.scripts.check_imports tpcore`
Expected: exit 0, no tpcore→engine/ops edge reported (only `enum` was added).

- [ ] **Step 6: ruff**

Run: `ruff check tpcore/lab/target.py tpcore/tests/test_lab_primary_metric.py`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add tpcore/lab/target.py tpcore/tests/test_lab_primary_metric.py
git commit -m "feat(lab-sp-d): LabPrimaryMetric vocabulary + defaulted LabTarget.primary_metric (T1)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Lab ranking generalization — `_RANKING_METRICS` + parameterized scorers (T2)

Adds the frozen dispatch table (`SHARPE` char-identical, `MAXDD_REDUCTION`, reserved fail-loud sentinels) + the §4.5 non-finite clamp, and parameterizes `_score_for_ranking`/`rank_candidates` with a **defaulted** `metric=SHARPE`. The char anchor (Task 1) and the byte-frozen oracle must stay GREEN — this is the byte-identity proof.

**Files:**
- Modify: `ops/lab/run.py:461-495` (the Ranking section)
- Test: `tpcore/tests/test_lab_sp_d_char_anchor.py` (Task 1 — must stay green, no edit) + new units appended to `tpcore/tests/test_lab_sp_d_units.py`

- [ ] **Step 1: Append the MAXDD_REDUCTION + NaN-clamp unit tests to `test_lab_sp_d_units.py`**

Append to `tpcore/tests/test_lab_sp_d_units.py`:

```python
def test_score_sharpe_metric_equals_pre_refactor_closed_form():
    import math

    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    m = sp.SliceMetrics(n_trades=10, sharpe=1.4, profit_factor=1.5,
                         max_drawdown=-0.1, win_rate=0.5)
    expected = 1.4 + 0.05 * math.log10(max(10, 1))
    assert sp._score_for_ranking(m, LabPrimaryMetric.SHARPE) == expected
    assert sp._score_for_ranking(m) == expected  # defaulted == SHARPE


def test_score_maxdd_reduction_is_the_drawdown_value_itself():
    # §8-A15: MAXDD_REDUCTION == m.max_drawdown itself (≤0 by the
    # run.py:370 .min() construction). NOT -max_drawdown (sign-inverted).
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    deep = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                            max_drawdown=-0.30, win_rate=0.5)
    shallow = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                              max_drawdown=-0.05, win_rate=0.5)
    assert sp._score_for_ranking(
        deep, LabPrimaryMetric.MAXDD_REDUCTION) == pytest.approx(-0.30)
    assert sp._score_for_ranking(
        shallow, LabPrimaryMetric.MAXDD_REDUCTION) == pytest.approx(-0.05)
    # Shallower (less-negative) drawdown ranks HIGHER under the
    # descending reverse=True sort: -0.05 > -0.30.
    assert sp._score_for_ranking(
        shallow, LabPrimaryMetric.MAXDD_REDUCTION) > sp._score_for_ranking(
        deep, LabPrimaryMetric.MAXDD_REDUCTION)


def test_score_n_trades_floor_is_metric_independent():
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    thin = sp.SliceMetrics(n_trades=2, sharpe=9.9, profit_factor=9.0,
                            max_drawdown=-0.01, win_rate=1.0)
    for mt in (LabPrimaryMetric.SHARPE, LabPrimaryMetric.MAXDD_REDUCTION):
        assert sp._score_for_ranking(thin, mt) == -1.0


def test_non_finite_metric_value_clamps_to_floor_not_nan():
    import math

    import numpy as np

    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    m = sp.SliceMetrics(n_trades=10, sharpe=float("nan"),
                        profit_factor=1.0, max_drawdown=-0.1,
                        win_rate=0.5)
    v = sp._score_for_ranking(m, LabPrimaryMetric.SHARPE)
    assert v == -1.0
    assert math.isfinite(v)
    # never poisons np.mean / the sort
    assert math.isfinite(float(np.mean([v, 1.0, 2.0])))


def test_reserved_metric_score_raises_clear_value_error():
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    m = sp.SliceMetrics(n_trades=10, sharpe=1.0, profit_factor=1.0,
                        max_drawdown=-0.1, win_rate=0.5)
    with pytest.raises(ValueError, match="reserved objective"):
        sp._score_for_ranking(m, LabPrimaryMetric.ULCER)
    with pytest.raises(ValueError, match="reserved objective"):
        sp._score_for_ranking(m, LabPrimaryMetric.INVERSE_ETF_HOLD)


def test_ranking_metrics_table_is_exhaustive_over_the_enum():
    # Hardening: a future LabPrimaryMetric member added without a
    # _RANKING_METRICS entry must red LOUDLY and PRECISELY here, not as a
    # cryptic bare KeyError deep inside _score_for_ranking on a
    # live-money-adjacent ranking path. Also rejects a stray table key
    # with no enum member (set equality both directions).
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    assert set(sp._RANKING_METRICS) == set(LabPrimaryMetric)


def test_score_maxdd_reduction_zero_drawdown_is_finite_max_and_ranks_first():
    # §8-A15 boundary: a flawless equity curve (max_drawdown == 0.0)
    # scores exactly 0.0 — the MAXIMUM possible MAXDD_REDUCTION value
    # (every real drawdown is <0 by the run.py:370 .min() construction)
    # — and is finite (the _clamp identity holds at the boundary, no
    # nan/inf). A 0.0-DD candidate must therefore rank ABOVE any
    # negative-DD one under the descending reverse=True sort.
    import math

    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    flawless = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                               max_drawdown=0.0, win_rate=0.5)
    drawn = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                            max_drawdown=-0.05, win_rate=0.5)
    flawless_score = sp._score_for_ranking(
        flawless, LabPrimaryMetric.MAXDD_REDUCTION)
    assert flawless_score == 0.0
    assert math.isfinite(flawless_score)
    assert flawless_score > sp._score_for_ranking(
        drawn, LabPrimaryMetric.MAXDD_REDUCTION)
```

> **Plan correction (T4 hardened, 2026-05-20, code-quality review):** T4 hardened — table-exhaustiveness guard (`test_ranking_metrics_table_is_exhaustive_over_the_enum`, test-form chosen: no build-time `set(...)==set(Enum)` dispatch-table precedent in the repo, lower blast radius, `ops/lab/run.py` byte-untouched) + MAXDD-0.0 boundary unit (`test_score_maxdd_reduction_zero_drawdown_is_finite_max_and_ranks_first`); the Step-1 test block above reflects the shipped code.

- [ ] **Step 2: Run the new units — expect RED**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_units.py -p no:xdist -q -k "score_ or non_finite or reserved_metric"`
Expected: **FAIL** — `TypeError: _score_for_ranking() takes 1 positional argument but 2 were given` (the `metric` param doesn't exist yet).

- [ ] **Step 3: Implement the dispatch table + parameterized scorers (minimal, SHARPE char-identical)**

Replace `ops/lab/run.py:466-495` (the `_score_for_ranking` + `rank_candidates` defs) with:

```python
def _unimplemented_metric(name: str) -> Callable[[SliceMetrics], float]:
    """SP-D §4.3 — a reserved-but-unimplemented objective. Returns a
    callable that fail-louds at resolve/score time (NEVER a silent
    fallback). The pre-spend `_resolve_ranking_metric` fence (run.py,
    §4.3) detects this BEFORE the SP-A ledger spend so a reserved
    declaration never burns a cumulative-trial increment."""

    def _raise(_m: SliceMetrics) -> float:
        raise ValueError(
            f"LabPrimaryMetric.{name} is a reserved objective with no "
            f"Lab implementation yet — declare it only when its scoring "
            f"function ships (SP-E owns the Sentinel bar). See spec "
            f"2026-05-20-lab-sp-d §4.3."
        )

    return _raise


def _clamp(v: float) -> float:
    """SP-D §4.5 — a non-finite metric value sorts a candidate LAST
    (same semantics as the n_trades<3 floor) instead of poisoning
    np.mean / the sort with nan. Gate-invariant (the ranking value never
    reaches `survived`, §1.2) and oracle-neutral (pinned inputs are
    finite ⇒ the SHARPE clamp is never exercised on them)."""
    return v if math.isfinite(v) else -1.0


_RANKING_METRICS: Mapping[LabPrimaryMetric, Callable[[SliceMetrics], float]] = {
    # The EXACT current expression, character-for-character (Task-1 char
    # anchor + the byte-frozen oracle pin this).
    LabPrimaryMetric.SHARPE: lambda m: _clamp(
        float(m.sharpe) + 0.05 * math.log10(max(m.n_trades, 1))
    ),
    # §8-A15: max_drawdown is <=0 by construction (run.py:370,
    # ((equity-peak)/peak).min(), equity<=peak). Return the VALUE ITSELF
    # — a shallower (less-negative) drawdown is the LARGER score, so
    # under rank_candidates' descending reverse=True sort the shallowest-
    # drawdown candidate ranks first (the correct "minimize drawdown"
    # objective). `-float(m.max_drawdown)` was sign-INVERTED (it ranked
    # the DEEPER drawdown first). The §0.5 SP-E need, from the existing
    # universe.
    LabPrimaryMetric.MAXDD_REDUCTION: lambda m: _clamp(
        float(m.max_drawdown)
    ),
    # Reserved vocabulary, no speculative impl (§1.3 YAGNI / §4.3).
    LabPrimaryMetric.ULCER: _unimplemented_metric("ULCER"),
    LabPrimaryMetric.INVERSE_ETF_HOLD: _unimplemented_metric(
        "INVERSE_ETF_HOLD"
    ),
}


def _score_for_ranking(
    metrics: SliceMetrics,
    metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE,
) -> float:
    """OOS score used to rank candidates. Higher is better.

    The ``n_trades < 3 -> -1.0`` guard is OUTSIDE the metric dispatch,
    UNCHANGED — a statistical-power floor on *rankability*, metric-
    independent (every metric inherits it identically; it is also below
    any sane metric score so a thin candidate always sorts last). The
    per-metric mapping lives in the frozen ``_RANKING_METRICS`` table;
    ``SHARPE`` is the current expression character-for-character so the
    defaulted call is byte-identical (spec §2.2)."""
    if metrics.n_trades < 3:
        return -1.0  # trade count too low to be statistically meaningful
    return _RANKING_METRICS[metric](metrics)


def rank_candidates(
    trials: list[TrialResult],
    metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE,
) -> list[tuple[dict, float, int]]:
    """Aggregate trials by parameters (deterministic key), return ranked
    list of (parameters, mean_score, n_windows_evaluated). ``metric``
    defaults to ``SHARPE`` so the byte-frozen characterization oracle's
    no-arg call is byte-identical (spec §2.3, §8-A6)."""
    by_param: dict[str, list[TrialResult]] = {}
    for t in trials:
        if t.error:
            continue
        key = json.dumps(t.parameters, sort_keys=True)
        by_param.setdefault(key, []).append(t)
    ranked: list[tuple[dict, float, int]] = []
    for key, group in by_param.items():
        scores = [_score_for_ranking(t.holdout, metric) for t in group]
        if not scores:
            continue
        ranked.append((json.loads(key), float(np.mean(scores)), len(group)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked
```

Add the imports needed (`LabPrimaryMetric`, `Mapping` is already imported per `run.py:63`). Edit the `tpcore.lab.target` import line (`run.py:83`) from `from tpcore.lab.target import LabTarget` to:

```python
from tpcore.lab.target import LabPrimaryMetric, LabTarget
```

- [ ] **Step 4: Run the new units + the char anchor + the byte-frozen oracle — expect ALL GREEN**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_units.py tpcore/tests/test_lab_sp_d_char_anchor.py scripts/tests/test_search_parameters_characterization.py -p no:xdist -q`
Expected: **PASS** — the char anchor is byte-identical (defaulted `SHARPE` == current closed form), the oracle's no-arg `rank_candidates([...])` call is byte-identical (zero oracle churn), and the new MAXDD/clamp/reserved units pass.

- [ ] **Step 5: Confirm the oracle file is byte-unmodified**

Run: `git diff --stat scripts/tests/test_search_parameters_characterization.py`
Expected: **no output** (zero bytes changed — the defaulted-arg shape achieves zero oracle churn, §8-A6).

- [ ] **Step 6: ruff + check_imports**

Run: `ruff check ops/lab/run.py tpcore/tests/test_lab_sp_d_units.py && python -m tpcore.scripts.check_imports tpcore`
Expected: no output / exit 0.

- [ ] **Step 7: Commit**

```bash
git add ops/lab/run.py tpcore/tests/test_lab_sp_d_units.py
git commit -m "feat(lab-sp-d): _RANKING_METRICS table + parameterized scorers (SHARPE char-identical) (T2)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Wire the resolved metric + the pinned pre-spend fence (T3)

Adds the pure side-effect-free `_resolve_ranking_metric` and calls it at the **pinned insertion point between `run.py:794` and `run.py:808`** (strictly before the SP-A `record_trial_spend` block `:811-822`, §4.3), holds the resolved metric in a local, and threads it into `rank_candidates(trials, metric=…)` at the `run.py:891` call site. Proves: the make-or-break (all steps incl. the `_validate_modify` adversarial probe) GREEN, and a reserved metric fail-louds **before** any ledger spend (ledger-spy, mirrors `test_undeclared_target_hard_rejects_before_any_ledger_spend` verbatim).

**Files:**
- Modify: `ops/lab/run.py:793-822` (insert the fence call between `:794` and `:808`) and `ops/lab/run.py:891` (the `rank_candidates` call site)
- Modify: `ops/lab/run.py` Ranking section (add `_resolve_ranking_metric`)
- Test: `tpcore/tests/test_lab_sp_d_make_or_break.py` (Task 2 — now GREEN) + pre-spend-reject test appended to `tpcore/tests/test_lab_sp_d_units.py`

- [ ] **Step 1: Append the §4.3 pre-spend-reject ledger-spy test to `test_lab_sp_d_units.py`**

Mirrors `test_lab_targeting_consistency.py::test_undeclared_target_hard_rejects_before_any_ledger_spend` verbatim (same `_SharedPool`/`_FakeConn` ledger-spy), but for a stub engine declaring `ULCER` — the reject must fire BEFORE `record_trial_spend` (no `lab_trial_ledger.*` row written).

Append to `tpcore/tests/test_lab_sp_d_units.py`:

```python
async def test_reserved_metric_rejects_before_any_ledger_spend(
        monkeypatch, tmp_path):
    """SP-D §4.3 / §8-A4 — a stub engine declaring ULCER raises the clear
    ValueError BEFORE record_trial_spend (the SP-B 'spend then crash'
    footgun class). Asserts NO lab_trial_ledger row is ever written —
    mirrors test_lab_targeting_consistency.py::
    test_undeclared_target_hard_rejects_before_any_ledger_spend verbatim
    (same _SharedPool/_FakeConn ledger-spy)."""
    import argparse
    from datetime import date

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    # Reuse the make-or-break ledger-spy doubles.
    from tpcore.tests.test_lab_sp_d_make_or_break import _SharedPool

    async def _runner(*a, **k):
        raise AssertionError("must not reach runner — reject is pre-spend")

    async def _loader(*a, **k):
        return object()

    def _ctx_runner(c, *, overrides=None):
        raise AssertionError("must not reach ctx_runner")

    tgt = LabTarget(
        param_ranges={"choice": (0, 1, "choice:A,B")},
        run_for_search=_runner, load_window_context=_loader,
        run_with_context=_ctx_runner, default_params=lambda: {"choice": "A"},
        primary_metric=LabPrimaryMetric.ULCER,
    )
    monkeypatch.setattr("ops.lab.run._lab_target_for", lambda e: tgt)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _loader)
    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)

    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    ns = argparse.Namespace(
        engine="reversion", trials=10, per_window_trials=2,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=0, output=tmp_path / "x.csv",
        database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None)

    async with LabContext(db_url="postgres://fake/db"):
        with pytest.raises(ValueError, match="reserved objective"):
            await lab_run._run_lab_core(ns, candidate="bad_reserved")

    # The reserved-metric reject is PRE-SPEND: no ledger row exists.
    assert not any(
        str(r["source"]).startswith("lab_trial_ledger")
        for r in shared.rows), (
        "a reserved-metric reject must NOT spend SP-A ledger budget "
        "(spec §4.3 / §8-A4)")
```

- [ ] **Step 2: Run the make-or-break + pre-spend-reject — expect RED**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_make_or_break.py "tpcore/tests/test_lab_sp_d_units.py::test_reserved_metric_rejects_before_any_ledger_spend" -p no:xdist -q`
Expected: **FAIL** — the make-or-break runs `_run_lab_core` but `rank_candidates` is still called with no metric (so `winner_params` is Sharpe-ranked in both runs ⇒ step 4 `winning_params` differ assertion fails), and `_resolve_ranking_metric` does not exist (pre-spend-reject `AttributeError`/no reject).

- [ ] **Step 3: Add `_resolve_ranking_metric` to the Ranking section**

Append to `ops/lab/run.py` immediately after `rank_candidates` (the function added in Task 4):

```python
def _resolve_ranking_metric(
    engine: str,
) -> tuple[LabPrimaryMetric, Callable[[SliceMetrics], float]]:
    """SP-D §4.3 — the PURE, side-effect-free pre-spend fence.

    Resolves the engine's declared ``primary_metric`` via the already-
    idempotent ``_lab_target_for`` and proves its ``_RANKING_METRICS``
    entry is a real implementation (NOT the ``_unimplemented_metric``
    sentinel) WITHOUT executing it. A reserved-but-unimplemented
    declaration raises the clear ``ValueError`` HERE — invoked strictly
    before the SP-A ``record_trial_spend`` block — so a reserved
    declaration never burns a cumulative-trial increment (the SP-B
    'spend then crash' footgun class, §8-A4). Returns the metric + its
    resolved callable so the caller threads it into ``rank_candidates``
    without re-resolving."""
    metric = _lab_target_for(engine).primary_metric
    fn = _RANKING_METRICS[metric]
    # Probe-only: a 1-trade SliceMetrics is below the n_trades<3 floor so
    # _score_for_ranking would never call fn — but the SENTINEL must
    # still fail-loud here, so call fn directly on a probe metrics value.
    # A real mapping (SHARPE/MAXDD_REDUCTION) is a pure arithmetic lambda;
    # the sentinel raises. This proves implementability without a ledger
    # spend and without depending on _score_for_ranking's floor.
    _probe = SliceMetrics(n_trades=3, sharpe=0.0, profit_factor=1.0,
                          max_drawdown=0.0, win_rate=0.0)
    fn(_probe)  # raises ValueError iff `metric` is reserved-unimplemented
    return metric, fn
```

> Implementer note: the probe call is deliberate — `_score_for_ranking`'s `n_trades<3 -> -1.0` floor would short-circuit a sentinel that only fires inside the mapping, so the fence calls the mapping directly on a finite `n_trades=3` probe. A real mapping returns a harmless float (discarded); the sentinel raises. This keeps the fence a pure function with no DB / no ledger touch.

- [ ] **Step 4: Wire the fence at the pinned insertion point + thread the metric into `rank_candidates`**

In `ops/lab/run.py`, the current `:793-794` reads:

```python
    candidates = sample_parameters(args.engine, args.trials, seed=args.seed)
    print(f"  → sampled {len(candidates)} parameter combinations  (seed={args.seed})")
```

and `:808` opens the SP-A spend block (`from tpcore.lab.context import active_credibility_pool`). Insert the fence call **between `:794` and `:808`** — directly after the `print(...)` and before the SP-A comment block. Add:

```python
    candidates = sample_parameters(args.engine, args.trials, seed=args.seed)
    print(f"  → sampled {len(candidates)} parameter combinations  (seed={args.seed})")

    # SP-D §4.3 — the PINNED pre-spend fence. Resolve (and prove
    # implementable) the declared ranking metric HERE: strictly AFTER
    # sample_parameters (so a malformed-param_ranges _lab_target_for
    # reject still precedes it, unchanged) and strictly BEFORE the SP-A
    # record_trial_spend block below (:811-822) — a reserved-but-
    # unimplemented metric fail-louds before any cumulative-ledger
    # increment is burned (the SP-B 'spend then crash' footgun class,
    # §8-A4). Resolved ONCE and threaded into rank_candidates below; NOT
    # re-resolved at the :891 call site.
    _ranking_metric, _ = _resolve_ranking_metric(args.engine)
```

Then change the `rank_candidates` call (currently `ranked = rank_candidates(trials)` at `run.py:891`) to:

```python
    ranked = rank_candidates(trials, _ranking_metric)
```

- [ ] **Step 5: Run the make-or-break + pre-spend-reject — expect GREEN**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_make_or_break.py "tpcore/tests/test_lab_sp_d_units.py::test_reserved_metric_rejects_before_any_ledger_spend" -p no:xdist -q`
Expected: **PASS** — step 0 non-vacuity holds (SHARPE→A, MAXDD→B, orders invert, C fails via n_trades<3); the two-pipeline run yields different `winning_params` (A vs B) while the gate predicate is metric-invariant; the adversarial metric pushes C to `ranked[0]` but `survived=False`/`verdict=="FAILED"` and `_validate_modify` hard-rejects; the reserved-metric reject fires with NO ledger row.

- [ ] **Step 6: Run the char anchor + the byte-frozen oracle again (regression)**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_char_anchor.py scripts/tests/test_search_parameters_characterization.py -p no:xdist -q && git diff --stat scripts/tests/test_search_parameters_characterization.py`
Expected: **PASS** and **no diff output** (the wiring is the default `SHARPE` for reversion/vector/momentum ⇒ byte-identical; oracle byte-unmodified).

- [ ] **Step 7: ruff + check_imports**

Run: `ruff check ops/lab/run.py tpcore/tests/test_lab_sp_d_units.py && python -m tpcore.scripts.check_imports tpcore`
Expected: no output / exit 0.

- [ ] **Step 8: Commit**

```bash
git add ops/lab/run.py tpcore/tests/test_lab_sp_d_units.py
git commit -m "feat(lab-sp-d): pinned pre-spend _resolve_ranking_metric fence + wire metric into rank_candidates (T3)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Provenance — defaulted `LabResult.primary_metric` + `_build_lab_result` line (T4 part 1)

Adds `LabResult.primary_metric` as `= LabPrimaryMetric.SHARPE` **defaulted** (mandatory for pre-SP-D sidecar read-compat — §2.4 / §8-A11) and sets it explicitly from the resolved metric in `_build_lab_result`. The Task-2 pre-SP-D-sidecar regression goes GREEN here.

**Files:**
- Modify: `tpcore/lab/models.py:40-58`
- Modify: `ops/lab/run.py:1132-1149` (the `LabResult(...)` construction in `_build_lab_result`)
- Test: `tpcore/tests/test_lab_sp_d_units.py` (Task 2's two pre-SP-D-sidecar tests — now GREEN)

- [ ] **Step 1: Run the Task-2 pre-SP-D-sidecar tests — confirm still RED**

Run: `python -m pytest "tpcore/tests/test_lab_sp_d_units.py::test_pre_sp_d_sidecar_validates_and_defaults_to_sharpe" "tpcore/tests/test_lab_sp_d_units.py::test_pre_sp_d_sidecar_still_accepted_by_evidence_loader" -p no:xdist -q`
Expected: **FAIL** — `AttributeError: 'LabResult' object has no attribute 'primary_metric'`.

- [ ] **Step 2: Add the defaulted field to `LabResult`**

Edit `tpcore/lab/models.py`. Add the import and the field. Change the import block (`:6-8`) to also import the enum:

```python
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tpcore.backtest.credibility import CredibilityScore
from tpcore.lab.target import LabPrimaryMetric
```

Add the field to `LabResult` immediately after `generated_at` (`:57`):

```python
    n_trials: int
    seed: int
    generated_at: AwareDatetime
    # SP-D §2.4 / §8-A11 — DEFAULTED (NOT required). LabResult is
    # extra="forbid" and ops/engine_sdlc/_evidence.py model_validates
    # pre-existing on-disk sidecars that have NO `primary_metric` key
    # (verified: docs/lab/2026-05-18-exp1-SURVIVED-seed7.json). pydantic
    # v2 fills the default for an ABSENT key under extra="forbid" (forbid
    # rejects UNKNOWN keys, not absent defaulted ones), so legacy
    # sidecars validate -> SHARPE (semantically exact: every pre-SP-D run
    # WAS Sharpe-ranked). Display/provenance ONLY — the planner/ECR
    # NEVER reads this for a gate decision (it re-derives
    # verdict/dsr/credibility_score/winning_params, §0.2a).
    primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE
```

> Implementer note: `tpcore/lab/models.py` already imports from `tpcore.backtest.credibility`; importing `tpcore.lab.target` keeps the dependency engine-free (target.py is stdlib+pydantic only). Confirm no import cycle: `target.py` does not import `models.py` (verified — target.py imports only `enum`/`collections`/`typing`/`pydantic`).

- [ ] **Step 3: Set it explicitly in `_build_lab_result`**

In `ops/lab/run.py::_build_lab_result`, the `return LabResult(...)` (`:1132-1149`) currently ends with `generated_at=datetime.now(UTC),`. Add the field. Change the construction to include:

```python
    return LabResult(
        candidate=candidate.name,
        target_engine=candidate.target_engine,
        intent=candidate.intent,
        verdict=verdict,
        dsr=core.dsr,
        credibility_score=core.full_credibility_score,
        credibility_rubric=core.credibility_rubric,
        held_metrics=core.held_metrics.to_dict(),
        winning_params=core.winner_params,
        param_diff=param_diff,
        recommended_exit=recommended_exit,
        ranked_alternatives=[p for p, _s, _n in core.ranked[:5]],
        walk_windows=walk_windows,
        n_trials=core.effective_n_trials,
        seed=args.seed,
        generated_at=datetime.now(UTC),
        # SP-D §2.4 — the TRUE objective on every new run; the default
        # only services the read of legacy artifacts. Resolved via the
        # idempotent _lab_target_for (the SP-B resolver, no new dispatch).
        primary_metric=_lab_target_for(args.engine).primary_metric,
    )
```

- [ ] **Step 4: Run the Task-2 pre-SP-D-sidecar tests — expect GREEN**

Run: `python -m pytest "tpcore/tests/test_lab_sp_d_units.py::test_pre_sp_d_sidecar_validates_and_defaults_to_sharpe" "tpcore/tests/test_lab_sp_d_units.py::test_pre_sp_d_sidecar_still_accepted_by_evidence_loader" -p no:xdist -q`
Expected: **PASS** — the legacy sidecar (no `primary_metric` key) validates → `SHARPE`, and `load_labresult_sidecar` still accepts it (no `EvidenceError`; the live SP3 MODIFY-ECR gate is not regressed).

- [ ] **Step 5: Run the make-or-break again (regression — it builds LabResult)**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_make_or_break.py -p no:xdist -q`
Expected: **PASS** (still green — `_build_lab_result` now also carries `primary_metric` but the ECR 4-tuple `(verdict,dsr,credibility_score,winning_params)` is unaffected).

- [ ] **Step 6: ruff + check_imports**

Run: `ruff check tpcore/lab/models.py ops/lab/run.py && python -m tpcore.scripts.check_imports tpcore`
Expected: no output / exit 0 (models.py importing tpcore.lab.target keeps tpcore engine-free).

- [ ] **Step 7: Commit**

```bash
git add tpcore/lab/models.py ops/lab/run.py
git commit -m "feat(lab-sp-d): defaulted LabResult.primary_metric + _build_lab_result provenance (T4, A11)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Objective-keyed dossier block (T4 part 2)

Adds the objective line to "## 1. Verdict" (with the mandatory ranking-only parenthetical) + a new "## 2a. Objective-appropriate summary" block keyed off the metric. For `SHARPE` the dossier is byte-identical to pre-SP-D (the three live engines unchanged); for `MAXDD_REDUCTION` the objective line + "## 2a" appear with `max_drawdown` as the headline. The SP-C `_next_step` cross-link block and the SP3 `.json` sidecar derivation are untouched.

**Files:**
- Modify: `ops/lab/dossier.py:27-55`
- Test: dossier units appended to `tpcore/tests/test_lab_sp_d_units.py`

- [ ] **Step 1: Append the dossier tests to `test_lab_sp_d_units.py`**

```python
def _labresult(metric_name: str):
    from datetime import UTC, datetime

    from tpcore.backtest.credibility import CredibilityScore
    from tpcore.lab.models import LabResult
    from tpcore.lab.target import LabPrimaryMetric

    rubric = CredibilityScore(
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True,
        score=80)
    return LabResult(
        candidate="exp1", target_engine="reversion",
        intent="fold_existing", verdict="SURVIVED", dsr=0.97,
        credibility_score=72, credibility_rubric=rubric,
        held_metrics={"sharpe": 1.1, "profit_factor": 1.6,
                      "max_drawdown": -0.08, "n_trades": 12,
                      "win_rate": 0.55},
        winning_params={"z_threshold": 3.2},
        param_diff=[], recommended_exit="fold_existing",
        ranked_alternatives=[], walk_windows=[], n_trials=200, seed=7,
        generated_at=datetime(2026, 5, 18, tzinfo=UTC),
        primary_metric=LabPrimaryMetric(metric_name))


def test_dossier_sharpe_is_byte_identical_to_pre_sp_d():
    """For a SHARPE LabResult the three live engines' dossier text is
    UNCHANGED — no objective line, no '## 2a' block (spec §5.5)."""
    from ops.lab.dossier import render_lab_dossier

    out = render_lab_dossier(_labresult("sharpe"))
    assert "## 2a" not in out
    assert "Primary objective" not in out
    # SP-C _next_step cross-link is untouched.
    assert "## 4. Next step (SP3 — NOT applied by the Lab)" in out
    assert "lab_candidate_readiness.md" in out


def test_dossier_maxdd_reduction_adds_objective_block():
    """For MAXDD_REDUCTION the objective line (with the mandatory
    ranking-only parenthetical) + the '## 2a' block appear; the SP-C
    _next_step block is still present (cross-link not disturbed)."""
    from ops.lab.dossier import render_lab_dossier

    out = render_lab_dossier(_labresult("maxdd_reduction"))
    assert ("**Primary objective:** maxdd_reduction "
            "(ranking metric — does NOT affect the gate)") in out
    assert "## 2a. Objective-appropriate summary" in out
    assert "max_drawdown" in out
    assert "## 4. Next step (SP3 — NOT applied by the Lab)" in out
    assert "lab_candidate_readiness.md" in out


def test_dossier_json_sidecar_carries_primary_metric():
    """The SP3 .json sidecar (model_dump_json) carries primary_metric;
    the derivation itself (write_lab_dossier) is unchanged."""
    from tpcore.lab.models import LabResult

    r = _labresult("maxdd_reduction")
    round_trip = LabResult.model_validate_json(r.model_dump_json())
    assert round_trip.primary_metric == r.primary_metric
```

- [ ] **Step 2: Run the dossier tests — expect RED**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_units.py -p no:xdist -q -k dossier`
Expected: **FAIL** — `test_dossier_maxdd_reduction_adds_objective_block` fails (no objective line / no "## 2a"); the SHARPE one may pass coincidentally but the MAXDD one is RED.

- [ ] **Step 3: Add the objective block to `render_lab_dossier` (SHARPE byte-identical)**

Replace `ops/lab/dossier.py:27-55` (`render_lab_dossier`) with:

```python
def _objective_line(r: LabResult) -> str:
    """SP-D §2.4 — for a non-SHARPE objective, an operator-facing line in
    '## 1. Verdict' naming the objective. The parenthetical is MANDATORY
    copy (the 'the gate is sacred' doctrine, SP-C §6): the metric is
    ranking-only and provably does NOT affect the gate. For SHARPE the
    dossier is byte-identical to pre-SP-D (no line emitted)."""
    from tpcore.lab.target import LabPrimaryMetric

    if r.primary_metric == LabPrimaryMetric.SHARPE:
        return ""
    return (f"\n- **Primary objective:** {r.primary_metric.value} "
            f"(ranking metric — does NOT affect the gate)")


def _objective_block(r: LabResult) -> str:
    """SP-D §2.4 — a '## 2a' block keyed off the declared metric. SHARPE:
    empty (byte-identical pre-SP-D). MAXDD_REDUCTION: max_drawdown is the
    headline, Sharpe demoted. Pure fn of held_metrics + the declared
    metric — no new data, no query, no gate read."""
    from tpcore.lab.target import LabPrimaryMetric

    if r.primary_metric == LabPrimaryMetric.SHARPE:
        return ""
    if r.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION:
        hm = r.held_metrics
        return (
            "\n## 2a. Objective-appropriate summary"
            f"\n- **Headline (max_drawdown):** {hm.get('max_drawdown')}"
            f"\n- Sharpe (secondary): {hm.get('sharpe')}"
            f"\n- Profit factor (secondary): {hm.get('profit_factor')}\n"
        )
    # Reserved objectives have no dossier block until their scorer ships
    # (SP-E). Naming them is harmless (the run could never have ranked —
    # the §4.3 pre-spend fence rejects before any LabResult is built).
    return (
        f"\n## 2a. Objective-appropriate summary"
        f"\n- (objective {r.primary_metric.value} has no dossier block "
        f"yet — reserved, SP-E)\n"
    )


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
- Credibility: {r.credibility_score}  (gate ≥ 60){_objective_line(r)}
- Held metrics:

{_fmt_metrics(r.held_metrics)}
{_objective_block(r)}
## 2. Winning parameters vs current engine defaults
{diff}

## 3. Ranked alternatives
{alts}

## 4. Next step (SP3 — NOT applied by the Lab)
{_next_step(r)}

## 5. Credibility rubric
{_fmt_rubric(r.credibility_rubric)}
"""
```

> Implementer note: for `SHARPE`, `_objective_line` returns `""` and `_objective_block` returns `""`. The f-string interpolates them as empty so the rendered text has `(gate ≥ 60)` followed by the newline (byte-identical to pre-SP-D where the line ended `(gate ≥ 60)`) and an empty line where `{_objective_block(r)}` sits — verify byte-identity via the Step-5 golden compare; if a stray blank line appears for SHARPE, the `_objective_block`-empty case must collapse to exactly the pre-SP-D spacing (the SHARPE test pins `"## 2a" not in out` and the existing `test_lab_dossier.py` pins the rest).

- [ ] **Step 4: Run the dossier tests + the existing dossier test — expect GREEN**

Run: `python -m pytest tpcore/tests/test_lab_sp_d_units.py -p no:xdist -q -k dossier && python -m pytest scripts/tests/test_lab_dossier.py -p no:xdist -q`
Expected: **PASS** — MAXDD adds the objective line + "## 2a"; SHARPE has neither; the existing `test_lab_dossier.py` (the pre-SP-D dossier contract incl. the SP-C `_next_step` present-sentinel) stays green.

- [ ] **Step 5: SHARPE byte-identity spot-check**

Run: `python -m pytest "tpcore/tests/test_lab_sp_d_units.py::test_dossier_sharpe_is_byte_identical_to_pre_sp_d" -p no:xdist -q`
Expected: **PASS** (`"## 2a" not in out`, `"Primary objective" not in out`, SP-C `_next_step` + readiness cross-link intact). If this fails because of a stray blank line, fix `_objective_block`'s SHARPE-empty return to preserve exact pre-SP-D spacing, then re-run.

- [ ] **Step 6: ruff**

Run: `ruff check ops/lab/dossier.py tpcore/tests/test_lab_sp_d_units.py`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add ops/lab/dossier.py tpcore/tests/test_lab_sp_d_units.py
git commit -m "feat(lab-sp-d): objective-keyed dossier block (SHARPE byte-identical) (T4)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Clockwork + diff-scope fence + authoritative gate (T5)

Adds the §5.4 metric-implementability clockwork (every roster-`lab_targetable` engine's declared metric resolves; no reserved-unimplemented declared) and the §5.3 AST/source-hash diff-scope fence (the gate functions AND `planner._validate_modify`/`_evidence.py` byte-unchanged + forbidden to read `primary_metric`), then runs the full authoritative gate.

**Files:**
- Create: `tpcore/tests/test_lab_primary_metric_consistency.py`
- Create: `tpcore/tests/test_lab_sp_d_diff_fence.py`
- Modify: `pyproject.toml` (per-file SLF ignores for both new test files)

- [ ] **Step 1: Write the §5.4 metric-implementability clockwork (failing first)**

Create `tpcore/tests/test_lab_primary_metric_consistency.py`:

```python
"""SP-D §5.4 — metric-implementability clockwork (the SP-B clockwork
idiom: a pure runtime-derived consistency test, NOT a byte-shadow). The
build REDs if any roster-lab_targetable engine declares a
reserved-unimplemented objective (the §4.3 footgun caught at CI, not at
a burned ledger spend)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_every_declared_engine_metric_is_implemented():
    from ops.lab.run import _RANKING_METRICS, _lab_target_for
    from tpcore.engine_profile import lab_targetable_engines
    from tpcore.lab.target import LabPrimaryMetric

    for engine in lab_targetable_engines():
        try:
            tgt = _lab_target_for(engine)
        except ValueError:
            # roster-eligible but LAB_TARGET not yet declared (SP-E/SP-F
            # forward step) — nothing to check for THIS engine.
            continue
        metric = tgt.primary_metric
        fn = _RANKING_METRICS[metric]
        # A reserved-unimplemented mapping raises on call; a real one is
        # a pure arithmetic lambda. Probe with a finite n_trades=3 value.
        from ops.lab.run import SliceMetrics
        probe = SliceMetrics(n_trades=3, sharpe=0.0, profit_factor=1.0,
                             max_drawdown=0.0, win_rate=0.0)
        try:
            fn(probe)
        except ValueError as exc:
            pytest.fail(
                f"engine {engine!r} declares reserved-unimplemented "
                f"primary_metric {metric!r}: {exc}")


def test_sharpe_is_always_implemented_and_a_vocabulary_member():
    from ops.lab.run import _RANKING_METRICS
    from tpcore.lab.target import LabPrimaryMetric

    assert LabPrimaryMetric.SHARPE in _RANKING_METRICS
    # every implemented key is a declared vocabulary member
    assert set(_RANKING_METRICS.keys()) <= set(LabPrimaryMetric)
    # the default can never become undeclarable
    assert set(LabPrimaryMetric) >= {LabPrimaryMetric.SHARPE,
                                     LabPrimaryMetric.MAXDD_REDUCTION}
```

- [ ] **Step 2: Write the §5.3 diff-scope / AST-source-hash fence (failing first)**

Create `tpcore/tests/test_lab_sp_d_diff_fence.py`. It pins, by source hash, the named gate functions AND the §0.2a downstream re-derivation byte-unchanged vs `origin/main`, and asserts `_evidence.py`/`planner._validate_modify` never read `primary_metric`.

```python
"""SP-D §5.3 — the structural fence. The gate functions AND the §0.2a
downstream gate re-derivation (planner._validate_modify + _evidence.py)
are AST/source-hash-pinned byte-unchanged vs origin/main, and the
planner/evidence layer is forbidden to read `primary_metric` for any
decision. Complements §5.2: §5.2 proves behaviour, §5.3 fences the diff
so a LATER PR cannot quietly cross the line."""
from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


def _func_src_from_text(text: str, qualname: str) -> str:
    """Return the exact source segment of a top-level (or one-level
    nested) function/method by name from a file's text."""
    tree = ast.parse(text)
    parts = qualname.split(".")
    target_name = parts[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == target_name:
            return ast.get_source_segment(text, node)
    raise AssertionError(f"{qualname} not found")


def _origin_main_text(rel: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"origin/main:{rel}"], text=True)


# (file, function-name) pairs that MUST be byte-unchanged vs origin/main.
_PINNED = [
    ("ops/lab/run.py", "compute_dsr_for_verdict"),
    ("ops/lab/run.py", "compute_slice_metrics_from_trades"),
    ("ops/lab/run.py", "period_returns_from_trades"),
    ("ops/engine_sdlc/planner.py", "_validate_modify"),
    ("ops/engine_sdlc/_evidence.py", "load_labresult_sidecar"),
    ("ops/engine_sdlc/_evidence.py", "assert_identity_fresh"),
    ("ops/engine_sdlc/_evidence.py", "parse_dossier_name"),
]


@pytest.mark.parametrize("rel,fn", _PINNED)
def test_gate_and_downstream_functions_byte_unchanged(rel, fn):
    cur = Path(rel).read_text()
    base = _origin_main_text(rel)
    cur_h = hashlib.sha256(
        _func_src_from_text(cur, fn).encode()).hexdigest()
    base_h = hashlib.sha256(
        _func_src_from_text(base, fn).encode()).hexdigest()
    assert cur_h == base_h, (
        f"{rel}::{fn} changed vs origin/main — SP-D MUST NOT touch the "
        f"gate or its §0.2a downstream re-derivation (spec §2.5/§5.3)")


def test_survived_expression_byte_unchanged():
    """The `survived = (...)` block in _run_lab_core is byte-unchanged."""
    cur = Path("ops/lab/run.py").read_text()
    base = _origin_main_text("ops/lab/run.py")
    needle = "    survived = (\n"
    for txt, label in ((cur, "current"), (base, "origin/main")):
        i = txt.index(needle)
        # the 5-line survived block (open + 3 conditions + close paren)
        block = txt[i:txt.index(")\n", i) + 2]
        if label == "current":
            cur_block = block
        else:
            base_block = block
    assert cur_block == base_block, (
        "the `survived = (...)` gate expression changed vs origin/main")


def test_planner_and_evidence_never_read_primary_metric():
    """The §0.2a downstream gate re-derivation must keep reading only
    verdict/dsr/credibility_score/winning_params — NEVER primary_metric
    (spec §2.5: _evidence.py / _validate_modify forbidden to read it)."""
    for rel in ("ops/engine_sdlc/planner.py",
                "ops/engine_sdlc/_evidence.py"):
        assert "primary_metric" not in Path(rel).read_text(), (
            f"{rel} references primary_metric — the planner/ECR must "
            f"never read it for any decision (spec §0.2a/§2.5)")


def test_sp_a_ledger_and_credibility_files_byte_unchanged():
    """SP-A / credibility / overfitting are read-only under SP-D (§6)."""
    for rel in ("tpcore/lab/ledger.py",
                "tpcore/backtest/credibility.py",
                "tpcore/backtest/overfitting.py"):
        cur = hashlib.sha256(Path(rel).read_bytes()).hexdigest()
        base = hashlib.sha256(
            _origin_main_text(rel).encode()).hexdigest()
        assert cur == base, (
            f"{rel} changed vs origin/main — SP-D is read-only here "
            f"(spec §6 NON-GOAL)")
```

> Implementer note: if `origin/main` is not fetched in the worktree, run `git fetch origin main` once before this test (the CI runner has it; locally `git fetch origin` first). The `_func_src_from_text` AST segment compare tolerates surrounding-line shifts (it hashes the function body's exact source, not file offsets), so adding `_RANKING_METRICS` ABOVE these functions does not red the fence — only editing a pinned function's own source does.

- [ ] **Step 3: Add scoped SLF per-file ignores for both new test files**

Append to `pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
# SP-D §5.4 clockwork + §5.3 diff-fence: asserting metric-
# implementability + the gate/downstream byte-fence REQUIRES naming
# `ops.lab.run`-private symbols (`_RANKING_METRICS`, `_lab_target_for`,
# `SliceMetrics`) — that IS the tests' purpose (SP-B clockwork idiom).
# Engine-lane-module-private (NOT tpcore-private) access; scoped
# per-file ignore is the correct form — never an inline `# noqa: SLF001`.
"tpcore/tests/test_lab_primary_metric_consistency.py" = ["SLF"]
"tpcore/tests/test_lab_sp_d_diff_fence.py" = ["SLF"]
```

- [ ] **Step 4: Run the two new tests — expect GREEN (the code is already correct from Tasks 3–7)**

Run: `git fetch origin main >/dev/null 2>&1; python -m pytest tpcore/tests/test_lab_primary_metric_consistency.py tpcore/tests/test_lab_sp_d_diff_fence.py -p no:xdist -q`
Expected: **PASS** — every declared engine (reversion/vector/momentum default `SHARPE`) resolves; the gate functions + `_validate_modify`/`_evidence.py` + the `survived` block + SP-A/credibility/overfitting are byte-unchanged vs `origin/main`; no `primary_metric` reference in planner/evidence. If the diff fence REDs, SP-D touched a forbidden surface — STOP and revert that edit (it is a spec violation, not a test bug).

- [ ] **Step 5: gen_engine_manifest --check (no roster shadow disturbed)**

Run: `python scripts/gen_engine_manifest.py --check`
Expected: exit 0 (SP-D adds no engine and no roster shadow — `_RANKING_METRICS` is not a roster shadow, §3 "Non-Python shadow check: none").

- [ ] **Step 6: ruff + check_imports (whole touched surface)**

Run: `ruff check . && python -m tpcore.scripts.check_imports tpcore`
Expected: no output / exit 0.

- [ ] **Step 7: The authoritative gate — full single-process suite + order-flip**

The CLAUDE.md ops-package shadow rule: `ops/*.py` ↔ `scripts/ops.py` collision ⇒ the authoritative gate is the FULL single-process suite (never a subset) plus an order-flip. The known #148/ops-package-shadow subset artifact is **NOT a blocker** (only the full xdist suite + `gh pr checks` is authoritative), but a NEW red introduced by SP-D **is**.

Run: `python -m pytest -q -p no:randomly 2>&1 | tail -30`
Expected: full suite green (or only the documented pre-existing #148/ops-shadow subset artifact — if so, confirm it is byte-identical to the pre-SP-D baseline failure set, NOT a new red).

Then the order-flip:

Run: `python -m pytest -q -p no:randomly --reverse 2>&1 | tail -30`
Expected: same pass/known-artifact set (no NEW order-dependent red).

> If `--reverse` is unavailable, use the project's canonical order-flip wrapper (`scripts/run_smoke_test.sh` step 3 or the documented `pytest -p randomly` reseed); the requirement is "the full suite passes under a flipped collection order", not the specific flag.

- [ ] **Step 8: Lane assertion — no data-SDLC file in the diff**

Run: `git diff --name-only origin/main... | grep -E '^(tpcore/(selfheal|auditheal|datasupervisor|ladder|providers)|ops/(weekly_digest|llm_data_triage|defect_register))' && echo "LANE VIOLATION" || echo "engine-lane clean"`
Expected: `engine-lane clean` (SP-D touches only Lab ranking/dossier + the engine-free contract field + new tests + pyproject).

- [ ] **Step 9: Commit**

```bash
git add tpcore/tests/test_lab_primary_metric_consistency.py tpcore/tests/test_lab_sp_d_diff_fence.py pyproject.toml
git commit -m "test(lab-sp-d): metric-implementability clockwork + gate/downstream diff-scope fence (T5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 10: Push + open PR (controller does the merge; this step prepares it)**

> The controller commits/PRs per the brief. If the executor is authorized to push: branch first (never push to `main`), then `gh pr create` with a body ending in the `🤖 Generated with [Claude Code]` trailer and `gh pr checks` must be green before any merge. The authoritative gate is `gh pr checks` (the full xdist CI), NOT the local subset.

---

## Self-Review (performed against the spec, fresh eyes)

### 1. Spec-coverage map (every §1/§2/§3/§4/§5/§7/§8 item → task)

| Spec item | Task |
| --- | --- |
| §1.1 metric on `LabTarget` + tpcore `LabPrimaryMetric`; mapping in `ops/lab/run.py` | Task 3 (enum+field), Task 4 (`_RANKING_METRICS`) |
| §1.2 ranking-vs-gate structural separation (by construction) | Task 5 (wiring untouches gate), Task 8 (§5.3 fence) |
| §1.3 rejected alternatives (closed enum, no `score_fn`, no registry, not `_PROFILE`) | Honored by Task 3 design (enum on `LabTarget`); no registry/`_PROFILE`/callable introduced |
| §2.1 declaration contract; `model_post_init` no new logic | Task 3 Step 3 (no `model_post_init` edit) |
| §2.2 `_score_for_ranking` generalization; guard outside dispatch | Task 4 Step 3 |
| §2.3 `rank_candidates` defaulted metric; `_run_lab_core` passes it | Task 4 (signature), Task 5 (call site) |
| §2.4 objective dossier + DEFAULTED `LabResult.primary_metric` (A11) | Task 6 (model+build), Task 7 (dossier) |
| §2.5 build-time diff-scope fence | Task 8 Step 2 |
| §3 component table incl. `test_lab_primary_metric_consistency.py` | Task 8 Step 1 |
| §4.1 no metric ⇒ Sharpe byte-identical | Task 1 (anchor), Task 4 Step 4-5 (oracle byte-unmodified) |
| §4.2 metric not computable from SliceMetrics (closed enum + seam) | Covered by closed enum (Task 3) + reserved sentinels (Task 4); seam documented (no code, YAGNI) |
| §4.3 reserved metric pre-spend reject (pinned insertion point) | Task 5 (fence between `:794` and `:808`) + Task 5 Step 1 ledger-spy test |
| §4.4 ties (sort unchanged, partition pinned) | Task 4 (sort line untouched); make-or-break pins partition not within-order |
| §4.5 NaN/degenerate clamp inside each mapping | Task 4 (`_clamp`) + Task 4 Step 1 clamp test |
| §4.6 SP-E forward proof (MAXDD end-to-end without Sentinel) | Task 5/7 (full path under MAXDD via synthetic stub) |
| §4.7 amain vs run_lab both via `_run_lab_core` | Task 5 (fence in `_run_lab_core` — both paths) + make-or-break drives `_run_lab_core` |
| §5.1 char-before-refactor | Task 1 |
| §5.2 make-or-break (step 0 non-vacuity + ECR 4-tuple + adversarial through `_validate_modify`) | Task 2 (skeleton) + Task 5 (GREEN) |
| §5.3 AST/source-hash diff fence incl. planner/_evidence + no-primary_metric-read | Task 8 Step 2 |
| §5.4 metric-implementability clockwork | Task 8 Step 1 |
| §5.5 focused units incl. pre-SP-D-sidecar forcing regression | Task 2 (regression skeleton), Task 4/5/6/7 (each unit) |
| §5.6 authoritative gate (full+order-flip, ruff, check_imports, oracle, lane) | Task 8 Steps 6-8 |
| §7 T0..T5 phasing | Tasks 1-2 (T0), 3 (T1), 4 (T2), 5 (T3), 6-7 (T4), 8 (T5) |
| §8 A1 (metric re-ranks failing candidate) | Task 2/5 make-or-break step 5 (adversarial → `survived=False`) |
| §8 A2 (gate edit gamed) | Task 8 §5.3 fence (independent of behaviour test) |
| §8 A3 (tautology) | Task 2 step 0 non-vacuity + runtime partition equality |
| §8 A4 (reserved → spend then crash) | Task 5 pinned pre-spend fence + ledger-spy test |
| §8 A5 (SliceMetrics can't express future bar) | closed enum + documented seam (no speculative code) |
| §8 A6 (oracle churn) | Task 4 Step 5 `git diff --stat` zero-byte assertion |
| §8 A7 (`_PROFILE`/registry drift) | design: field on `LabTarget` only (Task 3) |
| §8 A8 (misspelled metric silent fallback) | Task 3 `test_labtarget_rejects_unknown_metric_string` |
| §8 A9 (NaN poisons sort → gate) | Task 4 `_clamp` + clamp unit |
| §8 A10 (`--metric` flag) | NON-GOAL — no CLI flag added anywhere in the plan |
| §8 A11 (LabResult non-defaulted regression) | Task 6 (defaulted field) + Task 2/6 forcing regression test |
| §8 A12 (downstream leak surface under-proven) | Task 2/5 make-or-break asserts ECR 4-tuple + `_validate_modify` probe; Task 8 §5.3 fences `_validate_modify`/`_evidence.py` |
| §8 A13 (imprecise pre-spend fence) | Task 5 pinned insertion point between `run.py:794` and `:808`, verbatim ledger-spy precedent |
| §8 A14 (make-or-break unconstructable/vacuous) | Task 2 step 0 concrete `choice:A,B,C` recipe with self-asserting non-vacuity that `pytest.fail`s |

**Gaps found and fixed inline during self-review:**

1. **A12/§0.2a highest-residual-risk downstream-leak proof — initially under-specified.** First draft of Task 2 only asserted `core.survived`. Fixed: the make-or-break now (a) drives the FAILED-via-adversarial sidecar through a real `planner._validate_modify` and asserts the hard-reject, and (b) the §5.3 fence (Task 8) AST-pins `_validate_modify` + `_evidence.py` byte-unchanged AND asserts `"primary_metric" not in` either file — closing the §0.2a indirect-leak surface from both the behaviour side and the diff side. This is the single highest-residual-risk item per the spec and is now double-covered.
2. **A14 non-vacuity must ERROR not pass.** Fixed: Task 2 `test_step0_non_vacuity_preconditions` uses `pytest.fail("VACUOUS: …")` on every precondition (winner==A under SHARPE, winner==B under MAXDD, winners differ, C's final-holdout `n_trades<3`) so a future stub edit that no-ops the disagreement fails loudly rather than silently green.
3. **A13 pinned insertion point.** Fixed: Task 5 Step 4 quotes the exact `run.py:793-794` lines and inserts strictly before the `:808` SP-A import that opens the spend block — not "somewhere before". The ledger-spy test is the verbatim `_SharedPool`/`_FakeConn` from `test_lab_targeting_consistency.py`.
4. **`_resolve_ranking_metric` sentinel-vs-floor interaction.** Found during type-consistency review: `_score_for_ranking`'s `n_trades<3 -> -1.0` floor would short-circuit a sentinel only fired inside the mapping, so the fence could be vacuous for `n_trades<3` probes. Fixed: `_resolve_ranking_metric` calls the mapping **directly** on a finite `n_trades=3` probe (not via `_score_for_ranking`), so the sentinel always raises at the fence regardless of the floor. The §5.4 clockwork uses the same direct-probe technique for consistency.
5. **A11 import-cycle check.** Found during type-consistency review: Task 6 adds `from tpcore.lab.target import LabPrimaryMetric` to `tpcore/lab/models.py`. Verified `target.py` imports only `enum`/`collections`/`typing`/`pydantic` (no `models` import) ⇒ no cycle; check_imports green is asserted in Task 6 Step 6.

### 2. Placeholder scan

Scanned for `TBD`/`TODO`/`implement later`/`add error handling`/`similar to Task N`/`<…>`/"write tests for the above": **none present.** Every code step contains complete, runnable code. The two "Implementer note" blocks are clarifying guidance attached to *complete* code (the sentinel-probe rationale and the SHARPE-spacing byte-identity check), not deferred work. No task references a symbol not defined in an earlier task (`LabPrimaryMetric` Task 3 → used Tasks 4-8; `_RANKING_METRICS` Task 4 → used Tasks 5/8; `_resolve_ranking_metric` Task 5; `LabResult.primary_metric` Task 6 → used Task 7).

### 3. Type / signature consistency

- `_score_for_ranking(metrics: SliceMetrics, metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE) -> float` — defined Task 4, called identically in Task 4 units, Task 8 clockwork, and Task 1 anchor (no-arg defaulted form). Consistent.
- `rank_candidates(trials, metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE)` — defined Task 4; called no-arg by the byte-frozen oracle (proven zero-churn Task 4 Step 5) and with `_ranking_metric` positionally in `_run_lab_core` Task 5. Consistent.
- `_resolve_ranking_metric(engine: str) -> tuple[LabPrimaryMetric, Callable[[SliceMetrics], float]]` — Task 5; Task 5 Step 4 unpacks `_ranking_metric, _ = _resolve_ranking_metric(args.engine)`. Tuple arity matches.
- `LabPrimaryMetric` StrEnum members (`SHARPE`/`MAXDD_REDUCTION`/`ULCER`/`INVERSE_ETF_HOLD`) — defined Task 3; string values (`"sharpe"`, `"maxdd_reduction"`) used consistently in dossier (`r.primary_metric.value`) and the `LabPrimaryMetric(metric_name)` constructions in tests.
- `LabResult.primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE` — Task 6; round-tripped via `model_dump_json`/`model_validate_json` in Task 7; default-fill on legacy sidecar asserted Task 2/6.
- Dossier helpers `_objective_line`/`_objective_block` return `str` (possibly `""`); `render_lab_dossier` interpolates them — Task 7. No signature drift.

No inconsistencies remain after the fixes above.

### Highest-risk task for the implementer

**Task 5 (wire the resolved metric + the pinned pre-spend fence).** It is the single load-bearing task: it simultaneously (a) makes the make-or-break GREEN (the §0.2a downstream-leak proof — the spec's highest residual risk), (b) must place the `_resolve_ranking_metric` call at the *exact* pinned point strictly before the SP-A `record_trial_spend` block (an off-by-one here re-creates the §8-A4 "spend then crash" footgun the spec explicitly closes), and (c) must not perturb the gate (the §5.3 fence in Task 8 will red if it does). The make-or-break stub construction (`choice:A,B,C` with provably-inverting orders) is intricate and its non-vacuity step-0 must genuinely ERROR on a degenerate stub — get the `_PROFILES` deep/shallow drawdown levers wrong and step 0 will (correctly) fail, blocking the task until the stub truly creates gate/ranking disagreement. Recommend the implementer run Task 5 Step 5 in isolation and inspect `core.winner_params` under each metric before trusting the green.
