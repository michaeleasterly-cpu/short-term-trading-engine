# SP-A — Cross-Candidate n_trials Ledger (Hardened Spec)

**Epic:** Lab front-half (`docs/superpowers/specs/2026-05-19-lab-front-half-epic.md`). FIRST sub-project — the safety floor.
**Status:** spec hardened, NOT yet implemented (controller runs writing-plans → subagent-driven-exec next).
**Lane:** engine lane only. Data-SDLC files read-only symmetry references.
**Memory lens:** `project_ml_research_track` (the DSR/n_trials overfit verdict — THE binding platform constraint), `project_lab_front_half_epic`, `project_research_llm_edge_discovery`.

---

## 1. Problem

`ops/lab/run.py:726` (`_run_lab_core`):

```python
dsr = compute_dsr_for_verdict(held_period_returns, n_trials=args.trials)
```

The Deflated Sharpe Ratio is corrected for **only this run's** `--trials`. `compute_dsr_for_verdict` (`ops/lab/run.py:423`) feeds `n_trials` into the expected-max-Sharpe-under-null term `e_max` (López de Prado DSR eqn 8/9): `e_max` is **monotone increasing** in `n_trials`, so a larger `n_trials` raises the bar and *lowers* DSR. That correction is mathematically the multiple-testing penalty: it must reflect *how many configurations were tried in pursuit of an edge for this target/decision* — not how many were tried in one arbitrary CLI invocation.

Today the Lab has **zero cross-run / cross-candidate memory**. Ten Lab runs of 40 trials each against `reversion` (or an LLM's 50 hypotheses under #242) each get the small `n_trials=40` penalty. The true multiple-testing penalty is **cumulative** (≈400, or the LLM's full search budget). A candidate that should fail the deflated bar can be passed by *slicing the search into many small runs* — each individually under-penalized. This silently launders overfit past the one gate the platform treats as ungameable (`project_ml_research_track`: "every accepted edge is ONE pre-registered single primary spec, the gate is sacred and never weakened/bypassed, the n_trials ledger is the structural defense").

This is a **Critical** latent design defect: it is the precise `project_ml_research_track` failure mode, and it is the literal precondition for #242 (an LLM proposing N strategies *is* that failure mode at scale unless every proposed config is cumulatively counted).

---

## 2. The cumulative-trial definition + keying + math rationale

### 2.1 What "a trial" is (rigorous, ungameable)

**A trial is one candidate parameter configuration that the Lab SCORED against a given target engine.**

Operationally, the countable unit is **`args.trials` per Lab run** — the total number of parameter combinations the run pre-sampled and put through the walk-forward search (`sample_parameters(args.engine, args.trials, …)`, `ops/lab/run.py:625`). This is the same number already fed to `compute_dsr_for_verdict` as the per-run correction, so the unit is *self-consistent with the existing DSR math* — the ledger does not redefine "trial", it makes the existing per-run unit cumulative.

Rationale for `args.trials` (not "evaluations" = `per_window_trials × n_windows`, not "1 per winner"):

- The DSR `e_max` correction (`_expected_max_sharpe_under_null`, `tpcore/backtest/overfitting.py:108`) is "expected max sample Sharpe across **`n_trials` independent configurations** under the null." The configuration *space the search drew the winner from* is `args.trials` distinct sampled configs — that is exactly the López de Prado `n_trials` semantic. The current per-run code already uses `args.trials` for this; the ledger preserves that meaning, summed.
- Counting *evaluations* would double-count (each config is re-evaluated across windows — same config, not a new hypothesis). Counting *1 per run* (the winner) would massively under-count (the search tried `args.trials`). `args.trials` is the honest, already-blessed unit.
- It is **ungameable in the right direction**: a human/LLM who runs more configs (more fishing) necessarily increments the ledger by exactly what they fished with; running *more, smaller* runs sums to the same total (the laundering path is closed); the only way to keep the ledger low is to genuinely fish less.

A run that produces no rankable trial / no rubric STILL spent `args.trials` configurations searching — see §3.4 (the spend is recorded regardless of outcome; this is the genuine constraint found in the code and is load-bearing for ungameability).

### 2.2 Keying — per **target engine**

`cumulative_n_trials` is keyed on the **target engine** (`args.engine` / `candidate.target_engine`), summed across **all candidates, all runs, all sessions, all worktrees, forever**.

NOT per `(target, candidate-family)`. Justification against the overfit math: the DSR correction protects a *decision* — "is there a tradable edge for engine E?". Every Lab configuration ever scored against E — regardless of which candidate family proposed it (a `reversion` z-threshold sweep and a future `reversion` PCA-residual sweep are both searches for a `reversion` edge) — is a draw from the multiple-testing budget spent answering that one question. Splitting the ledger per candidate-family would re-open the laundering hole at the family granularity (spin up a "new family" to reset the count). Per-target is the *coarsest honest* key and the only ungameable one: it makes the answer to "has the Lab been fishing hard for a `reversion` edge?" a single monotone number that a human/LLM cannot fragment.

(SP-B will make targeting roster-driven; the key is the roster engine name — forward-compatible by construction, no per-family sub-keys ever.)

### 2.3 The math integration

DSR deflation switches from:

```
n_trials = args.trials                                  # per-run only (today)
```

to:

```
n_trials = cumulative_n_trials(target) + args.trials    # cumulative (SP-A)
```

where `cumulative_n_trials(target)` = Σ of `args.trials` over every **prior** Lab run against `target` (this run's own `args.trials` is added explicitly so the current run is always counted exactly once, and the read is taken *before* this run records its own spend — see §3.3 ordering). `e_max` is monotone ↑ in `n_trials` ⇒ the deflated bar rises with cumulative Lab activity ⇒ DSR falls. This is the correct, honest direction (§5).

---

## 3. Persistence design — event-sourced, append-only, ungameable

### 3.1 Substrate decision: REUSE the existing event-sourced credibility log — **no new table**

Verified in code:

- Every Lab run that produces a credibility rubric writes one row to `platform.data_quality_log` via `write_credibility_score` (`tpcore/backtest/statistical_validation.py:196`) with `source = "backtest_credibility.lab.<candidate>"`, `timestamp = now(UTC)`, `confidence = score/100`, `notes = CredibilityScore.model_dump_json()`.
- `platform.data_quality_log` is **append-only**: PK `(source, timestamp)`, `INSERT … ON CONFLICT (source, timestamp) DO NOTHING RETURNING 1` (`tpcore/quality/data_quality.py:48`). No UPDATE, no DELETE path anywhere. This is the *exact event-sourced read-the-events-back* pattern `tpcore/supervisor_state.py` (schema:1) uses — read state by aggregating an append-only event log, never a mutable counter.

**The cumulative count is DERIVED by aggregating these append-only Lab events — it is never a stored mutable number.** This honors the platform norm (`project_three_service_architecture` / supervisor_state precedent: prefer reading existing event-sourced persistence over a new table) and is ungameable by construction (you cannot un-emit an append-only row; there is no counter to reset).

### 3.2 The genuine constraint found in the real code — and its fix (load-bearing)

**Constraint:** the credibility write in `_run_lab_core` is **conditional**: `if final_result.credibility_rubric is not None:` (`ops/lab/run.py:731`). Also, the three non-result outcomes (no DSN, no walk-forward window, no rankable trial — `ops/lab/run.py:621/639/698`) return an `int` rc *before* reaching the DSR/credibility code at all. **A run that errors out, or whose winner produced no rubric, spends `args.trials` configurations but writes ZERO `backtest_credibility.lab.*` rows.** Therefore *deriving the cumulative count solely from credibility rows would silently UNDER-COUNT exactly the runs an adversary would deliberately produce (abort-after-fishing) — re-opening the laundering hole.* This is the make-or-break constraint; the design MUST record the trial **spend** as its own append-only fact, decoupled from whether a rubric/verdict was produced.

**Fix — a dedicated append-only trial-spend event in the same substrate:** the Lab emits one `platform.data_quality_log` row per run recording the spend, under a distinct, schema-locked source namespace:

```
source     = f"lab_trial_ledger.{target_engine}"
timestamp  = now(UTC)                       # the run's spend instant; PK component
confidence = 0                              # unused for this source (schema requires 0..1; 0 = N/A)
stale      = False
notes      = JSON: {"schema": 1, "target_engine": <str>, "candidate": <str|null>,
                     "trials": <int args.trials>, "seed": <int>, "run_outcome": <str>}
```

- `source` is `lab_trial_ledger.<target>` — a NEW source *namespace*, NOT a new table. Distinct from `backtest_credibility.*` so it is unconditional and never confused with the gate-read source. `graduation_ready` reads `backtest_credibility.{engine}` only — it never sees `lab_trial_ledger.*` (live-safety preserved by namespace disjointness, §4).
- The spend row is emitted **unconditionally for every run that sampled trials**, *before* the DSR is computed and *regardless* of rubric/verdict/error — including the non-result rc paths that got far enough to sample (the emit point is right after `sample_parameters`, see §3.3). A run that fails to even sample (no DSN: rc 2 before sampling) spent nothing and correctly records nothing.
- `cumulative_n_trials(target)` = `SELECT COALESCE(SUM((notes::jsonb->>'trials')::int), 0) FROM platform.data_quality_log WHERE source = 'lab_trial_ledger.' || $1` over all rows **strictly prior** to this run's own spend row.

**Why a new source-namespace and not overloading `backtest_credibility.lab.*`:** the credibility row is conditional and semantically a *verdict* artifact; the trial-spend is an unconditional *expenditure* fact. Conflating them is exactly the under-count bug. A new source within the existing append-only table is the minimal honest artifact — it is NOT a new table (no migration, reuses `DataQualityWriter`'s `ON CONFLICT (source,timestamp) DO NOTHING` append-only contract verbatim), and it is schema-locked via the `notes.schema:1` discriminator + a contract test.

### 3.3 Emit ordering + read ordering (exact, race-honest)

In `_run_lab_core`, after `candidates = sample_parameters(...)` succeeds (`ops/lab/run.py:625`) and **before** `compute_dsr_for_verdict` (`:726`):

1. **Emit the spend row** for *this* run (`trials = args.trials`, `run_outcome` provisional = `"sampled"`; outcome refinement is out of scope — the *trials* field is the only load-bearing value and it is known at sample time). Emitted as early as possible so an abort *after* sampling still counts. The emit uses the **same RW credibility pool** the existing credibility write uses under an active `LabContext` (`active_credibility_pool()`, `tpcore/lab/context.py:16`) — the ONE allowlisted RW handle; no second ad-hoc pool inside the isolation boundary (reuses the H-S3-8 mechanism verbatim). The legacy non-Lab `scripts/search_parameters.py` path (`candidate is None`, no LabContext) is **excluded** — it is not a Lab run and must stay byte-identical (H-S2-3 symmetry; legacy path emits no ledger row, reads no ledger).
2. **Read `cumulative_n_trials(target)`** = SUM over all `lab_trial_ledger.<target>` rows with `timestamp < ` this run's spend-row timestamp (strict `<` excludes the row just emitted; cumulative therefore = all *prior* spend).
3. `dsr = compute_dsr_for_verdict(held_period_returns, n_trials=cumulative + args.trials)`.

Append-only `ON CONFLICT (source,timestamp) DO NOTHING` means a pathological same-timestamp collision is dropped (loses one count) but never double-counts and never errors — fail-safe toward *under* count only under microsecond collision, which is acceptable and not adversarially reachable (timestamps are server-side `now()` per distinct run; the adversary cannot force collisions to reduce their own penalty without also dropping their own run).

### 3.4 Ungameability properties (enumerated)

- **No reset path:** append-only table, no UPDATE/DELETE in any code path; no CLI flag, env var, or argument reduces the SUM. There is no "counter" object — the number is a query over immutable history.
- **No under-declare:** the spend is `args.trials` recorded at sample time, before any verdict/abort, in a source `graduation_ready` ignores, so an adversary cannot abort-after-fishing to dodge the count, nor mislabel it as a non-Lab run (the namespacing/`candidate` check mirrors the proven H-S2-3 seam).
- **Monotone:** SUM over an append-only log is non-decreasing in the number of runs against the target. The Nth candidate against a target gets `n_trials ≥` the (N-1)th's → DSR bar non-decreasing → graduation strictly harder as Lab activity accumulates. Make-or-break (§7).
- **Cross-session / cross-worktree:** the substrate is the shared Postgres `platform.data_quality_log` — not a file, not a process-local var — so it survives sessions, worktrees, and machines (same DB).

---

## 4. Live-safety / isolation reuse

This is a **Lab-internal accounting change only**. It MUST NOT touch live engines, the data lane, or the 8 forbidden files.

- **Live graduation read untouched:** `graduation_ready(pool, <live_engine>)` (`tpcore/backtest/credibility.py:230`) reads ONLY `source = backtest_credibility.{engine}`. The ledger writes/reads `source = lab_trial_ledger.<target>` — a disjoint namespace. By construction the live gate read is byte-identical: zero `lab_trial_ledger.*` rows are visible to `graduation_ready`. Pin with a binding test (§7 T-LIVE).
- **H-S2-3 reuse:** the candidate/non-Lab discrimination already proven for the credibility write (`candidate is not None` ⇒ Lab path) is reused verbatim to scope ledger emit/read to Lab runs only; the legacy `scripts/search_parameters.py` operator path stays byte-identical (emits/reads nothing).
- **H-S3-8 reuse:** the ledger emit uses `active_credibility_pool()` — the single allowlisted RW handle inside `LabContext` — exactly as the existing credibility write does; no new RW pool is opened inside the isolation boundary, no new reentrancy guard surface.
- **Gate unchanged:** `survived = dsr ≥ args.dsr_threshold ∧ credibility_score ≥ args.credibility_threshold ∧ n_trades ≥ 3` (`ops/lab/run.py:787`) is **textually unchanged**. Only the `n_trials` *input* to `compute_dsr_for_verdict` grows. The thresholds (0.95 / 60 / 3) are not touched, not weakened, not bypassed.
- **No data-lane / forbidden-file contact:** no edit to `tpcore/calendar.py`, `tpcore/risk/*`, `ops/engine_*`, `tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`, the data-SDLC spec/checklist. `platform.data_quality_log` is written via the existing `DataQualityWriter` (no schema migration — new *source value*, not new column/table).

---

## 5. The honest-behavior invariant (the point of the whole thing)

A candidate that **would have SURVIVED under per-run `args.trials` but FAILS under cumulative** is the **CORRECT, honest** outcome — it means the edge only "passed" because the multiple-testing penalty was being laundered across runs. SP-A surfacing that failure is the deliverable working, not a regression. This is pinned as the make-or-break test T-CUMUL (§7) and stated explicitly so no future reviewer/operator "fixes" it back to per-run.

Non-goal restatement: SP-A makes the gate *correctly harder*; it does **not** weaken, re-weight, or bypass DSR≥0.95 ∧ cred≥60 ∧ n_trades≥3.

---

## 6. The `_run_lab_core` integration point (exact)

File `ops/lab/run.py`, function `_run_lab_core` (`:597`):

- **Emit point:** immediately after `candidates = sample_parameters(args.engine, args.trials, seed=args.seed)` (`:625`) and the sampled-count print — gated on `candidate is not None` (Lab run) and an active `active_credibility_pool()`. Emit the `lab_trial_ledger.<args.engine>` row (`trials=args.trials`).
- **Read point:** just before `dsr = compute_dsr_for_verdict(...)` (`:726`). Compute `cumulative = cumulative_n_trials(args.engine)` (SUM of prior `lab_trial_ledger.<args.engine>` rows, `timestamp <` this run's spend-row ts).
- **Changed line:** `:726` becomes
  `dsr = compute_dsr_for_verdict(held_period_returns, n_trials=cumulative + args.trials)`.
- **Print honesty:** the human report line currently `DSR (n_trials={args.trials:>3})` (`ops/lab/run.py:828`, in `amain`) MUST show the **effective cumulative** n_trials, not `args.trials`, so the operator sees the true penalty. The dossier `LabResult.n_trials` (`tpcore/lab/models.py:55`, set at `ops/lab/run.py:892`) MUST carry the **effective cumulative** value (the number that actually deflated the DSR), not the per-run `args.trials` — the dossier must not lie about the penalty applied. (Mechanically the simplest faithful change: `_LabCore` carries the effective `n_trials`; `amain`/`_build_lab_result` read it from there. Exact plumbing is a writing-plans concern; the *contract* is: every surfaced n_trials = the cumulative value that deflated the verdict.)
- **Helper home:** the read/emit live in a small engine-FREE helper. **`tpcore/lab/` is engine-free (H-S2-1) and is the right home** for the pure ledger read/emit (no engine import; only asyncpg + the existing `DataQualityScore`/`DataQualityWriter` + `active_credibility_pool`). Add `tpcore/lab/ledger.py` (engine-free, mirrors `tpcore/supervisor_state.py`'s pure-read shape). `ops/lab/run.py` calls it. This keeps `check_imports tpcore` green (verified: no engine symbol involved).

---

## 7. T0–Tn TDD decomposition (make-or-break tests called out)

All tests in a **collected** path. Per H-S2-6, `tpcore/tests/` and `scripts/tests/` are in `pyproject.toml:testpaths`; the ledger unit/contract tests go in `tpcore/tests/test_lab_ntrials_ledger.py` (collected) and the integration assertions extend `tpcore/tests/test_lab_isolation.py` (already collected, the binding-isolation home).

- **T0 (decision, no code):** confirm substrate = reuse `platform.data_quality_log` via a new `lab_trial_ledger.<target>` source (no new table); helper home = `tpcore/lab/ledger.py` (engine-free); emit point + read point + changed line per §6. Record the §3.2 constraint (conditional credibility write ⇒ must NOT derive count from credibility rows) as the binding rationale.
- **T1 — ledger helper unit (RED→GREEN):** `tpcore/lab/ledger.py` `record_trial_spend(pool, *, target, candidate, trials, seed)` and `cumulative_n_trials(pool, target, before_ts)`. Pure asyncpg against a test DB / fake. Assert: emit writes exactly one `lab_trial_ledger.<target>` row with the JSON `schema:1, trials=N`; SUM aggregates only that target's prior rows; unknown target → 0.
- **T2 — schema-lock contract test:** the `notes` JSON shape is frozen (`schema:1, target_engine, candidate, trials, seed, run_outcome`); a drift fails the build (mirrors the supervisor_state schema:1 locked-vocabulary discipline).
- **T3 — append-only / no-reset (MAKE-OR-BREAK · T-NORESET):** assert there is NO code path (CLI flag, env, function arg) that UPDATEs/DELETEs a `lab_trial_ledger.*` row or otherwise reduces `cumulative_n_trials`; emitting the same `(source,timestamp)` twice is `ON CONFLICT DO NOTHING` (no error, no double-count); the function surface exposes only append + sum.
- **T4 — monotone-harder (MAKE-OR-BREAK · T-MONO):** simulate run 1 then run 2 against the same target; assert `cumulative_n_trials` after run 1 < after run 2, and that the `n_trials` fed to `compute_dsr_for_verdict` for run 2 is strictly greater than for run 1, and (with fixed returns) DSR(run 2) ≤ DSR(run 1). The Nth candidate against the same target gets a strictly larger n_trials and a correspondingly harder gate.
- **T5 — cumulative-fails-where-per-run-survived (MAKE-OR-BREAK · T-CUMUL):** construct held returns + thresholds such that `compute_dsr_for_verdict(returns, n_trials=args.trials)` ≥ 0.95 (would SURVIVE per-run) but `compute_dsr_for_verdict(returns, n_trials=cumulative+args.trials)` < 0.95 (FAILS cumulative). Assert `_run_lab_core` returns FAILED. **This failure is the correct honest behavior — the test asserts FAILED is right, and a docstring forbids "fixing" it back to per-run.**
- **T6 — gate-threshold-unchanged (MAKE-OR-BREAK · T-GATE):** assert the `survived` expression and the 0.95/60/3 thresholds are byte-identical pre/post SP-A (grep/AST pin + a behavioral test that with `cumulative=0` the verdict is identical to pre-SP-A for the same inputs — SP-A is a strict superset that reduces to old behavior when no prior trials exist).
- **T7 — live-graduation-untouched (MAKE-OR-BREAK · T-LIVE):** after a `fold_existing` Lab run targeting a live engine (e.g. `reversion`), assert `graduation_ready(pool, "reversion")` is byte-identical to before AND zero rows exist under `backtest_credibility.reversion` AND `graduation_ready` never reads `lab_trial_ledger.*` (the ledger source is invisible to the live gate). Extends the existing H-S2-3 isolation test.
- **T8 — under-count-closed (MAKE-OR-BREAK · T-ABORT):** a Lab run that samples `args.trials` then takes a non-result rc path (no rankable trial / no rubric) STILL records its `args.trials` spend in the ledger; a subsequent run against the same target sees the aborted run's trials in `cumulative_n_trials`. (This is the §3.2 constraint proven closed — the abort-after-fishing laundering path is dead.)
- **T9 — legacy-path-byte-identical:** the non-Lab `scripts/search_parameters.py` path (`candidate is None`, no `LabContext`) emits NO `lab_trial_ledger.*` row and reads NO ledger; its DSR uses per-run `args.trials` exactly as today (H-S2-3 symmetry; the characterization oracle stays green).
- **T10 — dossier/print honesty:** `LabResult.n_trials` and the `amain` `DSR (n_trials=…)` line carry the **effective cumulative** value, not `args.trials`; pin with a run where `cumulative > 0`.
- **T11 — full suite + CI-exact ruff + `check_imports reversion vector momentum sentinel canary tpcore` (proves `tpcore/lab/ledger.py` is engine-free) + lane assertion (no forbidden-file diff) + finishing-a-development-branch.**

---

## 8. §Hardening register

| ID | Risk → binding correction |
| --- | --- |
| **H-LL-1** | *Deriving the count from `backtest_credibility.lab.*` rows under-counts* (the credibility write is conditional, `ops/lab/run.py:731`; non-result rc paths write nothing). **Correction:** a dedicated, *unconditional* append-only spend event under a new source namespace `lab_trial_ledger.<target>`, emitted right after `sample_parameters`, before DSR, regardless of verdict/abort. The make-or-break §3.2 constraint; pinned by T8. |
| **H-LL-2** | *Per-`(target,candidate-family)` keying re-opens laundering at family granularity* (spin a "new family" to reset). **Correction:** key strictly on **target engine**; the coarsest honest key; the DSR correction protects the per-target *decision*, not a family. §2.2; pinned by T4. |
| **H-LL-3** | *A second ad-hoc RW pool inside `LabContext` would violate the SP2 isolation boundary.* **Correction:** the emit reuses `active_credibility_pool()` (the ONE allowlisted RW handle, H-S3-8) exactly as the existing credibility write; no new RW pool, no new guard surface. §4. |
| **H-LL-4** | *Counting could poison the live gate if it shared the credibility source.* **Correction:** disjoint source namespace `lab_trial_ledger.*`; `graduation_ready` reads only `backtest_credibility.{engine}` — never sees the ledger. Pinned by T7. |
| **H-LL-5** | *A new mutable counter table would be resettable / a migration burden / off the platform event-sourced norm.* **Correction:** NO new table — derive the SUM from the existing append-only `platform.data_quality_log` (PK `(source,timestamp)`, `ON CONFLICT DO NOTHING`), exactly the `tpcore/supervisor_state.py` event-sourced read precedent. No UPDATE/DELETE path exists ⇒ ungameable by construction. §3.1; pinned by T3. |
| **H-LL-6** | *The dossier/print still showing per-run `args.trials` would lie about the applied penalty* (operator/LLM under-perceives the true bar). **Correction:** every surfaced n_trials (the `amain` line, `LabResult.n_trials`) carries the effective cumulative value that actually deflated the verdict. §6; pinned by T10. |
| **H-LL-7** | *A future reviewer/operator "fixes" the (correct) new failures back to per-run.* **Correction:** §5 states the honest-behavior invariant explicitly; T5 asserts the FAILED outcome is correct with a docstring forbidding the reversion; T6 pins the gate threshold byte-identical so the only sanctioned change is the n_trials input growing. |
| **H-LL-8** | *Same-microsecond `(source,timestamp)` collision drops a count (`ON CONFLICT DO NOTHING`).* **Assessment:** fail-safe toward under-count only, not adversarially reachable (server-side `now()` per distinct run; an adversary forcing collisions also drops their own run). Accepted; documented; T3 asserts no error + no double-count on collision. |
| **H-LL-9** | *The offline fakes (T1/T4/T8) re-implement the WHERE/JSON semantics in Python ⇒ a SQL-TEXT regression in `cumulative_n_trials` (wrong JSON key `->>'trials'`, `SUM`→`COUNT`, dropped `source=$1` predicate, `<`→`<=` strict-prior boundary, wrong cast) is invisible to every offline test — and a silently-wrong cumulative SUM defeats SP-A's entire anti-laundering purpose (the safety mechanism the whole edge-discovery/#242 vision rests on).* Surfaced by the T0+T1 review (Concern-2, Important). **Correction:** T7 carries a **mandatory** DB-gated `test_cumulative_n_trials_real_db_integer_correctness` (Step 1b) that seeds KNOWN `lab_trial_ledger.*` rows in a real Postgres and asserts the EXACT integer return — exercising SUM, the `notes::jsonb->>'trials'` int-cast, the per-target `source=$1` predicate (cross-target isolation), and the strict `< before_ts` boundary end-to-end. Skips locally (no `DATABASE_URL`), runs in CI; a SQL-text regression FAILs it in CI. Pinned by T7 Step 1b. |

---

## 9. Failure modes

- **DB unreachable at emit/read:** the run already requires a DSN and an active `LabContext` RW pool (existing precondition). If the emit fails, the run must fail loud (no silent DSR with a stale/zero cumulative) — the ledger read is a *precondition of a trustworthy verdict*; a run that cannot record/read its trial spend MUST NOT produce a SURVIVED verdict. (Mechanically: propagate the asyncpg error as the existing non-result rc / `RuntimeError` path — never swallow to `cumulative=0`.)
- **First-ever run against a target:** `cumulative_n_trials` = 0 ⇒ `n_trials = 0 + args.trials` = exactly today's behavior. SP-A reduces to the status quo when no prior trials exist (T6) — strictly additive, no regression on a clean target.
- **Legacy non-Lab search:** no ledger interaction (T9) — characterization oracle unaffected.
- **Microsecond collision:** §8 H-LL-8 — accepted under-count-only, not adversarially reachable.

---

## 10. Reused-vs-new

| Concern | Decision |
| --- | --- |
| Persistence table | **REUSE** `platform.data_quality_log` (append-only, `ON CONFLICT (source,timestamp) DO NOTHING`). No new table, no migration. |
| Writer | **REUSE** `tpcore/quality/data_quality.py` `DataQualityWriter` / `DataQualityScore` verbatim. |
| RW handle inside Lab | **REUSE** `active_credibility_pool()` (H-S3-8 single allowlisted handle). |
| Lab/non-Lab discrimination | **REUSE** the H-S2-3 `candidate is not None` seam. |
| Event-sourced read pattern | **REUSE** the `tpcore/supervisor_state.py` schema:1 append-only-aggregate precedent (symmetry-of-approach, engine-lane). |
| NEW (minimal) | A source *namespace* `lab_trial_ledger.<target>` + `tpcore/lab/ledger.py` (engine-free read/emit helper). Justified: the credibility row is conditional/verdict-semantic; the spend must be unconditional/expenditure-semantic — conflating them is the H-LL-1 under-count bug. The new artifact is a *source value + a pure helper*, not a table. |

---

## 11. Non-goals

- **NOT a gate weakening.** DSR≥0.95 ∧ cred≥60 ∧ n_trades≥3 and the thresholds are byte-identical; only the n_trials *input* grows. New failures are correct (§5).
- **NOT SP-B/SP-C/SP-D.** No roster-driven targeting, no readiness checklist, no pluggable scoring here. SP-A is targeting-agnostic (keys on `args.engine`, forward-compatible with SP-B's roster key).
- **NOT ML.** Pure deterministic accounting (`project_ml_research_track`).
- **NOT a touch of the 8 forbidden files or the data lane.** No `tpcore/calendar.py`, `tpcore/risk/*`, `ops/engine_*`, `tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`, data-SDLC spec/checklist. No schema migration (new source value only).
- **NOT #242.** SP-A is the precondition; the LLM emitter is SP-G.

---

## 12. Self-review

- **Placeholder scan:** no TODO/TBD/`???`/`<placeholder>` — every section concrete; file:line citations verified against the read code (`ops/lab/run.py:625/726/731/787/828/892`, `tpcore/backtest/credibility.py:230`, `tpcore/quality/data_quality.py:48`, `tpcore/lab/context.py:16`).
- **Internal consistency:** the trial unit (`args.trials`), the key (target engine), the substrate (`lab_trial_ledger.<target>` in `data_quality_log`), and the integration point (`ops/lab/run.py:726`) are referenced identically across §2/§3/§6/§10. The §3.2 constraint (conditional credibility write) is the spine that forces the dedicated unconditional spend event and is traced through H-LL-1 → T8.
- **Scope:** spec-only; SP-A bounded; SP-B/C/D explicitly excluded (§11). Engine lane; forbidden files untouched (§4/§11).
- **Ambiguity:** the make-or-break behaviors are pinned as named tests (T-NORESET/T-MONO/T-CUMUL/T-GATE/T-LIVE/T-ABORT); the honest-failure invariant (§5) is stated so it is not "fixed" back.
- **Every requirement → a task:** cumulative definition→T1/T4; ungameable/no-reset→T3; cumulative-fails-where-per-run-survived→T5; gate-unchanged→T6; live-safety→T7; under-count-closed→T8; legacy byte-identical→T9; dossier honesty→T10; engine-free helper / lane→T11.
- **Genuine constraint surfaced (not hand-waved):** the conditional credibility write + non-result rc paths (`ops/lab/run.py:731` + `:621/639/698`) means a credibility-row-derived count under-counts the exact runs an adversary produces — driving the dedicated unconditional `lab_trial_ledger.*` event. Stated precisely in §1/§3.2/§9/H-LL-1.
