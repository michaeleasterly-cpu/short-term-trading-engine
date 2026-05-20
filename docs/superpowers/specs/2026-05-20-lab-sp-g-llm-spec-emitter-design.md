# SP-G — Lab Front-Half Thin Advisory LLM Spec-Emitter (Design Spec)

**Status:** DESIGN. The seventh and final sub-project of the Lab
front-half epic. Pre-implementation; no code lands until the operator
spec-read gate clears.
**Epic:** `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md`
§SP-G (the final piece).
**Predecessors (all SHIPPED on `main`):**
SP-A `2026-05-19-lab-ntrials-ledger.md` (PR #93),
SP-B `2026-05-19-lab-sp-b-roster-driven-targeting-design.md` (PR #131),
SP-C `docs/superpowers/checklists/lab_candidate_readiness.md` (PR #132),
SP-D `2026-05-20-lab-sp-d-pluggable-scoring-design.md` (PR #135),
SP-E `2026-05-20-sentinel-maxdd-lab-candidate.md` (PR #136),
SP-F Catalyst engine + roster-driven Lab proof (PR #138 / sibling).
**Lane:** engine lane. Heavy-lane per `docs/DEV_PIPELINE_STANDARD.md`
§0 (new advisory mechanism on safety-adjacent surface — the Lab
front-half graduation rail).
**Discipline:** brainstorm→expert-harden→spec/plan gated PRs→
subagent-driven exec→split review→whole-suite + order-flip→squash-merge.

---

## §1 Motivation — why thin emitter NOW, why richer is deferred

The Lab front-half (SP-A..F) is built. The cumulative `n_trials` ledger
(SP-A) is the safety floor: every Lab probe against a target engine
strictly tightens that target's DSR-deflation. Roster-driven targeting
(SP-B), the Lab Candidate Readiness checklist (SP-C), pluggable per-
engine scoring (SP-D), and the Sentinel/Catalyst proof cases (SP-E/F)
together turn "add a Lab candidate" into a single, mechanical, gated
operation. SP-G is the bookend: a **thin, advisory, human-gated LLM
spec-emitter** that drives that pipeline by proposing one candidate at
a time, never bypassing the gate, never touching the roster, never
auto-applying anything to live capital.

**Why thin, why now (the scope decision — operator 2026-05-20).** The
operator memo `project_research_llm_edge_discovery`
*"⚠ OPERATOR AMBITION RAISED 2026-05-20"* sub-section is explicit: the
operator explicitly wants more than a thin emitter eventually — an LLM
that drives a real quantitative toolkit (`statsmodels` / `arch` /
`linearmodels` / `scikit-learn` / `scipy.stats`), internalises trading-
environment context from the curated reference set
(`ref_carver_systematic_trading`, `ref_chan_algorithmic_trading`), and
operates a disciplined data→analysis→idea→Lab loop. When asked whether
to restructure SP-G to that ambition, the operator answered *"keep
going / stick to the plan"* — i.e. NO restructure now. SP-G keeps its
**originally-planned thin scope**; the richer autonomous-quant ambition
becomes a separate **follow-on epic, task #25**, with its own
brainstorm at kick-off (see §10 open question Q1, and the §7 explicit
out-of-scope list).

**The binding constraint that survives either scope.** Per
`project_ml_research_track` (the commissioned-expert verdict), naïve
automated edge-search inflates the DSR `n_trials` /
multiple-testing count and manufactures overfit "edges" that die out-
of-sample. An LLM proposing N hypotheses is exactly that failure mode
at scale. The fence — *the LLM proposes, the deterministic gate
(cumulatively deflated) disposes* — is non-negotiable and applies to
both SP-G (thin) and the eventual task #25 (rich). SP-G is the
minimum, hardest-fenced form of that fence; task #25 will inherit it
verbatim.

---

## §2 Hard constraints (non-negotiable — every later section is consistent with these)

Each constraint is binding by construction (SP-G's diff cannot land
without it satisfied) and cited to its source.

1. **Cumulative n_trials honesty (SP-A).** Every LLM-emitted candidate
   increments the SP-A cumulative ledger
   (`tpcore.lab.ledger.record_trial_spend` →
   `lab_trial_ledger.<target>` in `platform.data_quality_log`)
   **unconditionally at emission time**, not at Lab-run time, not at
   evaluation time. The LLM cannot under-declare its trial spend; the
   ledger row is written by SP-G's emitter code path *before* the
   draft-PR is opened (§4.3). Source:
   `project_research_llm_edge_discovery` HARD CONSTRAINT clause (b),
   `tpcore/lab/ledger.py`, `lab_candidate_readiness.md` §4.

2. **Single pre-registered primary hypothesis per emission.** One
   emission per cycle, one hypothesis per emission, one primary metric
   per emission. NEVER a batch; NEVER a multi-hypothesis grid. Source:
   `lab_candidate_readiness.md` §1 (the most-cited readiness item).

3. **The gate is sacred — the LLM never bypasses it.** The
   deterministic floor (`DSR ≥ 0.95 ∧ credibility ≥ 60 ∧ n_trades ≥ 3`)
   is unchanged. The LLM does not modify the gate, the rubric, the
   credibility scorer, the n_trials ledger semantics, the readiness
   checklist, the ECR mechanism, the `_PROFILE` roster, the data-feed
   roster, or any engine plug. Every emitted candidate routes through
   `_run_lab_core` → `survived` → dossier → ECR like every other
   candidate. Source: CLAUDE.md Universal invariants;
   `lab_candidate_readiness.md` §6; epic §SP-G.

4. **Advisory + human-gated only.** The emitter produces **draft PRs**
   only (`gh pr create --draft`); a draft PR cannot self-merge; the
   operator (or the SP-G human reviewer) is the merge authority.
   Source: `project_research_llm_edge_discovery` HARD CONSTRAINT clause
   (a); mirrors data-LLM-triage (#187) and engine-LLM-triage (Epic E)
   discipline — both BUILT, both draft-PR-only.

5. **Credential-starved + crash-isolated.** The emitter runs as a
   crash-isolated co-task on the existing LLM-triage daemon (the
   `ops/llm_triage_service.py` consolidated DA-3 process), with no
   access to live trading credentials (`ALPACA_API_KEY` /
   `ALPACA_SECRET_KEY` are never in the emitter's environment), no
   `tools` payload to the Anthropic SDK, no real-tree write outside
   the draft-PR boundary. CI fence job is credential-starved (no
   `ANTHROPIC_API_KEY`). Source: `feedback_event_driven_not_scheduled`;
   `ops/llm_triage_service.py` shipped pattern; #187 + Epic E precedent
   verbatim.

6. **Roster-mediated, never roster-mutating.** The LLM reads
   `tpcore.engine_profile._PROFILE` (existing public surface
   `lab_targetable_engines()` from SP-B) and the per-engine
   `LAB_TARGET.primary_metric` (SP-D). It NEVER edits `_PROFILE`,
   `providers.py`, or any engine's `backtest.py::LAB_TARGET`. A roster
   ADD/REMOVE is an operator ECR (`/ecr` skill); a data-feed change is
   a DFCR. The `.claude/hooks/` ECR/DFCR-gated edit hooks already
   block the LLM from these files; SP-G adds a defence-in-depth
   diff-scope allow-list (§4.4). Source: CLAUDE.md "Engine roster
   changes → `/ecr` skill"; `engine_change_request.md`; existing
   hooks.

7. **No autonomous loop.** SP-G emits **on operator command** (a
   slash-skill, `/lab-spec-emit`) and on a single explicit event class
   (`LAB_LEDGER_CAPACITY_AVAILABLE` — §4.1), never on a free-running
   cron, never on an LLM-decided "I should propose something" tick.
   The "richer autonomous data→analysis→idea loop" is task #25 by
   construction (§7). Source: operator memo
   *"keep going / stick to the plan"* on the
   `project_research_llm_edge_discovery` ambition; epic §SP-G
   *"thin advisory spec-emitter"*.

---

## §3 Architecture

### §3.1 Package layout

```
tpcore/lab/llm_emitter/                     # engine-free contract layer
    __init__.py
    models.py          # EmittedSpec, EmissionContext (pydantic v2 frozen)
    contract.py        # what the LLM is allowed to read / emit (typed)
    ledger_gate.py     # pre-emission "ledger has budget?" check (reads SP-A)
ops/llm_lab_emitter.py                     # the agent (Anthropic SDK call)
ops/llm_triage_service.py                  # AUGMENTED: add 3rd co-task
                                           # (existing 2 unchanged)
docs/lab_emitter_persona.md                # the persona file + PERSONA_VERSION
docs/llm_lab_emitter_operator_runbook.md   # operator runbook
.claude/skills/lab-spec-emit.md            # operator slash-skill
```

The `tpcore/lab/llm_emitter/` layer is **engine-free** (stdlib +
pydantic + `tpcore.lab.ledger` + `tpcore.engine_profile` only — the
same engine-free discipline SP-B established for `tpcore/lab/target.py`
and SP-A for `tpcore/lab/ledger.py`). The agent lives in `ops/`
because it imports the Anthropic SDK and writes a draft PR; engine
packages never import it.

### §3.2 Input contract — what the LLM is allowed to read

The LLM's `EmissionContext` is a **frozen pydantic-v2 model**
(`tpcore/lab/llm_emitter/models.py`), assembled by the agent before
the Anthropic call. The schema is the only thing the LLM sees; the
agent never embeds raw repo paths or live credentials in the prompt.

```python
class EmissionContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    roster_targets: tuple[RosterTarget, ...]        # from lab_targetable_engines()
    ledger_state: tuple[LedgerEntry, ...]           # cumulative_n_trials per target
    readiness_checklist_version: str                # lab_candidate_readiness.md SHA
    reference_excerpts: tuple[ReferenceExcerpt, ...]  # operator-curated, opt-in
    persona_version: str                            # docs/lab_emitter_persona.md SHA
    emission_quota_remaining: int                   # §4.1 budget enforcement

class RosterTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    lifecycle_state: Literal["LAB", "PAPER", "LIVE"]
    primary_metric: LabPrimaryMetric                # SP-D
    declared_param_ranges: dict[str, tuple]         # from LAB_TARGET.param_ranges
```

The `ReferenceExcerpt` tuple is operator-staged (the curated
Carver/Chan reference set per `ref_carver_systematic_trading` and
`ref_chan_algorithmic_trading`); the LLM cannot fetch new references
itself (no `tools`, no network calls beyond the Anthropic SDK call).
Staging is the operator's hand on the wheel for what reference
material the emitter sees this cycle.

### §3.3 Output contract — `EmittedSpec`

The LLM returns a structured response the agent validates against
`EmittedSpec`:

```python
class EmittedSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]+$")]
    target_engine: str                              # MUST ∈ roster_targets
    intent: Literal["fold_existing", "promote_new"]
    primary_hypothesis: str                         # one sentence, pre-registered
    primary_metric: LabPrimaryMetric                # MUST match target's declared
    param_ranges: dict[str, tuple]                  # SP-B LabTarget shape; exactly ONE toggle
    rationale: str                                  # why this hypothesis, citing references
    falsification_criterion: str                    # what makes this FAIL (red-is-red)
    expected_trials: int                            # what increments the ledger
```

The agent then **renders** the `EmittedSpec` into the canonical
candidate spec markdown (the SP-E `2026-05-20-sentinel-maxdd-lab-
candidate.md` shape — the ten Readiness sections). The rendered
markdown is what the draft PR contains; the JSON `EmittedSpec` is the
machine-checkable sidecar (mirrors SP3 `.json` sidecar pattern from
`tpcore/lab/models.py::LabResult`).

### §3.4 Interaction with the SP-A ledger (the safety-floor wiring)

The agent's emission sequence is **strictly ordered** (the structural
invariant that closes constraint §2.1):

1. `ledger_gate.check_budget(target)` — read
   `cumulative_n_trials(target)` and `EMISSION_QUOTA_PER_TARGET`
   (config, §4.1). If `cumulative + 1 > quota`, **hard-reject the
   emission** with a clear operator message, never invoke the LLM.
2. Build `EmissionContext` (§3.2).
3. Invoke the Anthropic SDK (the shipped wrapper from
   `ops.llm_data_triage`, reused verbatim — no new SDK code).
4. Validate the response against `EmittedSpec` (§3.3); a malformed
   response is REJECTED, no draft PR, no ledger spend.
5. `record_trial_spend(target=emitted.target_engine,
   trials=emitted.expected_trials, source="llm_emitter:<persona_sha>")`
   — the SP-A ledger row is written **before** the draft PR is opened.
   `source` carries a structured provenance prefix so an operator
   audit can grep all LLM-attributable trial spends.
6. Render the markdown spec; `gh pr create --draft` with the markdown
   body and the JSON sidecar committed.

If any step 1–5 fails, no draft PR is opened and no ledger row is
written (step 5 is the single point of mutation). If step 5 succeeds
but step 6 fails (gh CLI flake), the ledger row stands — by design:
the LLM consumed budget the moment it received the response, even if
the operator never sees the draft PR. The operator runbook
(`docs/llm_lab_emitter_operator_runbook.md`) documents the
"orphaned-spend" recovery: re-run the agent with `--replay <session>`
to reproduce the draft PR from the persisted `EmittedSpec`.

### §3.5 Interaction with the Lab Candidate Readiness checklist

The rendered markdown spec includes all ten Readiness sections,
mechanically populated from the `EmittedSpec`:

- §1 single pre-registered hypothesis ← `primary_hypothesis` +
  `falsification_criterion`.
- §2 feature-flag-variant pattern ← `param_ranges` (the SP-B shape
  requires exactly one `choice:` toggle for a `fold_existing`
  candidate; SP-G's pydantic validator pins this).
- §4 n_trials acknowledgement ← auto-generated paragraph citing the
  pre-emission ledger row from step 5.
- §5 roster-targeting prereq ← auto-generated one-liner using
  `target_engine` (proven targetable by step 1).
- §6 gate sacred ← static boilerplate from the persona.
- §7 lab credibility namespacing ← static boilerplate
  (`lab.<candidate_name>`).
- §10 compliance verifications ← auto-generated grep set.

§3 (byte-identical live path), §8 (data prereqs), §9 (lookahead
honesty) are the **operator review** sections — the LLM emits a
*draft* of these but the operator hardens them before the draft PR
leaves draft status. This is the explicit human-in-the-loop seam.

---

## §4 Safety posture (mirrors #187 + Epic E)

### §4.1 Emission budget (the rate-limit fence)

`EMISSION_QUOTA_PER_TARGET` (config in
`tpcore/lab/llm_emitter/contract.py`, frozen-defaults-overridable-by-
operator-only): the maximum cumulative LLM-attributable trial spend
per target engine before SP-G refuses to emit. Default: a low integer
(decision-point Q2, §10 — likely 20–50 per target). This is a
**second** rate limit *on top of* the SP-A cumulative-DSR fence: SP-A
makes more trials harder to graduate; SP-G further bounds how many
the LLM may even propose, so the operator's review budget is not
overwhelmed and the multiple-testing pollution per target stays
visibly bounded. Operator can reset/raise the quota via a separate
operator-only mechanism (NOT via the LLM — `feedback_event_driven_not
_scheduled` pattern: an operator-driven event, not an autonomous
tick).

### §4.2 Crash isolation (the daemon pattern)

The emitter co-task is added as a **third** crash-isolated
`_run_supervised` co-task inside `ops/llm_triage_service.py` (which
today hosts two: data-triage + engine-triage, per the DA-3
consolidated daemon spec). The two-daemon invariant test
(`tests/test_two_daemon_invariant.py`) is preserved — SP-G adds a
co-task, not a daemon. The 4-token process whitelist and launchd
label are unchanged.

### §4.3 Draft-PR-only

The agent invokes `gh pr create --draft` exclusively. A CI sentinel
test asserts the agent's source NEVER contains `gh pr create` without
`--draft` (mirrors the engine-LLM-triage `additive-only` fence). A
draft PR cannot self-merge; the operator (or a designated human
reviewer per the §1 review discipline) is the merge authority.

### §4.4 Diff-scope allow-list (build-time fence)

The agent's draft PR is allowed to touch ONLY:

- `docs/superpowers/specs/<date>-<candidate>-lab-candidate.md` (the
  rendered spec)
- `docs/lab/<date>-<candidate>-emitted-spec.json` (the machine-
  readable sidecar)
- A single new test file under the target engine (`<engine>/tests/
  test_lab_<candidate>_byte_identical.py` — the Readiness §3
  characterization test stub)

The agent is **forbidden** from touching `tpcore/`, `ops/` (other
than the sidecar), any engine `backtest.py` / `scheduler.py` /
`plugs/` / `order_manager.py`, `pyproject.toml`, `platform/
migrations/`, the `.claude/` tree, or any SoT/roster file. A CI
diff-scope test reds the build if the agent's draft PR crosses any
forbidden path. This is the build-time enforcement of §2.6.

### §4.5 Persona versioning lockstep

`docs/lab_emitter_persona.md` carries a `PERSONA_VERSION` constant
(SHA-pinned, mirrors `docs/engine_llm_triage_persona.md`
mechanism). A persona edit without a `PERSONA_VERSION` bump reds the
build (`tests/test_lab_emitter_persona_versioned.py`). The persona is
NOT a safety boundary — the deterministic fences above are — but the
versioning lets an operator audit "which persona produced this draft
PR" cleanly.

---

## §5 The graduation path (where SP-G stops, where the existing pipeline takes over)

```
operator: /lab-spec-emit --target <engine> [--reference-bundle <name>]
   │
   ▼
SP-G agent (this design)
   │  1. ledger gate (SP-A)         — REJECTS if over-budget
   │  2. build EmissionContext      — roster (SP-B), metrics (SP-D), refs
   │  3. Anthropic SDK call          — crash-isolated, credential-starved
   │  4. validate EmittedSpec        — pydantic v2 frozen, extra=forbid
   │  5. record_trial_spend(...)     — SP-A ledger row, BEFORE step 6
   │  6. gh pr create --draft        — markdown spec + JSON sidecar
   ▼
(operator review — the human-in-the-loop seam, §3.5)
   │  - hardens §3 byte-identical proof, §8 data prereqs, §9 lookahead
   │  - confirms readiness §1–§10 all ticked
   │  - moves PR from draft → ready-for-review
   ▼
Lab Candidate Readiness checklist (SP-C) — pre-flight gate
   │  - exactly one PARAM_RANGES toggle
   │  - live path files untouched (grep proof)
   │  - characterization golden present + RED-first
   ▼
python -m ops.lab --candidate <name> --target-engine <engine> --intent <i>
   │  - SP-B roster-resolved dispatch
   │  - SP-D pluggable metric ranking
   │  - SP-A cumulative-DSR-deflated gate
   ▼
held-back DSR/credibility gate (UNCHANGED, sacred)
   │  - survived = dsr >= 0.95 AND cred >= 60 AND n_trades >= 3
   ▼
dossier (ops/lab/dossier.py)  → docs/lab/<date>-<name>-{SURVIVED|FAILED}-seed*.json
   ▼
operator decision: ECR ADD or MODIFY (via /ecr skill)
   │  - /ecr is the SINGLE structured touchpoint
   │  - planner re-derives gate from dossier sidecar; never trusts text
   ▼
engine SDLC: LAB → PAPER (deterministic, automated post-ECR)
```

**SP-G's stop point is step 6 (draft PR open).** Every step after is
existing infrastructure, unchanged. The LLM never automates the
operator review, never invokes `ops.lab`, never moves a PR out of
draft, never authors an ECR, never edits the roster. The chain is
deliberately discontinuous at every gate.

---

## §7 Explicitly NOT in scope for SP-G (deferred to task #25)

SP-G ships the **thin emitter only**. The following are explicitly
deferred to the follow-on epic, task #25 (the "richer autonomous-LLM
+ quant" ambition per `project_research_llm_edge_discovery`):

1. **Autonomous data→analysis→idea loop.** SP-G emits on operator
   command + one explicit event class (§2.7). Task #25 will brainstorm
   the autonomous loop afresh, with the full §2 hard-constraint set
   re-bound (the SP-A ledger fence and the sacred gate are
   non-negotiable on the autonomous version too).
2. **Statistical toolkit integration.** No `statsmodels`, no `arch`,
   no `linearmodels`, no `scikit-learn`, no `scipy.stats` in SP-G.
   The LLM reads the operator-staged reference excerpts as text; it
   does not run statistical code. Task #25 will design the credential-
   starved statistical-toolkit sandbox.
3. **Driving the Lab repeatedly.** SP-G emits one candidate per
   `/lab-spec-emit` invocation. Task #25 may design a bounded-budget
   loop (still SP-A-gated, still draft-PR-only) but that design does
   not exist yet.
4. **Multi-hypothesis search.** SP-G's `EmittedSpec` is single-
   hypothesis by pydantic contract (§3.3, the Readiness §1 mandate).
   Task #25 may relax this only by adding *N* SP-A ledger increments
   per emission and *N* draft PRs, each independently gate-routed —
   never a single multi-hypothesis emission.
5. **Reading the n_trials ledger to *plan* which target is "cheapest"
   to attack.** SP-G's `EmissionContext` includes the ledger state
   because the operator may steer; the agent itself does NOT use it
   to optimise emission rate. Task #25 may explicitly design that
   optimisation under the §2 fences.
6. **Any auto-merge, auto-ready-for-review, or auto-ECR step.** Every
   gate in §5 is a deliberate operator hand-on-the-wheel.

The §10 open question Q1 surfaces task #25 explicitly for operator
sign-off at SP-G kick-off.

---

## §8 Test plan

### §8.1 Unit tests (`tpcore/lab/llm_emitter/tests/`)

- `test_emission_context_frozen.py` — `EmissionContext`,
  `EmittedSpec`, `RosterTarget` are frozen + `extra="forbid"`;
  malformed input raises pydantic `ValidationError`.
- `test_ledger_gate_rejects_over_budget.py` — `ledger_gate.check_
  budget` rejects when `cumulative_n_trials(target) + 1 >
  EMISSION_QUOTA_PER_TARGET`; verified the Anthropic SDK is NOT
  invoked in the rejected path (no ledger spend, no network).
- `test_emitted_spec_single_toggle.py` — `EmittedSpec.param_ranges`
  with >1 `choice:` toggle for `intent=fold_existing` is rejected
  (Readiness §1+§2 mandate).
- `test_emitted_spec_target_roster_membership.py` —
  `EmittedSpec.target_engine` must be ∈ `lab_targetable_engines()`
  at validation time; an LLM response naming `canary` / `sigma` /
  `allocator` is rejected.
- `test_ledger_spend_ordering.py` — `record_trial_spend` is called
  **before** `gh pr create`; a mock failure of step 6 leaves the
  ledger row standing (the documented "orphaned-spend" recovery
  contract).
- `test_persona_versioned.py` — a persona edit without
  `PERSONA_VERSION` bump reds the build.

### §8.2 Integration tests (`ops/tests/`)

- `test_llm_emitter_round_trip.py` — Anthropic client **mocked**
  (mirrors `ops.llm_data_triage` pattern); a synthetic `EmittedSpec`
  response renders into a valid Lab candidate markdown spec that
  the SP-C Readiness checklist's mechanical compliance set
  (`grep`-able items, §10) passes against. **The single load-bearing
  integration proof:** an emitted spec passes through SP-B (roster
  resolution), SP-C (Readiness), and SP-D (metric declaration) with
  **zero hand-editing**.
- `test_llm_emitter_draft_only.py` — agent source contains no
  `gh pr create` invocation without `--draft`; a CI sentinel test
  pins this.
- `test_three_cotask_invariant.py` — the
  `ops/llm_triage_service.py` daemon now runs three crash-isolated
  co-tasks; the two-daemon invariant
  (`tests/test_two_daemon_invariant.py`) is preserved
  (still two daemons, three co-tasks in one).
- `test_emitter_persona_isolation.py` — the credential-starved CI
  fence job has no `ANTHROPIC_API_KEY` and no Alpaca credentials in
  its environment.

### §8.3 Safety tests (the make-or-break, mirrors SP-D §5.2)

- `test_emitter_diff_scope_allow_list.py` — a synthetic emitted PR
  touching any path in the forbidden set (`tpcore/`, `ops/` non-
  sidecar, `pyproject.toml`, `platform/migrations/`, `.claude/`,
  `_PROFILE`, `providers.py`, any engine plug/scheduler) reds the
  build. This is the build-time enforcement of §2.6.
- `test_emitter_cannot_bypass_gate.py` — a contrived emitted spec
  that attempts to set `--dsr-threshold 0.5` or
  `--credibility-threshold 30` in its rendered run command is
  rejected by the renderer (a static grep against the rendered
  markdown — the run command must contain no gate-override flags
  below the floor).
- `test_emitter_cannot_edit_roster.py` — the agent's permission set
  has no write access to `tpcore/engine_profile.py` or
  `tpcore/providers.py`; verified by the existing `.claude/hooks/`
  ECR/DFCR-gated edit hooks (the hooks already block these paths
  for any actor; SP-G adds a test asserting the hooks are loaded in
  the emitter's CI environment).
- `test_emitter_cannot_self_merge.py` — a draft PR cannot be
  merged via `gh pr merge` without a `--undraft` (operator action)
  step; CI verification of GitHub branch protection on `main` is
  out of scope (operator-owned), but the sentinel test asserts the
  agent code path contains no `--undraft` invocation.

### §8.4 Clockwork (the consistency test, mirrors SP-B §2.6)

- `tpcore/tests/test_lab_emitter_consistency.py` — a roster ADD
  (synthetic `_PROFILE` mutation) makes the new engine appear in
  `EmissionContext.roster_targets` automatically; a roster REMOVE
  (RETIRED) drops it. SP-G's emitter never needs to be edited when
  the roster changes — the SP-B / Sigma 22-site-drift discipline
  applied to the LLM input surface.

### §8.5 Lane discipline

All new tests under `tpcore/tests/` and `ops/tests/` that import
`ops.llm_triage_service` or touch `sys.modules['ops']` carry
`pytestmark = pytest.mark.xdist_group("ops_shadow")` (the
`docs/DEV_PIPELINE_STANDARD.md` §2 ops-shadow rule).

---

## §9 Lane — heavy lane (mandatory full §1 pipeline)

SP-G is **heavy lane** per `docs/DEV_PIPELINE_STANDARD.md` §0
(triggers: a new advisory mechanism on the Lab front-half graduation
rail, touches `ops/llm_triage_service.py`, adds a new `tpcore/lab/`
sub-package, augments the operator-visible slash-skill set). The full
§1 pipeline applies: brainstorm → expert-harden → spec PR (this doc)
→ operator spec-read gate → plan PR → subagent-driven exec → split
review (spec-compliance first, then code-quality) → whole-suite +
order-flip authoritative gate → squash-merge.

---

## §10 Open questions / decision-point flags

Items requiring operator sign-off **before** plan-writing begins. The
first is the most important.

- **Q1 — Task #25 kick-off framing.** Per
  `project_research_llm_edge_discovery` *"⚠ OPERATOR AMBITION RAISED
  2026-05-20"*: at SP-G kick-off (i.e. now, before plan-writing) the
  operator should confirm: **(a)** SP-G ships exactly the thin
  emitter as designed here (current default per the "keep going /
  stick to the plan" answer); **(b)** task #25 is opened as a
  follow-on epic with its own brainstorm at start (NOT folded into
  SP-G); **(c)** task #25's brainstorm explicitly carries the §2
  hard-constraint set forward — the SP-A ledger fence and the sacred
  gate apply to the autonomous-quant version verbatim. This spec
  assumes (a)+(b)+(c); flag if any clause needs to change.

- **Q2 — `EMISSION_QUOTA_PER_TARGET` default.** §4.1 reserves the
  knob but does not pin a number. Suggested default: **20** per
  target (a single operator review session can plausibly absorb 20
  draft PRs across the roster; SP-A's monotone-harder DSR fence does
  the heavy lifting beyond that). Operator decides the pinned
  default at plan-write.

- **Q3 — `reference_excerpts` staging mechanism.** §3.2 says
  "operator-staged"; the concrete mechanism is open. Options:
  (i) a `docs/lab_emitter_references/` directory the operator
  populates manually, the agent reads the lot;
  (ii) a per-emission `--reference-bundle <name>` skill argument
  pointing at a named subset;
  (iii) the agent reads ALL of `ref_carver_systematic_trading` /
  `ref_chan_algorithmic_trading` (and future adds) verbatim every
  cycle. **Recommended (ii)** — bounded, operator-steered, mirrors
  the data-LLM-triage scoping discipline. Operator confirms.

- **Q4 — Where the rendered candidate spec lands.** §5 shows
  `docs/superpowers/specs/<date>-<candidate>-lab-candidate.md` (the
  SP-E pattern). Confirm vs an alternative
  `docs/lab/<date>-<candidate>-emitted-spec.md` (closer to the
  dossier output dir but inconsistent with the SP-E precedent).
  Recommendation: **stick with the SP-E path** — it is the form an
  operator already knows how to read.

- **Q5 — Slash-skill name.** `/lab-spec-emit` is proposed; alternatives
  `/lab-emit-candidate`, `/lab-llm-propose`. Operator decides at plan-
  write. Whichever is chosen, the skill file goes under
  `.claude/skills/` and is invocable per the CLAUDE.md skills
  convention.

- **Q6 — `LAB_LEDGER_CAPACITY_AVAILABLE` event class.** §2.7 reserves
  the event class for the explicit-event emission path; the schema +
  emitter (a deterministic ledger watcher, NOT the LLM) is a small
  follow-up. Confirm whether SP-G plan-PR should include this or
  defer to a sibling tracked-followup. Recommendation: **defer** —
  the operator-command path (`/lab-spec-emit`) is sufficient for
  v1 and avoids growing SP-G's scope.

---

## §11 Cross-references

- Epic: `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md`
  §SP-G.
- Predecessor specs: SP-A `2026-05-19-lab-ntrials-ledger.md`,
  SP-B `2026-05-19-lab-sp-b-roster-driven-targeting-design.md`,
  SP-D `2026-05-20-lab-sp-d-pluggable-scoring-design.md`,
  SP-E `2026-05-20-sentinel-maxdd-lab-candidate.md`.
- Checklists: `docs/superpowers/checklists/lab_candidate_readiness.md`
  (SP-C), `docs/superpowers/checklists/engine_change_request.md`.
- Safety precedents: `docs/superpowers/specs/2026-05-18-llm-triage-
  advisory-layer-design.md` (#187 data-LLM-triage),
  `docs/superpowers/specs/2026-05-18-engine-llm-triage-advisory-layer-
  design.md` (Epic E engine-LLM-triage).
- Memory (cited by name in §1, §2, §7): `project_research_llm_edge_
  discovery` (HARD CONSTRAINT section + "⚠ OPERATOR AMBITION RAISED
  2026-05-20" sub-section), `project_ml_research_track` (commissioned-
  expert verdict on automated edge-search), `ref_carver_systematic_
  trading`, `ref_chan_algorithmic_trading`,
  `feedback_event_driven_not_scheduled`,
  `feedback_symmetry_not_copy`.
- Lane standard: `docs/DEV_PIPELINE_STANDARD.md` §0 (heavy-lane
  trigger), §1 (mandatory pipeline), §2 (ops-shadow xdist_group),
  §3 (authoritative gate = whole-suite + order-flip).
- CLAUDE.md: Universal invariants (paper-only mandate, SIP default,
  no yfinance/Discord/manual), Engine roster changes → `/ecr`,
  Hard safety invariant (DATA_OPERATIONS_COMPLETE), the engine-build
  compliance shortlist.
