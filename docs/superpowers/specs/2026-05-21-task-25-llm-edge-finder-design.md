# Task #25 — Autonomous LLM+quant Edge Finder (Design Spec, v1 scope)

**Status:** DESIGN. Build does not begin until the operator spec-read
gate clears and a plan PR follows.
**Composes with (verbatim):** SP-G thin advisory LLM spec-emitter
(`docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md`;
spec PR #146, build PR #152). Task #25 emits THROUGH SP-G's
`emit_once`, never around it — the fence stack (ledger pre-check,
diff-scope allow-list, gate-override grep, draft-PR-only,
`record_trial_spend` BEFORE PR) is inherited unchanged.
**Predecessor PRs:** SP-A n_trials ledger (#93), SP-B roster-driven
targeting (#131), SP-G build (#152), autonomous Lab criteria (#158),
ECR-MODIFY data-dependencies threading (#210).
**Lane:** heavy (engine-SDLC-adjacent: new autonomous advisory layer
on the Lab graduation rail; new `tpcore/lab/llm_finder/` sub-package;
augments `ops/llm_triage_service.py`; adds a slash-skill).
**Discipline:** brainstorm → expert-harden → spec PR (this doc) →
operator spec-read gate → plan PR → subagent-driven exec → ONE
consolidated review (per `feedback_cut_process_overhead_ship`) →
whole-suite + order-flip → squash-merge.

---

## §1 Motivation

SP-G shipped the **thin emitter**: the LLM proposes one candidate per
operator command; the deterministic gate (SP-A cumulative-DSR-deflated
+ autonomous Lab criteria, PR #158) disposes. What it deliberately
did NOT do: drive a quantitative toolkit, ingest a market snapshot,
run statistical analysis, propose hypotheses grounded in *computed*
evidence rather than rationale-only text.

Per `project_research_llm_edge_discovery` *"⚠ OPERATOR AMBITION
RAISED 2026-05-20"*: the operator wants an LLM that finds edges **on
its own**, driving a real quant toolkit, internalising trading-
environment context from the curated reference set, and operating a
disciplined **data → analysis → idea → Lab → graduation gate** loop.
Operator framing: the reference set teaches (1) the *trading
environment* (market structure / micro-structure / interconnection);
(2) a *repeatable workflow* (collect → analyse → find ideas to
automate). The finder internalises (1) as domain context and operates
(2) as its loop — NOT free-form strategy mining.

**Why v1 is scoped tight.** Per `project_ml_research_track` (the
commissioned-expert verdict, binding): naïve automated edge-search
inflates DSR `n_trials` and manufactures overfit "edges" that die
out-of-sample. An LLM proposing N hypotheses with N tool calls is
that failure mode at scale. v1 therefore inherits SP-G's fence stack
verbatim AND adds:

- v1 toolkit = `statsmodels` + `scipy.stats` ONLY (no `arch`, no
  `scikit-learn`, no `linearmodels`); the named callables in §6 are
  the WHITELIST.
- v1 quota = **3 specs/run × max 1 run/day** (multiplicative with
  SP-G's per-target `EMISSION_QUOTA_PER_TARGET = 20`).
- v1 trigger = operator-command only (`/lab-edge-find`); event-driven
  trigger deferred.
- v1 success criterion (operator-pinned 2026-05-20): **ONE finder-
  emitted candidate reaches PAPER via the standard ECR path** —
  proves the whole graduation loop end-to-end. Not "N candidates",
  not "Sharpe N". One walks the whole path.

---

## §2 Hard constraints (non-negotiable)

Each constraint is binding by construction (a diff that violates it
cannot land) and cited to source.

1. **Cumulative n_trials honesty (SP-A; inherited from SP-G §2.1).**
   Every finder emission writes one `record_trial_spend(...)` row
   UNCONDITIONALLY at emission time, BEFORE the draft PR is opened
   (via SP-G `emit_once` step 5). Source: `project_research_llm_edge_
   discovery` HARD CONSTRAINT clause (b); `tpcore/lab/ledger.py`.

2. **Single pre-registered primary hypothesis per emission.** One
   `ProposedSpec` → one `EmittedSpec` → one ledger row → one draft
   PR. The `EDGE_FINDER_RUN_QUOTA = 3` means up to three SEPARATE
   emissions per run, each independently routed through `emit_once`.
   NEVER a multi-hypothesis grid. Source:
   `docs/superpowers/checklists/lab_candidate_readiness.md` §1; SP-G
   §2.2.

3. **The gate is sacred.** Autonomous Lab criteria (PR #158) +
   SP-A-deflated `n_trials` are unchanged. The finder NEVER modifies
   `_assess_new_engine_signal`, `_assess_improvement`, the
   credibility scorer, the readiness checklist, the ECR mechanism,
   the `_PROFILE` roster, or any engine plug. Source: `2026-05-20-
   autonomous-lab-criteria.md` §3; SP-G §2.3.

4. **Advisory + human-gated only.** Draft PR only (SP-G invariant);
   no `--undraft` code path; the operator is the merge authority.
   Finder NEVER self-issues an ECR — that is operator-only via
   `/ecr`. Source: `project_research_llm_edge_discovery` HARD
   CONSTRAINT clause (a); SP-G §2.4.

5. **Credential-starved + crash-isolated.** Co-task on
   `ops/llm_triage_service.py`; no `ALPACA_*` in env; no `tools`
   payload to the Anthropic SDK (the §6 sandbox is dispatched IN-
   PROCESS by the agent on the LLM's structured request, not by the
   SDK). Source: SP-G §2.5; `feedback_event_driven_not_scheduled`.

6. **Roster-mediated, never roster-mutating.** Reads
   `tpcore.engine_profile.lab_targetable_engines()` and per-engine
   `LAB_TARGET.primary_metric`. NEVER edits `_PROFILE`, `providers.py`,
   or any engine's `backtest.py::LAB_TARGET`. Roster ADD/REMOVE is
   operator-only via `/ecr`; data-feed change is operator-only via
   `/dfcr`. SP-G's `enforce_diff_scope` is the build-time fence.
   Source: SP-G §2.6 + §4.4.

7. **Two-daemon invariant preserved.** Adding the finder co-task
   brings the LLM-triage daemon co-task count to FOUR (data-triage +
   engine-triage + SP-G emitter + Task #25 finder). Still two
   daemons; `tests/test_two_daemon_invariant.py` still passes.
   Source: `2026-05-18-da3-two-daemon-consolidation-design.md`; SP-G
   §4.2.

8. **No network beyond the Anthropic SDK call.** `MarketSnapshot`
   (§4) is assembled from local Postgres reads (`platform.prices_
   daily`, `platform.fundamentals_quarterly`). The LLM cannot fetch
   new data, references, or docs. Source: SP-G §2.5;
   `feedback_use_official_docs` (references staged at spec-time, not
   fetched at runtime).

9. **Toolkit whitelist — `statsmodels` + `scipy.stats` ONLY (v1).**
   §6 callables are the complete v1 surface. Importing anything else
   from the sandbox is a fatal CI error. NO `arch`, NO `sklearn`, NO
   `linearmodels`, NO `pandas-ta`, NO network libs. Source: operator
   decision 2026-05-20; `project_ml_research_track` (low-DOF
   discipline applied to the tool surface).

10. **The LLM's analysis IS counted against n_trials.** Per
    `project_research_llm_edge_discovery` HARD CONSTRAINT clause (b),
    the LLM's exploration is part of the multiple-testing count. v1
    accounting: every emitted spec's `expected_trials` is fed to
    `record_trial_spend` (SP-G). v1 does NOT fold pre-emission
    analysis turns into the ledger directly (analysis is bounded by
    `ANALYSIS_TURN_QUOTA` and is not a Lab probe in the formal
    sense). v2 may reify analysis-into-ledger (§9).

---

## §3 Architecture

### §3.1 Package layout (engine-FREE; sibling to `tpcore/lab/llm_emitter/`)

```
tpcore/lab/llm_finder/
    __init__.py
    models.py            # MarketSnapshot, AnalysisRequest,
                         # AnalysisResult, FinderRun (pydantic v2 frozen)
    tool_sandbox.py      # statsmodels + scipy.stats whitelist dispatcher
    snapshot.py          # MarketSnapshot assembler (pure Postgres read)
    reference_loader.py  # docs/lab_emitter_references/<name>.md → ReferenceExcerpt
    tests/               # unit tests (engine-FREE)
ops/llm_edge_finder.py                       # the agent (Anthropic SDK)
ops/llm_triage_service.py                    # AUGMENTED: add 4th co-task
docs/lab_finder_persona.md                   # persona + PERSONA_VERSION
docs/llm_edge_finder_operator_runbook.md     # operator runbook
docs/lab_emitter_references/                 # SHARED with SP-G
    carver_systematic_trading.md             # SHIPPED (SP-G v1.0)
    chan_algorithmic_trading.md              # SHIPPED (SP-G v1.0)
    dsr_ntrials_discipline.md                # NEW (v1.0, mandatory-always-include)
    market_structure_primer.md               # NEW (v1.0, operator-authored later)
.claude/skills/lab-edge-find/SKILL.md        # operator slash-skill
```

The `tpcore/lab/llm_finder/` layer is engine-FREE: stdlib + pydantic
+ `tpcore.lab.ledger` + `tpcore.engine_profile` + `tpcore.lab.llm_
emitter.*` + tightly-scoped `statsmodels.api` / `scipy.stats` imports
inside `tool_sandbox.py`. The agent lives in `ops/` (Anthropic SDK +
draft-PR machinery). Reference-bundle dir is **shared** with SP-G.

### §3.2 The loop shape (data → analysis → idea → Lab → gate)

```
operator: /lab-edge-find [--reference-bundle <name>] [--target <engine>]
   │
   ▼
Task #25 finder agent (ops/llm_edge_finder.py)
   │  Phase A — DATA ASSEMBLY (deterministic, pre-LLM)
   │    A1 read roster (SP-B) + ledger state (SP-A)
   │    A2 assemble MarketSnapshot (local Postgres; bounded payload)
   │    A3 load reference bundles (dsr_ntrials_discipline ALWAYS)
   │  ─────────────────────────────────────────────────
   │  Phase B — ANALYSIS (LLM-driven, tool-sandboxed)
   │    B1 invoke Anthropic SDK with snapshot + refs + persona
   │    B2 LLM emits structured AnalysisRequest → agent dispatches
   │       in-process via tool_sandbox.dispatch → ToolResult
   │    B3 loop B1↔B2 bounded by ANALYSIS_TURN_QUOTA = 8
   │  ─────────────────────────────────────────────────
   │  Phase C — IDEA EMISSION (compose with SP-G)
   │    C1 LLM emits AnalysisResult.proposed_specs (≤ 3)
   │    C2 for each proposed spec (≤ EDGE_FINDER_RUN_QUOTA = 3):
   │         ops.llm_lab_emitter.emit_once(proposed_spec=..., ...)
   │       (SP-G fences run verbatim: ledger pre-check → EmittedSpec
   │        validate → record_trial_spend → render → enforce_diff_scope
   │        → validate_no_gate_override → gh pr create --draft)
   │    C3 write FinderRun row (run-level provenance, §4.4)
   ▼
(SP-G human-in-the-loop seam: operator hardens §3/§8/§9, undraft PR)
   ▼
SP-C Readiness → ops.lab → autonomous Lab criteria (PR #158) → /ecr
   ▼
engine SDLC: LAB → PAPER
```

Task #25's stop point is C3. Steps after are existing infrastructure
unchanged. The chain is deliberately discontinuous at every gate.

### §3.3 Composition with SP-G (the structural invariant)

Task #25 is a **caller** of `emit_once`; it NEVER reimplements an
SP-G function. If the finder needs a feature SP-G doesn't expose, the
v1 answer is **add it to SP-G**, keeping the fence stack single-
sourced (mirrors the SP-A → SP-G single-source pattern). Concretely
the finder calls into: `ops.llm_lab_emitter.emit_once` (per
emission), `tpcore.engine_profile.lab_targetable_engines()` (Phase
A1), `tpcore.lab.ledger.cumulative_n_trials` (read-only, Phase A1).
Render / diff-scope / gate-override / ledger-spend all happen inside
`emit_once`.

---

## §4 Contracts (pydantic v2, all frozen + `extra="forbid"`)

The LLM sees only these schemas — never raw Postgres rows, repo
paths, or live credentials.

### §4.1 `MarketSnapshot` (Phase A output)

```python
class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot_ts: datetime                          # UTC, tpcore.calendar.now_utc()
    session_date: date                             # XNYS session
    universe: Literal["sp500", "sp1500", "rus3k"]  # v1: sp500 only
    price_window: tuple[PricePanelRow, ...]        # last 252 sessions × ≤500 tickers
    fundamentals: tuple[FundRow, ...]              # latest quarter per ticker
    ledger_state: tuple[LedgerEntry, ...]          # SP-A cumulative per target
    roster: tuple[RosterTarget, ...]               # SP-B lab_targetable_engines()
```

`PricePanelRow` / `FundRow` are stdlib-pydantic shims around existing
`platform.prices_daily` / `platform.fundamentals_quarterly` shapes,
loaded via one parameterised `asyncpg` read each. Total payload
bounded by `MAX_SNAPSHOT_BYTES = 512 KiB` (pydantic validator;
fail-loud on overflow — downsample N or M, never silent truncation).

### §4.2 `AnalysisRequest` (Phase B: LLM → agent)

```python
class AnalysisRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    turn: Annotated[int, Field(ge=1, le=8)]                  # ANALYSIS_TURN_QUOTA
    rationale: Annotated[str, Field(min_length=1, max_length=4_000)]
    tool_calls: tuple[ToolCall, ...]                         # ≤ 4 per turn

class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    callable_name: Literal[
        "OLS", "adfuller", "coint", "ARIMA_1_0_0",
        "spearmanr", "pearsonr", "ttest_1samp",
    ]
    args_json: Annotated[str, Field(max_length=16_000)]
```

The `callable_name` Literal IS the whitelist. Anything else fails
pydantic validation BEFORE the dispatcher. ARIMA order is hard-pinned
to `(1,0,0)` for v1 — the LLM cannot vary order (keeping `n_trials`
honesty intact; order is not Lab-searched).

### §4.3 `AnalysisResult` (Phase B: agent → LLM, then Phase C output)

```python
class AnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    turn: int
    tool_results: tuple[ToolResult, ...]
    proposed_specs: tuple[ProposedSpec, ...]       # ≤ EDGE_FINDER_RUN_QUOTA = 3
    finder_rationale: Annotated[str, Field(max_length=8_000)]

class ProposedSpec(BaseModel):
    """Upstream of SP-G EmittedSpec; emit_once consumes via thin adapter."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]+$")]
    target_engine: str                              # MUST ∈ MarketSnapshot.roster
    intent: Literal["fold_existing", "promote_new"]
    primary_hypothesis: str
    primary_metric: LabPrimaryMetric
    param_ranges: dict[str, tuple]                  # SP-G validates downstream
    rationale: str                                  # CITES analysis evidence + refs
    falsification_criterion: str
    expected_trials: int
    analysis_evidence_refs: tuple[int, ...]         # indices into tool_results
```

### §4.4 `FinderRun` (run-level provenance, the audit trail)

```python
class FinderRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: UUID
    started_ts: datetime
    completed_ts: datetime | None
    snapshot_session_date: date
    persona_version: str
    reference_bundle: str
    analysis_turn_count: int                       # ≤ ANALYSIS_TURN_QUOTA
    proposed_spec_count: int                       # ≤ EDGE_FINDER_RUN_QUOTA
    emitted_pr_urls: tuple[str, ...]
    rejection_reason: str | None
```

Persisted as one append-only row in `platform.data_quality_log` under
`lab_edge_finder_run.<session_date>` (disjoint from
`lab_trial_ledger.*`; reuses SP-A substrate — no migration). Per-
emission ledger rows are SP-G's existing `lab_trial_ledger.<target>`.

---

## §5 Safety posture

Every v1 fence, cited to source.

| # | Fence | Mechanism | Source |
| --- | --- | --- | --- |
| 1 | SP-A cumulative n_trials honesty | `record_trial_spend` BEFORE draft PR (SP-G `emit_once` step 5) | SP-G §2.1; `tpcore/lab/ledger.py` |
| 2 | Single hypothesis per emission | SP-G `EmittedSpec` pydantic | SP-G `models.py`; Readiness §1 |
| 3 | Gate sacred | Autonomous Lab criteria (PR #158) unchanged | `2026-05-20-autonomous-lab-criteria.md` §3 |
| 4 | Advisory + human-gated | Draft PR only; no `--undraft` code path | SP-G §4.3 |
| 5 | Credential-starved | No `ALPACA_*` in env; CI fence has no `ANTHROPIC_API_KEY` | SP-G §2.5 |
| 6 | Crash-isolated | 4th `_run_supervised` co-task; two-daemon invariant preserved | SP-G §4.2 |
| 7 | Roster-mediated, never mutating | Reads `lab_targetable_engines()`; SP-G diff-scope reds over-broad PR | SP-G §4.4 + §2.6 |
| 8 | No network beyond Anthropic SDK | No `tools` payload; snapshot is local Postgres | SP-G §2.5 |
| 9 | Toolkit whitelist | `ToolCall.callable_name: Literal[...]`; in-process attribute-allowlist | §6; `project_ml_research_track` |
| 10 | Run-level quota | `EDGE_FINDER_RUN_QUOTA = 3`, `ANALYSIS_TURN_QUOTA = 8`, ≤ 1 run/day | §3.2, §9 |
| 11 | Per-target quota | SP-G `EMISSION_QUOTA_PER_TARGET = 20` (multiplicative) | SP-G `ledger_gate.py` |
| 12 | Diff-scope allow-list | SP-G `enforce_diff_scope` | SP-G §4.4 |
| 13 | Gate-override grep | SP-G `validate_no_gate_override` | SP-G `emitter.py::GATE_OVERRIDE_FORBIDDEN_FLAGS` |

Every constraint is enforced at build/runtime (pydantic validator,
diff-scope test, sentinel grep) — none rely on the LLM "respecting"
the rule.

---

## §6 Tool-sandbox (`statsmodels` + `scipy.stats` whitelist)

### §6.1 The whitelist (v1)

| `callable_name` | Resolves to | Use |
| --- | --- | --- |
| `OLS` | `statsmodels.api.OLS(...).fit()` | Regression / factor exposure |
| `adfuller` | `statsmodels.tsa.stattools.adfuller` | Stationarity (mean-reversion screen) |
| `coint` | `statsmodels.tsa.stattools.coint` | Cointegration (pairs/baskets; Chan ch. 7) |
| `ARIMA_1_0_0` | `statsmodels.tsa.arima.model.ARIMA(order=(1,0,0)).fit()` | AR(1) on returns — bounded order |
| `spearmanr` | `scipy.stats.spearmanr` | Rank correlation (factor IC) |
| `pearsonr` | `scipy.stats.pearsonr` | Linear correlation |
| `ttest_1samp` | `scipy.stats.ttest_1samp` | Mean ≠ 0 (Sharpe-significance proxy) |

Nothing else in v1. v1.5+ extends by ADDING rows; v1.5 is a separate
spec → plan → build cycle.

### §6.2 Dispatcher shape

`tool_sandbox.dispatch(call: ToolCall, snapshot: MarketSnapshot) ->
ToolResult` — pure-Python switch on `call.callable_name`. Each branch:

1. JSON-decodes `args_json` into a per-callable pydantic `Args`
   model (frozen + `extra="forbid"`; e.g. `OLSArgs(y_series_id: str,
   x_series_ids: tuple[str, ...])`).
2. Resolves series from `snapshot` BY ID against a fixed column
   whitelist (`adj_close`, `log_return`, `vol_20d`, ...) — no path
   traversal, no `eval`, no `exec`.
3. Calls the resolved callable; wraps in `try/except`; any exception
   becomes `ToolResult.error` with exception-type name only (no
   traceback, no payload echo).
4. Returns `ToolResult(numeric_summary: NumericSummary)` — bounded
   shape (`coefficients`, `pvalues`, `statistic`, `summary_text` ≤
   4 KiB). NEVER raw numpy arrays.

**In-process attribute-allowlist (v1).** Imports ONLY the named
callables at module top. No `importlib`, no `__import__`, no
`getattr(stats, name)`. A CI test (`test_tool_sandbox_no_dynamic_
import.py`) greps the module source for `importlib`, `__import__`,
`getattr(.*, name)`, `eval`, `exec`, `subprocess`, `os.system`,
`socket`; reds the build on any hit.

**Subprocess v2.** When the v2 surface grows (e.g. `arch` GARCH), the
v2 spec moves dispatch to subprocess (`subprocess.run([sys.
executable, "-m", "tpcore.lab.llm_finder.tool_runner", ...],
timeout=N, env=scrubbed_env)` reading `ToolCall` from stdin / writing
`ToolResult` to stdout). v1 stays in-process — surface is small
enough that attribute-allowlist is strictly cheaper.

### §6.3 Determinism

v1 callables are deterministic given inputs. The dispatcher pins
`numpy.random.seed(0)` at the top of `dispatch()` (belt-and-braces).

---

## §7 Reference-bundle system

### §7.1 The four v1 bundles

| Bundle | Status | Authored by | Purpose |
| --- | --- | --- | --- |
| `carver_systematic_trading.md` | SHIPPED (SP-G v1.0) | expert subagent at SP-G brainstorm | Carver's anti-overfit framing, forecast scaling, diversification multiplier, vol targeting |
| `chan_algorithmic_trading.md` | SHIPPED (SP-G v1.0) | expert subagent at SP-G brainstorm | Mean-reversion (pairs/cointegration), momentum, factor; + overfit failure-modes |
| `dsr_ntrials_discipline.md` | NEW (v1.0), **mandatory always-include** | expert subagent at task #25 brainstorm | `project_ml_research_track` verdict operationalised: every emission is an `n_trials` increment; the gate is cumulatively deflated; the LLM cannot relax. Most-load-bearing of the four. |
| `market_structure_primer.md` | NEW (v1.0), operator-authored later | operator (expert-stub at brainstorm if delegated) | The (1)-half of operator framing: market structure / micro-structure / interconnection |

**Authoring delegated to expert subagent at brainstorm time** per
`feedback_stop_over_asking_use_expert`: this spec NAMES the bundles;
the brainstorm/plan PR drives the subagent that writes them. The two
SP-G bundles already exist (the precedent confirms the pattern).
`dsr_ntrials_discipline.md` is the new MANDATORY bundle and is the
most-load-bearing — the dispatcher includes it in every
`ReferenceExcerpt` tuple regardless of `--reference-bundle`
selection.

### §7.2 Selection mechanism (mirrors SP-G Q3)

```
/lab-edge-find [--reference-bundle <name>] [...]
```

`<name>` is a stem under `docs/lab_emitter_references/`. The agent
loads the named bundle PLUS the mandatory `dsr_ntrials_discipline.md`
PLUS the persona-default (v1 default: `carver_systematic_trading`).
Bundles compose; the LLM sees a tuple of `ReferenceExcerpt` instances
per SP-G's existing `EmissionContext` shape (reused unchanged).

### §7.3 Bundle authoring constraints

- One markdown file per bundle under `docs/lab_emitter_references/`,
  max 64 KB (matches SP-G `ReferenceExcerpt.text` `max_length=
  64_000`).
- Authoring delegated to an expert subagent: read the source
  (Carver/Chan PDFs per `feedback_use_official_docs`, or internalise
  `project_ml_research_track` for DSR), produce a teaching artefact
  with the formulas / decision rules a finder needs.
- Operator-staged at plan-PR time — the LLM cannot fetch new
  references at runtime (§2.8).

---

## §8 The graduation path (the explicit walk-through)

The journey from `/lab-edge-find` to PAPER (Task #25 owns steps 1–4;
everything after is existing infrastructure):

1. **Operator runs** `/lab-edge-find --reference-bundle <name>`.
2. **Finder Phase A** reads roster + ledger; assembles snapshot;
   loads bundles; writes a `FinderRun` row.
3. **Finder Phase B** invokes Anthropic SDK with snapshot + refs +
   persona; LLM emits `AnalysisRequest`s; agent dispatches
   `tool_sandbox.dispatch` in-process; results returned. Loop
   bounded by `ANALYSIS_TURN_QUOTA = 8`.
4. **Finder Phase C** receives `AnalysisResult` with ≤ 3
   `ProposedSpec`s. For each, calls `ops.llm_lab_emitter.emit_once`.
   SP-G fence stack runs verbatim (ledger_gate → EmittedSpec
   validate → `record_trial_spend` → render → `enforce_diff_scope` →
   `validate_no_gate_override` → `gh pr create --draft`).
5. **Operator review** (SP-G §3.5 human-in-the-loop): hardens §3
   byte-identical, §8 data prereqs, §9 lookahead. Undrafts PR.
6. **SP-C Readiness** — mechanical pre-flight (exactly one
   PARAM_RANGES toggle, live path files untouched, golden present).
7. **`python -m ops.lab --candidate ... --target-engine ... --intent
   ...`** — SP-B dispatch, SP-D ranking, SP-A-deflated gate. Dossier
   lands at `docs/lab/<date>-<name>-{SURVIVED|FAILED}-seed*.json`.
8. **Autonomous Lab criteria adjudication (PR #158).**
   - `promote_new`: `_assess_new_engine_signal(dossier)` evaluates
     positive Sharpe ∧ trades ≥ 10 ∧ MaxDD ≥ −0.50 ∧ ruin ≤ 0.30 ∧
     profit_factor ≥ 1.0 ∧ min_btl_gap ≤ 365.
   - `fold_existing`: `_assess_improvement(...)` evaluates
     candidate-beats-incumbent (strict on `primary_metric`) ∧
     new-engine-floor ∧ trade-count-drift-bounded.
9. **Operator opens ECR** via `/ecr` (ADD or MODIFY). PR #210 threads
   `data_dependencies` through MODIFY. Planner re-derives gate from
   dossier sidecar; never trusts text.
10. **Engine SDLC: LAB → PAPER** — deterministic, automated post-
    ECR. **v1 success criterion satisfied** when ONE finder-emitted
    candidate reaches this state.

---

## §9 Out of scope

### §9.1 Deferred to the plan PR (the brief's 7 implementation-decisions)

1. **`ANALYSIS_TURN_QUOTA` default** — spec uses 8 as a working
   number; plan PR pins.
2. **`MAX_SNAPSHOT_BYTES` default** — spec uses 512 KiB; plan PR
   pins.
3. **Universe scope** — spec restricts v1 to `sp500`; plan PR
   confirms (or narrows).
4. **`series_id` column whitelist** for `tool_sandbox` — spec names
   a starter set; plan PR pins the complete v1 set.
5. **`FinderRun` source-namespace string** —
   `lab_edge_finder_run.<session_date>`; plan PR refines for grep-
   ability.
6. **Persona SHA-pinning** — spec mandates `PERSONA_VERSION` constant
   mirroring SP-G; plan PR fixes location and CI sentinel.
7. **Slash-skill exact filename** — `.claude/skills/lab-edge-find/
   SKILL.md` (SP-G precedent); plan PR confirms.

### §9.2 v1.5+ — Event-driven trigger (`LAB_LEDGER_CAPACITY_AVAILABLE`); larger universe (`sp1500`, `rus3k`); operator-authored `market_structure_primer.md` enrichment.

### §9.3 v2.0 — Subprocess tool-sandbox; add `arch` GARCH + `linearmodels` to whitelist.

### §9.4 v2.5 — Cross-engine combiner framing (`project_ml_research_track` defensible-use 2): finder proposes combiner WEIGHTS as a distinct hypothesis shape; separate spec.

### §9.5 v3.0 — Meta-labeling framing (`project_ml_research_track` defensible-use 1): fixed-hyperparam `scikit-learn` shallow classifiers as upstream guards in `lifecycle_analysis` (NOT a finder hypothesis; folded into engine plugs).

### §9.6 v3.5 — Diversification memory: the finder remembers prior hypotheses to avoid re-proposing; bounded, audit-able, still n_trials-fenced.

### §9.7 Permanently out of scope

- Auto-merge / auto-undraft / auto-ECR / auto-promote — every gate
  in §8 is a deliberate operator hand-on-the-wheel per
  `project_research_llm_edge_discovery` HARD CONSTRAINT clause (a).
- Live-capital signal generation — the finder produces Lab specs,
  never live signals.
- LLM network access — `MarketSnapshot` is the only data the LLM
  sees, always agent-assembled from local Postgres.
- Multi-hypothesis emission per ledger row.
- Modifying autonomous Lab criteria (`ops/engine_sdlc/lab_criteria.
  py`).

---

## §10 Test plan

### §10.1 Unit (`tpcore/lab/llm_finder/tests/`)

- `test_models_frozen.py` — all four models frozen + `extra="forbid"`; malformed input raises `ValidationError`.
- `test_snapshot_assembler.py` — synthetic Postgres rows → bounded `MarketSnapshot`; `MAX_SNAPSHOT_BYTES` overflow fail-loud.
- `test_tool_sandbox_whitelist.py` — `ToolCall.callable_name` outside Literal raises `ValidationError` BEFORE dispatcher.
- `test_tool_sandbox_no_dynamic_import.py` — grep `tool_sandbox.py` for `importlib`, `__import__`, `getattr(.*, name)`, `eval`, `exec`, `subprocess`, `os.system`, `socket`; reds on any hit.
- `test_tool_sandbox_determinism.py` — same inputs → same `ToolResult` byte-for-byte.
- `test_reference_loader_bundles.py` — four named bundles load; `dsr_ntrials_discipline.md` ALWAYS included regardless of `--reference-bundle`.

### §10.2 Integration (`ops/tests/`, mocked Anthropic)

- `test_llm_edge_finder_round_trip.py` — synthetic `AnalysisResult` with 1 `ProposedSpec` round-trips through `emit_once`; rendered spec validates against frozen golden.
- `test_llm_edge_finder_composes_with_sp_g.py` — CI grep asserts `ops/llm_edge_finder.py` imports `emit_once` and does NOT re-define `record_trial_spend`, `render_candidate_spec`, `enforce_diff_scope`, or `validate_no_gate_override`.
- `test_llm_edge_finder_quota.py` — `EDGE_FINDER_RUN_QUOTA = 3` enforced (synthetic 4-spec response truncated to 3 with loud warning; 4th never emitted).
- `test_four_cotask_invariant.py` — `ops/llm_triage_service.py` runs 4 crash-isolated co-tasks; the two-daemon invariant test still passes.
- `test_persona_versioned.py` — persona edit without `PERSONA_VERSION` bump reds the build.

### §10.3 Safety (the make-or-break)

- `test_finder_cannot_bypass_sp_g.py` — CI grep over `ops/llm_edge_finder.py`: NO `gh pr create` invocation outside `emit_once`.
- `test_finder_cannot_import_non_whitelisted.py` — CI grep of `tool_sandbox.py`: NO import of `arch`, `sklearn`, `scikit_learn`, `linearmodels`, `pandas_ta`, `requests`, `urllib`, `http`, `socket`.
- `test_finder_cannot_write_to_db.py` — finder process has read-only Postgres role; sentinel attempts a write and expects `InsufficientPrivilege`. ONLY writes are SP-G `record_trial_spend` (its own role) and the `FinderRun` row (structured append-only event).
- `test_finder_diff_scope_inherits_sp_g.py` — finder's draft PRs pass through SP-G's `enforce_diff_scope` (no `tpcore/`, no `ops/` non-sidecar, etc.).

### §10.4 Lane discipline

All new tests under `tpcore/tests/` and `ops/tests/` that import
`ops.llm_triage_service` or touch `sys.modules['ops']` carry
`pytestmark = pytest.mark.xdist_group("ops_shadow")` (per
`feedback_ops_package_shadow_full_suite_gate`).

### §10.5 The single load-bearing E2E proof (v1 success criterion, mock-driven)

`ops/tests/test_llm_edge_finder_to_paper.py` — mocks the Anthropic
boundary + `ops.lab` dispatch + ECR flow; demonstrates a finder-
emitted `ProposedSpec` walks ALL TEN steps in §8 and lands a synthetic
engine PAPER. The discipline check for v1 success criterion (the
real-data version runs once at v1 GA-time per §12).

---

## §11 Lane — heavy

Per `docs/DEV_PIPELINE_STANDARD.md` §0: new autonomous advisory
mechanism on the Lab graduation rail; touches `ops/llm_triage_
service.py`; adds a new `tpcore/lab/` sub-package; augments the
operator-visible slash-skill set; introduces a new statistical-tool
sandbox surface. Full §1 pipeline applies.

---

## §12 v1 success criterion (operator-pinned 2026-05-20)

**ONE finder-emitted candidate reaches PAPER via the standard ECR
path.** Concretely:

1. Operator runs `/lab-edge-find`;
2. The finder emits a `ProposedSpec` via SP-G `emit_once`;
3. The draft PR is opened, operator hardens §3/§8/§9, undrafts;
4. `ops.lab` runs the candidate through the SP-A-deflated gate;
5. The dossier clears autonomous Lab criteria (PR #158);
6. Operator opens an ECR; SDLC moves the engine to PAPER.

Until that ONE candidate completes step 6, v1 is incomplete. After
that ONE candidate, v1 is shipped — irrespective of how the LLM
performs on subsequent runs (subsequent runs are v1.5+ territory).
v1 is a proof of the LOOP, not a proof of LLM quality.

---

## §13 Phasing roadmap

| Version | Scope | Status |
| --- | --- | --- |
| **v1.0** | This spec: operator-command finder; `statsmodels` + `scipy.stats` whitelist; 3 specs/run × 1 run/day; 4 reference bundles; ONE-candidate-to-PAPER success bar | THIS SPEC (DESIGN) |
| **v1.5** | Event-driven trigger (`LAB_LEDGER_CAPACITY_AVAILABLE`); operator-authored `market_structure_primer.md` enrichment; sp1500/rus3k universe | Deferred |
| **v2.0** | Subprocess tool-sandbox; `arch` GARCH + `linearmodels` panel | Deferred |
| **v2.5** | Cross-engine combiner framing (`project_ml_research_track` defensible-use 2) | Deferred |
| **v3.0** | Meta-labeling framing (defensible-use 1) — `scikit-learn` shallow classifiers as upstream `lifecycle_analysis` guards (NOT a finder hypothesis) | Deferred |
| **v3.5** | Diversification memory — bounded, audit-able, n_trials-fenced | Deferred |

Every phase inherits §2 verbatim. No phase relaxes the gate.

---

## §14 Cross-references

**Specs.** SP-G design (predecessor):
`docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md`.
SP-A: `2026-05-19-lab-ntrials-ledger.md`. SP-B:
`2026-05-19-lab-sp-b-roster-driven-targeting-design.md`. SP-D:
`2026-05-20-lab-sp-d-pluggable-scoring-design.md`. Autonomous Lab
criteria: `2026-05-20-autonomous-lab-criteria.md`. Lab front-half
epic: `2026-05-19-lab-front-half-epic.md`. DA-3 two-daemon:
`2026-05-18-da3-two-daemon-consolidation-design.md`.

**PRs.** SP-A build **#93**, SP-B **#131**, SP-G design **#146**,
SP-G build **#152**, autonomous Lab criteria **#158**, ECR-MODIFY
data-dependencies **#210**.

**Checklists.** `docs/superpowers/checklists/lab_candidate_readiness.md`
(SP-C); `docs/superpowers/checklists/engine_change_request.md`.

**SP-G shipped code (Task #25 composes with).**
`tpcore/lab/llm_emitter/{models.py, emitter.py, ledger_gate.py,
diff_fence.py}`; `tpcore/lab/ledger.py`; `ops/llm_lab_emitter.py`
(`emit_once`); `ops/llm_triage_service.py` (co-task host).

**Memory (cited by name).** `project_research_llm_edge_discovery`
(HARD CONSTRAINT + ⚠ OPERATOR AMBITION RAISED 2026-05-20 + reference-
set framing); `project_ml_research_track` (commissioned-expert
verdict on n_trials inflation); `ref_carver_systematic_trading`;
`ref_chan_algorithmic_trading`; `feedback_event_driven_not_scheduled`
(co-task pattern); `feedback_stop_over_asking_use_expert`
(delegate reference authoring to subagent); `feedback_use_official_docs`
(stage references at spec time); `feedback_ops_package_shadow_full_
suite_gate` (`xdist_group("ops_shadow")` rule); `feedback_cut_
process_overhead_ship` (one consolidated review); `feedback_symmetry_
not_copy` (compose with SP-G, don't mirror).

**Lane standard.** `docs/DEV_PIPELINE_STANDARD.md` §0/§1/§2/§3.

**CLAUDE.md.** Universal invariants (paper-only, SIP default, no
yfinance/Discord/manual); engine roster changes → `/ecr`; hard
safety invariant DATA_OPERATIONS_COMPLETE; engine-build compliance
shortlist.
