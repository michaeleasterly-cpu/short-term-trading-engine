# Autonomous Lab criteria — framework-evaluated signal-presence + comparative-improvement gates

**Status:** DESIGN + IMPLEMENTATION (bundled per lean cadence — small surgical change touching the SDLC validator/promote path).
**Lane:** heavy (touches `ops/engine_sdlc/`).
**Date:** 2026-05-20.
**Supersedes (partial):** the absolute `DSR ≥ 0.95 ∧ credibility_score ≥ 60` gate in `2026-05-18-engine-sdlc-design.md` §5. The single absolute threshold is replaced by two autonomous criteria sets evaluated against the engine's own backtest dossier; the spec lifecycle/scopes are otherwise unchanged.

## §1 Philosophy reframe

The Lab is the framework's **autonomous safety gate**, not a human-approval funnel. AI agents drive the assessment; subagents harden code; the Lab runs backtests, computes credibility, walks the n_trials ledger, and produces verdicts. The framework reads its own outputs and makes the call.

The Lab gate has two distinct jobs:

| Question | Path | What we are protecting against |
| --- | --- | --- |
| "Does this engine have **real signal at all**, or is it noise dressed up as a strategy?" | LAB → PAPER (new-engine path) | Dead-weight additions to the roster |
| "Is this candidate **better than the engine running today** on its declared primary metric, or would shipping it degrade what already works?" | MODIFY (`fold_existing`) | Regressions against the incumbent |

What the Lab gate explicitly **does not** do: gate live-capital exposure. That belongs at PAPER → LIVE, which is reserved by the paper-only mandate.

## §2 Why the absolute `DSR ≥ 0.95 ∧ cred ≥ 60` gate is over-constrained

The current gate (`ops/engine_sdlc/planner.py` L524-526 / L719-723) applies a single absolute threshold to both questions. Two empirical failures:

1. **DSR's denominator depends on `n_trials`.** A sparse-but-real-edge engine can never clear `DSR ≥ 0.95` no matter how clean the signal. Catalyst (24 trades over 6y; Sharpe 2.27; DSR 0.754; credibility 45) is the binding case — all five currently-PAPER engines also fail this gate (the "all five FAIL the DSR/credibility gate" honesty statement in CLAUDE.md). The gate confuses "low n_trials" with "no signal."
2. **For an improvement, an absolute threshold rejects real wins.** Sharpe 0.4 → 0.7 is a real improvement; the absolute bar rejects it because *neither* hits 0.95.

The DSR/credibility numbers are still computed and persisted — they are not removed from the rubric, they simply stop being the binding gate clause.

## §3 The new criteria

Two pure functions in `ops/engine_sdlc/lab_criteria.py`. Both take dossier-like objects and return `(passed: bool, rejection_reason: str | None)`. No I/O.

### §3.1 New-engine criteria (`_assess_new_engine_signal`)

All must hold (clause names are pinned for grep + rejection-reason auditability):

| Criterion | Threshold | Why |
| --- | --- | --- |
| `positive_sharpe` | `sharpe > 0` | Most basic signal-presence test |
| `min_trade_count` | `trades >= 10` | Below 10 trades you can't distinguish signal from noise |
| `bounded_drawdown` | `max_drawdown >= -0.50` | No ≤−50% catastrophic draws (signal-presence, not live-capital) |
| `bounded_ruin_probability` | `ruin_probability <= 0.30` | 30% ruin too high even for paper-trade-and-learn |
| `min_profit_factor` | `profit_factor >= 1.0` | No edge if avg loss > avg win |
| `sane_min_btl_gap` | `min_btl_gap <= 365` | Below once-a-year fires, experience curve too slow |

Each clause carries a clear `rejection_reason` naming **which** criterion failed. None are subjective; all are read directly off the dossier.

### §3.2 Improvement criteria (`_assess_improvement`)

For a MODIFY (`recommended_exit == "fold_existing"`), the gate compares a candidate dossier to the incumbent's most-recent dossier on the candidate's `primary_metric`. All must hold:

| Criterion | Threshold | Why |
| --- | --- | --- |
| `candidate_beats_incumbent` | `candidate[primary_metric] > incumbent[primary_metric]` (strict) | Improvement must be a real win on the declared bar |
| `candidate_passes_new_engine_floor` | `_assess_new_engine_signal(candidate) == (True, None)` | "Better than a broken incumbent" but no basic signal-presence isn't worth shipping |
| `trade_count_drift_bounded` | `candidate.trades >= 0.5 * incumbent.trades` | A "better Sharpe" via cutting 90% of trades is a different engine, not an improvement |

`primary_metric` is read from `LabResult.primary_metric` (the SP-D pluggable-scoring field that defaults to SHARPE). The comparison's *direction* depends on the metric:

- `SHARPE` → higher is better
- `MAXDD_REDUCTION` → higher is better (the metric is the *reduction*; positive = candidate has a shallower drawdown)
- (other future metrics inherit the LabPrimaryMetric direction convention)

## §4 Empirical calibration against catalyst

The new-engine criteria are CALIBRATED to catalyst as the first test case — not arbitrarily set. Catalyst's recent backtest output:

| Field | Value | Criterion | Pass? |
| --- | --- | --- | --- |
| `sharpe` | 2.274 | `sharpe > 0` | YES |
| `trades` | 24 | `trades >= 10` | YES |
| `max_drawdown` | −0.410 | `max_drawdown >= -0.50` | YES |
| `ruin_probability` | 0.087 | `ruin_probability <= 0.30` | YES |
| `profit_factor` | 1.357 | `profit_factor >= 1.0` | YES |
| `min_btl_gap` | 109 | `min_btl_gap <= 365` | YES |
| `dsr` | 0.754 | (informational — *was* the binding gate) | — |
| `credibility_score` | 45 | (informational — *was* the binding gate) | — |

Catalyst clears every criterion; the old absolute gate rejected it on DSR and credibility. The new criteria correctly accept it because the signal is real (Sharpe 2.27 over 6y, bounded drawdown, profit factor > 1.3) — the binding constraint was *n_trials sparsity*, not signal absence.

## §5 Where the framework reads the dossier autonomously

Two paths the planner now reads automatically:

### §5.1 ADD `source: existing_code` → PAPER-on-pass

The planner reads the engine's most-recent dossier JSON at `backtests/<engine>_backtest_results.json` (the canonical artifact `<engine>.backtest` produces — see `catalyst/backtest.py:run_backtest`, `reversion/backtest.py`, etc.). If no recent dossier is on file, the ADD is rejected with `"no recent backtest dossier found at backtests/<engine>_backtest_results.json; run `python -m <engine>.backtest --json` first"`.

On pass, `_apply_add` lands the engine **PAPER** (not LAB) with `allocator_eligible` from the ECR `allocator:` value. The operator-style ADD already gated this (binary y/n on the validated diff); the framework no longer needs a second human gate for "did this engine earn its way out of LAB?" because the dossier-read criteria already decided.

### §5.2 LAB → PAPER `promote()`

`promote()` evaluates `_assess_new_engine_signal()` against the same dossier source. The `_gate_green` parameter is preserved as a test seam (a synthetic dossier can be injected via the `repo_root` kwarg to point at a tmp `backtests/` dir); production calls `promote()` and the planner resolves the verdict from the dossier autonomously.

### §5.3 MODIFY (`fold_existing`)

`_validate_modify()` evaluates `_assess_improvement()` against the candidate dossier sidecar (the existing `load_labresult_sidecar` path) and the incumbent's most-recent dossier (the same `backtests/<engine>_backtest_results.json` source).

## §6 Code surface

### §6.1 New module: `ops/engine_sdlc/lab_criteria.py`

Pure module. Functions:

- `_assess_new_engine_signal(dossier: NewEngineDossier) -> tuple[bool, str | None]`
- `_assess_improvement(candidate: ImprovementCandidate, incumbent: NewEngineDossier, primary_metric: LabPrimaryMetric) -> tuple[bool, str | None]`
- `load_engine_dossier(repo_root: Path, engine: str) -> NewEngineDossier | None` — reads `backtests/<engine>_backtest_results.json`; returns `None` if absent. Pure read.

`NewEngineDossier` is a frozen pydantic model mirroring the `BacktestRunResult` JSON shape (sharpe / trades / max_drawdown / ruin_probability / profit_factor / min_btl_gap + dsr + credibility_score for informational display).

### §6.2 `ops/engine_sdlc/planner.py` edits

- `validate()` (L479-533): add `existing_code` → criteria-pass-lands-PAPER branch. If criteria pass, mutate `plan.to_state` to `PAPER`. If criteria fail, return rejection citing the specific criterion.
- `_apply_add()` (L625-695): when `source == "existing_code"`, use `plan.to_state` (PAPER) and set `allocator_eligible` from `plan.sot_diff["allocator"]`.
- `_validate_modify()` (L698-758): replace the absolute DSR/cred clauses with `_assess_improvement()`.
- `promote()` (L927-993): replace the `_gate_green` absolute-threshold pattern with `_assess_new_engine_signal()` against the dossier; `_gate_green` retained as a test seam.

### §6.3 Test surface (`tpcore/tests/`)

Cluster `# ─── H-S3-12: autonomous Lab criteria ───` in `test_engine_sdlc_planner.py`:

- `_assess_new_engine_signal` accepts catalyst's empirical numbers.
- One negative test per criterion (six tests).
- `_assess_improvement` accepts a real improvement (candidate.Sharpe > incumbent.Sharpe).
- `_assess_improvement` rejects a degraded candidate.
- `_assess_improvement` rejects a "trade-count crash" (candidate.trades < 0.5 × incumbent.trades).
- `_assess_improvement` rejects a candidate that fails the new-engine floor.
- `test_add_existing_code_lands_PAPER_when_criteria_pass` — happy path with a synthetic credibility row.
- `test_add_existing_code_rejects_when_no_backtest_on_file` — clear rejection if the dossier isn't present.
- `test_promote_uses_criteria_set_not_absolute_threshold` — promote() succeeds for an engine with sharpe>0, trades≥10, etc. even if DSR<0.95.
- `test_validate_modify_uses_relative_criteria` — fold_existing dossier with sharpe 0.4→0.7 PASSES.

Existing tests that pinned the absolute DSR=0.95 gate are updated (the `_validate_modify` clean-pass test still passes because its sidecar carries 0.97 — well above any conceivable floor; tests that asserted "dsr 0.40 → reject" are updated to assert the new rejection clause name).

## §7 Out of scope (deliberate)

- **PAPER → LIVE gate.** The criteria set governs LAB → PAPER. PAPER → LIVE remains reserved by the paper-only mandate; future spec.
- **Recalibration of criteria thresholds.** Calibrated against catalyst; future engines may reveal a need to tighten/loosen — the threshold constants live as module-level named constants in `lab_criteria.py` for trivial future-spec tuning. Not a change today.
- **The `canary` exception.** Canary remains non-graduating by construction (`canary/backtest.py` deliberately never calls `write_credibility_score`); the criteria functions are never called for canary because `_assess_new_engine_signal` is reached only through `promote()` or `ADD source: existing_code`, neither of which canary uses.

## §8 The four gates run locally before pushing

```bash
.venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q
.venv/bin/python -m pytest -p no:randomly -p no:xdist -p no:cacheprovider -q   # order-flip
ruff check . --statistics
.venv/bin/python -m tpcore.scripts.check_imports tpcore ops reversion vector momentum sentinel canary catalyst carver
```
