# Engine-Lane LLM Triage Advisory Layer (Engine Ladder R5 / Epic E) — Design **v1 (expert-hardened)**

**Status:** spec **v1.1 (expert-hardened + parity-scoped)** 2026-05-18
(ENGINE lane). Brainstorm-by-investigation (integration contract
locked, read-not-guessed) → spec (draft) → expert-harden (FORK A &
FORK B resolved, §7 novelty premise FIXED after found structurally
broken) → **operator parity-scope directive folded in (v1.1: §1
coverage-parity, §7a deterministic detection-gap + Phase 0, §9/§10/§11
updated)** → operator spec-review gate → plan → phased subagent build.
Engine Escalation & Hardening Ladder **R5** (`docs/ENGINE_ESCALATION_
HARDENING_LADDER.md`, currently "LLM/agentic triage: OUT of scope
(Epic E)").

**Ownership:** the engine-lane symmetric agent was never started
(deferred Epic E); ownership was transferred to the data-lane session
2026-05-18 (memory `project_engine_llm_triage_ownership`). This is the
**symmetric mirror of the shipped data-lane #187**
(`docs/superpowers/specs/2026-05-18-llm-triage-advisory-layer-design.md`,
BUILT) — **symmetry-of-approach, NOT a clone** (`feedback_symmetry_not_copy`):
reuse the pattern/contract/decision-shape; design engine-native
components.

## 0. Locked constraints (carried from the #187 expert envelope + the engine handoff — do NOT re-litigate)

- Advisory + human-gated **only**. Never auto-applied to live
  trading. Accelerates the engine Ladder **R3** human
  (discovery→dossier→convert); never replaces fail-closed **R1–R4**.
- Deterministic agents (DA-1 `engine_supervisor`, DA-2 `aar_autotune`,
  forensics, allocator, RiskGovernor) stay deterministic. The LLM
  layer sits strictly **atop** the existing fail-closed engine Ladder
  — not in any trading / risk / data-mutation runtime path.
- The LLM is never the mutating actor: no `tools`, no live creds, no
  real-tree write, no merge authority; produces only a **draft,
  human-merge-only PR** (additive, mechanism-free binding pointing an
  already-proven existing mechanism at the novel class) + a
  machine-checkable dossier + a non-authoritative proposal event.
- Deterministic fence gates everything (LLM self-judgment gates
  nothing): hard-denied protected paths auto-fail; provenance check
  (additive-only / mechanism-free / proven-only) required in CI;
  two-human review; inert-until-merged; post-merge canary/shadow.
- Credential-starved ephemeral sandbox; CI fence credential-starved
  (never references `ANTHROPIC_API_KEY`). Official Anthropic SDK,
  doc-grounded (`feedback_use_official_docs`). Created/versioned
  persona (not a safety boundary — the fence is).

## 1. Role

For a genuinely NOVEL engine-lane escalation **instance/pattern** that
the deterministic Ladder left open + undispositioned past grace (the
overdue-R3 set — see the FIXED §7; NOT an "unknown failure_class",
which is structurally impossible), the agent diagnoses in a starved
worktree (reads repo + runs the fixture suite/ruff) and produces a
review-ready **draft PR**: an additive, mechanism-free
`DISPOSITION_POLICIES` binding (engine self-heal layer confirmed
absent — §3) that proposes an **existing
`EngineEscalationDisposition`** verb for the pattern, plus a dossier.
It accelerates the R3 human; it does not replace R1–R4.

**Coverage parity with the data LLM (operator directive 2026-05-18,
post-expert).** This agent must reach the **same level of operation as
the shipped data-lane #187**: just as the data LLM is *aware of the
whole data service* but its **primary job is to jump in for the novel
failure the deterministic self-heal/audit suite does NOT cover** (and
produce the permanent fix so the deterministic suite *will* cover it
next time — the engine is never mutated by the LLM; the deterministic
path keeps it whole), the engine LLM's coverage surface is **the full
engine lane *and* every platform service co-hosted in the engine
daemon** (the engines/schedulers, plus the sweep poll-loop, the
`TradeMonitor` Alpaca stream, the weekly-digest trigger, and the
engine daemon itself). Its primary job is the **novel subset the
deterministic engine suite (DA-1 supervisor / DA-2 autotune / Ladder
R1–R4) leaves open + undispositioned**. Achieving this requires
closing a **deterministic detection gap** that exists upstream of the
LLM — see §7a; the LLM never becomes the detector (that would violate
"deterministic agents stay deterministic" / the LLM sits strictly
atop the fail-closed Ladder).

## 2. The engine stays deterministic (the "data stays 100%" analogue)

The agent/PR NEVER triggers/runs/queues a repair, a self-heal, a
trade, an allocation, a hold, or a disposition. A present escalation
is resolved only by the existing deterministic Ladder path (the R3
human, or R1–R4). The PR is the *future permanent* fix only, inert
until a human merges it; even merged it is shadow/canary before it
can influence anything.

## 3. Deterministic enforcement (reuse the #187 fence; engine registries)

The hard-denied + provenance + canary machinery is **pure and
lane-agnostic** in `tpcore/llm_data_triage/{fence,canary}.py`.

**FORK A — RESOLVED: option (a-deferred) — engine-native thin modules
that IMPORT the shipped pure data-lane functions as-is; the
shared-core *rename/extraction refactor* is explicitly OUT of Phase
1.** Rationale: a single fence/provenance/canary implementation is a
SAFETY asset (two could silently diverge — one hard-denied list to
audit), so the engine lane MUST consume the *same code object*, not a
twin. But the data-lane fence/canary/SDK-wrapper are already merged,
reviewed and live (#187, PRs #56/#58/#59) on a live-money system; a
package-rename extraction (`tpcore/llm_data_triage` →
`tpcore/llm_triage`) is a broad regression surface (every import site,
the CI fence script path, the persona-lockstep test) for **zero
behavioural gain**. Therefore: (1) the engine lane gets thin
engine-native modules `tpcore/engine_llm_triage/{select,packet}.py` +
`docs/engine_llm_triage_persona.md` (the lane-specific inputs); (2)
those modules and the engine agent **import the shipped pure
functions verbatim** from `tpcore.llm_data_triage.fence` (hard-denied
path matcher, provenance evaluator) and `tpcore.llm_data_triage.canary`
and the Anthropic-call/no-key/malformed-envelope wrapper in
`ops.llm_data_triage` — no fork, no copy, one fence object; (3) those
pure modules are **promoted to lane-agnostic by parameter, not by
move**: where they reference the data-lane provenance baseline they
take it as an injected argument (the engine passes
`ops.engine_ladder.DISPOSITION_POLICIES`; data passes its
HealSpec/RemediationSpec set) — a small, additive, locally-reviewable
signature change on shipped code, NOT a package relocation. (4) A
follow-up hardening item (post-build, separate PR, NON-blocking)
records the *option to* rename the now-shared package to a
lane-neutral name once both lanes are green — purely cosmetic, never
on this spec's critical path. Net: one fence, zero clone, minimal
blast radius on live code. The misnomer `tpcore.llm_data_triage` being
imported by an engine module is acceptable and documented; correctness
> nominal purity. Provenance baseline = the engine registry
`ops/engine_ladder.DISPOSITION_POLICIES` **only**. **Confirmed by
reading (resolves the §11 open question): there is NO engine-lane
HealSpec/RemediationSpec set** — `tpcore/selfheal/registry.py` is
data-lane-only; the engine lane's sole declarative SoT is
`DISPOSITION_POLICIES`. The engine provenance check therefore proves
exactly one property: the proposed additive `DISPOSITION_POLICIES`
entry binds the novel pattern to an **already-existing
`EngineEscalationDisposition` value** (`converted`/`structural`/
`removed`) — it can NEVER introduce a new disposition enum member, a
new escalation-class semantic, or edit an existing policy (all
human-only, hard-denied). There is no "point an existing proven
`ops.py --stage` at it" analogue because the engine lane has no
auto-repair actor — the only "proven mechanism" the LLM may point at a
novel pattern is an existing disposition *verb*. Hard-denied
set must include the engine-lane protected paths (`tpcore/risk/`,
`tpcore/order_management/`, `platform/migrations/`, `*/providers.py`,
`scripts/ops.py`, the DSR/credibility gate, **and** the engine
deterministic actors `ops/engine_supervisor.py`, `ops/aar_autotune.py`,
`tpcore/supervisor_state.py`, `ops/engine_ladder.py` mechanism code —
the agent may add an additive *policy binding* but never edit the
ladder/​supervisor *mechanism*).

## 4. Vetoes (verbatim from #187 — apply to the engine lane)

Any LLM-proposed new/widened mechanism (new disposition semantics,
new escalation class semantics, loosened policy, edit to an existing
policy/spec, new daemon, new param/bound); merge authority; live
creds in the sandbox; real-tree writes; treating the persona as a
safety boundary. All vetoed; the provenance check + hard-denied paths
enforce it deterministically.

## 5. Persona (`docs/engine_llm_triage_persona.md`, created + versioned)

An engine-native, versioned system prompt (lockstep `PERSONA_VERSION`),
mirroring the #187 persona structure but with the engine output
contract: proposed additive `DISPOSITION_POLICIES` binding for the
novel escalation **pattern** (an existing `EngineEscalationDisposition`
verb), dossier, confidence, explicit "could not determine"; hard
guardrails (no authority, defer to the R3 human, never invent
internals, never propose a new mechanism/disposition member); states
it is **NOT a safety boundary**.

## 6. Official Anthropic SDK (reuse #187 exactly)

Same `client.messages.create(model="claude-sonnet-4-6",
max_tokens=…, temperature=0.0, system=<persona>, messages=[…])`, **no
`tools`**, `tpcore.outage.with_retry`, no-key/AuthenticationError safe
no-op, malformed-response per-escalation isolation, crash-isolated.
Doc-grounded; mock matches the real `Message` shape. **Per resolved
FORK A: the engine agent imports and reuses the shipped
`ops.llm_data_triage` Anthropic-call/no-key/malformed-envelope wrapper
verbatim — one SDK-call implementation, no twin.** The plan re-fetches
+ pins the exact current model id / SDK / `Message` shape from
official docs (context7; `feedback_use_official_docs`) — not from
memory; `claude-sonnet-4-6` above is illustrative, not the pinned
constant.

## 7. Trigger / novelty predicate (engine-native) — **PREMISE FIXED (was structurally broken)**

> **EXPERT-HARDEN — FATAL FLAW FOUND & CORRECTED.** The draft defined
> *novel ⇔ `policy_for(failure_class) is None`*. Read against the
> code, **that predicate is structurally unreachable in production —
> the engine "novel" set is provably empty.** Evidence:
> `ops.engine_supervisor.INFRA_FAILURE_CLASSES` is a hardcoded
> `frozenset({"crashed_startup","scheduler_crash","data_request_
> timeout","data_repair_escalated","missed_cycle"})` and
> `ops.aar_autotune._BEHAVIORAL = "behavioral"`. Those two pinned
> constants are the **only** values either DA-1's or DA-2's
> `_emit_escalated` can ever stamp into `data->>'failure_class'`.
> `engine_ladder.KNOWN_ESCALATION_CLASSES = INFRA_FAILURE_CLASSES |
> {_BEHAVIORAL}` and `escalation_drift()` **fails the build** the
> instant any KNOWN class lacks a `DISPOSITION_POLICIES` row (the R2
> tooth). All 6 are already policied today. So an emitted
> `failure_class` is *always* in `DISPOSITION_POLICIES` ⇒
> `policy_for(failure_class)` is *never* `None` in production. A class
> for which it could be `None` cannot reach `main` (CI red), so it can
> never reach the bus to be triaged. **The clockwork that makes the
> ladder safe is exactly what makes the draft's novelty trigger dead
> code.** The data-lane analogue does not transfer: the data lane has
> an `ESCALATE_OPERATOR` *disposition value* a class can carry while
> still being known; the engine `EngineEscalationDisposition` enum has
> **no `ESCALATE_OPERATOR` member** (`converted`/`structural`/
> `removed` only) and **no auto-conversion actor** — every engine
> escalation, of every known class, requires a human R3 disposition.

**Corrected novelty predicate — the genuinely-novel engine input is a
new escalation *instance/pattern* the deterministic policy could not
auto-dispose and that has aged past grace, NOT an unknown class:**

- Trigger event: **`ENGINE_ESCALATED`** (single event; covers DA-1
  infra + DA-2 behavioral) on `platform.application_log`.
- Escalation-ref key: `data->>'hold_id'` (from
  `engine_supervisor._emit_escalated`/`aar_autotune._emit_escalated`,
  payload `{schema, hold_id, engine, failure_class, reason, …}`).
- **Novel-and-actionable** ⇔ the `hold_id` is in
  `ops.engine_ladder.list_undispositioned(pool)` — i.e. it is an
  **open, undispositioned escalation that has aged past
  `_GRACE_DAYS`** (the engine lane's own R3-overdue set, which already
  applies the held / escalate-only-fingerprint-resolved open-set
  logic). This is the engine-native mirror of the data lane's
  "open + undispositioned + no deterministic auto-conversion" set:
  *no deterministic actor will ever dispose it* (no engine
  auto-conversion exists), the grace window proves the R3 human has
  not yet acted, and `policy_for(failure_class).default` is the
  *recommended* disposition the LLM must justify or argue against —
  NOT a gate that filters it out. `select_novel_escalations` therefore
  **calls `engine_ladder.list_undispositioned()` directly** (it
  already encodes the correct open/grace/escalate-only semantics) and
  filters its result; it does **not** test `policy_for() is None`
  (dead) and does **not** reimplement the open-set (the bug
  symmetry-not-copy forbids).
- **Still-open / grace / escalate-only** are NOT re-derived here:
  they are *exactly what `list_undispositioned()` already computes*
  (the `_CANDIDATE_SQL` anti-join on `ENGINE_CLEARED` XOR
  `ENGINE_ESCALATION_DISPOSITIONED`, the `_GRACE_DAYS` cutoff, and the
  shared `_escalate_only_still_open` fingerprint-resolution gate).
  Consuming that one function is the anti-divergence guarantee.
- Dedup: no prior `ENGINE_LLM_TRIAGE_PROPOSAL` for that `hold_id`
  (one-terminal dedup; exactly one attempt per escalation, ever).
  Bounded oldest-first, `MAX_TRIAGE_PER_CYCLE` (reuse the #187 cap).
- `select_novel_escalations` = `list_undispositioned(pool)` →
  drop any `hold_id` with a prior `ENGINE_LLM_TRIAGE_PROPOSAL` →
  oldest-first cap. It **must not** reimplement the open-set,
  grace, or escalate-only logic and **must not** test
  `policy_for() is None` (proven dead) — reimplementation is the
  exact bug symmetry-not-copy forbids. `policy_for(failure_class)` is
  still called, but only to attach the *recommended* policy default +
  rationale to the packet (advisory context for the LLM), never as a
  selection gate.

## 7a. Deterministic detection prerequisite — close the engine-daemon platform-service blind spot (**Phase 0; deterministic, NO LLM**)

> **GAP FOUND (read, not guessed; the operator's parity directive
> surfaced it).** The corrected §7 trigger consumes
> `engine_ladder.list_undispositioned()`, which draws from
> `ENGINE_ESCALATED`. DA-1 (`engine_supervisor`) only monitors
> **per-engine scheduler lifecycle** (STARTUP/SHUTDOWN/DATA_REQUEST
> for the roster engines); DA-2 (`aar_autotune`) is purely behavioral
> (`forensics_triggers`). **The platform services co-hosted in the
> engine daemon have NO escalation path at all:** when the sweep
> poll-loop, the `TradeMonitor.run_forever()` Alpaca stream, or the
> weekly-digest trigger dies, `ops/engine_service._run_supervised`
> only logs `engine_service.task_crashed` and silently restarts after
> backoff — **no `ENGINE_ESCALATED`, nothing in the Ladder**; the
> weekly-digest subprocess failure is swallowed (`logger.error` +
> return); a dead `engine_service` daemon is respawned by launchd
> `KeepAlive` with no application_log event. So an LLM that triggers
> off the Ladder is **structurally blind to exactly the
> engine-daemon-platform-service failures the parity directive
> requires covered.** The data lane has no analogue of this gap
> because its deterministic detector (the validation suite) already
> spans the whole data surface.

**Resolution — a small DETERMINISTIC emitter, not an LLM poller.** To
give the LLM the same complete surface the data lane has, the
engine-daemon platform-service failures must be escalated **into the
Ladder by a deterministic emitter**, after which they flow through
`list_undispositioned()` and the §7 predicate **with zero predicate
change** (the elegance of consuming the existing function). The LLM
must **never** be the silence-detector (that would make the LLM a
deterministic-control component — vetoed; "deterministic agents stay
deterministic", the LLM sits strictly atop the fail-closed Ladder;
symmetric with the data lane where the validation suite — not the LLM
— is the detector). Concretely, **Phase 0 (deterministic, no LLM,
own gated PR, lands first)**:

- Add a deterministic escalation emitter for co-hosted-task death in
  `ops/engine_service._run_supervised` (and the swallowed
  weekly-digest failure path): on a task crash that recurs past a
  bounded restart budget, emit `ENGINE_ESCALATED` with a new
  **platform-service** `failure_class` (e.g.
  `engine_service_task_crashloop`) + a stable `hold_id`, mirroring
  `engine_supervisor._emit_escalated`'s payload/`_INSERT_SQL` exactly
  (reuse, do not re-author).
- Register the new class(es) in
  `engine_supervisor.INFRA_FAILURE_CLASSES` (or a sibling
  platform-service set) **and** add the matching
  `DISPOSITION_POLICIES` row(s) in the **same change** — the R2
  `escalation_drift()` clockwork *fails the build* otherwise (this is
  the forcing function working as designed; it also keeps the §7
  "every emitted class is policied" invariant true).
- A silent-absence detector (sweep produced no trigger / no trade
  updates streamed / digest idempotence-key never advanced past its
  due window) MAY be added to DA-1 as a deterministic check that
  emits the same `ENGINE_ESCALATED` — deterministic, bounded,
  testable; **scope/'how far' is a Phase-0 design sub-question for
  the plan's expert pass**, but it is deterministic engine-lane work,
  explicitly NOT the LLM's job.
- Phase 0 is **deterministic-agent / Ladder-coverage work** (it
  extends DA-1's reach), not LLM work. It is a hard **prerequisite**:
  Phases 1–4 (the LLM) do not deliver the operator's parity goal
  without it, because the LLM can only triage what the deterministic
  layer escalates. Sequenced first; its own brainstorm-confirmed
  sub-scope, spec section, and gated PR.

Net: deterministic detection feeds the Ladder (Phase 0); the LLM
triages the novel/undispositioned subset of the **now-complete**
surface (Phases 1–4) — true symmetry with the data lane.

### §7a — Phase-0 scope FROZEN (plan Task 0.1 expert sub-pass, code-grounded; build to this verbatim)

| Decision | Frozen value |
|---|---|
| Class 1 (Task 0.2) | `engine_service_task_crashloop` — covers any co-hosted `_run_supervised` task (`sweep`, `monitor`) |
| Class 2 (Task 0.3) | `engine_service_digest_failed` — swallowed weekly-digest failure path |
| Class set | new `PLATFORM_SERVICE_FAILURE_CLASSES` frozenset in `ops/engine_supervisor.py`; `engine_ladder.KNOWN_ESCALATION_CLASSES = INFRA_FAILURE_CLASSES \| PLATFORM_SERVICE_FAILURE_CLASSES \| {_BEHAVIORAL}`. NOT added to `INFRA_FAILURE_CLASSES` (keeps `_auto_clear` correctly inert — these have no per-engine cycle/clearer) |
| Crash-loop budget | **3 crashes within a rolling 600s window**, per-task `deque` of crash timestamps in `_run_supervised`, emit once per crossing, reset `escalated` flag when the deque empties (recovered task that re-loops re-escalates) |
| `hold_id` | `"engsvc-" + hashlib.sha256(f"{failure_class}\|{task_name}".encode()).hexdigest()[:16]` — deterministic, stable per fault identity (NOT uuid4 — required for §7 prior-proposal dedup + Ladder open-set anti-join) |
| `engine` column | `f"engine_service:{task_name}"` (e.g. `engine_service:sweep`, `engine_service:weekly_digest`) — human-legible Ladder/digest line |
| Emit mechanism | reuse `ops.engine_supervisor._emit_escalated(pool, engine, hold_id, failure_class, reason, attempts)` **verbatim** (acyclic import; payload/`_INSERT_SQL` byte-parity). **Escalate-only — NO paired `_emit_held`** (no per-engine hold lifecycle/clearer; an empty-`triggers` escalate-only row is permanently open in `list_undispositioned` until R3 disposition — exactly the desired surface). Wrap the emit in `try/except Exception: logger.error` so an emit DB failure cannot defeat the "one crashed co-task must never kill its sibling" invariant |
| Digest path | `_maybe_fire_weekly_digest` (`ops/engine_service.py` ~L101–121), BOTH the `except` and `else rc!=0` branches; success (`rc==0`) emits nothing; function already structurally never raises — keep it so |
| Detectors IN | (0.2) crash-loop emitter; (0.3) digest-failure emitter — the only two with a crisp non-flaky deterministic predicate today |
| Detectors DEFERRED (follow-up) | (a) no-sweep-in-N-windows, (b) no-trade-updates-while-market-open, (c) digest-key-stalled — each lacks a non-flaky deterministic predicate now (false-positive on a live trading daemon has real cost); when built they live in new `engine_supervisor._detect_*` (DA-1 is the detector home; `engine_service` stays a thin co-host). Record as a tracked follow-up. |
| Disposition (both) | `EngineEscalationDisposition.STRUCTURAL` (identical semantics to `scheduler_crash`/`missed_cycle`); add `DISPOSITION_POLICIES` rows in the SAME PR (R2 `escalation_drift()` forces it) |
| Plumbing | add `pool` param to `_run_supervised` (2 call sites in `_amain`, both hold `pool`) and to `_maybe_fire_weekly_digest` (2 call sites in `_main_loop`, both hold `pool`). Additive, no behavior change. No cycle (`engine_supervisor` does not import `ops.engine_service`). |
| Fatal objection | NONE. Pool headroom (`POOL_MAX_SIZE=6`) absorbs the rare bounded emit. |

## 8. One canonical mechanism — invocation (**FORK B — RESOLVED: B1**)

The data lane shipped a dedicated advisory daemon
`ops/llm_triage_service.py` (event-driven, `application_log` bus).
`scripts/tests/test_two_daemon_invariant.py` enforces a **closed
4-token installer whitelist** (verbatim):
`{install_launchd_engine_service,
install_launchd_data_repair_service, install_launchd_data_operations,
install_launchd_llm_triage_service}` — asserted by
`test_manifest_loop_is_exactly_the_three_surviving_installers` as
`_installer_loop_tokens() == {…}` (an exact set equality on the
`for installer in …; do` loop, with a guardrail-of-the-guardrail test
proving it still bites on any rogue token). `install_launchd_llm_
triage_service` is *already* in the whitelist (the #187 advisory
lane).

**FORK B — RESOLVED: (B1) — extend `ops/llm_triage_service.py` to
co-host BOTH lanes' triage loops as two independent `_run_supervised`
co-tasks on the one advisory pool.** This is the single biggest call
and the SAFETY argument is decisive, not merely the topology
convenience:

- **Process isolation from the live trade path is a HARD
  REQUIREMENT, so B2 is rejected.** B2 would put an LLM
  `messages.create` call (network, multi-second, retried via
  `with_retry`) plus `git worktree`/`gh` subprocess spawns *inside the
  same process, event loop, asyncpg pool and signal-handler set as the
  live trade-submit sweep* (`engine_service._run_supervised`). On a
  live-money daemon that is an unacceptable blast radius: a slow/hung
  LLM call starves the event loop that must place/monitor real orders;
  worktree/gh subprocess pressure and FD/temp churn share the
  trade process; a malformed-envelope or SDK crash, even if
  per-escalation-isolated, raises the crash surface of the trade
  daemon; and the advisory pool's connections contend with the
  trade-submit pool. None of these risks buy anything — the advisory
  loop has zero coupling to the trade path. B2 is vetoed on safety.
- **B3 (5th daemon) is rejected** — it breaks the closed-whitelist
  invariant and the operator's DA-3 "two long-lived per lane + the
  data-ops cron" consolidation for no gain B1 doesn't already give.
- **B1 is correct.** The advisory daemon *already exists, is already
  in the whitelist, is already process-isolated from
  `engine_service`*, and is the natural home for a second
  lane-agnostic triage loop. Add an engine co-task
  (`_run_supervised`-wrapped, cursor-polling `ENGINE_ESCALATED`) beside
  the existing data co-task; the daemon's *concept* is generalised to
  "triage-service" in comments/docstrings **but the installer name,
  the launchd label, and the 4-token whitelist are UNCHANGED** — so
  `test_two_daemon_invariant.py` requires **zero edits** and the
  topology invariant is preserved by construction (verify in Phase 3
  that the test still passes untouched; if any whitelist edit is ever
  needed, that is a red flag the placement is wrong). Both loops crash-
  isolated from each other (independent `_run_supervised`); a hung
  engine LLM call cannot starve the data loop or any trade process.

The chosen placement reuses the `_run_supervised` + `mkdir`-atomic
self-exclusion lock + `_startup_worktree_prune` + cursor-poll
idioms verbatim; event-driven on the existing bus
(`feedback_event_driven_not_scheduled`); NOT scheduled, NOT a linear
script step. The git-hygiene rule-3 isolation
(`feedback_git_hygiene_method`) applies to every new test/CI path
(no real git/gh against the working repo; loud host-guard); the CI
fence script reuses the #63-hardened worktree-prune pattern.

## 9. Non-goals / scope

- **In scope (parity, per §1):** the full engine lane + every
  platform service co-hosted in the engine daemon (sweep poll-loop,
  `TradeMonitor` stream, weekly-digest trigger, the daemon itself) —
  via Phase 0 making them escalate deterministically into the Ladder,
  then the LLM triaging the novel/undispositioned subset (§7a).
- The LLM is **never** the detector/silence-poller for platform
  services (deterministic Phase-0 work owns detection); the LLM only
  triages what the deterministic layer escalated. "Deterministic
  agents stay deterministic" is preserved.
- Engine-lane only (the data lane is shipped; aar-lane is the same
  Ladder family — in-scope only as the behavioral escalation source,
  not a separate agent).
- Not in any trading / risk / allocation / deterministic-agent
  runtime path. No auto-apply, ever. Not a dashboard write surface.
- Branch-protection / CODEOWNERS / merge-less bot identity / the
  label / removing the CI secret are GitHub-settings — extend the
  **existing** `docs/llm_data_triage_operator_runbook.md` (one
  runbook for both lanes) rather than a second runbook.

## 10. Phasing (fence-first; mirrors #187; reuse-maximising per resolved FORK A/B)

| Phase | Deliverable |
|---|---|
| **0** | **Deterministic detection-gap closure (NO LLM; prerequisite; lands first).** Per §7a: deterministic `ENGINE_ESCALATED` emitter for engine-daemon co-hosted-task crash-loop / swallowed-digest-failure (and, per the plan's Phase-0 expert sub-pass, bounded silent-absence detection) in `ops/engine_service`/DA-1, mirroring `engine_supervisor._emit_escalated` verbatim; new platform-service `failure_class` + its `DISPOSITION_POLICIES` row added in the SAME change (R2 `escalation_drift()` forcing function); fixture tests for each new escalation path; engine Ladder doc + `escalation_drift` coverage updated. Own gated PR. This makes the Ladder/​`list_undispositioned()` surface *complete* so Phases 1–4 can achieve the operator's parity goal. Deterministic-agent work, not LLM. |
| 1 | **Safety skeleton, no LLM, dark.** Per FORK A(a-deferred): engine-native `tpcore/engine_llm_triage/{select,packet}.py` that **import the shipped pure `tpcore.llm_data_triage.{fence,canary}` functions verbatim** (NO package extraction/rename — out of scope; correctness > nominal purity); the small additive *parameterise-the-provenance-baseline-by-arg* change to those shipped pure modules (engine passes `engine_ladder.DISPOSITION_POLICIES`), locally reviewed, no behaviour change to the data lane (re-run #187's fence tests as a regression gate). `select` = `engine_ladder.list_undispositioned()` + no-prior-`ENGINE_LLM_TRIAGE_PROPOSAL` dedup + oldest-first cap (**per the FIXED §7 — NOT `policy_for() is None`**). Read-only `packet` (ENGINE_ESCALATED payload + `current_hold` + open `forensics_triggers` + engine profile + the *advisory-only* `policy_for` default/rationale). Hard-denied set incl. the engine actor paths (`ops/engine_supervisor.py`, `ops/aar_autotune.py`, `tpcore/supervisor_state.py`, `ops/engine_ladder.py` mechanism). **`docs/engine_llm_triage_persona.md`** + `PERSONA_VERSION` lockstep. Unit-tested; dark. |
| 2 | **The agent + official Anthropic call (mocked, dark).** Engine `run_triage` reusing the shipped `ops.llm_data_triage` Anthropic-call/no-key/malformed-envelope wrapper verbatim, `ENGINE_LLM_TRIAGE_PROPOSAL` emit (mirror `engine_ladder._INSERT_SQL`/`_emit`), no-key/crash/malformed fail-safes, import-isolation clockwork guard (the agent's import closure must NOT pull `tpcore.risk`/`order_management`/`engine_supervisor`/`aar_autotune`/`engine_ladder` actor paths). Mocked in CI; dark. |
| 3 | **Wire event-driven (FORK B = B1).** Add the engine co-task inside the existing `ops/llm_triage_service.py` (second `_run_supervised` loop, `ENGINE_ESCALATED` cursor-poll, shared advisory pool, crash-isolated from the data loop); **`scripts/tests/test_two_daemon_invariant.py` MUST pass with zero edits** (installer/label/4-token whitelist unchanged — assert this explicitly; any required whitelist edit means the placement is wrong). Label-gated CI fence job extended in the existing `ci.yml` (credential-starved), draft-PR sandbox (#63-hardened worktree handling), proposal surfaced on the engine weekly-digest line. |
| 4 | **Docs.** Engine Ladder R5 OUT→BUILT (same R1–R4 convention); CLAUDE.md engine-lane bullet; the shared `docs/llm_data_triage_operator_runbook.md` extended; this spec → BUILT + build record; memory `project_engine_llm_triage_ownership` updated to BUILT. Optional non-blocking follow-up item recorded: cosmetic lane-neutral rename of the shared pure package (NEVER on this spec's critical path). |

## 11. Open questions — ALL RESOLVED (expert-hardened; read, not guessed)

- **§7 novelty premise: FOUND BROKEN, FIXED.** `policy_for(failure_
  class) is None` is structurally unreachable (closed
  `INFRA_FAILURE_CLASSES`+`_BEHAVIORAL` set; `escalation_drift()`
  fails the build on any unpoliced known class ⇒ every emitted class
  is always policied). Corrected predicate = the `hold_id` is in
  `engine_ladder.list_undispositioned()` (open + undispositioned +
  past `_GRACE_DAYS`), minus prior-proposal dedup. See §7.
- **FORK A: RESOLVED** — engine-native thin `select`/`packet`/persona
  that import the shipped pure data-lane fence/canary/SDK-wrapper
  verbatim (provenance baseline injected by arg); NO package
  extraction/rename in Phase 1 (one fence object, minimal blast
  radius on live code). §3 + §10.
- **FORK B: RESOLVED — B1** — co-host an engine triage co-task inside
  the existing process-isolated `ops/llm_triage_service.py`; B2
  vetoed on safety (no LLM/worktree/gh loop in the live trade
  daemon), B3 vetoed (breaks the closed-whitelist invariant + DA-3).
  Topology test passes unedited. §8 + §10.
- **Engine self-heal spec set: CONFIRMED ABSENT** —
  `tpcore/selfheal/registry.py` is data-lane-only; the engine's sole
  declarative SoT is `DISPOSITION_POLICIES`. Provenance gates exactly
  one property: an additive binding to an existing
  `EngineEscalationDisposition` member. §3.
- Trigger/ref/open-set/escalate-only predicates: resolved (§7, exact
  `engine_ladder`/`supervisor_state` APIs).
- `project_ml_research_track`: **no veto** — the ML verdict targets
  DSR-`n_trials` inflation in *backtest/credibility*; this layer is
  advisory, human-gated, non-runtime, and never touches a backtest,
  rubric, or trading path. Out of that veto's scope.

- **Coverage-parity directive (operator 2026-05-18, post-expert):
  RESOLVED via §1 + §7a + Phase 0.** Parity with the data LLM
  requires covering the engine-daemon platform services; those
  currently produce NO escalation (gap proven by reading
  `engine_service._run_supervised`). Resolution: a deterministic
  Phase-0 emitter feeds them into the Ladder; the LLM then triages
  the novel/undispositioned subset with **no §7 predicate change**.
  The LLM is never the detector. Phase 0 is a deterministic
  prerequisite, sequenced first, its own gated PR.

**Both forks resolved; the §7 fatal flaw fixed; the operator
parity-scope directive folded in (§1/§7a/§9/Phase 0). Spec is
internally consistent and ready for the operator spec-review gate.**
