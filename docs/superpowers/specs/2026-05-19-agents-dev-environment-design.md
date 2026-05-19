# Agents + Development Environment — Design **v1 (expert-hardened, operator-approved)**

**Status:** design **v1** 2026-05-19. Brainstorm → expert-harden →
operator-approved (scope "b", recommended-sequence autonomous) → **spec
(this doc)** → implementation plan → phased subagent build. **Spec 2 of
2** (spec 1 = the merged Lean Dev Env + Codebase Health design,
`2026-05-19-lean-dev-env-codebase-health-design.md`). Continuity:
`[[project_spec2_agents_dev_env]]`, `[[always-subagent-driven]]`,
`[[cross-session-coordination]]`.

## 0. Problem

Two concerns, one initiative:

- **Cross-session coordination.** Two independent Claude Code CLI
  sessions (a "data lane" and an "engine lane") work the same repo
  concurrently and coordinate only by the operator manually
  copy-pasting handoff messages between terminals. There is no
  first-class mechanism; the relay is correct but operator-laborious
  and the only safeguard against the two lanes diverging.
- **Process drift.** The composite development pipeline both lanes
  converged on (and the operator explicitly endorsed, 2026-05-19: *"i
  do like the development process that both sessions have been
  using"*) lives only in this session's memory + habit. Nothing
  canonical encodes it, so a future session can silently drift off it
  (skip the split review, trust `gh run watch`, run a subset instead
  of the whole-suite gate, etc.).

## 1. Verdict — codify the proven pipeline now; design Agent Teams as the deferred target

Two pillars. **Pillar B (the documented standard) is the high-value,
produce-now-safe half** — it freezes the operator-endorsed pipeline
into a canonical artifact with an anti-rot tripwire. **Pillar A (Agent
Teams) is the correct official coordination target but its ADOPTION is
deferred** (experimental tooling on a live-money shared-tree repo;
zero-cost fallback to the human relay). The design is produced now;
adoption is gated and reversible.

Reject (per prior research, re-verified): MCP-as-bus, hooks+shared-file,
a committed handoff-file — unofficial / risky for a live-money
deterministic repo. Agent Teams is the only official primitive.

## 2. Re-verified Agent Teams facts (current official docs, fetched 2026-05-19)

Sources: `https://code.claude.com/docs/en/agent-teams`,
`.../hooks`, `.../worktrees`.

- Experimental, default-off: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
  (env or `settings.json` `"env"`). Requires Claude Code **≥ v2.1.32**.
- **Lead-first, fixed lead for the team's lifetime** — cannot promote
  a teammate or transfer leadership. ⇒ two already-running independent
  sessions **cannot be retrofitted** into a team; adoption = end both,
  start one lead, lead spawns teammates.
- Mechanism: shared task list (file-locked claim), mailbox
  (auto-delivered messages), machine-managed team config (do not
  hand-edit). This replaces the manual paste relay.
- **Shared working tree, NOT auto-worktrees.** Docs explicitly warn
  same-file edits overwrite. Teams do not add isolation; the
  shared-tree / repo-global `git stash` hazard is unchanged, and Teams
  ADD a same-file race the serialized human relay implicitly
  prevented.
- Enforcement primitives: `TeammateIdle` / `TaskCreated` /
  `TaskCompleted` hooks (deterministic local commands; exit 2 =
  block).
- Live-money-relevant limits: no `/resume`/`/rewind` of in-process
  teammates; task-status can lag and silently block dependents; slow
  shutdown; one team at a time; no nested teams; teammates inherit
  lead permission mode at spawn.

## 3. Phased architecture

### Phase A — Pillar B written standard (docs-only, produce-now-safe; build now)

- **A1 — `docs/DEV_PIPELINE_STANDARD.md`** (new canonical doc beside
  `STYLE_GUIDE.md`). A numbered, non-optional sequence: brainstorm →
  commission a skeptical EXPERT subagent to harden → spec = its own
  gated docs-only PR → operator spec-read gate → writing-plans = its
  own gated docs-only PR → subagent-driven execution → **SPLIT review:
  dispatch a fresh-context spec/intent reviewer; ONLY on its PASS
  dispatch a SEPARATE fresh-context code-quality reviewer — never one
  combined two-gate reviewer** → implementer folds findings → gated
  PR → CI verified via **`gh pr checks <n>`**, NEVER `gh run watch`'s
  exit code (documented misreport) → the **whole single-process
  `pytest -p no:xdist` + bidirectional module-order-flip** =
  authoritative gate (Lean P1 `-n auto --dist loadgroup` is
  accelerator-only) → squash-merge `--delete-branch` →
  `git switch main && git pull` sync → emit paste-ready cross-session
  handoff message.
  Plus a **Standing Discipline Rules** section, each with the *why*:
  split-review separate dispatches; `gh pr checks` not
  `gh run watch`; whole-suite + order-flip authoritative; ops-shadow
  `xdist_group("ops_shadow")` sentinel discipline (a new test touching
  `sys.modules['ops']` / `spec_from_file_location(ops)` /
  `importlib`-of-ops MUST carry the mark or
  `tests/test_xdist_group_manifest.py` reds CI); no `dashboard.py`
  import in a CI test (no `streamlit` in `pip install -e .[dev]`);
  cross-session non-stomp (no touching the other lane's
  files/worktrees; **no `git stash` — repo-global, cross-session
  hazard**); the snapshot/restore-`sys.modules['ops']` precedent **vs**
  the counter-rule that a `tpcore/tests/` test importing `ops.lab.run`
  uses a PLAIN import with NO `del sys.modules` eviction guard (both
  live, both cited — see `[[feedback_ops_package_shadow_full_suite_gate]]`,
  `#148`); `git switch` never `git checkout <sha|branch>`; one
  canonical cleanup `scripts/git_hygiene.sh`; backfills via
  `ops.py --stage`, never one-off scripts.
- **A2 — Lean-integration subsection** (inside A1): parallel
  (`-n auto --dist loadgroup`) is the fast accelerator; the
  serial+order-flip pair (`ci.yml` "AUTHORITATIVE gate" step) is the
  gate of record; the tool-walk excludes (`pyproject.toml` ruff
  `extend-exclude` + pytest `norecursedirs` + tracked `.ignore`,
  `respect-gitignore=false`) are why grep/ruff are fast — do not
  re-derive, do not re-enable `respect-gitignore`.
- **A3 — CLAUDE.md pointer**: a one-paragraph "Dev Pipeline Standard"
  entry in the Session Rules register pointing at A1. Authored as part
  of this phase's build (not a separate now-edit).
- **A4 — anti-rot presence-sentinel** `tests/test_dev_pipeline_standard_present.py`:
  asserts `docs/DEV_PIPELINE_STANDARD.md` exists and contains the
  load-bearing literal anchors (`gh pr checks`, `no:xdist`,
  `xdist_group("ops_shadow")`, split-review, `git stash`). A
  presence/anti-rot tripwire, NOT a behavioural test of the process
  (un-testable; that is operator + reviewer discipline). Mirrors the
  existing `gen_engine_manifest` / `test_xdist_group_manifest`
  manifest-discipline the repo already trusts. ~15 lines; bounds the
  "Pillar B becomes dead documentation" risk.

Phase A = 1 new doc + 1 new test (existing `tests/` testpath) + 1
CLAUDE.md paragraph — additive, zero cross-session collision (the
engine session has ended; single-session window). Build now.

### Phase B — Agent Teams target design (in this spec now; ADOPTION deferred, canary-first)

- **B1 — target topology.** A lead session spawns two teammates,
  `data-lane` and `engine-lane`, each auto-loading CLAUDE.md + a
  lane-scoped spawn prompt with a strict file-ownership partition.
  Shared task list replaces the paste relay; mailbox replaces
  copy-paste handoffs; A1 step "handoff message" becomes a teammate
  message; "operator spec-read gate" becomes an operator message to
  the lead. **Lead identity = decided AT ADOPTION TIME** (operator
  "hold, let the other session finish"); expert recommendation was
  engine-lane-as-lead (owns the broader roster/SDLC + Lab surface).
- **B2 — invariants under Teams.** Each teammate still runs the full
  Phase-A pipeline incl. its own gated PR and the authoritative
  whole-suite + order-flip gate (CI is unchanged — Teams change who
  types, not the gate). A `TaskCompleted` hook (deterministic, exit
  2 = block) refuses marking a "ship" task complete without a
  merged-PR SHA — clockwork at the team layer. Cross-session
  non-stomp: strict file-ownership partition in spawn prompts +
  singleton dependency-gated "merge" task + the team-wide no-`git
  stash` rule.
- **B3 — rollback/fallback.** Zero-cost and total: unset the flag,
  clean up the team, resume two independent sessions + the human
  relay. No repo artifact depends on Teams (A1 is topology-agnostic).
  Fallback triggers: experimental-flag instability/crash-loop;
  task-status lag silently blocking a merge; any detected same-file
  overwrite; any determinism/gate erosion; Claude Code < v2.1.32.

Phase B is **documented in this spec now**; the flag flip + session
restructure + relay retirement are the **deferred adoption tail**,
**canary-one-task-first** (run ONE low-risk docs-only task fully
through Teams before routing live-money work through it).

## 4. Decisions

- **D1 lead identity** — *decided at adoption time* (operator hold);
  expert rec engine-lane-as-lead. OPEN, deferred.
- **D2 Pillar-B presence-sentinel** — **YES** (A4; cheap,
  pattern-consistent, kills the dead-doc risk). Reject testing the
  *process* mechanically.
- **D3 adoption shape** — **canary-one-task-first**, not big-bang.
- **D4 `TaskCompleted` enforcement hook** — designed in B2, **adopted
  with Phase B** (adding shared `settings.json` now is itself a
  non-stomp hazard; the engine session just ended but the rule keeps
  the design clean).
- **D5 CLAUDE.md pointer** — bundled into the Phase-A build (A3), not
  a separate edit.

## 5. Scope boundary / fatal-objection self-check

**OUT:** changing the live trade/data runtime, determinism,
never-fail-open, the deterministic-agent / credential-starved
LLM-triage envelope, the serial+order-flip authoritative gate,
`data/`-not-moved; MCP/hooks/handoff-file buses; the Agent Teams
*adoption actions* (deferred); `#148` (engine-lane-tracked — do not
fix opportunistically).

**Fatal-objection check:**
- *Teammates race the shared git tree* — real (docs warn). Bounded:
  file-ownership partition, singleton dependency-gated merge task,
  team-wide no-`git stash`, and the authoritative CI gate is unchanged
  (a bad merge still reds `gh pr checks`). Canary-first + instant
  human-relay rollback contain residual risk.
- *Experimental-flag instability on a live-money repo* — bounded:
  touches only the dev/authoring layer, never runtime/trade/data; CI
  + deterministic agents untouched; instant zero-cost rollback.
- *Task-status lag silently blocks a ship* — documented limitation;
  bounded by operator monitoring + fallback trigger; the gate cannot
  be bypassed (lag delays, never weakens).
- *Pillar B becomes dead documentation* — the sharpest risk; bounded
  by A4's presence-sentinel (reds CI on deletion/clause-loss) + the
  CLAUDE.md pointer (every session reads it) — the same anti-rot
  mechanism the repo already relies on.
- *Determinism/gate erosion via Teams* — none: Teams reorganize
  human/agent authoring; the serial+order-flip gate, `check_imports`,
  ruff, and the deterministic LLM-triage fences in `ci.yml` are wholly
  outside the Teams envelope.

## 6. Phasing (gated PR; subagent-driven; Phase A now, Phase B adoption deferred)

| Phase | Deliverable | Cross-session | When |
|---|---|---|---|
| **A** | `docs/DEV_PIPELINE_STANDARD.md` (pipeline + Standing Discipline Rules + Lean-integration) + `tests/test_dev_pipeline_standard_present.py` anti-rot sentinel + the CLAUDE.md pointer paragraph. One gated PR (or split A1+A4 / A3 if cleaner). | additive — safe | now (single-session window) |
| **B** | This spec's §3 Phase B IS the design deliverable. ADOPTION (flag, lead+teammates topology, `TaskCompleted` hook, relay retirement) — own plan, **canary-one-task-first**. | adoption = experimental | deferred (operator green-light; Claude Code ≥ v2.1.32) |

**Design ready for the implementation plan.** Pillar B codifies the
operator-endorsed pipeline with a cheap anti-rot tripwire (produce-now,
zero-collision); Pillar A is the official, doc-grounded coordination
target with honest experimental risk, correctly deferred with a
zero-cost human-relay fallback. Every live-money / determinism /
cross-session invariant is preserved by construction.

## 7. Decision Record — Pillar A adoption: **SKIP (single-session), 2026-05-19**

**Status of this doc:** Pillar B = SHIPPED (`docs/DEV_PIPELINE_STANDARD.md`
+ `tests/test_dev_pipeline_standard_present.py`, PRs #109/#110/#111).
Pillar A = **adoption decided AGAINST single-session.** This section is
the terminal decision for §3 Phase B / §6 row B.

**Trigger.** Pillar A's design premise (§0/§1) was *replacing the
two-session human relay*. The operator permanently collapsed to ONE
session (`[[cross-session-coordination]]`), so that premise is moot.
The master remaining-program kept "Agent-Teams adoption" as a sequence
step on the operator's broader durable intent ("new dev environment /
develop with agents / make everything faster / accelerate the Lab
front-half epic") and prescribed an **expert-led brainstorm**
(claude-code-guide = the verdict source) to resolve the moot-ness
rather than pre-deciding.

**Verdict (claude-code-guide, current official docs fetched 2026-05-19,
code.claude.com/docs/en/agent-teams.md):** **Skip Agent Teams
single-session.** Evidence:

- Agent Teams' only novel delta over the already-in-use
  subagent-driven-development + dispatching-parallel-agents + per-feature
  git worktrees is *teammate-to-teammate messaging*. It adds **no**
  capability the existing stack lacks for ~6 sequential
  brainstorm→spec→plan→build sub-projects: parallelism (subagents +
  parallel-agents already), isolation (worktrees already, **strictly
  safer** — filesystem-level vs Teams' advisory file-ownership
  partition), or a better merge path (the serialized PR+CI+squash gate
  is unchanged).
- Teams uses a **single shared working tree with no automatic
  isolation** (docs: *"Agent teams don't isolate teammates in
  worktrees"*; *"Two teammates editing the same file leads to
  overwrites"*). On a live-money platform this reintroduces a same-file
  race the serialized relay implicitly prevented, contained only by
  advisory (non-enforced) partition + hooks — net risk without
  commensurate single-session benefit.
- The operator's durable intent ("develop with agents / faster") is
  **already realized** by the shipped Pillar B pipeline + the existing
  agent tooling. Relay-replacement, Teams' raison d'être here, is moot.

**Decision.** Do **not** set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`;
do **not** build the lead+teammates topology / `TaskCompleted` hook /
relay-retirement adoption. Zero repo artifact depends on Teams, so this
is a no-op (not a rollback). The §3 Phase B *design* is retained as the
documented official target **iff** a future multi-human / multi-session
topology re-creates the premise — re-open this Decision Record then;
until then it is closed.

**Scope discipline.** The expert's ancillary "tighten the existing
flow" suggestions (consolidate subagent definitions, pre-checklists,
`/loop` health checks) are **explicitly OUT** — unsolicited scope
beyond the operator's step; the shipped DEV_PIPELINE_STANDARD already
codifies the split-review / parallel-accelerator / worktree-isolation
contract. No action. Master sequence advances to the Lab front-half
epic (`[[lab-front-half-epic]]`).
