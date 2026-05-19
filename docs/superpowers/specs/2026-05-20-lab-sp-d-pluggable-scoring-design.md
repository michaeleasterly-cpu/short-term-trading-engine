# SP-D — Pluggable Per-Engine Success Scoring + Richer Dossier (Hardened Spec)

**Status:** DESIGN (skeptical-staff hardened). Build contract for the SP-D sub-project of the Lab front-half epic.
**Epic:** `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md` §SP-D.
**Lane:** engine lane only. Data-SDLC files are read-only symmetry references, never edited here.
**Dependency:** after SP-A (MERGED), independent of SP-B (MERGED) / SP-C (MERGED). Must precede SP-E.
**Discipline:** brainstorm→expert-decompose→spec→expert-harden→writing-plans→subagent-driven-exec (split spec + code-quality review)→PR→CI-green merge. `docs/DEV_PIPELINE_STANDARD.md`.

---

## §0 Context — verified in code (file:line)

The Lab back-half is built; SP-A/SP-B/SP-C of the front-half are MERGED. SP-D is the fourth front-half piece. Everything below is read from the tree as-is, not assumed.

### §0.1 The ranking surface (what SP-D generalizes)

- `ops/lab/run.py:466-476` — `_score_for_ranking(metrics: SliceMetrics) -> float`. Today: `if metrics.n_trades < 3: return -1.0`; else `base = float(metrics.sharpe)`; return `base + 0.05 * math.log10(max(metrics.n_trades, 1))`. **Sharpe-only, hardwired, engine-agnostic** — takes a `SliceMetrics` and returns a scalar; higher is better.
- `ops/lab/run.py:479-495` — `rank_candidates(trials) -> list[tuple[dict, float, int]]`. Groups `TrialResult`s by `json.dumps(parameters, sort_keys=True)`, drops `t.error` rows, computes `np.mean([_score_for_ranking(t.holdout) for t in group])` per param-set, sorts **descending by mean score** (`ranked.sort(key=lambda x: x[1], reverse=True)`). Returns `(params, mean_score, n_windows)`.
- `ops/lab/run.py:286-314` — `SliceMetrics`: the only fields available to any ranking function are `n_trades:int`, `sharpe:float`, `profit_factor:float`, `max_drawdown:float` (negative-or-0 convention, `run.py:370`), `win_rate:float`, `holdout_sharpe_per_period:float`. **These six are the entire ranking-input universe.** Anything an engine wants to rank on must be derivable from these (or from the period-return series — see §4.4).
- `ops/lab/run.py:891-902` — `_run_lab_core`: `ranked = rank_candidates(trials)`; `if not ranked: return 1` (oracle-pinned non-result rc); `winner_params, winner_score, _ = ranked[0]`. **`ranked[0]` is the only consumer that influences anything downstream.** `winner_params` then drives the final held-back replay (`run.py:904-910`).
- `ops/lab/run.py:1092-1093,1144` — `core.ranked[:5]` renders the top-5 in `amain`'s FAILED block and becomes `LabResult.ranked_alternatives`. `winner_score` is carried on `_LabCore` (`run.py:754`) and `LabResult` but is **display/provenance only** — nothing reads it for the verdict.

### §0.2 The sacred gate (what SP-D must NOT touch — verified byte-exact)

`ops/lab/run.py:1040-1044`:
```
survived = (
    dsr >= args.dsr_threshold
    and final_result.credibility_score >= args.credibility_threshold
    and held_metrics.n_trades >= 3
)
```
- `dsr` ← `compute_dsr_for_verdict(held_period_returns, n_trials=effective_n_trials, trial_sharpe_variance=trial_sharpe_var)` (`run.py:962-966`).
- `held_period_returns` ← `period_returns_from_trades(held_trades)` (`run.py:921`) where `held_trades` is the **winner replayed on the final-holdout window** (`run.py:904-918`).
- `held_metrics` ← `compute_slice_metrics_from_trades(held_trades, span_days)` (`run.py:920`).
- `final_result.credibility_score` ← the engine's own credibility on the full window (`run.py:904-910` → engine `run_for_search`).

**The structural fact SP-D exploits (the entire safety argument in one sentence):** the gate is a pure function of `winner_params` (the chosen candidate's parameter dict) replayed on the final-holdout window — it reads **nothing** from `_score_for_ranking`, `rank_candidates`, `winner_score`, or `core.ranked`. Ranking selects *which `winner_params`* is taken to the gate; it does not, and structurally cannot, alter the gate computation applied to that `winner_params`.

### §0.2a The DOWNSTREAM gate re-derivation: the SP3/ECR sidecar path (verified — the indirect-leakage surface the make-or-break MUST cover)

`winner_params` does not end at `_run_lab_core`. Verified trace of the **second** place a gate verdict is re-derived from a metric-selected winner:

- `_build_lab_result` (`run.py:1097-1149`) writes `winning_params=core.winner_params`, `dsr=core.dsr`, `credibility_score=core.full_credibility_score`, `verdict=("SURVIVED" if core.survived else "FAILED")` into the frozen `LabResult`; `write_lab_dossier` (`dossier.py:107-114`) persists it as the `.json` sidecar via `r.model_dump_json()`.
- `ops/engine_sdlc/planner.py::_validate_modify` (`planner.py:660-712`) loads that sidecar (`_evidence.load_labresult_sidecar`, `extra="forbid"` model-validate) and **re-rejects** unless `lr.verdict=="SURVIVED"` ∧ `lr.dsr>=0.95` ∧ `lr.credibility_score>=60`, then value-equality-checks `ecr.param_change` against `lr.winning_params`.

**The honest statement of the invariant (corrected — the §0.2 one-liner was true but incomplete):** the declared metric **does** determine *which candidate's* `(dsr, credibility_score, winning_params, verdict)` is frozen into the sidecar (that is the entire point of pluggable ranking — pick the best graduating candidate under the engine's objective). What it provably **cannot** do is cause a *gate-failing* candidate's params to be promoted: every consumer downstream of ranking (the in-`_run_lab_core` `survived`, AND the ECR `_validate_modify` re-derivation) **re-applies the byte-identical `dsr>=0.95 ∧ cred>=60 ∧ SURVIVED` predicate to whatever `winner_params` ranking selected.** A metric reorders *among the already-graduating*; if ranking elevates a FAILED candidate to `ranked[0]`, `core.survived` is `False`, `verdict=="FAILED"`, and `_validate_modify`'s `lr.verdict != "SURVIVED"` hard-rejects it. The leak surface is therefore **fully covered iff the make-or-break test (§5.2) asserts the partition equality over the SAME quantities the ECR re-reads** — `verdict`/`dsr`/`credibility_score`/`winning_params` on the produced `LabResult`, not only the in-core `survived` bool. §5.2 step 3 is amended accordingly.

### §0.3 The contract layer & precedents (SP-B, MERGED)

- `tpcore/lab/target.py:25-84` — `LabTarget(BaseModel, frozen, extra="forbid", arbitrary_types_allowed)`. Fields: `param_ranges`, `run_for_search`, `load_window_context`, `run_with_context`, `default_params`. `model_post_init` (`:46-81`) fail-loud validates `param_ranges` at **declaration time** (the live-money-adjacent-path rationale). **This is the engine-owned Lab contract; SP-B established "engine declares its Lab contract here, never Lab surgery".**
- `ops/lab/run.py:104-171` — `_lab_target_for(engine)`: the single resolver. Returns the engine's `LAB_TARGET`. Hard-rejects non-targetable/undeclared/malformed with a clear `ValueError` (re-raised as `KeyError` by `_LazyParamRanges.__getitem__`, `run.py:184-188`). **SP-D's metric declaration rides this exact resolver — zero new dispatch.**
- `reversion/backtest.py:1110-1121`, `vector/backtest.py:985`, `momentum/backtest.py:557` — the three live `LAB_TARGET = LabTarget(...)` declarations. None declares any scoring metric today (Sharpe is implicit).
- SP-B clockwork idiom (`docs/superpowers/specs/2026-05-19-lab-sp-b-roster-driven-targeting-design.md:101-107`): the consistency guarantee is a **pure runtime-derived consistency test**, NOT a second generated byte-shadow. SP-D follows the same idiom (§5.4).

### §0.4 The characterization oracle (the hardest compat constraint)

`scripts/tests/test_search_parameters_characterization.py:115-130` — `test_rank_candidates_groups_and_sorts()` pins, **byte-exact**, the current Sharpe behaviour: three `TrialResult`s with `SliceMetrics(n_trades=10, sharpe∈{0.5,1.5,0.2}, ...)`, asserts `rank_candidates(...)[0][0] == p1` (mean Sharpe 1.0 > 0.2) and `[0][2] == 2`. **This test must pass byte-unmodified post-SP-D** (it is the strongest available proof the default path is byte-identical). The oracle docstring (`:25-26`, per SP-B §0) sanctions updating monkeypatch targets but the design target is **zero oracle churn** — SP-D achieves it (§5.1).

### §0.5 The SP-E forward need (verified — Sentinel is NOT Lab-wired today)

`grep` of `sentinel/backtest.py` confirms: **no `run_for_search`, no `load_*_window_context`, no `run_*_with_context`, no `LAB_TARGET`.** Sentinel is roster-`lab_targetable` (`PAPER`, `tpcore/engine_profile.py:75`) but cannot run through the Lab until SP-E declares its `LAB_TARGET`. Sentinel's success bar (CLAUDE.md; epic §SP-E) is **maxDD-reduction / ulcer / inverse-ETF-hold**, NOT Sharpe. The only Sentinel-relevant quantity computable from a Lab `SliceMetrics` today is `max_drawdown` (`run.py:370`, the geometric-equity drawdown). **SP-D must make a non-Sharpe primary metric declarable and rankable using only the §0.1 six-field universe (or the period-return series); it must NOT itself wire Sentinel** (that is SP-E — §6).

---

## §1 Verdict — chosen mechanism

### §1.1 Where the primary-metric is declared: a new optional field on `LabTarget`

**Decision:** add ONE optional field to `tpcore/lab/target.py::LabTarget`:

```
primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE
```

where `LabPrimaryMetric` is a new `StrEnum` in `tpcore/lab/target.py` (engine-free, the same module). The default value is `SHARPE` — an engine that does not declare it (reversion/vector/momentum unchanged today) gets exactly today's behaviour, **byte-identical, by construction** (§1.4).

The metric→scalar mapping itself (`SHARPE → f(SliceMetrics)`, `MAXDD_REDUCTION → g(SliceMetrics)`, …) lives in **`ops/lab/run.py`** as a frozen dispatch table (`_RANKING_METRICS`), NOT in `tpcore`. Rationale: the scoring functions read `SliceMetrics`, a `ops/lab/run.py` dataclass; putting the mapping in `tpcore` would either drag `SliceMetrics` into `tpcore` or duplicate it. `LabPrimaryMetric` (the *vocabulary*) is engine-free and tpcore-resident so the engine can name its objective without importing `ops/`; the *implementation* of each objective is Lab-resident (the Lab owns how a `SliceMetrics` becomes a rank score — exactly as it does today for Sharpe). This is the same engine↔tpcore split SP-B used: the engine declares its contract in tpcore-resident types; `ops/lab/run.py` resolves and executes it.

### §1.2 The ranking-vs-gate structural separation (the make-or-break, by construction)

The gate (§0.2) is computed in `_run_lab_core` from `winner_params` replayed on the final-holdout window. SP-D changes **only** `_score_for_ranking` (now metric-parameterized) and `rank_candidates` (now passes the engine's declared metric through). The structural invariant, true *by construction* not merely by test:

> **`_run_lab_core` reads exactly one value out of the ranking subsystem: `winner_params = ranked[0][0]`. `winner_params` is a member of the candidate parameter set `sample_parameters(...)` — a set that is fixed *before* ranking runs and is independent of the metric. The metric only permutes the *order* of `ranked`; the gate function `survived(winner_params)` is then applied to whichever params sit at `ranked[0]`. For SP-D to leak into the verdict, a declared metric would have to change the *contents* of the candidate set or the *gate function itself* — SP-D touches neither.**

Concretely, the separation is enforced by three structural facts SP-D preserves:

1. **The gate function is metric-blind.** `survived` (`run.py:1040-1044`), `compute_dsr_for_verdict`, `compute_slice_metrics_from_trades`, `period_returns_from_trades`, `credibility_score`, `effective_n_trials`/the SP-A ledger — **SP-D edits none of these files/functions.** The diff is fenced to `_score_for_ranking`/`rank_candidates`/`LabTarget`/the dossier. A line-level `git diff --stat` allow-list test (§5.3) reds the build if SP-D touches any gate function.
2. **The metric cannot enlarge or filter the candidate set.** `sample_parameters` (`run.py:222-233`) and the per-window subsample (`run.py:860`) are computed from `(engine, trials, seed)` — never from the metric. Ranking re-orders an already-fixed set; it cannot inject a candidate the gate would reject nor remove one it would pass. Every candidate the gate *would* pass remains in the set regardless of metric; the metric only decides which of the **already-graduating-eligible** is taken first.
3. **`winner_score` is inert.** Nothing downstream of `ranked[0]` reads `winner_score`/`core.ranked` for the verdict (verified §0.1) — they are display/`ranked_alternatives` only. So even the *value* the metric produces never reaches `survived`.

**Scope correction (A12):** the "reads nothing metric-dependent" statement is precise for the *in-`_run_lab_core` gate*. `winner_params` does flow onward into the frozen `LabResult` and the SP3/ECR `_validate_modify` re-derivation (full trace: **§0.2a**). That downstream path also re-applies the byte-identical `verdict==SURVIVED ∧ dsr>=0.95 ∧ cred>=60` predicate to whatever ranking selected, so a gate-FAILED candidate cannot be laundered through it either — but the make-or-break proof must (and now does, §5.2 step 3/5) assert byte-equality over the ECR-re-derived 4-tuple, not only the in-core bool.

The make-or-break test (§5.2) proves this **empirically and non-tautologically**: for a fixed candidate set + fixed final-holdout replay, the ECR-relevant 4-tuple `(LabResult.verdict, dsr, credibility_score, winning_params)` partition over every candidate is **identical** under `SHARPE` vs a deliberately-adversarial `MAXDD_REDUCTION` (or an injected always-`0.0` / always-`NaN` / adversarial-`±1e9` metric); only which candidate sits at `ranked[0]` (hence which 4-tuple is the run headline) changes. The proof is non-tautological because it does not assert "the gate code is unchanged" (that would be circular) — it (a) asserts its own non-vacuity (§5.2 step 0: ≥2 SURVIVE, ≥1 FAIL, SHARPE/MAXDD orders provably invert), then (b) runs the *whole `_run_lab_core`→`_build_lab_result` pipeline twice with different declared metrics and asserts the FAIL-vs-SURVIVE partition AND every ECR-re-derived number is byte-identical** while the winner differs, and (c) drives an adversarial metric through both the in-core gate and `_validate_modify`.

### §1.3 Rejected alternatives

| Where to declare the metric | Verdict | Why rejected / chosen |
| --- | --- | --- |
| **(i) Optional field on `LabTarget` + tpcore `LabPrimaryMetric` enum; mapping table in `ops/lab/run.py`** | **CHOSEN** | Engine owns its Lab contract (SP-B precedent, the Sigma-22-site-drift discipline); engine-free vocabulary in tpcore; Lab-resident implementation (SliceMetrics is Lab-resident); default=`SHARPE` ⇒ byte-identical for the three live engines; SP-E only needs to add an enum member + a mapping fn + declare it on Sentinel's `LAB_TARGET` — no Lab surgery. Frozen-pydantic `extra="forbid"` ⇒ a typo is a declaration-time `ValidationError`, not a silent Sharpe fallback. |
| **(ii) New field on `tpcore.engine_profile._PROFILE`** | rejected | `_PROFILE` is the frozen *roster* SoT (existence/order/cadence/lifecycle), edited only via the Engine Change Request (CLAUDE.md "the Sigma 22-site-drift rule"). A *Lab-only* scoring objective is not roster identity; widening the live-money roster SoT for a Lab-only need is exactly the "widens a frozen live SoT for a Lab-only need" anti-pattern SP-B rejected for the same reason. It would also force every roster ECR to reason about scoring. |
| **(iii) Separate `_RANKING_METRIC_BY_ENGINE` registry dict in `ops/lab/run.py`** | rejected | A second mutable SoT-shaped object keyed by engine name — the parallel-SoT anti-pattern the project explicitly rejects (defect-register ADR, SP-B §rejected). Re-creates the Sigma drift class one file over: a new engine must be added to *two* places (its `LAB_TARGET` and this registry). The whole point of SP-B was that the engine's `LAB_TARGET` is the single declaration. |
| **(iv) A 5th callable on `LabTarget` (`score_fn: Callable[[SliceMetrics], float]`) — engine ships its own scoring function** | rejected | `SliceMetrics` is a `ops/lab/run.py` symbol; an engine shipping a `Callable[[SliceMetrics],float]` either imports `ops.lab.run` (engine→ops dependency — illegal, breaks the engine-free contract layer rationale) or types it as `Callable[[Any],float]` (unvalidated, defeats the frozen-contract fail-loud guarantee). It also lets an engine ship an *arbitrary* ranking function — an unbounded blast radius for a Lab-only knob, and a YAGNI over-generalization vs. the four named objectives the epic enumerates. A **closed enum + a Lab-owned mapping table** is the bounded, auditable, fail-loud form; an open callable is the gold-plated one. |

YAGNI line drawn explicitly: SP-D ships `SHARPE` (the byte-identical default) and `MAXDD_REDUCTION` (the minimum needed so SP-E has a non-Sharpe member to declare and the make-or-break test has a genuinely-different metric to contrast). `ULCER` / `INVERSE_ETF_HOLD` are **enum members reserved with NotImplemented mappings that fail-loud at resolve time** (§4.3) — declared in the vocabulary so the enum is forward-complete and the dossier can name them, but not implemented until an engine actually declares one (SP-E decides Sentinel's exact bar). This avoids speculative scoring code with no caller while keeping the contract forward-stable.

---

## §2 Architecture

### §2.1 The declaration contract (`tpcore/lab/target.py`)

Add to `tpcore/lab/target.py` (engine-free; stdlib + pydantic only — unchanged import surface):

- `class LabPrimaryMetric(StrEnum)` with members `SHARPE = "sharpe"`, `MAXDD_REDUCTION = "maxdd_reduction"`, `ULCER = "ulcer"`, `INVERSE_ETF_HOLD = "inverse_etf_hold"`. `StrEnum` so it serializes cleanly into the dossier JSON and reads as a plain string.
- `LabTarget.primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE` — optional, defaulted. `extra="forbid"` already on the model ⇒ a misspelled metric name in a declaration is a pydantic `ValidationError` at import of `<engine>.backtest`, surfaced through `_lab_target_for`'s existing clear-`ValueError` fence (`run.py:153-170` already converts a malformed `LAB_TARGET` to the clear operator message). No new fail path.

`model_post_init` gains **no new logic** — the enum type already constrains the value; the metric→fn mapping's existence is validated Lab-side at resolve (§4.3) so a tpcore-resident `LabTarget` never needs to know which metrics `ops/lab/run.py` has implemented (keeps the engine-free layer truly engine/Lab-free).

### §2.2 `_score_for_ranking` generalization (`ops/lab/run.py`)

Today's body becomes the `SHARPE` mapping verbatim. The function gains one parameter:

```
def _score_for_ranking(metrics: SliceMetrics, metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE) -> float:
    if metrics.n_trades < 3:
        return -1.0                      # UNCHANGED — statistical-power floor, metric-independent
    return _RANKING_METRICS[metric](metrics)
```

- **The `n_trades < 3 → -1.0` guard stays OUTSIDE the metric dispatch, unchanged.** It is a statistical-power floor on *rankability*, not a Sharpe-specific choice; every metric inherits it identically. (It is also numerically below any sane metric score, so a thin candidate always sorts last regardless of metric — preserved.)
- `_RANKING_METRICS: Mapping[LabPrimaryMetric, Callable[[SliceMetrics], float]]` is a **module-level frozen dict** in `ops/lab/run.py`.
  - `LabPrimaryMetric.SHARPE`: `lambda m: float(m.sharpe) + 0.05 * math.log10(max(m.n_trades, 1))` — **the exact current expression, character-for-character** (§5.1 char-before-refactor proof).
  - `LabPrimaryMetric.MAXDD_REDUCTION`: `lambda m: -float(m.max_drawdown)` — `max_drawdown` is ≤0 by convention (`run.py:370`), so `-max_drawdown` ≥ 0 and **higher = shallower drawdown = better**, consistent with `rank_candidates`' descending sort. (Why this and not Sharpe for a defensive engine: Sentinel's edge is *not losing* in regime crashes; a defensive basket can have a poor Sharpe yet be exactly the candidate you want — ranking it by Sharpe would pick the wrong winner. This is the §0.5 SP-E need, expressible from the existing six-field universe.)
  - `LabPrimaryMetric.ULCER`, `LabPrimaryMetric.INVERSE_ETF_HOLD`: bound to a `_unimplemented_metric` sentinel that raises a clear `ValueError` at resolve (§4.3) — reserved vocabulary, no speculative impl.
- The default arg value preserves every existing `_score_for_ranking(t.holdout)` call should one exist; the only caller is `rank_candidates` (§0.1) which is updated to pass the metric through (§2.3).

### §2.3 `rank_candidates` generalization (`ops/lab/run.py`)

`rank_candidates` gains the engine's declared metric as a parameter and passes it to `_score_for_ranking`:

```
def rank_candidates(trials, metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE) -> list[tuple[dict, float, int]]:
    ...
        scores = [_score_for_ranking(t.holdout, metric) for t in group]
    ...
```

- **Default `metric=SHARPE`** ⇒ the characterization oracle (`test_rank_candidates_groups_and_sorts`, §0.4) calls `sp.rank_candidates([...])` with **no metric arg** and gets byte-identical behaviour — **zero oracle churn** (§5.1). This is the single hardest compat constraint and the defaulted-arg shape is chosen specifically to satisfy it.
- `_run_lab_core` (`run.py:891`) resolves the metric from the already-resolved `LabTarget` and passes it: `ranked = rank_candidates(trials, _lab_target_for(args.engine).primary_metric)`. The resolver is already called multiple times in `_run_lab_core` (`run.py:841-843` via the thin views) and is cheap/idempotent; no new dispatch, no new import.
- The legacy `scripts/search_parameters.py` shim re-exports `rank_candidates` (`scripts/search_parameters.py:50`); the defaulted signature keeps that re-export byte-compatible.

### §2.4 The objective-keyed dossier block (`ops/lab/dossier.py`)

`ops/lab/dossier.py::render_lab_dossier` (`dossier.py:27-55`) renders five fixed sections. SP-D adds an **objective block** without disturbing the SP-C `_next_step` cross-links (`dossier.py:58-75`, untouched) or the SP3 `.json` sidecar (`dossier.py:95`, `r.model_dump_json()` — see below):

- A new line in the rendered "## 1. Verdict" block naming the declared objective: e.g. `**Primary objective:** maxdd_reduction (ranking metric — does NOT affect the gate)`. The parenthetical is mandatory copy: the dossier must state, in operator-facing text, that the metric is ranking-only — the same "the gate is sacred" doctrine SP-C §6 encodes.
- A new "## 2a. Objective-appropriate summary" block keyed off the metric: for `SHARPE` it renders the existing held-metrics table unchanged (byte-identical dossier for the three live engines — §4.1); for `MAXDD_REDUCTION` it surfaces `max_drawdown` (and the period-return-derived shallowest/deepest) as the headline, with Sharpe demoted to a secondary row. The block is a pure function of `LabResult.held_metrics` (already a dict, `models.py:49`) + the declared metric — **no new data, no new query, no gate read.**

**Provenance / SP3 sidecar (HARDENED — the `extra="forbid"` backward-compat blocker):** SP-D adds the field to `tpcore/lab/models.py::LabResult` as **`primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE` — DEFAULTED, not required.** This is mandatory, not stylistic: `LabResult` is `model_config = ConfigDict(frozen=True, extra="forbid")` and `ops/engine_sdlc/_evidence.py::load_labresult_sidecar` does `LabResult.model_validate_json(sidecar.read_text())` on **pre-existing on-disk `.json` sidecars** (verified: `docs/lab/2026-05-18-exp1-SURVIVED-seed7.json` exists today and its key set has **no `primary_metric`**). A *required* (non-defaulted) field would make `model_validate_json` raise `ValidationError` on every pre-SP-D sidecar → `_evidence.EvidenceError` → `planner._validate_modify` would **reject a previously-valid, already-graduated dossier** the moment SP-D merges (a live-adjacent regression: the SP3 MODIFY-ECR gate breaks for any in-flight dossier). With the field **defaulted to `SHARPE`**, `extra="forbid"` is irrelevant to a *missing* key (forbid rejects *unknown* keys, not absent defaulted ones — pydantic v2 fills the default), so old sidecars validate unchanged and resolve to `SHARPE` — semantically exact, since every pre-SP-D run WAS Sharpe-ranked. `_build_lab_result` (`run.py:1097-1149`) sets it explicitly from `_lab_target_for(args.engine).primary_metric` on every new run, so new sidecars carry the true objective; the default only services the read of legacy artifacts. This resolves the §1.1/§2.1-vs-§2.4 contradiction (both the `LabTarget` field AND the `LabResult` field are `= LabPrimaryMetric.SHARPE` defaulted — §8-A11). This is **display/provenance only** — the planner/ECR never reads `primary_metric` for a gate decision (it reads `verdict`/`dsr`/`credibility_score`/`winning_params` — §0.2a — exactly as today); §5.3 pins the ECR-relevant fields byte-unchanged AND §5.5 adds an explicit "a pre-SP-D sidecar with no `primary_metric` key still `model_validate`s and the planner still accepts it" regression test.

### §2.5 The gate-invariance separation (restated as the build-time fence)

The §1.2 by-construction argument is enforced at build time by a **diff-scope allow-list test** (§5.3): SP-D's production diff is allowed to touch ONLY `tpcore/lab/target.py`, `tpcore/lab/models.py` (the single defaulted `LabResult.primary_metric` field, §2.4), `ops/lab/run.py` (`_score_for_ranking`/`rank_candidates`/`_RANKING_METRICS`/`_resolve_ranking_metric` (NEW, the §4.3 pre-spend fence)/the `_build_lab_result` provenance line/the `_run_lab_core` pre-spend `_resolve_ranking_metric` call site at `run.py:~795` (§4.3)/the `rank_candidates` call site at `run.py:891`), and `ops/lab/dossier.py`. It is **forbidden** to touch `compute_dsr_for_verdict`, `compute_slice_metrics_from_trades`, `period_returns_from_trades`, the `survived = (...)` block, the `record_trial_spend`/`cumulative_n_trials` block (`run.py:811-822,930-933`), `tpcore/lab/ledger.py`, `tpcore/backtest/credibility.py`, `tpcore/backtest/overfitting.py`, **AND the §0.2a downstream gate re-derivation `ops/engine_sdlc/planner.py::_validate_modify` + `ops/engine_sdlc/_evidence.py` (the planner must never read `primary_metric` for any decision).** The make-or-break runtime test (§5.2) proves the *behaviour*; this fences the *diff* so a future edit cannot quietly cross the line.

---

## §3 Component / interface breakdown

| Component | Change | Read by | Depends on |
| --- | --- | --- | --- |
| `tpcore/lab/target.py::LabPrimaryMetric` (NEW `StrEnum`) | `SHARPE`/`MAXDD_REDUCTION`/`ULCER`/`INVERSE_ETF_HOLD` | engines (their `LAB_TARGET`), `ops/lab/run.py`, `ops/lab/dossier.py`, `LabResult` | stdlib `enum` only (engine-free preserved) |
| `tpcore/lab/target.py::LabTarget.primary_metric` (NEW field, default `SHARPE`) | one optional frozen field | `_lab_target_for` consumers | `LabPrimaryMetric` |
| `<engine>/backtest.py::LAB_TARGET` | **reversion/vector/momentum UNCHANGED** (omit field ⇒ default `SHARPE`). SP-E adds `primary_metric=MAXDD_REDUCTION` to Sentinel's NEW `LAB_TARGET` — **out of SP-D scope** | `_lab_target_for` | `LabTarget` |
| `ops/lab/run.py::_RANKING_METRICS` (NEW frozen dict) | `LabPrimaryMetric → Callable[[SliceMetrics],float]` | `_score_for_ranking` | `SliceMetrics`, `LabPrimaryMetric` |
| `ops/lab/run.py::_score_for_ranking` (REFACTORED, name+default-call-compat) | gains `metric` param (default `SHARPE`); body = guard + table dispatch; `SHARPE` mapping char-identical | `rank_candidates` | `_RANKING_METRICS` |
| `ops/lab/run.py::rank_candidates` (REFACTORED, name+default-call-compat) | gains `metric` param (default `SHARPE`) | `_run_lab_core`, oracle, `scripts/search_parameters` shim | `_score_for_ranking` |
| `ops/lab/run.py::_run_lab_core` (1 line) | `rank_candidates(trials, _lab_target_for(args.engine).primary_metric)` | — | `_lab_target_for` (existing) |
| `ops/lab/run.py::_build_lab_result` (1 line) | `primary_metric=_lab_target_for(args.engine).primary_metric` on `LabResult` | dossier `.json` sidecar | `_lab_target_for` |
| `tpcore/lab/models.py::LabResult.primary_metric` (NEW field, **default `SHARPE`** — mandatory for pre-SP-D sidecar read-compat, §2.4) | provenance only | dossier render + `.json`, `_evidence.load_labresult_sidecar` | `LabPrimaryMetric` |
| `ops/lab/dossier.py::render_lab_dossier` (objective block) | adds objective line + "## 2a" block; SHARPE path byte-identical | operators | `LabResult.primary_metric`, `held_metrics` |
| `tpcore/tests/test_lab_primary_metric_consistency.py` (NEW clockwork) | every roster-`lab_targetable` engine's declared `primary_metric` resolves in `_RANKING_METRICS` (no unimplemented declared) | CI | `tpcore.engine_profile`, `ops.lab.run` |

**Non-Python shadow check:** none. `_RANKING_METRICS` is not a roster shadow; the engine-set membership is the SP-B clockwork's job (unchanged). SP-D's only clockwork is the metric-implementability test (§5.4).

---

## §4 Edge cases & failure modes

### §4.1 No declared metric ⇒ Sharpe, byte-identical (the backward-compat hard constraint)

`LabTarget.primary_metric` defaults to `SHARPE`. reversion/vector/momentum `LAB_TARGET` declarations are **not edited** (verified they don't reference scoring today — §0.3). `_RANKING_METRICS[SHARPE]` is the current expression character-for-character. `rank_candidates`'s default arg is `SHARPE`. ⇒ Every existing path — the oracle (§0.4), every reversion/vector/momentum Lab run, the `scripts/search_parameters.py` shim, the dossier for those engines — is **byte-identical**. Proven by §5.1 (char-before-refactor) + the unmodified oracle (§5.2).

### §4.2 An engine declares a metric the Lab can't compute from `SliceMetrics`

The §0.1 six-field universe is the hard ceiling. `MAXDD_REDUCTION` is computable (`max_drawdown` is in `SliceMetrics`). A hypothetical objective needing e.g. the full intra-window equity path is **not** expressible — `SliceMetrics` already collapsed that to scalars. **Resolution:** the enum is closed; you cannot declare a metric that has no `_RANKING_METRICS` entry without it being a reserved-but-unimplemented member (§4.3). If SP-E finds Sentinel needs a quantity not in `SliceMetrics` (e.g. true ulcer index needs the equity path), the correct move is **add the scalar to `SliceMetrics`** (additive, ranking-neutral, oracle-neutral — exactly how SP-A2 added `holdout_sharpe_per_period`, `run.py:295-314`) in SP-E's diff, then map the new enum member to it. SP-D explicitly does **not** pre-build that (YAGNI — §6); it documents the seam.

### §4.3 Reserved-but-unimplemented metric declared (`ULCER`/`INVERSE_ETF_HOLD`)

`_RANKING_METRICS[ULCER]` / `[INVERSE_ETF_HOLD]` map to a `_unimplemented_metric(name)` that raises `ValueError(f"LabPrimaryMetric.{name} is a reserved objective with no Lab implementation yet — declare it only when its scoring function ships (SP-E owns the Sentinel bar). See spec 2026-05-20-lab-sp-d §4.3.")`. **Where it fires matters:** the metric is consumed at `rank_candidates` (`run.py:891`) which is **after** `sample_parameters` (`run.py:793`) and therefore **after the SP-A `record_trial_spend`** (verified exact span: `run.py:811-822`, the `_ledger_pool`/`spend_ts = await record_trial_spend(...)` block). A naïve "resolve at rank time" would burn a real cumulative-ledger increment on a run that cannot rank — the SP-B-class "spend then crash" footgun.

**Resolution — pinned insertion point (not "somewhere before"):** SP-D adds a pure, side-effect-free `_resolve_ranking_metric(engine) -> Callable` (it calls the already-idempotent `_lab_target_for(engine)`, reads `.primary_metric`, looks it up in `_RANKING_METRICS`, and raises the clear `ValueError` if the entry is the `_unimplemented_metric` sentinel — it does NOT execute the metric, only proves it is callable/implemented). It is invoked in `_run_lab_core` **between `run.py:794` (the `print(f"  → sampled …")` immediately after `candidates = sample_parameters(...)` at `:793`) and `run.py:808` (the `from tpcore.lab.context import active_credibility_pool` import that opens the SP-A spend block)** — i.e. strictly after candidates exist (so a malformed-`param_ranges` `_lab_target_for` reject still precedes it, unchanged) and strictly before the *first line* of the ledger-spend block. The resolved callable is held in a local and threaded into the later `rank_candidates(trials, metric=…)` call (it is resolved once, not re-resolved at `:891`). `_lab_target_for` is already called for `_runner_for`/`_context_*_for` at `run.py:841-843` (after the spend) — adding ONE earlier idempotent call before the spend is the same cheap lazy import, no new DB, no new dispatch. An engine that declares `ULCER` before its impl ships fails **loud, fast, at `run.py:~795`, before any ledger write** — byte-symmetric to how `_lab_target_for`'s non-targetable reject already fires pre-spend (verified `run.py:104-114` docstring + `test_lab_targeting_consistency.py::test_undeclared_target_hard_rejects_before_any_ledger_spend`). §5.5's pre-spend-reject test asserts `record_trial_spend` was never invoked, mirroring that existing SP-B test verbatim (same `_SharedPool`/`_FakeConn` ledger-spy pattern).

### §4.4 Ties

`rank_candidates`'s `ranked.sort(key=lambda x: x[1], reverse=True)` (`run.py:494`) is Python's stable Timsort. SP-D does not change the sort. Under any metric, equal mean scores preserve insertion order — which is `by_param` dict order = first-seen `json.dumps(params, sort_keys=True)` order, deterministic for a fixed `(engine, trials, seed)`. **Tie behaviour is metric-independent and unchanged.** The make-or-break test (§5.2) uses a candidate set engineered so the SURVIVE/FAIL partition has no ties on the gate-relevant boundary (the partition is what's pinned, not the within-partition order), so a tie cannot make the proof tautological or flaky.

### §4.5 Ranking metric NaN / degenerate

`SliceMetrics.to_dict` (`run.py:303-314`) already coerces non-finite `sharpe`/`profit_factor` to `0.0`; but `_score_for_ranking` reads the raw `SliceMetrics` fields, not `to_dict`. Today `_score_for_ranking` can return `nan` if `m.sharpe` is `nan` (pre-existing, not introduced by SP-D). SP-D's `MAXDD_REDUCTION` reads `m.max_drawdown` which is always finite (`run.py:370`, a `.min()` over a real array; empty-trades path returns `SliceMetrics(0,0.0,0.0,0.0,0.0)` so `n_trades<3 → -1.0` guard catches it first). **SP-D adds a non-finite clamp INSIDE each `_RANKING_METRICS` mapping** (`return v if math.isfinite(v) else -1.0`) so a degenerate metric value sorts a candidate *last* (same semantics as the `n_trades<3` floor) rather than poisoning `np.mean`/the sort with `nan` — and this is *strictly a ranking-robustness improvement that cannot affect the gate* (§1.2: ranking value never reaches `survived`). The `SHARPE` mapping's clamp is byte-justified: the char-before-refactor oracle (§5.1) uses finite Sharpes so the clamp is never exercised on the pinned inputs ⇒ still byte-identical; the clamp only changes behaviour on inputs that were already broken (`nan`) and were never asserted. Documented as a deliberate, gate-invariant, oracle-neutral hardening, not a silent behaviour change.

### §4.6 The SP-E non-Sharpe case (the forward proof obligation)

§0.5 confirmed Sentinel has no `LAB_TARGET` and its bar is maxDD-style. SP-D delivers `MAXDD_REDUCTION` end-to-end (mapping + dossier block + clockwork) so that SP-E's *entire* Lab-side work is: declare `primary_metric=LabPrimaryMetric.MAXDD_REDUCTION` (or a new member if SP-E's brainstorm picks ulcer/inverse-ETF-hold — then SP-E adds that member's mapping, the §4.2 seam) on Sentinel's new `LAB_TARGET`, plus the Sentinel candidate itself. **SP-D's CI proof that this works without SP-E:** the make-or-break test (§5.2) and a focused unit test (§5.5) run the *full ranking path* under `MAXDD_REDUCTION` against a synthetic engine target, proving a non-Sharpe metric ranks correctly and the gate stays byte-identical — the SP-E mechanism is proven by SP-D's own tests before Sentinel exists.

### §4.7 An engine's `LAB_TARGET` declares `primary_metric` AND the run uses `--candidate` (Lab) vs `--engine` (legacy operator) path

Both `amain` (legacy, `candidate=None`) and `run_lab` (Lab) flow through `_run_lab_core` (`run.py:1073`, `:1164`). The metric resolution is in `_run_lab_core` (§2.3) so **both paths get the engine's declared metric identically** — there is no legacy-vs-Lab metric divergence. The H-S2-3 credibility namespacing (`run.py:980-984`) is orthogonal and untouched (ranking ≠ credibility persistence).

---

## §5 Test strategy

### §5.1 Char-before-refactor of the current Sharpe ranking (the byte-identical anchor)

Before any code change, capture a frozen golden: for a fixed list of `SliceMetrics` (including the oracle's exact `n_trades=10, sharpe∈{0.5,1.5,0.2}` triple, an `n_trades=2` thin case, and a high-trade-count case), record `_score_for_ranking(m)` and `rank_candidates([...])` outputs to a committed golden. Post-refactor, the **defaulted** (`SHARPE`, no metric arg) call must reproduce the golden **byte-for-byte**. This is the TDD-RED-first anchor (mirrors SP-C §3 / the Vector pilot C1 discipline): the byte-identical contract is locked before the feature is written.

### §5.2 The make-or-break: byte-identical verdict regardless of declared metric (NON-tautological)

The single most important test. Construction (deliberately non-circular). **The test proves its own non-vacuity** — step 0 asserts the stub actually creates gate/ranking disagreement, so a future careless stub edit ERRORs loudly instead of silently no-opping the proof.

0. **Anti-vacuity preconditions (asserted, not assumed).** Stub = a `LabTarget` whose four callables return a deterministic trade-log keyed by the candidate param dict (mirror `test_lab_targeting_consistency.py::_install_offline` + `_Trade`/`_RR` + the oracle `_FakeRubric`, `test_search_parameters_characterization.py:247+`). Use **one `choice:A,B,C` param** so `sample_parameters` yields a fixed, noise-free 3-set:
   - **A** — final-holdout replay: higher per-period Sharpe, *deep* max-drawdown, `n_trades≥3`, DSR & credibility **PASS** (SURVIVES).
   - **B** — final-holdout replay: lower Sharpe than A, *shallow* max-drawdown, `n_trades≥3`, DSR & credibility **PASS** (SURVIVES). ⇒ `SHARPE` ranks A>B; `MAXDD_REDUCTION` (`-max_drawdown`) ranks B>A — **orders provably invert**.
   - **C** — final-holdout replay engineered to **FAIL** the gate via the metric-blind, numerics-robust lever `held_metrics.n_trades < 3` (NOT a tuned DSR/credibility number), but with a finite *windowed* `holdout` score so C is a real `ranked` member (not pre-killed by the `n_trades<3 → -1.0` ranking floor).
   The test ASSERTS, on `rank_candidates(...)` output directly (a unit precondition before the full run): `winner(SHARPE)==A`, `winner(MAXDD_REDUCTION)==B`, `A!=B`, and that C's final-holdout replay yields `survived==False`. If any precondition is false the test **ERRORs** ("make-or-break stub no longer creates gate/ranking disagreement — proof would be vacuous"), never silently passes.
1. (folded into step 0.)
2. Run the **entire `_run_lab_core` pipeline twice**, then `_build_lab_result` on each result: once `primary_metric=SHARPE`, once `=MAXDD_REDUCTION` (same seed/windows/final-holdout — only the declared metric differs).
3. **Assert (gate-invariance over the ECR-relevant surface — §0.2a, not only the in-core bool):** for every param-set in {A,B,C}, independently drive it as `winner_params` through the gate (`compute_dsr_for_verdict` + `final_result.credibility_score` + `held_metrics.n_trades>=3`) and `_build_lab_result`, and assert the 4-tuple `(LabResult.verdict, LabResult.dsr, LabResult.credibility_score, LabResult.winning_params)` is **byte-identical between the SHARPE and MAXDD_REDUCTION runs** for that set. The SURVIVE/FAIL partition AND every number the downstream ECR `_validate_modify` re-derives (§0.2a) is metric-invariant; only *which* set sits at `ranked[0]` changes.
4. **Assert (pluggability):** the produced `LabResult.winning_params` differs between the two runs (==A vs ==B from step 0).
5. **Assert (sharpest leakage probe — through BOTH gates):** monkeypatch an adversarial `_RANKING_METRICS` entry returning `+1e9` for C and `-1e9` for A/B. Then `ranked[0]==C`, the produced `LabResult` has `verdict=="FAILED"`/`survived==False`/`recommended_exit=="none"`, AND a synthetic `planner._validate_modify` citing that sidecar **hard-rejects** on `lr.verdict != "SURVIVED"` — pluggable ranking cannot push a gate-rejected candidate past the in-core gate OR the downstream ECR gate (the §0.2a path the un-hardened step 5 missed).

Non-tautological because it (a) asserts its own non-vacuity (step 0), (b) asserts runtime equality of the exact ECR-re-derived 4-tuple over the real gate functions across two full pipeline executions, and (c) drives the adversarial probe through BOTH the in-core gate AND `_validate_modify` — it never asserts "the gate source is unchanged" (the §5.3 diff-fence's separate job).

Non-tautological because it asserts a *runtime partition equality over the real gate functions across two real pipeline executions*, plus an adversarial-metric leakage probe — it never asserts "the gate source is unchanged" (that is the §5.3 diff-fence's job, a separate, complementary mechanism).

### §5.3 Diff-scope allow-list fence (the structural guarantee)

A test that asserts SP-D's production changes are confined to the §2.5 allow-list and that the forbidden gate functions (`compute_dsr_for_verdict`, `compute_slice_metrics_from_trades`, `period_returns_from_trades`, the `survived=(...)` expression, `tpcore/lab/ledger.py`, `tpcore/backtest/{credibility,overfitting}.py`, **AND `ops/engine_sdlc/planner.py::_validate_modify` + `ops/engine_sdlc/_evidence.py` — the §0.2a downstream gate re-derivation, also a verdict-bearing surface SP-D must not touch**) are byte-unchanged vs `main` (an AST/source-hash pin on those named functions, in the established style of the SP-A "live-graduation-untouched" guard). The allow-list explicitly **includes `tpcore/lab/models.py`** (the single defaulted `LabResult.primary_metric` field, §2.4) — adding it there is in-scope; `_evidence.py`/`_validate_modify` consuming it is **forbidden** (the planner must keep reading only `verdict`/`dsr`/`credibility_score`/`winning_params`, never `primary_metric`, for any decision). Complements §5.2: §5.2 proves behaviour, §5.3 fences the diff so a *later* PR cannot quietly cross the line.

### §5.4 The metric-implementability clockwork (`tpcore/tests/test_lab_primary_metric_consistency.py`)

Mirrors the SP-B clockwork idiom (pure runtime-derived consistency test, NOT a byte-shadow — §0.3): for every engine in `lab_targetable_engines()` that has declared a `LAB_TARGET`, assert its `primary_metric` has a non-`_unimplemented_metric` entry in `_RANKING_METRICS`. ⇒ the build reds if any engine declares a reserved-unimplemented objective (the §4.3 footgun caught at CI, not at a burned ledger spend). Also asserts `set(LabPrimaryMetric) ⊇ set(_RANKING_METRICS.keys())` (every implemented metric is a declared vocabulary member) and that `SHARPE` is in `_RANKING_METRICS` (the default can never become undeclarable). `xdist_group("ops_shadow")` per the project's ops-package full-suite discipline.

### §5.5 Focused unit tests

- `_score_for_ranking(m, SHARPE)` == the pre-refactor expression for a property-based sample of finite `SliceMetrics` (the char anchor, generalized).
- `_score_for_ranking(m, MAXDD_REDUCTION)` == `-m.max_drawdown` for `n_trades>=3`, `-1.0` for `n_trades<3`, monotone-decreasing in drawdown depth.
- The §4.5 non-finite clamp: a `nan`/`inf` metric value yields `-1.0` and never `nan` in `np.mean`/the sort.
- The §4.3 pre-spend reject: a stub engine declaring `ULCER` raises the clear `ValueError` **before** `record_trial_spend` is called (assert the ledger pool's `record_trial_spend` was never invoked — the SP-B §4.5 ordering proof, mirrored).
- Dossier: `render_lab_dossier` for a `SHARPE` `LabResult` is byte-identical to pre-SP-D (the three live engines' dossier is unchanged); for `MAXDD_REDUCTION` the objective line + "## 2a" block appear and the SP-C `_next_step` block + `.json` sidecar derivation are unchanged.
- **Pre-SP-D sidecar read-compat (the §2.4 blocker, as a forcing regression test):** a `LabResult.model_validate_json` over a frozen fixture that is the *exact key set of an existing pre-SP-D sidecar with NO `primary_metric` key* (use the verified-real shape of `docs/lab/2026-05-18-exp1-SURVIVED-seed7.json` — copied into the test as an inline fixture, NOT read from the live docs/ tree) succeeds, yields `primary_metric == LabPrimaryMetric.SHARPE` (the default), and `ops.engine_sdlc._evidence.load_labresult_sidecar` + a `planner._validate_modify` happy-path over it still ACCEPT (no `EvidenceError`, no regression to the live SP3 MODIFY-ECR gate). This test would RED if `LabResult.primary_metric` were ever made non-defaulted/required — the §2.4 hard constraint, mechanically forced.

### §5.6 Authoritative gate

Full single-process suite + order-flip (the CLAUDE.md ops-package shadow rule: `ops/*.py` ↔ `scripts/ops.py` collision ⇒ never a subset gate), `ruff check .` clean, `python -m tpcore.scripts.check_imports tpcore` green (proves `tpcore/lab/target.py` stayed engine-free — only `enum` added), the characterization oracle (`scripts/tests/test_search_parameters_characterization.py`) **byte-unmodified and green**, `gh pr checks` green. Lane assertion: no data-SDLC file in the diff.

---

## §6 Scope / NON-GOALS

- **The graduation gate is untouched, byte-exact.** `compute_dsr_for_verdict`, `compute_slice_metrics_from_trades`, `period_returns_from_trades`, the `survived` expression, the 0.95/60/3 thresholds — **zero edits** (fenced by §5.3, proven by §5.2).
- **SP-A / credibility / overfitting untouched.** `tpcore/lab/ledger.py`, `tpcore/backtest/credibility.py`, `tpcore/backtest/overfitting.py` — read-only. SP-D adds a ranking layer *beside* the verdict, never inside it.
- **No live-trading-path change.** SP-D touches only Lab ranking/dossier + an engine-free contract field. No engine scheduler/order-manager/plug/`run_all_engines.sh`/`platform_pipeline.py` edit. reversion/vector/momentum behaviour byte-identical (they don't declare the field).
- **NOT SP-E.** SP-D does **not** wire Sentinel, does **not** add Sentinel's `LAB_TARGET`/`run_for_search`/context functions, does **not** decide Sentinel's exact bar (ulcer vs maxDD vs inverse-ETF-hold — SP-E's brainstorm). SP-D delivers `MAXDD_REDUCTION` end-to-end + reserved enum members + the §4.2 "add a scalar to SliceMetrics" seam so SP-E is a thin declare+candidate.
- **YAGNI.** No open `score_fn` callable (rejected §1.3-iv). No `ULCER`/`INVERSE_ETF_HOLD` implementations (reserved, fail-loud §4.3) — built when an engine actually declares one. No per-engine scoring config beyond the single declared primary metric. No new SoT/registry/table/migration/daemon.
- **No new dispatch.** Rides the existing `_lab_target_for` resolver (SP-B). No new CLI flag (the metric is engine-declared, not operator-chosen — making it a `--metric` flag would be exactly the n_trials-shopping / metric-shopping hazard SP-C §1 forbids).

---

## §7 Phasing hint (for writing-plans)

- **T0 — RED anchors.** Commit the §5.1 char-before-refactor golden of `_score_for_ranking`/`rank_candidates` (current Sharpe, no code change yet) + the §5.2 make-or-break test skeleton **including step 0's non-vacuity precondition asserts** authored against the *intended* signatures (RED: signatures don't exist) + the §5.5 pre-SP-D-sidecar regression fixture (RED until §2.4's defaulted field exists). Confirm the existing oracle is GREEN on the unmodified tree (baseline).
- **T1 — tpcore contract.** Add `LabPrimaryMetric` + `LabTarget.primary_metric` (default `SHARPE`) to `tpcore/lab/target.py`. `check_imports tpcore` green (engine-free preserved). reversion/vector/momentum `LAB_TARGET` declarations unedited.
- **T2 — Lab ranking generalization.** Add `_RANKING_METRICS` (`SHARPE` char-identical, `MAXDD_REDUCTION`, reserved fail-loud sentinels + §4.5 clamp). Parameterize `_score_for_ranking`/`rank_candidates` with defaulted `metric`. Char anchor (§5.1) GREEN; oracle byte-unmodified GREEN.
- **T3 — wire the resolved metric + pre-spend fence.** `_run_lab_core`: add `_resolve_ranking_metric` and call it at the **pinned insertion point between `run.py:794` and `run.py:808`** (strictly before the `record_trial_spend` block `:811-822`, §4.3), hold the resolved callable in a local, thread it into `rank_candidates(trials, metric=…)` at `run.py:891`. The make-or-break (§5.2, all of steps 0/3/4/5 incl. the `_validate_modify` adversarial probe) + §4.3 pre-spend-reject test (ledger-spy: `record_trial_spend` never invoked) GREEN.
- **T4 — provenance + dossier.** `LabResult.primary_metric` = **`LabPrimaryMetric.SHARPE` defaulted** (§2.4 — mandatory for pre-SP-D sidecar read-compat) + `_build_lab_result` line; `dossier.py` objective line + "## 2a" block (SHARPE byte-identical). §5.5 dossier + **pre-SP-D-sidecar regression** tests GREEN.
- **T5 — clockwork + authoritative gate.** `test_lab_primary_metric_consistency.py` (§5.4); diff-scope fence (§5.3); full-suite+order-flip, ruff, check_imports, oracle, lane assertion, `gh pr checks` (§5.6).

---

## §8 Adversarial hardening record

| # | Attack / risk | Resolution (in-spec ref) |
| --- | --- | --- |
| A1 | **Pluggable ranking leaks into the verdict via a metric that re-ranks a gate-failing candidate into `ranked[0]`.** | §1.2 by-construction: the gate is a pure fn of `winner_params` replayed on the final-holdout window; ranking only chooses *which* member of a metric-independent candidate set is `winner`. §5.2 step 5 makes this the explicit adversarial passing test: an injected metric that maximizes the *failing* candidate ⇒ it becomes `winner` ⇒ `survived==False` ⇒ **not graduated**. The metric reorders; the gate still rejects. |
| A2 | **SP-D edits a gate function "to support a metric" and the make-or-break test still passes (it only tests behaviour, could be gamed).** | §5.3 diff-scope allow-list fence: the gate functions are AST/source-hash-pinned byte-unchanged vs `main`. §5.2 (behaviour) and §5.3 (diff) are *complementary, independent* mechanisms — gaming both is not reachable by a single in-scope edit. |
| A3 | **The make-or-break test is tautological** (asserts gate-code-unchanged, proving nothing about runtime). | §5.2 is explicitly a *runtime SURVIVE/FAIL partition equality across two full pipeline executions with different declared metrics*, plus the A1 adversarial probe and a "winner differs" anti-vacuity assertion. It never asserts source equality — that is §5.3's separate job. |
| A4 | **Reserved metric (`ULCER`) declared ⇒ run spends a real cumulative-ledger increment then crashes** (the SP-B "spend then crash" footgun class). | §4.3: `_resolve_ranking_metric` fires **after `sample_parameters`, before `record_trial_spend`** — fail-loud pre-spend, mirroring SP-B §4.5/§8-A4 ordering. §5.5 pins `record_trial_spend` was never called. §5.4 clockwork reds the build before such a declaration can merge at all. |
| A5 | **`SliceMetrics` can't express a future Sentinel bar (ulcer needs the equity path).** | §4.2: the enum is closed; SP-D ships only `SHARPE`+`MAXDD_REDUCTION`. The documented seam is "add a scalar to `SliceMetrics`" (additive/ranking-neutral/oracle-neutral, the proven SP-A2 `holdout_sharpe_per_period` pattern) in SP-E's diff — SP-D does not pre-build it (YAGNI, §6). |
| A6 | **Oracle churn** (`test_rank_candidates_groups_and_sorts` calls `rank_candidates([...])` with no metric arg). | §2.3: defaulted `metric=SHARPE` on both `_score_for_ranking` and `rank_candidates` ⇒ the no-arg oracle call is byte-identical ⇒ **zero oracle edits**. §5.6 runs the oracle byte-unmodified as a gate. The defaulted-arg shape was chosen *specifically* to satisfy this hardest compat constraint. |
| A7 | **`primary_metric` placed on `_PROFILE`/a registry re-creates Sigma 22-site drift.** | §1.3: rejected (ii)/(iii) explicitly for exactly this reason; chosen (i) puts it on the engine-owned `LabTarget` (the SP-B precedent) — engine add/remove stays a single declaration, no Lab/roster surgery. |
| A8 | **`extra="forbid"` + a misspelled metric ⇒ silent Sharpe fallback** (a typo passes the gate at the wrong objective). | §2.1: `LabPrimaryMetric` is a closed `StrEnum`; a non-member value is a pydantic `ValidationError` at `<engine>.backtest` import, surfaced via `_lab_target_for`'s existing clear-`ValueError` fence — **fail-loud, never a silent fallback**. No new fail path; reuses SP-B's malformed-`LAB_TARGET` plumbing. |
| A9 | **Non-finite metric (`nan` Sharpe) poisons `np.mean`/the sort and indirectly perturbs which candidate is `winner` → gate.** | §4.5: each `_RANKING_METRICS` mapping clamps non-finite → `-1.0` (sorts last, same as the `n_trades<3` floor). This is a *ranking-robustness* fix; per §1.2 the ranking value never reaches `survived`, so even a maximally-adversarial metric value cannot move the gate. Oracle-neutral (pinned inputs are finite) — proven §5.1. |
| A10 | **A `--metric` CLI flag is "more flexible".** | §6 NON-GOAL: an operator-chosen ranking metric is precisely the post-hoc metric-shopping / n_trials-laundering hazard SP-C §1 forbids. The metric is engine-declared (single pre-registered objective), not run-time-selectable. Deliberately omitted. |
| **A11 (ADVERSARIAL, ACCEPTED — real defect)** | **Spec self-contradiction + a live-adjacent regression: §1.1/§2.1 default the `LabTarget.primary_metric` field but §2.4/§3 added `LabResult.primary_metric` with NO default. `LabResult` is `extra="forbid"` and `ops/engine_sdlc/_evidence.py::load_labresult_sidecar` `model_validate_json`s pre-existing on-disk sidecars. A verified-real one (`docs/lab/2026-05-18-exp1-SURVIVED-seed7.json`) has NO `primary_metric` key — a required field would `ValidationError` → `EvidenceError` → `planner._validate_modify` REJECTS a previously-valid graduated dossier the moment SP-D merges.** | **§2.4 rewritten:** `LabResult.primary_metric` is now mandated `= LabPrimaryMetric.SHARPE` defaulted (symmetric with `LabTarget`); pydantic v2 fills the default for a missing key under `extra="forbid"` (forbid rejects *unknown* keys, not absent defaulted ones), so legacy sidecars validate→`SHARPE` (semantically exact: every pre-SP-D run WAS Sharpe-ranked). §3 table row + §5.5 add a **forcing regression test** (inline fixture of the real pre-SP-D sidecar key set; reds if the field is ever made required). §5.3/§2.5 allow-list explicitly includes `tpcore/lab/models.py` and forbids `_evidence.py`/`_validate_modify` edits. Contradiction resolved; no residual. |
| **A12 (ADVERSARIAL, ACCEPTED — under-documented leak surface)** | **The §0.2 by-construction one-liner ("the gate reads nothing metric-dependent") is TRUE for the in-`_run_lab_core` `survived` but INCOMPLETE: `winner_params` flows on into the frozen `LabResult` sidecar, and `ops/engine_sdlc/planner.py::_validate_modify` RE-DERIVES a gate verdict from `lr.verdict`/`lr.dsr`/`lr.credibility_score`/`lr.winning_params` — all metric-influenced (the metric picks WHICH winner). The original §5.2 only asserted the in-core `survived` bool, not this downstream surface, so a leak through the ECR path would not be caught.** | **New §0.2a** documents the full trace and the corrected honest invariant: the metric *does* select which graduating candidate's numbers are frozen (intended), but BOTH the in-core gate AND the ECR `_validate_modify` re-apply byte-identical `verdict==SURVIVED ∧ dsr>=0.95 ∧ cred>=60` to whatever `winner_params` ranking picked — a gate-FAILED candidate cannot be laundered. **§5.2 step 3 amended** to assert byte-equality of the exact ECR-re-derived 4-tuple `(verdict, dsr, credibility_score, winning_params)` across the two metric runs; **§5.2 step 5** drives the adversarial probe through `_validate_modify` too. Not a code defect (the gate genuinely re-validates) — a *proof-coverage* defect, now closed. |
| **A13 (ADVERSARIAL, ACCEPTED — imprecise pre-spend fence)** | **§4.3's "resolve immediately after `sample_parameters` and before `record_trial_spend`" was directionally right but not pinned to verified lines, risking an implementation that places the fence after the spend (the exact SP-B "spend then crash" footgun it claims to prevent).** | **§4.3 rewritten** with the verified spans (`sample_parameters` `run.py:793`, the `print` `:794`, the spend block `:811-822`) and a pinned insertion point: a pure side-effect-free `_resolve_ranking_metric` between `run.py:794` and `run.py:808`, resolved once and threaded into the later `rank_candidates(..., metric=…)` at `:891`. §5.5's pre-spend-reject test mirrors `test_lab_targeting_consistency.py::test_undeclared_target_hard_rejects_before_any_ledger_spend` verbatim (same `_SharedPool` ledger-spy). |
| **A14 (ADVERSARIAL, ACCEPTED — make-or-break could be unconstructable/vacuous)** | **§5.2 asserted the *requirement* (≥2 SURVIVE, ≥1 FAIL, orders invert) but gave no construction recipe; an implementer could write a stub where SHARPE-winner==MAXDD-winner or nothing fails, and both §5.2 and §5.3 would pass green while proving nothing.** | **§5.2 gains step 0:** a concrete 3-set (`choice:A,B,C`) recipe — A (deep DD, survives), B (shallow DD, lower Sharpe, survives — orders provably invert), C (final-holdout `n_trades<3`, the metric-blind fail lever, but finite windowed score so it's a real `ranked` member). The test ASSERTS its own non-vacuity preconditions and ERRORs (not silently passes) if the stub stops creating gate/ranking disagreement. |

### Self-review (placeholders / contradictions / ambiguity / scope)

- Placeholder scan (`TODO`/`TBD`/`???`/`<…>`): none in this spec.
- Contradiction check 1: §4.5's clamp vs §5.1's byte-identical claim — resolved in-line (pinned oracle inputs are finite ⇒ clamp never exercised on them ⇒ still byte-identical; the clamp only changes already-broken `nan` inputs that were never asserted). No residual contradiction.
- Contradiction check 2 (FOUND + FIXED, A11): §1.1/§2.1 defaulted `LabTarget.primary_metric` but §2.4/§3 left `LabResult.primary_metric` non-defaulted — a real self-contradiction AND a live-adjacent sidecar-read regression. Resolved: §2.4 + §3 row now mandate `LabResult.primary_metric = LabPrimaryMetric.SHARPE` (symmetric defaulted), §5.5 forces it with a pre-SP-D-sidecar regression test. No residual contradiction (both fields are defaulted-`SHARPE`; §8-A11).
- Internal-consistency re-check §1↔§2↔§4↔§5↔§6: §0.2a (new) ↔ §1.2 (the by-construction claim is now scoped "in-core gate AND the ECR re-derivation", consistent) ↔ §5.2 step 3/5 (asserts over the ECR 4-tuple, consistent with §0.2a) ↔ §5.3/§2.5 (allow-list/forbidden list now name `tpcore/lab/models.py` in-scope and `_evidence.py`/`_validate_modify` forbidden, consistent with §2.4/§0.2a) ↔ §4.3 (pinned line numbers consistent with §2.5's `run.py:~795` call-site allow-list entry) ↔ §7 phasing (T3 covers the §4.3 pre-spend fence, T4 the §2.4 provenance; unchanged, still consistent). No placeholder/contradiction/ambiguity introduced by the hardening edits.
- Ambiguity check: "where the metric→fn mapping lives" (tpcore vs ops) explicitly resolved §1.1. "Does `primary_metric` reach the planner/ECR" explicitly resolved §0.2a + §2.4 (the metric *selects which* winner's numbers are frozen — intended; the planner re-validates `verdict`/`dsr`/`cred`/`winning_params` and never reads `primary_metric`; pinned §5.3).
- Scope check: SP-E boundary (§4.6, §6) stated three times to prevent scope creep into Sentinel wiring; YAGNI line (§1.3 last para, §6) draws `SHARPE`+`MAXDD_REDUCTION` only, the rest reserved-fail-loud. The hardening added NO new scope (no new feature; tighter proof + a defaulted field-value + a regression test only).

### Epic-SP-D ambiguity resolved

The epic §SP-D says "Sharpe / maxDD-reduction / ulcer / inverse-ETF-hold / …" — ambiguous on **how many** SP-D implements. Resolved (§1.3 YAGNI para, §6): SP-D implements **`SHARPE` (byte-identical default) + `MAXDD_REDUCTION` (the minimum non-Sharpe metric needed for the make-or-break contrast AND the SP-E forward dep)**; `ULCER`/`INVERSE_ETF_HOLD` are **reserved vocabulary members with fail-loud-at-resolve, pre-spend** stubs (forward-stable enum, no speculative scoring code, no burned ledger spend). SP-E owns the final choice of Sentinel's exact bar; SP-D guarantees the mechanism + the §4.2 "add a SliceMetrics scalar" seam so SP-E stays thin.
