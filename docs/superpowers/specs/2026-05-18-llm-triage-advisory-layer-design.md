# LLM Triage Advisory Layer (Ladder rung 5 / Epic E) — Design

**Status:** spec 2026-05-18 (DATA lane). Brainstorm → **spec (this
doc)** → plan → phased subagent build. #187 — the operator-deferred
"Epic E", now un-deferred. Rung 5 of the Escalation & Hardening Ladder
(`docs/ESCALATION_HARDENING_LADDER.md`).

**Operator decision (2026-05-18):** the platform **does** call an LLM
API with a purpose-built triage persona (operator overrode the
deterministic-packet-only recommendation; the operator is the
authority). The standing hard constraints still bind and shape the
safety envelope below.

## 1. Mandate & non-negotiable constraints

The LLM is an **advisory analyst at the escalation boundary only**:

- **Advisory-only / human-gated / never auto-applied.** The LLM never
  dispositions, never converts an escalation, never writes a HealSpec/
  RemediationSpec/AdapterContract, never touches risk/orders/data. Its
  sole output is a **non-authoritative proposal** a human reads.
- **Never the mutating actor.** Structurally incapable: text
  completion only — **no tool-use, no function-calling**. The proposal
  is a string in an `application_log` event; nothing consumes it
  programmatically.
- **The deterministic agents stay deterministic.** `tpcore/selfheal`,
  `tpcore/auditheal`, `tpcore/datasupervisor`, the trading path, the
  rung-2 registries are byte-untouched. The LLM is a *separate* lane
  that runs *after* deterministic escalation and *before* the human —
  it accelerates the rung-3 human; it does not replace rungs 1–4.
- **Fails safe to "human handles it."** Any LLM/API failure, missing
  key, or crash → the escalation simply stays undispositioned (its
  pre-#187 state). #187 can only *add* a suggestion; its absence
  changes nothing.
- **Data-lane only.** Engine/aar lanes are a separate session's
  territory (consistent with the Ladder scope).

## 2. Trigger predicate — genuinely novel only (reuses the Ladder SoT)

The agent fires for an escalation iff ALL hold (no new predicate —
reuses `tpcore.ladder` + the existing escalation reads):

1. it is an **open** escalation instance (the `ops.weekly_digest`
   undispositioned set — escalation event with no resolving terminal,
   not yet `DATA_ESCALATION_DISPOSITIONED`);
2. its Ladder `policy_for(<class>)` disposition is
   `ESCALATE_OPERATOR` — i.e. **no deterministic auto-conversion
   exists** (precisely rung-5's "NOVEL ambiguous failure the
   deterministic agents escalated"; auto-healed/known classes never
   reach the LLM);
3. there is **no prior `LLM_TRIAGE_PROPOSAL`** for its ref
   (one-terminal dedup — exactly one proposal per escalation, ever).

Bounded: a per-cycle cap (`_MAX_TRIAGE_PER_CYCLE`, default 5) so an
escalation storm cannot run up API cost; oldest-first.

## 3. Deterministic read-only context packet

Before any LLM call, the agent deterministically assembles
(pure reads, no mutation) a `TriagePacket`: the escalation event +
payload, the Ladder disposition policy + reason for its class, the
latest relevant `data_quality_log` / `cross_table_audit.%` rows for
the source, and the Sprint Dossier body if `tpcore/forensics/dossier`
has one. The packet is persisted (in the proposal event's data, plus
a `packet_hash`) so every proposal is auditable and reproducible.

## 4. The triage persona (versioned, documented artifact)

`docs/llm_triage_persona.md` — the frozen system prompt + the
operator's required "prompt format + documentation". Persona mandate
baked in:

- Role: an *advisory data-platform triage analyst* for ONE escalation.
- Output contract: exactly one `proposed_disposition ∈ {converted,
  structural, removed}` + (for `converted`) a concrete candidate
  mechanism (e.g. a sketched HealSpec / param / canonical stage) +
  `rationale` + explicit `confidence` (low/med/high) + an explicit
  "what I could NOT determine from the packet" section.
- Hard-baked guardrails in the prompt: it has **no authority**; it
  must **defer to the operator**; it must **never state or imply a
  change was made**; on thin evidence it must answer "insufficient
  context — escalate to operator" rather than guess; it must not
  invent platform internals not in the packet.
- `persona_version` (a string constant) is stamped into every
  proposal so prompt revisions are auditable; changing the persona
  bumps the version (a test asserts the constant matches the doc's
  declared version header).

## 5. The LLM call + airtight safety envelope

- **Provider:** Anthropic API via `ANTHROPIC_API_KEY` (env). Key
  absent → the agent logs `llm_triage.no_api_key` and no-ops (never
  blocks the cycle; fails safe).
- **Transport:** HTTP retry through the existing
  `tpcore.outage.with_retry` (the codebase HTTP-retry SoT — no local
  `tenacity`, no bespoke loop). Bounded `max_tokens`; a single
  completion; **`tools` / function-calling NEVER passed** (structural
  inability to act).
- **Output:** a schema'd, explicitly non-authoritative
  `LLM_TRIAGE_PROPOSAL` `application_log` event:
  `{schema:1, ref, escalation_class, persona_version, model,
  proposed_disposition, confidence, rationale, could_not_determine,
  packet_hash}`. Nothing in the platform consumes this
  programmatically — it is render-only (rung-5 → rung-3 human).
- **Import-isolation clockwork guard (load-bearing):** a test asserts
  `ops/llm_triage.py`'s import closure contains **no**
  `tpcore.selfheal` / `tpcore.auditheal` / `tpcore.datasupervisor` /
  `tpcore.risk` / order/registry-mutation module — the LLM lane is
  structurally fenced from every actor path. A new import that
  breaches the fence fails the build.
- **Crash-isolated:** any exception (API, parse, timeout) →
  structured `llm_triage.error` log → the agent returns, the cycle
  proceeds, the escalation stays undispositioned. Mirrors the
  datasupervisor/auditheal crash-isolation precedent.
- **Tests use a mocked client** — zero live API calls in CI.

## 6. Human-gated terminus (unchanged rung-3)

The proposal surfaces **attached to its escalation** in the
`ops/weekly_digest` UNDISPOSITIONED section, e.g.
`… | LLM: <disposition> (conf <c>) — <rationale one-liner>`. The
operator still runs the existing `python -m ops.weekly_digest
disposition <ref> <converted|structural|removed> [note]` verb. The
LLM proposal is *advice on the line the operator already had to act
on* — it adds no new write path, button, or authority. An operator
disposition clears the escalation exactly as before.

## 7. Non-goals

- No auto-apply, ever. No tool-use. No LLM in any deterministic-agent
  / repair / trading / data-mutation path.
- Not a new daemon — a thin agent on the existing `application_log`
  bus (sibling of `ops/cutover_agent.py` / `ops/data_repair_service`),
  invoked in the data-ops flow like the other agents.
- Not engine/aar (separate session).
- Not a replacement for rungs 1–4 — purely additive rung-5 advice.
- No dashboard write surface (the existing read-only escalation panel
  MAY later show the proposal; not in this spec's scope).
- Does not modify the persona at runtime / no self-tuning of the
  prompt (versioned static artifact only).

## 8. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `ops/llm_triage.py` — the deterministic, **no-LLM-yet** core: the trigger predicate (reusing `tpcore.ladder.policy_for` + the weekly-digest open/undispositioned read + the no-prior-`LLM_TRIAGE_PROPOSAL` dedup) and the `TriagePacket` read-only context builder + `packet_hash`. `docs/llm_triage_persona.md` (the versioned persona/system prompt + prompt-format doc). Unit tests (fake pool): predicate fires only on undispositioned + `ESCALATE_OPERATOR` + no-prior-proposal; packet assembled from scripted rows; persona_version constant == the doc header. **Landed dark** (no API call). |
| 2 | The LLM call + `LLM_TRIAGE_PROPOSAL` emission + the safety envelope: `ANTHROPIC_API_KEY` env gate, `tpcore.outage.with_retry` transport, NO tools, schema'd event, crash-isolation, the import-isolation clockwork guard test, the per-cycle cap. Tests inject a mock LLM client (no live calls); cover proposal emit, dedup, no-key no-op, crash-isolation, the import-fence assertion. **Landed dark** (agent exists, not yet wired into the cycle). |
| 3 | Wire the thin agent into the data-ops flow (sibling-of-cutover_agent placement; read the exact step) AND surface the proposal attached to its escalation in the `ops/weekly_digest` UNDISPOSITIONED line. Net behaviour: a novel escalation now also carries an LLM advisory the operator sees when dispositioning. The rung-3 verb + teeth are unchanged. |
| 4 | Docs — "all of it": CLAUDE.md (the rung-5 advisory lane + its hard constraints + that it never mutates), `docs/ESCALATION_HARDENING_LADDER.md` (rung 5 status → BUILT, advisory-only), the persona doc cross-links, this spec → BUILT + build record. |

## 9. Open questions for the plan phase (resolve by READING code, not guessing)

- **Exact data-ops wiring point** for the thin agent — read how
  `ops/cutover_agent.py` / the post-escalation agents are invoked
  (a step in `run_data_operations.sh`? after the datasupervisor Step
  4d?); place it AFTER the deterministic escalation/disposition state
  is final for the cycle, BEFORE the digest build, mirroring the
  established sibling-agent invocation idiom. Do not assume.
- **Anthropic SDK vs raw HTTP via `tpcore.outage.with_retry`** —
  read whether the repo already vendors an Anthropic client anywhere
  (investigation found none); prefer the minimal raw-HTTPS call
  wrapped in `with_retry` to avoid a new heavy dependency, unless the
  repo's HTTP conventions dictate otherwise. Decide from the actual
  `tpcore.outage` API + existing adapter HTTP patterns.
- **`LLM_TRIAGE_PROPOSAL` ↔ escalation-ref correlation** — confirm
  the exact ref key per escalation type (`request_id` for
  `DATA_REPAIR_ESCALATED`, `hold_id` for `DATA_SOURCE_ESCALATED`,
  feed for `AdapterContractDrift`) by reading the weekly-digest
  open-escalation query; the dedup + the digest attachment MUST key
  on the identical ref the rung-3 disposition uses, or the proposal
  attaches to the wrong line.
- **Per-cycle cap + cost** — confirm there is no existing cost-budget
  convention to reuse; if none, `_MAX_TRIAGE_PER_CYCLE` constant +
  the strict novelty predicate is the bound (documented).
- **Packet size bound** — the persona prompt must be bounded; the
  plan defines a hard cap on dossier/rows excerpt length so a huge
  escalation can't blow the token budget (truncate-with-marker,
  deterministic).
