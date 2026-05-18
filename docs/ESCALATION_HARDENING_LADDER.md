# Escalation & Hardening Ladder

**Scope: DATA lane only.** Engine/aar lanes are a separate session's territory; cross-lane unification is an operator cross-session decision, not implied here.

## Principle

Every data-lane escalation must terminate in exactly one of — `converted` (a new bounded deterministic capability), `structural` (a structural fix), or `removed` (the source taken out of live capital). Never by loosening an agent. Never silent best-effort. The system hardens by converting each escalation into a new bounded capability or a structural removal — it never improves by relaxing a gate or widening an agent's tolerance.

## The 5 rungs (data lane)

| Rung | Name | Status | Concrete mechanisms |
|---|---|---|---|
| 1 | Fail-closed escalation | BUILT | selfheal/auditheal exit-gate (no `DATA_OPERATIONS_COMPLETE`); `DATA_REPAIR_ESCALATED`; `DATA_SOURCE_ESCALATED`; contract-drift `INGESTION_FAILED`; audit known_knowns FAIL. |
| 2 | Coverage forcing-functions | BUILT | `HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` clockwork registry-drift tests — a new check fails the build until a decision is recorded. |
| 3 | Discovery → disposition → convert | BUILT | `tpcore/ladder` disposition SoT + clockwork drift-test + weekly-digest undispositioned section + `disposition` verb (PRs #44, #45). |
| 4 | Structural removal | BUILT | `RiskGovernor` kill-switch; `live_clearance` auto-de-escalation; DSR/credibility gate; provider RETIRE (Data Provider Lifecycle). |
| 5 | LLM/agentic triage | BUILT 2026-05-18 | `ops/llm_data_triage.py` + the event-driven `ops/llm_triage_service.py` daemon (#187, Epic E). Advisory-only: produces a draft, human-merge-only PR (additive mechanism-free HealSpec/RemediationSpec binding + dossier) + a non-authoritative `DATA_LLM_TRIAGE_PROPOSAL`; fenced by the deterministic label-gated `llm-triage-fence` CI job (provenance + hard-denied paths) + two-human review + post-merge canary/shadow. Spec: `docs/superpowers/specs/2026-05-18-llm-triage-advisory-layer-design.md`. |

## Disposition vocabulary

The four `Disposition` values in `tpcore/ladder/disposition.py`:

- `auto_converted` — a bounded deterministic capability already terminates this escalation class (e.g. a `healable=True` HealSpec; the datasupervisor auto-clear). Points to the capability.
- `escalate_operator` — no safe auto-termination; the operator dispositions each live instance. Carries the honest `unhealable_reason` / `escalate_reason`.
- `structural` — terminated by a recorded structural fix.
- `removed` — the source is removed from live capital (provider RETIRE / source de-clearance).

Rung-2-covered classes are **derived** from `HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` — not redeclared. The explicit `DISPOSITION_POLICIES` registry holds only the genuinely non-rung-2 classes (the two escalation event types and the audit known_knowns check names). `disposition_drift()` fails the build when any known escalation class lacks a recorded disposition — rung 3 cannot be silently skipped for a new class.

## Operator workflow

Undispositioned open escalations (a rung-1 terminal with no resolving terminal, older than the 7-day grace, not yet dispositioned) appear in the weekly digest's **UNDISPOSITIONED DATA-LANE ESCALATIONS** section. Disposition a live instance with:

```
python -m ops.weekly_digest disposition <ref> <converted|structural|removed> [note]
```

The existing non-skippable weekly ack and the existing **>=2 consecutive unacked weeks → `live_clearance` auto-de-escalation of live trading** are the enforcement teeth — unchanged. No new gate, no new daemon, no new table. The sacred `DATA_OPERATIONS_COMPLETE` 100%-green invariant is untouched.

## Non-goals

- Does NOT auto-convert anything. It forces the disposition decision to be recorded and surfaced; humans/PRs still do the converting (a new HealSpec, a structural fix, a RETIRE). Rung 3 is a forcing function, not an actor.
- No new gate, daemon, or table. Class SoT is code (clockwork test); instance teeth ride the existing weekly-digest event reads.
- Rung 5 (LLM/Epic E, BUILT 2026-05-18) is advisory-only and never auto-applies — see the responsible envelope below.
- Data lane only. No engine/aar files touched; no cross-lane convention prescribed.

## Rung 5 — the expert-vetted responsible envelope (LLM data-triage)

The LLM data-triage agent (`ops/llm_data_triage.py`, fired event-driven by `ops/llm_triage_service.py`) operates inside the maximal envelope two independent skeptical staff-architect expert passes defined, adopted verbatim from the spec §3/§4:

- **Advisory / human-gated.** The agent's sole deliverable is a **draft, human-merge-only PR** + a non-authoritative `DATA_LLM_TRIAGE_PROPOSAL`. It has no merge authority.
- **The LLM is never the mutating actor.** Data "stays 100%" only via the existing deterministic bounded path or a human running an existing canonical stage — exactly as before #187. The LLM/its PR never triggers, runs, queues, or schedules a repair and holds no creds or real tree to do so.
- **Additive, mechanism-free PR only.** Allowed change is an additive HealSpec / RemediationSpec / Ladder binding pointing an already-existing, already-proven, non-LLM-authored canonical `ops.py --stage` (with already-shipped bounded params) at the novel class. **Vetoed outright:** any new or widened repair mechanism (new stage, new/loosened param or bound, `healable` false→true, edit to an existing spec); any merge authority; any live credential in the sandbox; any real-working-tree write; treating the persona as a safety boundary.
- **Deterministic provenance / hard-denied fence.** A required label-gated `llm-triage-fence` CI job fails-and-auto-closes the PR on any hard-denied path or non-additive/new-mechanism diff; the LLM's self-classification of "minor vs major" gates nothing — the boundary is a property of the artifact, evaluated by code the LLM never runs on a tree it cannot write. The CI fence is credential-starved.
- **Two-human review, one ≠ the dispositioning operator** (CODEOWNERS). The change is **inert until merged**.
- **Post-merge canary/shadow.** A newly-merged LLM-authored spec fires but does NOT mutate (diffed vs no-op baseline) until a human promotes it after N cycles of detector-vs-healer agreement.
- **Credential-starved sandbox & CI.** The agent runs in an ephemeral `git worktree` with a credential-starved allowlisted env, official Anthropic `messages.create` with **no `tools`**, no-key/AuthenticationError safe no-op; any failure ⇒ proposal still emitted, no PR, no merge.

The engine/aar lane will, in a separate session, build a SYMMETRIC engine-native triage agent (symmetry-of-approach, not a clone) — it does not exist yet and is not implied here.
