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
| 5 | LLM/agentic triage | OUT, Epic E | Operator-deferred; advisory, human-gated, never auto-applied. Explicitly not designed here. |

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
- Rung 5 (LLM/Epic E) is out — operator-deferred, advisory-only, never auto-applied.
- Data lane only. No engine/aar files touched; no cross-lane convention prescribed.
