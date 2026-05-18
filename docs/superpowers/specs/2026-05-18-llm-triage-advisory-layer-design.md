# LLM Triage Advisory Layer (Ladder rung 5 / Epic E) — Design **v2**

**Status:** spec **v2.1** 2026-05-18 (DATA lane). Brainstorm → spec
(v1) → operator scope escalation ×3 + **two independent expert
opinions** → **spec v2** → plan → phased subagent build (P1, P2
merged) → **v2.1 invocation amendment (this rev)**. #187 — Ladder
rung 5 (`docs/ESCALATION_HARDENING_LADDER.md`).

**v2.1 amendment (operator directive 2026-05-18, after P2 merge):**
the agent is invoked **event-driven** off the existing
`platform.application_log` bus by a **sibling daemon** mirroring
`ops/data_repair_service.py` / `ops/engine_service.py` (the
`_main_loop` + `_run_supervised` + `scripts/install_all_daemons.sh`
pattern) — **NOT** a linear step inside `scripts/run_data_operations.sh`
and **NOT** a scheduled GitHub workflow. Operator: *"i dont want a
scheduled workflow i want an event driven incantation."* This reuses
the existing daemon infrastructure (same bus, same supervised-poll
pattern, same launchd installer) — it is the canonical mechanism, not
a second pipeline. §8/§9/§10(P3)/§11 below are amended accordingly;
the superseded v2 wording is struck, not silently rewritten.

**v2 supersedes v1.** v1 was "advisory text only, no tools." Operator
escalated: (a) the LLM may *run tests to investigate*; (b) it must
help *fix the data* while data stays **100%**; (c) it must *stage
new/updated deterministic agents & self-heal, review-ready, so it
won't intervene again*. Two skeptical staff-architect expert passes
(commissioned by the operator) defined the maximal **responsible**
envelope and its bright lines, adopted verbatim below. Operator
directives also locked: a purpose-built **persona is a created,
versioned artifact**; the Anthropic integration is built **against
official Claude documentation, not assumed knowledge**; **one
canonical mechanism — no on/off scripts / no rat's nest**.

## 1. What the LLM produces (per NOVEL escalation)

In a credential- & network-**starved ephemeral git worktree** (no
`.env`, no `DATABASE_URL`, no Alpaca/Supabase creds, egress-denied
except a read-only package mirror) the triage LLM may **read the
repo** and **run the fixture test suite / ruff / the clockwork-drift
tests** to investigate. Its sole deliverable is a **draft,
human-merge-only PR** containing:

1. an **additive, mechanism-free** HealSpec / RemediationSpec / Ladder
   binding that points an **already-existing, already-proven canonical
   `ops.py --stage`** (with already-shipped bounded params) at the
   novel escalation class — so the deterministic agent permanently
   owns it (the Ladder rung-3 "converted" outcome; "won't intervene
   again"); and
2. a **machine-checkable dossier**: escalation id, the novel failure
   signature, why this bounded repair is sufficient, and what it
   deliberately does NOT cover.

It also emits a non-authoritative `DATA_LLM_TRIAGE_PROPOSAL` event
(advisory record, pointer to the PR) that surfaces on the escalation's
line in the weekly digest.

## 2. "Data stays 100%" — the bright line

The LLM / its PR **never** triggers, runs, queues, or schedules a
repair, and has no creds or real tree to do so. The **present** outage
is restored independently and as today: the existing deterministic
path, or a human running an **existing** canonical stage. The PR is
the **future permanent** fix — it is **never on the critical path of
"stay 100%."** Data continues to be restored only by the canonical
bounded mechanism, gated, exactly as before #187.

## 3. Deterministic enforcement (expert-vetted; non-bypassable)

The LLM's self-judgement of "minor vs major" gates **nothing** — the
boundary is a property of the *artifact*, evaluated by code the LLM
never runs and on a tree it cannot write.

- **Hard-denied → required CI check auto-fails + auto-closes the PR
  (the "body"):** any diff touching `tpcore/risk/**`,
  `tpcore/order_management/**`, `tpcore/risk/limits_profile.py`, the
  DSR/credibility gate, `platform/migrations/**`, `**/providers.py`,
  `scripts/run_data_operations.sh`, `scripts/ops.py`.
- **Allowed-but-maximally-gated (the "brain"):** HealSpec /
  RemediationSpec / Ladder binding files — **additive entries ONLY**,
  enforced by a NEW required CI check **"HealSpec provenance"** that
  FAILS unless ALL hold: (a) every stage/param the new spec references
  already exists on `main` from a **non-LLM** PR; (b) **zero new
  mechanism** — no new stage, no new or loosened param/bound, no
  `healable` false→true, no edit to an existing spec (all of those are
  100%-human-authored, hard-denied to the LLM); (c) the clockwork-
  drift suite passes with the new spec **active in fixture** (3-way
  registry lockstep proven); (d) the machine-checkable dossier is
  present and well-formed.
- **Two-human review, one ≠ the dispositioning operator** (CODEOWNERS;
  operator-sole-review is a single point of failure). The change is
  **inert until merged**.
- **Post-merge mandatory shadow/canary, N cycles:** a newly-merged
  LLM-authored spec **fires but does NOT mutate** — its computed
  repair is diffed against the no-op baseline and logged/gated; a
  human **promotes** it after detector-vs-healer agreement is observed
  for N cycles. (The cheapest control against a plausible-but-wrong
  LLM HealSpec silently corrupting correct data every cycle.)
- **No merge authority, no live creds, no real-tree write, no
  mutating tool, no LLM self-classification gating anything.**

## 4. Vetoed outright (expert, non-negotiable)

Any LLM-proposed **new or widened repair mechanism** (new canonical
stage, new/loosened param or bound, `healable` flip, edit to an
existing spec); any **merge authority**; any **live credential** in
the sandbox; any **real-working-tree write**; treating the **persona /
operating instructions as a safety boundary** (they govern output
*quality* only). *"The LLM may choose which proven hammer to point at
a new nail; it may never forge a hammer."*

## 5. The triage persona — a created, versioned artifact (operator-required)

`docs/llm_data_triage_persona.md` is **created in P1** (not implied): the
frozen system prompt + the operator's required prompt-format
documentation. Mandate baked in: an *advisory data-platform triage
analyst* for ONE escalation; output contract = a proposed
mechanism-free binding to an existing canonical stage + dossier +
explicit confidence + an explicit "what I could NOT determine";
hard-baked guardrails (no authority; defer to humans; never imply a
change was made; "insufficient context" over guessing; never invent
internals not in the packet; never propose a new mechanism). A
`PERSONA_VERSION` constant is stamped into every proposal; a test
asserts the constant == the doc's declared version header (lockstep).
**The persona is explicitly NOT a safety boundary** (§3/§4 are) — it
is output-quality only; this is stated in the doc itself.

## 6. Anthropic integration — built against official docs (operator-required)

Implemented strictly against the **official Anthropic documentation**
(retrieved via the context7 MCP from `/anthropics/anthropic-sdk-python`
+ `docs.claude.com`; the plan re-fetches and pins exact references —
**no assumed knowledge**, per [[feedback_use_official_docs]]):

- Official `anthropic` Python SDK, `client.messages.create(...)`
  (`POST /v1/messages`). A thin official dependency added to
  `pyproject` (acceptable: operator chose system-calls-LLM; the
  official SDK is the least-assumption path).
- Request: top-level `system=` (the persona — **not** a system-role
  message), `messages=[{"role":"user","content": <packet>}]`,
  required `max_tokens` (bounded), `model` = a pinned current-model
  constant (plan confirms the current id from the official models doc;
  not hardcoded from memory). **`tools=` is NEVER passed** →
  structurally incapable of tool-use/acting.
- Transport wrapped in the codebase SoT `tpcore.outage.with_retry`
  (no bespoke retry loop, no local `tenacity`).
- Response parsed per the official `Message` shape: text from
  `content[0].text` (type `"text"`), `stop_reason`, `usage`
  (`input_tokens`/`output_tokens` logged for cost). `ANTHROPIC_API_KEY`
  from env; absent → agent logs `llm_data_triage.no_api_key` and no-ops
  (fails safe; never blocks the cycle).
- Tests **mock `messages.create`** returning a real-shaped `Message`
  (per the official doc) — **zero live API calls in CI**. A reviewer
  checks the mocked shape against the official doc, not against
  assertion.

## 7. Trigger predicate — genuinely novel only (reuses the Ladder SoT)

Fires for an escalation iff: (1) it is **open + undispositioned** (the
`ops.weekly_digest` open-escalation set); (2) its Ladder
`policy_for(<class>)` == `ESCALATE_OPERATOR` (no deterministic
auto-conversion exists — the genuinely novel class); (3) **no prior
`DATA_LLM_TRIAGE_PROPOSAL`** for its ref (one-terminal dedup; exactly one
attempt per escalation, ever). Bounded: `_MAX_TRIAGE_PER_CYCLE`
(default 5), oldest-first, so a storm cannot run up API cost. The
read-only context **packet** (deterministic) = the escalation event +
payload, the Ladder policy+reason, the relevant
`data_quality_log`/`cross_table_audit.%` rows, the Sprint Dossier if
present; size-bounded (deterministic truncate-with-marker) so it can't
blow the token budget; `packet_hash` recorded for reproducibility.

## 8. One canonical mechanism — no rat's nest (operator-required)

A **single** triage agent on the existing `application_log` bus,
consumed **event-driven** by **one** sibling daemon
(`ops/llm_triage_service.py`) that is a structural mirror of
`ops/data_repair_service.py` / `ops/engine_service.py` — the same
`_main_loop` cursor-poll of `platform.application_log`, the same
`_run_supervised` backoff, the same `main()` CLI shim, installed by
the same `scripts/install_all_daemons.sh`. It fires on a **novel
data-escalation event** (`DATA_REPAIR_ESCALATED` /
`DATA_SOURCE_ESCALATED`) landing on the bus; the P1
`select_novel_escalations` open-set re-check (the weekly-digest
`OPEN_ESCALATIONS` anti-join + no-prior-`DATA_LLM_TRIAGE_PROPOSAL`
dedup) makes it safe against an escalation the same cycle's self-heal
later resolves, so it needs no ordering coupling to the data-ops
script. ~~v2: invoked in the data-ops flow like the others.~~
**(v2.1 — operator: not a linear `run_data_operations.sh` step, not a
scheduled workflow; event-driven on the existing bus.)** Plus **one**
declarative `provenance` check wired into the **existing**
`.github/workflows/ci.yml` (a new required job/step, not a parallel
pipeline) + reuse of the existing PR / clockwork-drift /
branch-protection / canary machinery. **No on/off bash toggles, no
one-off scripts, no second pipeline** — it reuses the existing daemon
infrastructure, not a parallel one. Crash-isolated (any failure →
structured log, cycle proceeds, escalation stays undispositioned —
fails safe to "human").

## 9. Non-goals / scope

- Data-lane only (engine/aar = separate session — consistent with the
  Ladder).
- Not in any deterministic-agent / repair / trading / data-mutation
  runtime path. No auto-apply, ever.
- ~~v2: Not a new daemon.~~ **(v2.1 — operator directive
  2026-05-18:** the canonical event-driven consumer **is** a sibling
  daemon on the existing `application_log` bus, reusing the existing
  `_main_loop`/`_run_supervised`/`install_all_daemons.sh`
  infrastructure — this is the one canonical mechanism per §8, not a
  parallel pipeline.) Not a dashboard write surface.
- Branch-protection / CODEOWNERS / a merge-less bot identity are
  partly **GitHub repo settings**, not code: the code-side fence
  (provenance + hard-denied required checks + starved sandbox +
  no-merge-call + full existing gate + canary) is itself sufficient to
  make a system-breaking *merge* impossible without a human; the repo
  settings are an operator **runbook** deliverable (P4), honestly
  flagged as config-not-code.

## 10. Phasing — build the fence before the thing (gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | **Safety skeleton, deterministic, no LLM.** The "HealSpec provenance" check (additive-only / mechanism-free / stage-must-pre-exist / dossier-present / drift-in-fixture) as a pure module + its required-CI entrypoint + the **hard-denied protected-path check**; the post-merge **canary harness** (a merged LLM-authored spec is shadow-only until promoted); the trigger predicate (reuse `tpcore.ladder.policy_for` + weekly-digest open set + no-prior-proposal); the deterministic read-only context **packet** builder + `packet_hash`; **`docs/llm_data_triage_persona.md`** (created, versioned) + the `PERSONA_VERSION` lockstep test. Unit-tested; **landed dark**. |
| 2 | **The sandboxed LLM agent.** Ephemeral starved worktree runner; the official-SDK `messages.create` call (per §6, doc-grounded) wrapped in `tpcore.outage.with_retry`, **no `tools`**, bounded `max_tokens`, no-key no-op, crash-isolated; produces the branch + dossier + `DATA_LLM_TRIAGE_PROPOSAL`. The **import-isolation clockwork guard** (the agent's import closure excludes `tpcore.risk`/`order_management`/`selfheal`/`auditheal`/`datasupervisor` actor paths). Client **mocked** in CI (no live calls). Landed dark (not wired). |
| 3 | **Wire event-driven (v2.1).** A new sibling daemon `ops/llm_triage_service.py` — structural mirror of `ops/data_repair_service.py`/`ops/engine_service.py` (`_main_loop` cursor-poll + `_run_supervised` + `main()` shim) — fires `ops.llm_data_triage.run_triage` on a novel-escalation event (`DATA_REPAIR_ESCALATED`/`DATA_SOURCE_ESCALATED`) on the existing `application_log` bus; registered in `scripts/install_all_daemons.sh` (the same launchd installer). **NOT** a linear `run_data_operations.sh` step, **NOT** a scheduled workflow. Then: draft-PR open (merge-less identity); the provenance + protected-path checks added as **required** label-gated jobs in the existing `ci.yml`; auto-close on a hard-denied/provenance failure; the proposal surfaced on the escalation's weekly-digest line. Net: a novel escalation event now triggers a fenced, review-ready PR + advisory. |
| 4 | **Docs — "all of it".** CLAUDE.md (rung-5 + the bright lines + that data restoration never goes through the LLM); `docs/ESCALATION_HARDENING_LADDER.md` rung-5 → BUILT with the expert envelope + vetoes; the persona doc cross-links; the **operator runbook** for the GitHub branch-protection/CODEOWNERS/merge-less-identity repo settings (config-not-code, honestly flagged); spec → BUILT + build record. |

## 11. Open questions for the plan phase (resolve by READING code/docs, not guessing)

- **Re-fetch & pin the official Anthropic API/model/version refs**
  (context7 `/anthropics/anthropic-sdk-python` + `docs.claude.com`):
  exact current model id, `anthropic-version`/SDK version, the
  `Message`/`content`/`stop_reason`/`usage` shape the mock must match.
  Capture URLs in the plan. Do not code from memory.
- **Exact event-driven trigger (v2.1)** — read the
  `ops/data_repair_service.py` / `ops/engine_service.py` daemon
  pattern (`TRIGGER_EVENT_TYPES`, `_find_new_trigger`/`_poll_new_*`
  cursor-poll, `_run_supervised` backoff, `_main_loop`, `main()`
  shim, `POLL_INTERVAL_SEC`) and `scripts/install_all_daemons.sh`.
  Build `ops/llm_triage_service.py` as a structural sibling that
  triggers on `DATA_REPAIR_ESCALATED`/`DATA_SOURCE_ESCALATED`. No
  data-ops step-ordering coupling — the P1
  `select_novel_escalations` open-set re-check makes a same-cycle
  self-heal a no-op (the escalation is no longer open). NOT a
  `run_data_operations.sh` step; NOT a scheduled workflow.
- **Escalation-ref key per type** — confirm `request_id`
  (`DATA_REPAIR_ESCALATED`) / `hold_id` (`DATA_SOURCE_ESCALATED`) /
  feed (`AdapterContractDrift`) from the weekly-digest open-escalation
  query so dedup + the digest attachment key identically to the rung-3
  disposition.
- **`DATA_LLM_TRIAGE_PROPOSAL` insert** — mirror
  `ops/data_repair_service._INSERT_SQL` exactly.
- **Provenance "stage/param already exists on `main` from a non-LLM
  PR"** — define the deterministic check precisely against the actual
  HealSpec/RemediationSpec schemas + how PR authorship/label is
  detectable in CI (read `ci.yml` context + the registries).
- **Canary "promote" mechanism** — the minimal deterministic
  promotion record (an event/flag the deterministic agent reads to
  switch a spec from shadow→active); read how selfheal/datasupervisor
  read state so this reuses the bus, not a new toggle.
- **The drift/clockwork & import-isolation test technique** — mirror
  the existing `registry_drift` / fake-pool test precedents.
