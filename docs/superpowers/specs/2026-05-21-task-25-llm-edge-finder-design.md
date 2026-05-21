# Task #25 — Autonomous LLM+quant Edge Finder (Design Spec, **Path B v1.0**)

**Status:** DESIGN — Path B (true end-to-end autonomy). Operator chose
Path B 2026-05-21 after surfacing that the prior Path A spec
deliberately throttled AI capability by gate-keeping every step. This
spec REPLACES the Path A v1 spec (PR #213). Build does not begin
until the operator spec-read gate clears and a plan PR follows.

**Composes with (verbatim):** SP-G thin advisory LLM spec-emitter
(`docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md`;
spec PR #146, build PR #152). Task #25 emits through SP-G's
`emit_once` for the operator-driven lane AND through a new parallel
entry `emit_once_with_auto_promote` for the autonomous lane. The
SP-G fence stack (ledger pre-check, diff-scope allow-list, gate-
override grep, `record_trial_spend` BEFORE PR) is inherited unchanged
on both lanes. The autonomous lane ADDS auto-undraft + auto-merge +
auto-ECR + auto-retire on TOP of the SP-G fences; it does not bypass
them.

**Predecessor PRs:** SP-A n_trials ledger (#93), SP-B roster-driven
targeting (#131), SP-G build (#152), autonomous Lab criteria (#158),
ECR-MODIFY data-dependencies threading (#210), Path A v1 spec (#213,
superseded).

**Expert review folded:** all 3 BLOCKING + 4 HIGH + 6 MEDIUM + 2 LOW
findings from `docs/superpowers/reviews/2026-05-21-task-25-spec-
review.md` are folded IN THIS PR (zero deferred).

**Lane:** heavy (engine-SDLC-adjacent; new autonomous finder layer on
the Lab graduation rail; new `tpcore/lab/llm_finder/` sub-package;
augments `ops/llm_triage_service.py` with TWO new co-tasks — finder
+ live-paper outcome monitor; adds the auto-promote SP-G extension;
adds an auto-retire ECR path; adds a read-only finder-audit dashboard
renderer; adds a slash-skill).

**Discipline:** brainstorm → expert-harden → spec PR (this doc) →
operator spec-read gate → plan PR → subagent-driven exec → ONE
consolidated review (per `feedback_cut_process_overhead_ship`) →
whole-suite + order-flip → squash-merge.

---

## §1 Motivation + success criterion

### §1.1 The Path A → B reversal (operator decision 2026-05-21)

SP-G shipped the **thin emitter**: the LLM proposes one candidate per
operator command; the deterministic gate (SP-A cumulative-DSR-deflated
+ autonomous Lab criteria, PR #158) disposes. Per
`project_research_llm_edge_discovery` *"⚠ OPERATOR AMBITION RAISED
2026-05-20"*, the operator wants an LLM that finds edges **on its
own**, driving a real quantitative toolkit and operating a disciplined
data → analysis → idea → Lab → graduation loop.

The Path A v1 spec (PR #213) honored the AMBITION RAISED in surface
shape (toolkit, snapshot, persona, reference bundles) but **kept the
HARD CONSTRAINT clause (a) "Advisory + human-gated only"** —
operator-undrafts each PR, operator opens each ECR, operator never
delegates the merge or retire decision. **2026-05-21 the operator
explicitly reversed clause (a):** *"the finder finds AND automates
AND monitors AND retires; I become the auditor of OUTCOMES, not the
gate-keeper of EACH STEP."* This spec is the canonical instantiation
of that reversal.

### §1.2 Two-tier success ladder (replaces Path A single-criterion)

The Path A success bar was "ONE finder-emitted candidate reaches
PAPER" — a **reach** criterion. Operator binding 2026-05-21:
*"the outcome is edges that trade and make money. PAPER-reach is
necessary-not-sufficient."* v1 carries a two-tier ladder:

**Tier 1 — Reach criterion (necessary, not sufficient).** Finder-
emitted candidate passes (a) SP-A-deflated DSR / autonomous Lab
criteria gate; (b) SP-G fence stack; (c) readiness gate; (d) lands
in PAPER via the standard or finder-ADD ECR machine path.

**Tier 2 — Outcome criterion (BINDING success bar).** A finder-emitted
PAPER engine produces sustained positive risk-adjusted P&L over the
pre-registered outcome window:

- **Window:** 30 NYSE sessions of continuous PAPER dispatch
  (60-session enrichment is v1.5).
- **P&L floor:** rolling 30-session **net-of-cost Sharpe ≥ 0.5**
  (Newey-West HAC-adjusted SE; t-stat-on-Sharpe must clear 1.65
  one-sided at α=0.05 — Hayashi 2000 ch. 6).
- **Drawdown ceiling:** no single-session realised drawdown >
  **3% of allocated PAPER capital**.
- **Trade floor:** ≥ 10 closed trades over the window (defends
  against a dormant-engine accidental pass).
- **Bleed budget:** cumulative unrealised + realised drawdown ≤
  **$5,000** (operator-pinned; 20% of the $25k per-engine PAPER
  slot) over the window — see constraint 15.

**Failure mode (NEW in Path B).** A PAPER engine that violates ANY
clause AT ANY TIME in the window **auto-retires via the new ECR-
RETIRE machine path** (§3 Phase F). Operator audits the retirement
event but does not approve it pre-commit.

**Outcome-proven (NEW).** A PAPER engine satisfying every clause
through the full 30-session window receives an `outcome_proven=True`
marker on its `EngineProfile` (data-only; behaviour unchanged). Per
the paper-only mandate the engine stays PAPER; operator decides LIVE
graduation separately — never the finder.

### §1.3 What v1 holds at autonomy and what it does not

| Action | Path A | Path B v1 |
| --- | --- | --- |
| Spec emission | LLM | LLM |
| Draft PR open | LLM (SP-G) | LLM (SP-G) |
| **PR undraft / merge** | **operator** | **LLM (Phase D)** |
| **ECR open (ADD / MODIFY / RETIRE)** | **operator** | **LLM (Phase D6 / F2)** |
| LAB → PAPER | SDLC | SDLC |
| **PAPER outcome monitoring** | **none** | **LLM (Phase E co-task)** |
| **PAPER → RETIRED on bleed** | **operator** | **LLM (Phase F2)** |
| **PAPER → LIVE** | operator | **operator** (paper-only mandate) |
| Outcome verdict authority | n/a | operator audits AFTER via §12 dashboard |

The reversal is bounded by the paper-only mandate. LIVE remains
operator-only; every other gate the operator previously held flips to
deterministic mechanism with the LLM as the trigger and the operator
as the auditor.

---

## §2 Hard constraints (REWRITTEN for Path B; non-negotiable)

Each constraint is binding by construction (a diff that violates it
cannot land). **Constraint 4 is reversed; 14–16 are NEW.**

1. **Cumulative n_trials honesty (SP-A; inherited from SP-G §2.1).**
   Every finder emission writes one `record_trial_spend(...)` row
   UNCONDITIONALLY at emission time, BEFORE the draft PR is opened
   (SP-G `emit_once` step 5; inherited by `emit_once_with_auto_
   promote`).

2. **Single pre-registered primary hypothesis per emission.** One
   `ProposedSpec` → one `EmittedSpec` → one ledger row → one PR.
   `EDGE_FINDER_RUN_QUOTA = 3` allows up to three SEPARATE emissions
   per run, each independently routed. NEVER a multi-hypothesis grid.

3. **The gate is sacred.** Autonomous Lab criteria (PR #158) +
   SP-A-deflated `n_trials` are unchanged. The finder NEVER modifies
   `_assess_new_engine_signal`, `_assess_improvement`, the credibility
   scorer, the readiness checklist, the ECR mechanism, the `_PROFILE`
   roster, or any engine plug. The auto-promote / auto-ECR / auto-
   retire paths CALL these mechanisms; they do not re-implement them.

4. **(REVERSED 2026-05-21) Autonomous loop with deterministic
   outcome-gating.** The finder auto-undrafts PRs when SP-G fences
   pass, auto-merges PRs when CI is green AND the autonomous Lab
   criteria gate passes, auto-issues ECR-ADD / ECR-MODIFY / ECR-
   RETIRE via the machine path (`python -m ops.engine_sdlc --ecr
   <file>`). Operator is the **auditor of outcomes via the §12
   dashboard**, NOT the per-step gate-keeper. The reversal is bounded
   by clauses 14 (regime-aware ledger), 15 (bleed budget), 16
   (provenance) — those three NEW fences are what makes Path B safe
   at autonomous scale. Source: operator decision 2026-05-21 (this
   spec PR commit message); SUPERSEDES
   `project_research_llm_edge_discovery` HARD CONSTRAINT clause (a).

5. **Credential-starved + crash-isolated.** Two NEW co-tasks on
   `ops/llm_triage_service.py`: (i) `run_lab_finder_cotask` (Phase
   A–D); (ii) `run_finder_outcome_monitor_cotask` (Phase E–F). No
   `ALPACA_*` in env; no `tools` payload to the Anthropic SDK (§6
   sandbox is dispatched IN-PROCESS by the agent on the LLM's
   structured request, not by the SDK). Brings the LLM-triage co-
   task count to FIVE; two-daemon invariant preserved.

6. **Roster-mutated ONLY through the ECR machine path.** Reads
   `tpcore.engine_profile.lab_targetable_engines()` + per-engine
   `LAB_TARGET.primary_metric`. NEVER hand-edits `_PROFILE`,
   `providers.py`, or any engine's `backtest.py::LAB_TARGET`. Auto-
   ECR-ADD / -MODIFY / -RETIRE mutations route through `python -m
   ops.engine_sdlc --ecr <generated-file>`. The engine-roster hook
   in `.claude/hooks/` still blocks direct `_PROFILE` edits; the
   machine path is the only writer.

7. **Five-co-task invariant.** `ops/llm_triage_service.py` runs FIVE
   crash-isolated `_run_supervised` co-tasks (data-triage + engine-
   triage + SP-G emitter + Task #25 finder + Task #25 outcome-
   monitor). Two daemons total (data-repair + llm-triage);
   `tests/test_two_daemon_invariant.py` still passes; new
   `test_five_cotask_invariant.py` pins the cotask count.

8. **No network beyond the Anthropic SDK call.** `MarketSnapshot`
   (§4) is assembled from local Postgres reads ONLY. The LLM cannot
   fetch new data, references, or docs at runtime. Per expert review
   §3.7 (operator chose path (a)): **"venturing out" means richer
   operator-staged context (broader `MarketSnapshot` substrates +
   broader `docs/lab_emitter_references/*.md` bundles) PLUS the LLM's
   trained knowledge as a deliberate, NOT-mining supplement** —
   never runtime browsing, never unguided trained-knowledge spec
   generation. The persona §7 makes this explicit; trained-knowledge
   alone cannot ground a `ProposedSpec.rationale`.

9. **Toolkit whitelist — `statsmodels` + `scipy.stats` ONLY (v1).**
   §6 callables are the complete v1 surface. Importing anything else
   is a fatal CI error. NO `arch`, NO `sklearn`, NO `linearmodels`,
   NO `pandas-ta`, NO network libs. **Default OLS is HAC-robust**
   (Newey-West); raw OLS is removed from the whitelist per expert
   review BLOCKING #2.

10. **The LLM's analysis IS counted against n_trials.** Per
    `project_research_llm_edge_discovery` HARD CONSTRAINT clause (b),
    the LLM's exploration is part of the multiple-testing count. v1
    accounting: every emitted spec's `expected_trials` is fed to
    `record_trial_spend_with_regime` (§4.4). v1 does NOT fold pre-
    emission analysis turns into the ledger directly (analysis is
    bounded by `ANALYSIS_TURN_QUOTA` and is not a Lab probe in the
    formal sense). v2 may reify analysis-into-ledger.

11. **Run-level quotas (autonomous-scale raised vs Path A).**
    `ANALYSIS_TURN_QUOTA = 10` (raised from 8 per expert review §3.6
    — 1 turn snapshot review + 1 turn roster review + ~6 turns core
    analysis + 1 synthesis + 1 buffer); `EDGE_FINDER_RUN_QUOTA = 3`
    (unchanged); **run cadence event-driven, NOT scheduled**. Hard
    ceiling: ≤ 5 finder runs / UTC day, ≤ 30 / UTC week, enforced by
    `tpcore.lab.ledger.cumulative_finder_runs_by_day` /
    `_by_week`.

12. **Per-target quota.** SP-G `EMISSION_QUOTA_PER_TARGET = 20`
    (multiplicative, unchanged).

13. **Diff-scope allow-list + gate-override grep — three variants.**
    `enforce_diff_scope` (operator lane, unchanged) +
    `enforce_diff_scope_for_auto_promote` (auto lane; allows
    `_PROFILE` diff IFF generated by the ECR machine path) +
    `enforce_diff_scope_for_ecr_add` (engine-ADD lane; allows
    scaffolded engine package files matching `engine_template/`
    shape). `validate_no_gate_override` unchanged and re-run at Phase
    D2 (at HEAD, defends against in-flight history rewriting).

14. **(NEW) Autonomous-scale n_trials accounting — regime-aware
    ledger.** At autonomous scale, the same hypothesis re-fired in a
    different regime is structurally a fresh trial against the regime
    axis. v1 implements
    `tpcore.lab.ledger.cumulative_n_trials_by_regime(target,
    regime_tuple_id)`; emissions tagged with their regime tuple at
    `record_trial_spend_with_regime` time; the DSR deflation reads
    cumulatively WITHIN regime, making the gate monotone-harder
    PER REGIME. Defense against autonomous DSR-laundering by "novel
    regime" hypothesis-relabeling. Backwards-compat: SP-A's
    `cumulative_n_trials(target)` remains (returns the all-regimes
    aggregate, used as a secondary check by autonomous Lab criteria
    PR #158).

15. **(NEW) Bleed-budget per finder-emitted PAPER engine.** Structural
    max-bleed of **$5,000** (operator-pinned; 20% of the $25k per-
    engine PAPER slot) over the 30-session outcome window. Computed
    continuously by the Phase E outcome monitor; auto-retire ECR
    fires the moment the bleed cap is reached, NOT end-of-window.
    Defense against autonomous-scale capital destruction.

16. **(NEW) Provenance is non-negotiable.** Every autonomous action
    (draft, undraft, merge, ecr_add, ecr_modify, ecr_retire,
    outcome_proven) writes one append-only row to `application_log`
    with `category='LAB_FINDER_ACTION'`, payload fields `action`,
    `triggered_by` (one of `operator_command|ledger_capacity_event|
    regime_change_event|outcome_monitor_check|ci_green|gate_pass|
    bleed_cap|window_close`), `human_override` (always `'none'` in
    v1 — Path B has no override mechanism; operator audits AFTER the
    fact). The §12 dashboard reads from this.

---

## §3 Architecture

### §3.1 Package layout (engine-FREE; sibling to `tpcore/lab/llm_emitter/`)

```
tpcore/lab/llm_finder/
    __init__.py
    models.py            # MarketSnapshot, MarketRegime, AnalysisRequest,
                         # AnalysisResult, ProposedSpec, FinderRun, LiveOutcome
    tool_sandbox.py      # statsmodels + scipy.stats whitelist (HAC-default,
                         # variance_ratio, hurst, ljung_box, coint pair-fenced)
    snapshot.py          # MarketSnapshot assembler (Postgres read; includes
                         # macro/sentiment/calendar/regime/spread/short/borrow)
    regime.py            # MarketRegime detector (deterministic; reads
                         # macro_indicators + prices_daily + aaii + fear_greed
                         # + earnings_events + calendar)
    reference_loader.py  # docs/lab_emitter_references/<name>.md (shared SP-G)
    outcome.py           # LiveOutcome computer (rolling Sharpe HAC / DD / bleed)
    auto_promote.py      # emit_once_with_auto_promote(...) — auto-undraft +
                         # auto-merge + ECR machine call
    tests/
ops/llm_edge_finder.py                       # finder agent (Anthropic SDK, A–D)
ops/llm_finder_outcome_monitor.py            # outcome-monitor agent (E–F)
ops/llm_triage_service.py                    # AUGMENTED: +2 co-tasks
docs/lab_finder_persona.md                   # persona v2.0 (6 sections, §7)
docs/llm_edge_finder_operator_runbook.md     # operator runbook
docs/lab_emitter_references/                 # SHARED with SP-G
    carver_systematic_trading.md             # SHIPPED (SP-G v1.0)
    chan_algorithmic_trading.md              # SHIPPED (SP-G v1.0)
    dsr_ntrials_discipline.md                # NEW; outline §7.4; mandatory
    market_structure_primer.md               # NEW; outline §7.5
    regime_aware_trading.md                  # NEW; outline §7.6; mandatory
.claude/skills/lab-edge-find/SKILL.md        # operator slash-skill
dashboard_components/finder_audit.py         # NEW read-only renderer
```

The `tpcore/lab/llm_finder/` layer is engine-FREE: stdlib + pydantic
+ `tpcore.lab.ledger` + `tpcore.engine_profile` + `tpcore.lab.llm_
emitter.*` + scoped `statsmodels.api` / `scipy.stats` imports inside
`tool_sandbox.py`. Agents live in `ops/`. Reference-bundle dir is
**shared** with SP-G; per expert review §3.15, ownership of each
bundle is documented in the bundle frontmatter (`owner: sp_g |
task_25 | shared`).

### §3.2 The loop shape (Phases A → F)

**Triggers (§3.4):** (a) operator `/lab-edge-find`; (b)
`LAB_LEDGER_CAPACITY_AVAILABLE` event; (c) `REGIME_CHANGE_OBSERVED`
event.

**Phase A — DATA ASSEMBLY (deterministic, pre-LLM).** A1 read roster
(SP-B) + ledger state by regime (SP-A + §4.4). A2 assemble
`MarketSnapshot` (Postgres; bounded payload; includes `MarketRegime`
+ macro + sentiment + calendar + spreads). A3 load reference bundles
(`dsr_ntrials_discipline.md` + `regime_aware_trading.md` ALWAYS;
selected bundle additive). A4 write `FinderRun` row.

**Phase B — ANALYSIS (LLM-driven, tool-sandboxed).** B1 invoke
Anthropic SDK with snapshot + refs + persona v2.0. B2 LLM emits
structured `AnalysisRequest` → agent dispatches in-process via
`tool_sandbox.dispatch` → `ToolResult`. B3 loop B1↔B2 bounded by
`ANALYSIS_TURN_QUOTA = 10`.

**Phase C — IDEA EMISSION (compose with SP-G).** C1 LLM emits
`AnalysisResult.proposed_specs` (≤ 3). C2 for each:
`tpcore.lab.llm_finder.auto_promote.emit_once_with_auto_promote(...)`
— SP-G fences run verbatim INSIDE (ledger pre-check regime-aware §4.4
→ `EmittedSpec` validate → `record_trial_spend_with_regime` → render
→ `enforce_diff_scope` → `validate_no_gate_override` → `gh pr create
--draft`). C3 log `LAB_FINDER_ACTION(action='draft', ...)`.

**Phase D — AUTO-PROMOTION (Path B; replaces operator human-in-the-
loop).** D1 wait for CI green on the draft PR (`gh pr checks`
polling; bounded timeout = 45 min). D2 SP-G fences re-validated at
HEAD (defends against in-flight git history rewriting). D3 `gh pr
ready` (undraft); log `action='undraft'`. D4 `gh pr merge --squash
--auto`; sibling-bleed circuit-breaker check (§3.5); wait for merged
state; log `action='merge'`. D5 run Lab dispatch: `python -m ops.lab
--candidate <name> --target-engine <engine> --intent <intent>`. D6
read dossier sidecar; if SURVIVED:
- `fold_existing` → generate ECR-MODIFY; auto-issue via `python -m
  ops.engine_sdlc --ecr <file>`; log `action='ecr_modify'`.
- `promote_new` + existing roster slot → ECR-MODIFY on the slot;
  log `action='ecr_modify'`.
- `promote_new` + ENGINE-ADD (new from `engine_template`) → scaffold
  engine from `tpcore/templates/engine_template/`; generate ECR-ADD
  with `_PROFILE` row addition; auto-issue via the machine path; the
  `engine_readiness` checklist runs as part of that path; log
  `action='ecr_add'`.

Engine SDLC moves engine LAB → PAPER deterministically. **Tier 1
(reach) success satisfied.**

**Phase E — LIVE-PAPER MONITORING (continuous; separate co-task
`ops/llm_finder_outcome_monitor.py`).** E1 every NYSE-session close,
enumerate finder-emitted PAPER engines (read from `application_log
LAB_FINDER_ACTION(action='ecr_add'|'ecr_modify')` and
`EngineProfile.outcome_proven=False`). E2 for each, compute
`LiveOutcome` (rolling 30-session net-of-cost Sharpe HAC-adjusted;
max single-session DD; cumulative bleed; trade count). E3 emit
`LAB_FINDER_OUTCOME_CHECK` event for the §12 dashboard. E4 evaluate
outcome criterion (§1.2):
- all clauses met AT WINDOW CLOSE → Phase F1.
- ANY clause violated AT ANY TIME → Phase F2.
- intra-window, not yet violated, not closed → loop.

**Phase F — AUTO-RETIRE / OUTCOME-PROVEN.** F1 outcome-proven: write
`EngineProfile.outcome_proven=True` (data-only marker; engine STAYS
PAPER per paper-only mandate); log `action='outcome_proven'`. F2
outcome-violated: generate ECR-RETIRE; auto-issue via the machine
path; engine SDLC transitions PAPER → RETIRED; write EULOGY from
`tpcore/templates/eulogy_template.md` with the `LiveOutcome` metrics
that triggered retirement; log `action='ecr_retire'`.

Operator audits via §12 dashboard. LIVE graduation remains operator-
only (paper-only mandate). The chain is no longer discontinuous at
every gate — only at the LIVE boundary.

### §3.3 Composition with SP-G

Path B EXTENDS SP-G, never bypasses it.
`emit_once_with_auto_promote` CALLS `tpcore.lab.llm_emitter.emit_
once` for steps 1–5, then layers Phase D auto-promote steps (D1–D6)
on top. SP-G's fence stack is single-sourced; the new entry composes
it with auto-promote machinery. If the finder needs something SP-G
doesn't expose, the answer is **add it to SP-G**. v1 adds two
fence variants to `tpcore/lab/llm_emitter/`: `enforce_diff_scope_
for_auto_promote` and `enforce_diff_scope_for_ecr_add` (both tested
in `tpcore/lab/llm_emitter/tests/`).

### §3.4 Event-driven trigger surface

Three triggers; only the first is operator-authored:

- **(a) Operator command:** `/lab-edge-find [--reference-bundle
  <name>] [--target <engine>]` — slash-skill at
  `.claude/skills/lab-edge-find/SKILL.md`. Logs
  `triggered_by='operator_command'`.
- **(b) `LAB_LEDGER_CAPACITY_AVAILABLE`:** emitted by SP-A ledger
  decay logic (new in `tpcore/lab/ledger.py`). When a target's
  cumulative `n_trials` against the current regime tuple has decayed
  under the per-day-spend floor, the ledger fires the event on
  `application_log`. The finder co-task subscribes; on event, runs
  the full Phase A–D loop with the target pre-selected.
- **(c) `REGIME_CHANGE_OBSERVED`:** emitted by `regime.py` when a
  newly-computed `MarketRegime` differs from the prior session-close
  regime on any axis. The regime computation runs once per session-
  close in `ops/data_repair_service.py` post-close hook. The finder
  co-task subscribes; on event, runs Phase A–D with no target pre-
  selected (LLM picks the most regime-relevant target from the
  snapshot).

Run-rate ceiling (constraint 11) enforced at Phase A1 entry; a run
rejected by the ceiling logs `action='draft', rejection_reason='run_
rate_ceiling'` and returns without invoking the LLM.

### §3.5 Auto-merge mechanics (the load-bearing autonomous step)

Phase D4's `gh pr merge --squash --auto` is the most safety-critical
new action in Path B. The fence stack:

1. PR is on a `task-25-finder/...` branch (CI gate
   `test_finder_auto_merge_branch_pattern.py`).
2. PR title carries `[finder-auto]` tag.
3. SP-G fences re-validated at HEAD before undraft (Phase D2).
4. `gh pr merge --squash --auto` queues behind CI; CI red → merge
   does NOT happen; logs `rejection_reason='ci_red'`; draft PR stays
   open for operator triage from §12 dashboard.
5. Branch protection on `main` honored — required-status-checks =
   whole-suite + order-flip + ops-shadow group; required-reviews = 0
   (the finder is the reviewer, but status checks are the gate).
6. **Sibling-bleed circuit-breaker.** Phase D4 reads `LiveOutcome`
   for every other finder-emitted PAPER engine; if ANY sibling is
   within 80% of its bleed cap, the auto-merge for the NEW emission
   is HELD (logged `rejection_reason='sibling_bleed_warning'`) until
   the sibling outcome-proves or auto-retires. Defense against
   cascading capital destruction at autonomous scale.

---

## §4 Contracts (pydantic v2, all frozen + `extra="forbid"`)

The LLM sees only these schemas — never raw Postgres rows, repo
paths, or live credentials.

### §4.1 `MarketSnapshot` — REWRITTEN per expert-review BLOCKING #1

Path A exposed 2 of 15 ingested substrates. Path B exposes the (F1)
trading-environment substrates the operator has already built.
Bounded by `MAX_SNAPSHOT_BYTES = 512 KiB` (fail-loud on overflow).
Fields: `snapshot_ts`, `session_date`, `universe` (v1 `sp500`),
`price_window` (≤ 252 sessions × ≤ 500 tickers), `fundamentals`
(latest quarter per ticker), `market_regime` (§4.2),
`macro_state` (180 sessions × {VIX, DXY, US10Y, US2Y, HY-OAS,
CPI-yoy, unemployment}), `sentiment_state` (latest readings AAII /
fear_greed / social_sentiment), `event_calendar` (next 21 sessions
earnings + last 180 FOMC), `spread_observations` (Roll-1984 substrate,
per-ticker effective spread), `short_interest`, `borrow_rates`,
`calendar_context` (§4.3), `ledger_state` (§4.4), `roster` (SP-B
`lab_targetable_engines()`).

### §4.2 `MarketRegime` (NEW per operator binding 3)

Five sub-states, all derivable from already-ingested tables:

- `vol_regime: Literal["calm","stress","crisis"]` — VIX bands (calm
  < 20; stress 20–30; crisis > 30).
- `trend_regime: Literal["range","trend"]` — SPY 200d slope sign ∧
  ADX(14) > 25 → trend; else range.
- `macro_regime: Literal["expansion","contraction"]` — composite of
  Sahm rule + CFNAI + yield-curve inversion (thresholds pinned in
  plan PR; spec pins the inputs).
- `sentiment_regime: Literal["extreme_bull","extreme_bear","neutral"]`
  — AAII bull-bear spread × Fear & Greed cross thresholds.
- `cycle_position: tuple[Literal["earnings_season","fomc_week",
  "opex_week","year_end","normal"], ...]` — multi-tag; co-occurrence
  allowed.
- `regime_tuple_id: str` — SHA-12 of (vol, trend, macro, sentiment)
  sorted tuple; `cycle_position` excluded (too high-cardinality;
  would shatter the ledger).

`regime.py` computes once per session-close. Regime crosses fire
`REGIME_CHANGE_OBSERVED` events (constraint 11 (c)).

### §4.3 `CalendarContext` (per expert review §3.8)

`next_fomc_date`, `sessions_until_next_fomc`, `is_earnings_season`
(any S&P 500 reports in next 14 sessions), `sessions_until_quarter_
end`, `next_session_is_holiday_adjacent`, `russell_rebal_window`,
`year_end_window`. Computed from `tpcore.calendar` +
`platform.earnings_events` + a static FOMC schedule loaded from
`tpcore/calendar/fomc_dates.py`.

### §4.4 Regime-aware ledger (NEW per constraint 14)

```python
async def cumulative_n_trials_by_regime(
    target: str, regime_tuple_id: str,
) -> int: ...

async def record_trial_spend_with_regime(
    target: str, n_trials: int, regime_tuple_id: str,
    candidate_name: str, metadata: dict,
) -> None: ...

async def cumulative_finder_runs_by_day(utc_date: date) -> int: ...
async def cumulative_finder_runs_by_week(
    utc_year: int, utc_iso_week: int,
) -> int: ...
```

**Deflation rule:** the DSR floor reads `cumulative_n_trials_in_
regime` for the candidate's emission regime, NOT the all-regimes
aggregate. The gate becomes monotone-harder PER REGIME — the binding
fence against autonomous-scale "novel regime" DSR-laundering.
Backwards-compat: SP-A `cumulative_n_trials(target)` remains (all-
regimes aggregate; used as a secondary check guard against burning
ledger in one regime to mask in another).

### §4.5 `AnalysisRequest`, `ToolCall`, `AnalysisResult`, `ProposedSpec`

```python
class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    callable_name: Literal[
        "OLS_HAC_NW",          # HAC-default (constraint 9)
        "adfuller",
        "coint",               # secondary-only; pair pre-registered; ≤3 calls/run
        "ARIMA_1_0_0",
        "spearmanr", "pearsonr",
        "ttest_1samp_HAC",     # HAC-corrected
        "variance_ratio",      # Lo-MacKinlay 1988
        "hurst_exponent",
        "ljung_box",
    ]
    args_json: Annotated[str, Field(max_length=16_000)]
```

`AnalysisRequest` carries `turn ∈ [1,10]`, rationale (≤ 4 KiB),
`tool_calls` (≤ 4 per turn). `AnalysisResult` carries `tool_results`,
`proposed_specs` (≤ 3), `finder_rationale` (≤ 8 KiB).

`ProposedSpec` (upstream of SP-G `EmittedSpec`) carries:
`candidate_name`, `target_engine` (∈ snapshot.roster), `intent`
(`fold_existing | promote_new`), **`engine_add_path: bool`** (NEW
— True iff `intent=promote_new` AND the spec wants a new engine
scaffold from `engine_template`), `primary_hypothesis`,
`primary_metric` (`LabPrimaryMetric`), `param_ranges`, `rationale`
(MUST cite tool result or bundle excerpt — trained-knowledge alone
disallowed per constraint 8), `falsification_criterion`,
`expected_trials`, **`regime_tuple_id`** (NEW; MUST match
snapshot.market_regime; tagged onto `record_trial_spend_with_
regime`), `analysis_evidence_refs: tuple[EvidenceRef, ...]` (rich
per expert review §3.11; each carries `tool_result_index`,
`callable_name`, `claimed_statistic`, `claimed_value`,
`claimed_threshold` — build-time CI sentinel asserts claimed value
matches actual tool result).

`coint` fences (expert review §3.14): `coint_calls ≤ 3` per run;
each `coint` `ToolCall.args` MUST declare `pair_pre_registered=True`
against a candidate-name-scoped pair roster committed at turn 1 — no
post-hoc max-statistic pair mining.

### §4.6 `FinderRun` (provenance) and `LiveOutcome` (NEW)

`FinderRun`: `run_id`, `started_ts`, `completed_ts`, **`trigger`**
(`operator_command | ledger_capacity_event | regime_change_event`),
`snapshot_session_date`, **`snapshot_regime_tuple_id`**,
`persona_version` (SHA of `lab_finder_persona.md`),
`reference_bundles`, `analysis_turn_count`, `proposed_spec_count`,
`emitted_pr_urls`, **`auto_merged_pr_urls`**, **`auto_issued_ecr_
refs`**, `rejection_reason`. Persisted append-only under
`lab_edge_finder_run.<session_date>` in `platform.data_quality_log`
(no migration).

`LiveOutcome` (Phase E rolling snapshot per finder-emitted PAPER
engine): `engine`, `as_of_session`, `session_count_in_window` (0..30),
`pnl_realised_total_usd`, `pnl_unrealised_total_usd`,
`sharpe_30d_net_costs_hac` (None until ≥ 10 sessions),
`sharpe_30d_t_stat_hac`, `max_single_session_drawdown_pct`,
`cumulative_bleed_usd`, `trade_count_in_window`, `outcome_criterion_
status` (`pending|met|violated`), `violation_clause` (e.g.
`bleed_cap|drawdown_cap|sharpe_floor|trade_floor`),
`auto_retire_triggered`. Persisted per session-close under
`lab_finder_live_outcome.<engine>` (no migration).

---

## §5 Safety posture

| # | Fence | Mechanism | Source |
| --- | --- | --- | --- |
| 1 | n_trials honesty | `record_trial_spend_with_regime` BEFORE PR | SP-G §2.1; §4.4 |
| 2 | Single hypothesis / emission | SP-G `EmittedSpec` pydantic | SP-G `models.py` |
| 3 | Gate sacred | Autonomous Lab criteria PR #158 unchanged | `2026-05-20-autonomous-lab-criteria.md` §3 |
| 4 | **Regime-aware ledger** | `cumulative_n_trials_by_regime`; in-regime deflation | §4.4; constraint 14 |
| 5 | **Bleed budget** | Phase E monitor; auto-retire on $5k cumulative bleed | §1.2; constraint 15 |
| 6 | **Outcome criterion** | Phase E rolling Sharpe HAC / DD / trades | §1.2 |
| 7 | **Provenance audit** | `LAB_FINDER_ACTION` rows on every action | constraint 16; §12 |
| 8 | **Sibling-bleed breaker** | Phase D4 holds new auto-merges if any sibling ≥ 80% bleed | §3.5 |
| 9 | Credential-starved | No `ALPACA_*`; CI has no `ANTHROPIC_API_KEY` | SP-G §2.5 |
| 10 | Crash-isolated | 5-cotask invariant pinned | §3.1; SP-G §4.2 |
| 11 | Roster mutated only via ECR machine path | Auto-ECR routes through `python -m ops.engine_sdlc --ecr`; engine-roster hook blocks direct edits | constraint 6 |
| 12 | No network beyond Anthropic SDK | No `tools` payload; local Postgres only | SP-G §2.5 |
| 13 | Toolkit whitelist (HAC-default) | Literal + attribute-allowlist; raw OLS removed | §6; expert review §3.2 |
| 14 | Run-rate ceiling | ≤5/UTC-day, ≤30/UTC-week | constraint 11 |
| 15 | Per-target quota | SP-G `EMISSION_QUOTA_PER_TARGET = 20` | SP-G `ledger_gate.py` |
| 16 | Diff-scope (3 variants) | `enforce_diff_scope` + `_for_auto_promote` + `_for_ecr_add` | §3.3 |
| 17 | Gate-override grep at HEAD | SP-G `validate_no_gate_override` re-run Phase D2 | SP-G `emitter.py` |
| 18 | `coint` selection-bias cap | `coint_calls ≤ 3`/run; pair pre-registered | expert review §3.14 |
| 19 | LLM never reads gate source | Snapshot is data-only; persona describes gate behaviourally | expert review §3.12 |
| 20 | Persona SHA-pinned | `PERSONA_VERSION = "v2.0"` (Path B bump); CI sentinel reds drift | §7 |

Every constraint is enforced at build/runtime (pydantic, diff-scope
test, sentinel grep, ECR machine path, branch protection + status
checks) — none rely on the LLM "respecting" the rule.

---

## §6 Tool-sandbox (`statsmodels` + `scipy.stats` whitelist)

### §6.1 The whitelist (FOLDED expert review BLOCKING #2)

| `callable_name` | Resolves to | Use |
| --- | --- | --- |
| `OLS_HAC_NW` | `statsmodels.api.OLS(...).fit(cov_type="HAC", cov_kwds={"maxlags": L})`; `L = ceil(4*(T/100)^(2/9))` Newey-West default | Regression / factor exposure; HAC SEs by default per Hayashi 2000 ch. 6 |
| `adfuller` | `statsmodels.tsa.stattools.adfuller` | Stationarity (mean-reversion screen) |
| `coint` | `statsmodels.tsa.stattools.coint` | Cointegration; **secondary-only; pair pre-registered; ≤3 calls/run** |
| `ARIMA_1_0_0` | `statsmodels.tsa.arima.model.ARIMA(order=(1,0,0)).fit()` | AR(1) on returns; bounded order |
| `spearmanr` | `scipy.stats.spearmanr` | Rank correlation (factor IC) |
| `pearsonr` | `scipy.stats.pearsonr` | Linear correlation |
| `ttest_1samp_HAC` | `scipy.stats.ttest_1samp` + HAC-adjusted SE wrapper (in-house thin helper; uses Newey-West lag formula) | Mean ≠ 0 with autocorrelation correction |
| `variance_ratio` | In-house Lo-MacKinlay (1988) helper | Mean-reversion vs random-walk; complementary to ADF |
| `hurst_exponent` | In-house R/S analysis helper | Long-memory / momentum-vs-mean-reversion classifier |
| `ljung_box` | `statsmodels.stats.diagnostic.acorr_ljungbox` | Residual-whiteness diagnostic |

Raw `OLS` (homoskedastic SEs) is **removed from the whitelist** per
expert review §3.2 BLOCKING. The HAC-default is non-negotiable.

### §6.2 Dispatcher

`tool_sandbox.dispatch(call: ToolCall, snapshot: MarketSnapshot) ->
ToolResult` — pure-Python switch on `callable_name`. Each branch:
JSON-decodes `args_json` into a per-callable pydantic `Args` model
(frozen + `extra="forbid"`); resolves series from `snapshot` BY ID
against the §6.4 whitelist; calls the resolved callable; wraps in
`try/except`; exceptions become `ToolResult.error` with exception-
type name only (no traceback, no payload echo); returns `ToolResult
(numeric_summary: NumericSummary)` — bounded shape (`coefficients`,
`pvalues`, `statistic`, `summary_text` ≤ 4 KiB).

**In-process attribute-allowlist.** Imports ONLY named callables at
module top. No `importlib`, `__import__`, `getattr(stats, name)`,
`eval`, `exec`, `subprocess`, `os.system`, `socket`. A CI test
greps the module source; reds on any hit. **Subprocess v2** when the
v2 surface grows (`arch` GARCH); v1 stays in-process.

### §6.3 Determinism

`numpy.random.seed(0)` pinned at top of `dispatch()`. `pyproject.toml`
pins `statsmodels >= 0.14, < 0.15` and `scipy >= 1.11, < 1.13` per
expert review §3.10.

### §6.4 `series_id` whitelist (PINNED per expert review §3.9)

Per-ticker: `adj_close`, `log_return_1d`, `log_return_5d`,
`log_return_20d`, `vol_20d`, `vol_60d`, `dollar_volume_20d`,
`amihud_illiq_20d` (Amihud 2002), `effective_spread` (from
`spread_observations`), `roll_implied_spread` (Roll 1984).
Cross-section: `cross_section_return_zscore_20d`,
`cross_section_vol_zscore_20d`. Macro: `vix_level`, `vix_change_20d`,
`us10y_minus_us2y`, `hy_oas_level`, `hy_oas_change_20d`,
`dxy_change_20d`, `cpi_yoy`, `unemployment_rate`. Sentiment:
`aaii_bull_bear_spread_4wma`, `fear_greed_index`,
`social_sentiment_change_7d`.

---

## §7 Persona — Path B v2.0 (FOLDED expert review HIGH #3 + operator binding 3 + 4)

### §7.1 `PERSONA_VERSION`

`"v2.0"` (bumped from Path A v1.0). SHA-pinning mirrors SP-G's
`_persona_sha()`. The finder's `_persona_sha()` reads
`docs/lab_finder_persona.md`; SP-G's reads
`docs/lab_emitter_persona.md`. Both SHAs CI-sentinel-gated per
expert review §3.13 (two persona-version provenance fields, two
files, both pinned).

### §7.2 Six MANDATORY sections of `docs/lab_finder_persona.md`

Authoring delegated to an expert subagent at brainstorm time per
`feedback_stop_over_asking_use_expert`.

1. **Trading-environment framing (Harris / O'Hara; operator F1).**
   Operator framing verbatim as first paragraph. The LLM is told the
   kind of market it observes — mostly-efficient daily-bar US-equity
   in liquid large-caps; most published anomalies decay (McLean-
   Pontiff 2016); edges come from underexploited interactions of
   existing engines + clean data.

2. **Workflow doctrine (Carver / Chan / López de Prado; operator
   F2).** `collect → analyse → form edge hypothesis → automate via
   emission`. Explicit prose forbidding "try again outside the loop"
   — every exploration step happens through `tool_sandbox.dispatch`;
   nothing the LLM "thinks" off-the-tool-call counts as evidence in
   `ProposedSpec.rationale`.

3. **Regime-awareness directive (operator binding 3).** The LLM
   reads `MarketSnapshot.market_regime` FIRST in turn 1 and adjusts
   toolkit choices: vol_regime=crisis → bias to `variance_ratio` +
   `ttest_1samp_HAC` on shorter windows, suspicious of `coint`;
   trend_regime=trend → bias to factor IC (`spearmanr`/`pearsonr`)
   on momentum/value, suspicious of mean-reversion (Chan ch. 2);
   macro_regime=contraction → suspicious of cross-sectional
   hypotheses (Cooper-Gulen-Schill 2008 factor breaks);
   sentiment_regime=extreme_* → bias to fade-the-extreme (Baker-
   Wurgler 2007); `fomc_week` ∈ cycle_position → bias against fresh
   positions Tue/Wed (Lucca-Moench 2015 pre-FOMC drift).

4. **Reference-bundle internalization (operator binding 4 path
   (a)).** Verbatim: *"The bundles are your in-context truth; your
   training carries broader context but reference-bundle text wins
   on conflict. Trained-knowledge alone cannot ground a
   `ProposedSpec.rationale` — every claim must cite either a tool
   result or a bundle excerpt. Trained knowledge is a SUPPLEMENT to
   frame the hypothesis, never the load-bearing evidence."* Carver
   + Chan are starting points, not the whole world.

5. **n_trials discipline (López de Prado AFML ch. 14; HLZ 2016;
   McLean-Pontiff 2016).** Cumulative DSR deflation is monotone-
   harder per regime (constraint 14). The LLM CANNOT propose a
   candidate whose criterion of success relaxes DSR/credibility.
   The diff-scope allow-list reds the build on any such attempt.
   The LLM's analysis turns are not formally counted in `n_trials`
   (constraint 10) — but the LLM should budget AS IF they were.

6. **Outcome-criterion contract (operator binding 1).** The binding
   success bar is **30-session net-of-cost HAC-Sharpe ≥ 0.5** with
   bleed cap **$5k**, NOT gate-reach. The LLM is told explicitly: a
   candidate that passes the SP-A gate but is designed to barely-
   pass Tier 2 is a WORSE emission than one that fails the SP-A
   gate. The persona's job is to bias the LLM toward economically-
   defensible-in-expectation hypotheses, not statistically-
   defensible-at-gate-floor-minimum.

The persona is NOT a directive on engine internals (sizing /
entry/exit / crash-guard / cost model remain engine-owned), NOT a
license to roam (§6 toolkit + reference bundles + snapshot are the
complete v1 surfaces), and NOT gate source text (the LLM never reads
`tpcore/lab/scorer.py` or `ops/engine_sdlc/lab_criteria.py` — only
the contract described by persona + bundles, per expert review
§3.12).

### §7.3 `dsr_ntrials_discipline.md` outline (FOLDED expert review BLOCKING #3)

MANDATORY-always-include bundle. 9-bullet skeleton:

1. **What DSR is.** López de Prado AFML ch. 14; deflation formula;
   why raw Sharpe is wrong with N hypotheses.
2. **Cumulative `n_trials` is the ledger primitive — per regime**
   (constraint 14). `cumulative_n_trials_by_regime` reads;
   `record_trial_spend_with_regime` writes. Every emission strictly
   tightens the gate in-regime.
3. **HLZ (Harvey-Liu-Zhu 2016).** 316 anomalies; ~half don't
   survive multiple-testing.
4. **McLean-Pontiff (2016).** Post-publication 58% decay; the OOS
   failure mode the gate catches.
5. **PBO (López de Prado AFML ch. 11).** Above-gate Sharpe with
   high PBO is still rejected.
6. **LLM analysis turns NOT formally in `n_trials`** (constraint 10)
   — but should be budgeted AS IF they were.
7. **No-relax pledge.** Diff-scope allow-list reds the build on any
   candidate whose success criterion relaxes DSR/credibility.
8. **HAC default (constraint 9).** All time-series regression /
   t-stat callables default to HAC SEs.
9. **CPCV (López de Prado AFML ch. 12).** Combinatorial Purged
   Cross-Validation; v1.5+ enrichment of the existing walk-forward.

### §7.4 `market_structure_primer.md` outline (FOLDED expert review HIGH §3.5)

The (F1)-half of operator framing. 10-bullet skeleton:

1. **Venue / order-flow.** SIP vs IEX; dark pools / ATS; payment-
   for-order-flow effects.
2. **Order types and auctions.** Market, limit, MOC, LOC, MOO,
   LOO. Why MOC dominates daily-bar close prices.
3. **LULD / circuit breakers.** When prices stop being prices.
4. **Tick sizes.** Sub-penny vs penny; small-cap pilot.
5. **Bid-ask spread decomposition** (Roll 1984; Glosten-Milgrom
   1985; Stoll 2003). Why `platform.spread_observations` matters.
6. **Liquidity proxies.** Amihud (2002); dollar volume; relative
   volume. All from `prices_daily`.
7. **Cross-asset transmission.** VIX (Whaley 1993); 10Y-2Y inversion;
   HY-OAS; DXY.
8. **Calendar effects.** Lucca-Moench 2015 pre-FOMC drift; earnings
   clustering; Russell rebal Q3; year-end tax-loss harvest.
9. **Finder's environment posture.** Mostly-efficient daily
   timeframe; published anomalies decay; edge from underexploited
   interactions + clean data.
10. **What the finder CANNOT do.** Intraday signals; order-flow
    microstructure; anything sub-daily.

### §7.5 `regime_aware_trading.md` outline (NEW per operator binding 3)

Second MANDATORY-always-include bundle. 9-bullet skeleton:

1. **Why regime conditioning dominates parameter sweeps** (Chan
   ch. 2; Carver §15).
2. **The five v1 regime axes (§4.2).** Vol / trend / macro /
   sentiment / cycle_position. Derivation rules.
3. **Vol regime — crisis vs stress vs calm.** VIX bands; what
   cointegration relationships look like in each.
4. **Trend vs range regime.** SPY 200d slope + ADX(14); momentum
   bias in trend, mean-reversion bias in range.
5. **Macro regime — expansion vs contraction.** Sahm + CFNAI +
   yield curve; factor premia behave differently.
6. **Sentiment regime.** AAII × Fear & Greed; Baker-Wurgler 2007.
7. **Cycle position.** Lucca-Moench pre-FOMC drift; earnings
   clustering; opex; Russell rebal; year-end.
8. **Regime-aware ledger (constraint 14).** Same hypothesis re-fired
   in a new regime is a fresh trial; DSR deflation per-regime
   monotone-harder.
9. **What the LLM cannot do.** Re-label an in-sample regime-
   stratified result as a "novel regime hypothesis" to avoid per-
   regime ledger spend — the ledger tags emissions at
   `record_trial_spend_with_regime` time, not at hypothesis-author
   time.

---

## §8 The graduation path (Path B walk-through; Task #25 owns ALL ten steps)

1. **Trigger:** operator `/lab-edge-find` OR
   `LAB_LEDGER_CAPACITY_AVAILABLE` event OR `REGIME_CHANGE_OBSERVED`
   event.
2. **Phase A:** roster + regime-aware ledger + snapshot + bundles +
   `FinderRun` row.
3. **Phase B:** Anthropic SDK + analysis loop; ≤ 10 turns; ≤ 4
   tool calls / turn; HAC defaults.
4. **Phase C:** `ProposedSpec`s (≤ 3) → `emit_once_with_auto_
   promote`; SP-G fences inside; draft PRs opened.
5. **Phase D1–D2:** wait CI green; re-run SP-G fences at HEAD.
6. **Phase D3–D4:** undraft; `gh pr merge --squash --auto`; sibling-
   bleed breaker.
7. **Phase D5:** `python -m ops.lab` dispatched; SP-A-deflated gate;
   autonomous Lab criteria (PR #158).
8. **Phase D6:** on SURVIVED → auto-ECR via the machine path
   (`python -m ops.engine_sdlc --ecr <file>`); engine SDLC LAB →
   PAPER. **Tier 1 (reach) success satisfied.**
9. **Phase E:** outcome monitor co-task; per session-close
   `LiveOutcome` computed; `LAB_FINDER_OUTCOME_CHECK` events.
10. **Phase F:** F1 (outcome-proven; engine STAYS PAPER) OR F2 (auto-
    retire via ECR-RETIRE; EULOGY written). **Tier 2 (outcome)
    success satisfied (F1) or auto-defended (F2).**

LIVE graduation remains operator-only.

---

## §9 Roadmap — Path B v1 absorbs former v1.5 + revises out-of-scope

Per operator binding 5: **v1 emissions route through EITHER (i)
`promote_new` against existing roster slots via `emit_once_with_auto_
promote` OR (ii) a brand-new ENGINE-ADD path via `engine_template` +
ECR-ADD** — the autonomous loop closes both.

### §9.1 v1 scope (this spec)
Operator-command + event-driven trigger; HAC-default toolkit + new
callables; auto-promote / auto-merge / auto-ECR / auto-retire;
ENGINE-ADD via `engine_template`; outcome-criterion + bleed budget +
provenance audit lane; regime-aware ledger + `market_regime`; five
reference bundles; ≤ 5 runs / UTC day, ≤ 30 / UTC week.

### §9.2 v1.5 — Deferred enrichments
Bigger universe (`sp1500`, `rus3k`); subprocess tool-sandbox (`arch`
+ `linearmodels`); 60-session outcome window; CPCV; insider /
SEC-material / catalyst / options chains in `MarketSnapshot`.

### §9.3 v2.0 — Cross-engine combiner framing
(`project_ml_research_track` defensible-use 2.)

### §9.4 v2.5 — Meta-labeling framing
(Defensible-use 1; fixed-hyperparam `scikit-learn` shallow classifiers
as `lifecycle_analysis` guards — NOT a finder hypothesis.)

### §9.5 v3.0 — Diversification memory
Bounded, audit-able, regime-aware-n_trials-fenced.

### §9.6 Permanently out of scope
- **LIVE graduation by the finder.** Paper-only mandate; PAPER →
  LIVE is operator-only.
- **Live-capital signal generation.** Finder produces Lab specs +
  engine scaffolds, never live signals.
- **LLM runtime network access.** Snapshot + bundles + persona are
  the LLM's complete in-context world. No `tools=[...]` payload; no
  `requests`/`urllib`/`socket` in finder source.
- **Multi-hypothesis emission per ledger row.**
- **Modifying autonomous Lab criteria** (`ops/engine_sdlc/lab_
  criteria.py`).
- **Bypassing the engine-roster hook.** Auto-ECR always routes
  through `python -m ops.engine_sdlc --ecr <file>`.

---

## §10 Test plan

### §10.1 Unit (`tpcore/lab/llm_finder/tests/`)

- `test_models_frozen.py` — all models frozen + `extra="forbid"`.
- `test_market_regime_deterministic.py` — fixed input ⇒ fixed
  `MarketRegime` byte-for-byte; `regime_tuple_id` = SHA-12.
- `test_snapshot_assembler.py` — synthetic Postgres rows →
  bounded `MarketSnapshot`; overflow fail-loud.
- `test_snapshot_includes_macro_sentiment_calendar.py` — assembler
  populates `macro_state`, `sentiment_state`, `event_calendar`,
  `calendar_context`, `spread_observations`, `short_interest`,
  `borrow_rates`.
- `test_tool_sandbox_whitelist.py` — `ToolCall.callable_name`
  outside Literal raises BEFORE dispatcher.
- `test_tool_sandbox_ols_is_hac.py` — `OLS_HAC_NW` produces HAC SEs
  against statsmodels reference output.
- `test_tool_sandbox_no_dynamic_import.py` — grep reds on
  `importlib`, `__import__`, `getattr(.*, name)`, `eval`, `exec`,
  `subprocess`, `os.system`, `socket`.
- `test_coint_pair_pre_registered.py` — `coint` call without
  `pair_pre_registered=True` raises; 4th `coint` call in a run
  raises.
- `test_variance_ratio_helper.py` — Lo-MacKinlay implementation
  validated against published reference values.
- `test_reference_loader_bundles.py` — five named bundles load;
  `dsr_ntrials_discipline.md` + `regime_aware_trading.md` ALWAYS
  included regardless of `--reference-bundle`.
- `test_regime_aware_ledger.py` — `cumulative_n_trials_by_regime`
  monotone-increases per (target × regime_tuple_id); per-regime
  deflation distinct from all-regimes aggregate.
- `test_finder_run_rate_ceiling.py` — 6th run/day rejected with
  `rejection_reason='run_rate_ceiling'`.

### §10.2 Integration (`ops/tests/`, mocked Anthropic)

- `test_llm_edge_finder_round_trip.py` — synthetic `AnalysisResult`
  with 1 `ProposedSpec` round-trips through
  `emit_once_with_auto_promote`; rendered spec validates against
  frozen golden.
- `test_llm_edge_finder_composes_with_sp_g.py` — CI grep:
  `ops/llm_edge_finder.py` + `auto_promote.py` import `emit_once`
  and do NOT re-define `record_trial_spend`, `render_candidate_
  spec`, `enforce_diff_scope`, `validate_no_gate_override`.
- `test_llm_edge_finder_quota.py` — `EDGE_FINDER_RUN_QUOTA = 3` +
  `ANALYSIS_TURN_QUOTA = 10` enforced.
- `test_five_cotask_invariant.py` — 5 crash-isolated co-tasks; two-
  daemon invariant test still passes.
- `test_persona_versioned.py` — persona edit without
  `PERSONA_VERSION` bump reds the build.
- **(NEW per operator binding) `test_auto_promote_path_e2e.py`** —
  emitted spec walks Phases A→F with mocked Anthropic + DB. Happy
  path: auto-undraft → auto-merge → auto-ECR → LAB → PAPER →
  outcome stream positive → `outcome_proven=True`. Violation path:
  same up to PAPER → outcome stream breaches bleed cap → auto-ECR-
  RETIRE → engine PAPER → RETIRED → EULOGY written with violating
  `LiveOutcome` metrics.
- **(NEW per operator binding) `test_bleed_budget_fence.py`** —
  synthetic outcome stream monotonically losing → bleed cap hit at
  session 12 → auto-retire fires at session 12, not at window close.
- **(NEW per operator binding) `test_regime_aware_snapshot.py`** —
  synthetic regime input changes `MarketSnapshot.market_regime`
  across all five axes; `regime_tuple_id` SHA-12 deterministic.
- `test_sibling_bleed_circuit_breaker.py` — sibling within 80% of
  bleed cap → new emission's auto-merge held.
- `test_engine_add_path.py` — `intent=promote_new` with
  `engine_add_path=True` scaffolds engine from
  `tpcore/templates/engine_template/`, auto-issues ECR-ADD, engine
  SDLC moves to LAB.

### §10.3 Safety (the make-or-break)

- `test_finder_cannot_bypass_sp_g.py` — CI grep: NO `gh pr create`
  outside `emit_once`/`emit_once_with_auto_promote`.
- `test_finder_cannot_hand_edit_profile.py` — CI grep: no direct
  `_PROFILE`/`tpcore.engine_profile._PROFILE` write; ALL mutations
  via `python -m ops.engine_sdlc --ecr`.
- `test_finder_cannot_import_non_whitelisted.py` — CI grep of
  `tool_sandbox.py`: NO `arch`, `sklearn`, `scikit_learn`,
  `linearmodels`, `pandas_ta`, `requests`, `urllib`, `http`, `socket`.
- `test_finder_cannot_write_to_db.py` — finder has read-only
  Postgres role; sentinel write attempt expects
  `InsufficientPrivilege`. ONLY writes: `record_trial_spend_with_
  regime` (its own role) + `FinderRun` + `LiveOutcome` +
  `LAB_FINDER_ACTION` rows.
- `test_finder_diff_scope_three_variants.py` — each variant reds the
  build on its disallowed file set.
- `test_finder_auto_merge_branch_pattern.py` — auto-merge on a non-
  `task-25-finder/...` branch raises; `gh pr merge` never called
  outside the protected pattern.
- `test_finder_cannot_override_paper_only_mandate.py` — finder
  attempts to set `LifecycleState.LIVE` raises; paper-only mandate
  structural.

### §10.4 Provenance / audit lane

- `test_lab_finder_action_provenance.py` — every Phase D/E/F action
  writes one `LAB_FINDER_ACTION` row with required fields. Missing
  row reds the build.
- `test_finder_audit_dashboard_renderer.py` — `dashboard_components/
  finder_audit.py` renders without error on synthetic data; never
  imports `streamlit` at test time per
  `feedback_ops_package_shadow_full_suite_gate` (b).

### §10.5 Lane discipline

All new tests under `tpcore/tests/` and `ops/tests/` that import
`ops.llm_triage_service` or touch `sys.modules['ops']` carry
`pytestmark = pytest.mark.xdist_group("ops_shadow")`.

### §10.6 The load-bearing E2E proof (v1 success criterion, mock-driven)

`ops/tests/test_llm_edge_finder_to_outcome_proven.py` — mocks
Anthropic + `ops.lab` dispatch + ECR machine path + outcome stream;
demonstrates a finder-emitted `ProposedSpec` walks ALL TEN §8 steps
with the outcome stream satisfying every Tier 2 clause, ending at
`outcome_proven=True`. **This is the v1 success-criterion proof at
mock scale.** The real-data version runs once at v1 GA — operator
audits Tier 2 via §12 after one finder-emitted PAPER engine
completes its 30-session window.

---

## §11 Lane — heavy

Per `docs/DEV_PIPELINE_STANDARD.md` §0: new **autonomous** mechanism
on the Lab graduation rail with auto-merge / auto-ECR / auto-retire
authority; touches `ops/llm_triage_service.py`; adds
`tpcore/lab/llm_finder/`; augments slash-skills + new read-only
dashboard component; new statistical-tool sandbox surface; new
ECR-RETIRE machine path; new regime-aware ledger primitive. Full §1
pipeline applies (whole-suite + reverse-order; ops-shadow xdist
group; `gh pr checks` not `gh run watch`).

The auto-merge surface is gated by: existing SP-G `enforce_diff_
scope` + new variants; `validate_no_gate_override`; the new outcome-
criterion fence (Phase E); branch protection on `main`; the
sibling-bleed circuit-breaker (§3.5); the new run-rate ceiling
(constraint 11); the engine-roster hook; the regime-aware ledger
deflation.

---

## §12 AUDIT TRAIL (NEW — the operator's role at autonomous scale)

### §12.1 The audit lane is the operator's only role per the reversal

Per operator binding 2: *"operator becomes the auditor of OUTCOMES,
not the gate-keeper of EACH STEP."* The operator no longer reviews
each PR, undrafts each PR, opens each ECR, makes each retire
decision. The operator **reads the §12 dashboard daily** and decides
ONLY:

- Pause the finder (disable the co-task) if outcomes look
  systematically wrong.
- Manually graduate a `outcome_proven=True` engine to LIVE (paper-
  only mandate is operator-owned; finder cannot).
- Edit persona / reference bundles between cycles (operator-staged
  context; LLM cannot).
- Roll back a single auto-action manually if a defect surfaces
  (operator issues a counter-ECR by hand; the finder writes
  provenance, the operator reverses).

### §12.2 `dashboard_components/finder_audit.py` (NEW)

Read-only Streamlit component (no writes; mirrors
`dashboard_components/health.py` pattern). Renders:

- **Recent finder runs (last 7 UTC days).** `FinderRun` rows with
  trigger, snapshot regime tuple, proposed-spec count, auto-merged
  PRs, auto-issued ECRs.
- **Active finder-emitted PAPER engines (`outcome_proven=False`).**
  Per-engine `LiveOutcome` table: rolling Sharpe HAC, drawdown,
  bleed-budget usage (% of $5k cap), trade count, sessions-in-window,
  outcome-criterion-status.
- **Outcome-proven engines.** Archived list with final `LiveOutcome`.
- **Auto-retired engines.** Archived list with violating
  `LiveOutcome` clause + auto-ECR-RETIRE PR URL.
- **`LAB_FINDER_ACTION` audit feed.** Time-ordered log of every
  autonomous action with `triggered_by` + linked PR URL +
  `human_override` (always 'none' in v1).
- **Run-rate gauge.** Finder runs today / weekly against constraint-
  11 ceiling.

The component **never imports streamlit at test time** per
`feedback_ops_package_shadow_full_suite_gate` (b); Streamlit import
happens inside the render function (call-time, not import-time),
guarded by `if TYPE_CHECKING` shim at module top.

### §12.3 Operator runbook

`docs/llm_edge_finder_operator_runbook.md` — procedural counterpart
to the dashboard. Covers: pause the finder co-task (kill via
`ops.llm_triage_service.disable_lab_finder_cotask()` flag in
`application_log`); investigate an auto-retire (read EULOGY +
`LAB_FINDER_ACTION` chain); manually graduate a `outcome_proven=True`
engine to LIVE (existing ECR-MODIFY path; operator-only); roll back
an auto-action (issue counter-ECR by hand).

---

## §13 Phasing roadmap

| Version | Scope | Status |
| --- | --- | --- |
| **Path B v1.0** | This spec: event-driven finder; HAC-default toolkit; 3 specs/run × ≤5 runs/day; 5 bundles; auto-promote / auto-merge / auto-ECR (ADD + MODIFY + RETIRE); regime-aware ledger; bleed budget; outcome criterion; §12 audit dashboard; ENGINE-ADD via `engine_template` | THIS SPEC (DESIGN) |
| **v1.5** | Bigger universe; subprocess tool-sandbox; 60-session outcome window; CPCV; insider/SEC-material/catalyst/options chains in `MarketSnapshot` | Deferred |
| **v2.0** | Cross-engine combiner framing (`project_ml_research_track` use 2) | Deferred |
| **v2.5** | Meta-labeling framing (use 1) — `scikit-learn` shallow classifiers as `lifecycle_analysis` guards | Deferred |
| **v3.0** | Diversification memory — bounded, audit-able, regime-aware-n_trials-fenced | Deferred |

Every phase inherits §2 verbatim. No phase relaxes the gate, the
bleed budget, the outcome criterion, or the paper-only mandate.

---

## §14 Cross-references

**Specs.** SP-G design:
`docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md`.
SP-A: `2026-05-19-lab-ntrials-ledger.md`. SP-B:
`2026-05-19-lab-sp-b-roster-driven-targeting-design.md`. SP-D:
`2026-05-20-lab-sp-d-pluggable-scoring-design.md`. Autonomous Lab
criteria: `2026-05-20-autonomous-lab-criteria.md`. Lab front-half
epic: `2026-05-19-lab-front-half-epic.md`. DA-3 two-daemon:
`2026-05-18-da3-two-daemon-consolidation-design.md`.

**Expert review folded.**
`docs/superpowers/reviews/2026-05-21-task-25-spec-review.md` — all
3 BLOCKING + 4 HIGH + 6 MEDIUM + 2 LOW findings folded into this
rewrite. None deferred.

**PRs.** SP-A build **#93**, SP-B **#131**, SP-G design **#146**,
SP-G build **#152**, autonomous Lab criteria **#158**, ECR-MODIFY
data-dependencies **#210**, Path A v1 spec **#213** (superseded).

**Checklists.** `docs/superpowers/checklists/lab_candidate_readiness.md`
(SP-C; every finder emission still passes this);
`docs/superpowers/checklists/engine_change_request.md` (the ECR
machine path auto-ECR routes through);
`docs/superpowers/checklists/engine_readiness.md` (the engine-ADD
gate the `engine_add_path=True` branch passes).

**SP-G shipped code.** `tpcore/lab/llm_emitter/{models.py, emitter.py,
ledger_gate.py, diff_fence.py}`; `tpcore/lab/ledger.py`;
`ops/llm_lab_emitter.py` (`emit_once`); `ops/llm_triage_service.py`
(augmented to 5 co-tasks).

**Templates.** `tpcore/templates/engine_template/` — engine-ADD
scaffold the autonomous loop instantiates via the ECR-ADD machine
path. `tpcore/templates/eulogy_template.md` — EULOGY scaffold
auto-retire writes.

**Memory updated in this PR.**
- `docs/memory/project_research_llm_edge_discovery.md` — HARD
  CONSTRAINT clause (a) REVERSED; Path B autonomous-loop posture +
  bleed-budget + outcome-criterion + regime-aware ledger captured as
  the new structural fences.
- `docs/memory/project_ml_research_track.md` — paragraph added
  noting that Path B autonomous scale is fenced at the regime-aware
  ledger + bleed-budget level; ML-discipline-at-scale is preserved
  through these mechanisms, NOT through operator-gating.
- `docs/memory/MEMORY.md` — index descriptions refreshed.

**Memory cited (unchanged).** `ref_carver_systematic_trading`;
`ref_chan_algorithmic_trading`; `feedback_event_driven_not_
scheduled`; `feedback_stop_over_asking_use_expert`;
`feedback_use_official_docs`; `feedback_ops_package_shadow_full_
suite_gate`; `feedback_cut_process_overhead_ship`;
`feedback_symmetry_not_copy`; `feedback_no_shortcuts_100_pct`;
`feedback_ask_expert_then_execute`;
`feedback_authoritative_docs_override_claudemd`.

**Lane standard.** `docs/DEV_PIPELINE_STANDARD.md` §0/§1/§2/§3.

**CLAUDE.md universal invariants preserved.** Paper-only; SIP
default; no yfinance/Discord/manual; UTC timestamps;
`tpcore.calendar` for XNYS; `DATA_OPERATIONS_COMPLETE` never emitted
unless self-heal returns 100% green; `prices_daily_completeness`
the ungameable zero-tolerance invariant; engine roster changes
route through ECR (via the machine path for the autonomous loop);
engine-build compliance shortlist applies to any engine the finder
scaffolds via `engine_template`.
