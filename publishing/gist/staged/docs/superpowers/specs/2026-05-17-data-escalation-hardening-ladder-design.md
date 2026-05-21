# Data-Lane Escalation & Hardening Ladder — Formal Design

**Status:** BUILT 2026-05-17 (DATA lane). Brainstorm → **spec (this
doc)** → plan → phased subagent build complete (P1–P3). Formalises the
operator-named "Escalation & Hardening Ladder" (memory
`deterministic-agents-epic`), **scoped to the DATA lane only** (operator
decision 2026-05-17 — engine/aar lanes are a different session's
territory; cross-lane unification is the operator's future
cross-session call, NOT front-run here).

**Build record:**
- P1 (PR #44): `tpcore/ladder/disposition.py` — `Disposition` enum,
  `DispositionPolicy` model, `DISPOSITION_POLICIES` explicit registry
  (non-rung-2 classes), `data_lane_escalation_classes()` (rung-2 keys
  derived from `HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` +
  explicit non-rung-2 set), `policy_for()`, `disposition_drift()`.
  Clockwork drift-test == full known set. Landed dark.
- P2 (PR #45): `ops/weekly_digest.py` undispositioned-escalations
  section — open-escalation reads (one-terminal-liveness queries),
  each annotated with `policy_for`, age-bounded by 7-day grace;
  `python -m ops.weekly_digest disposition <ref> <converted|structural|removed> [note]`
  verb; integrated into existing digest render + ack/de-escalation path.
- P3 (this): `docs/ESCALATION_HARDENING_LADDER.md` canonical data-lane
  convention doc + CLAUDE.md one-line pointer + spec reconciled to BUILT.

## 1. Principle (the load-bearing invariant)

Deterministic agents are bounded BY DESIGN — they resolve only KNOWN
failure classes with KNOWN bounded repairs. The answer to an
*uncovered / unresolvable* data-lane failure is **not** a
smarter/looser agent (expert-vetoed). It is:

> **Every data-lane escalation must terminate in exactly one of —
> `converted` (a new bounded deterministic capability), `structural`
> (a structural fix), or `removed` (the source taken out of live
> capital). Never by loosening an agent. Never silent best-effort.**

The system *hardens* by converting each escalation into a new bounded
deterministic capability or a structural removal — it never improves
by relaxing a gate or widening an agent's tolerance.

## 2. The 5 rungs, as they apply to the DATA lane (state)

| Rung | What | Data-lane status |
|---|---|---|
| 1 | Fail-closed + honest escalation | **BUILT** — selfheal/auditheal exit-gate (no `DATA_OPERATIONS_COMPLETE`), `DATA_REPAIR_ESCALATED`, `DATA_SOURCE_ESCALATED`, contract-drift `INGESTION_FAILED`, audit known_knowns FAIL. |
| 2 | Coverage forcing-functions | **BUILT** — `HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` clockwork registry-drift tests (a new check fails the build until a decision is recorded). |
| 3 | Discovery → disposition → convert | **GAP (this spec)** — forensics/Sprint-Dossier exist but are AAR/engine-loss focused, not wired to data-lane escalations; conversion is a manual `open→resolved` markdown hand-edit; nothing tracks or *forces* that a data-lane escalation terminates in converted/structural/removed. |
| 4 | Structural removal | **BUILT** — `RiskGovernor` kill-switch, `live_clearance` auto-de-escalation, DSR/credibility gate, provider RETIRE (Data Provider Lifecycle). |
| 5 | LLM/agentic triage | **OUT — Epic E**, operator-deferred (advisory, human-gated; never auto-applied). Explicitly not this spec. |

This spec **codifies rungs 1/2/4/5 by reference** (already built /
deferred — no new code) and **closes rung 3** for the data lane.

## 3. Deliverable A — the canonical convention doc

`docs/ESCALATION_HARDENING_LADDER.md` (data-lane scope): the §1
principle, the §2 rung table with the concrete data-lane mechanisms,
the disposition vocabulary (§4), and the rung-3 forcing function (§5).
It is the single human-readable contract a future maintainer / Sprint
Dossier author follows. CLAUDE.md gains a one-line pointer; the
`deterministic-agents-epic` memory is updated to "data-lane Ladder
formalised; cross-lane convergence still operator-deferred".

## 4. Deliverable B — the rung-3 forcing function

### 4.1 Disposition vocabulary

A `DispositionPolicy` per data-lane escalation **class**, one of:

- `AUTO_CONVERTED` — a bounded deterministic capability already
  terminates this class (e.g. a `healable=True` HealSpec; the
  datasupervisor auto-clear). Pointer to the capability.
- `ESCALATE_OPERATOR` — no safe auto-termination; honest, the
  operator dispositions each live instance (the rung-3 instance teeth,
  §4.3). Carries the honest reason (the `unhealable_reason` /
  `escalate_reason` class).
- `STRUCTURAL` — terminated by a recorded structural fix.
- `REMOVED` — the source is removed from live capital (provider
  RETIRE / source de-clearance).

### 4.2 Class-level SoT + clockwork drift-test (DRY — no duplicate SoT)

`tpcore/ladder/disposition.py`:

- `data_lane_escalation_classes()` enumerates the FULL known set from
  the authoritative emitters (read, never guessed — the
  auditheal/contract-sentinel premise-defect discipline):
  - rung-2-covered classes: `HEAL_SPECS`, `REMEDIATION_SPECS`,
    `ADAPTER_CONTRACTS` keys;
  - NOT rung-2-covered: the escalation event types
    `DATA_REPAIR_ESCALATED`, `DATA_SOURCE_ESCALATED`, and the
    `audit_data_pipeline` known_knowns FAIL check names.
- For a rung-2-covered class the `DispositionPolicy` is **DERIVED, not
  re-declared** (`healable`/`remediable=True` → `AUTO_CONVERTED` with
  the stage as the capability pointer; `False` + reason →
  `ESCALATE_OPERATOR` carrying that reason). Single source of truth —
  the rung-2 registries remain authoritative; this layer only reads
  them.
- For the NOT-covered classes, an explicit `DISPOSITION_POLICIES`
  registry (frozen, evidence-backed, mirrors HealSpec/RemediationSpec
  style) declares the policy.
- `disposition_drift()` → `(missing, extra)` vs the full known set; a
  clockwork test asserts both empty. **A new data-lane escalation
  class fails the build until a disposition decision is recorded** —
  exactly the rung-2 pattern, applied to the escalation layer itself.
  Rung 3 can never be silently skipped for a new class.

### 4.3 Instance-level teeth — reuse the weekly digest (no new mechanism)

`ops/weekly_digest.py` gains an **"undispositioned data-lane
escalations"** section: each **open** escalation instance — a rung-1
terminal with no resolving terminal (`DATA_SOURCE_ESCALATED` with no
later `DATA_SOURCE_CLEARED`; `DATA_REPAIR_ESCALATED` with no later
`DATA_REPAIR_COMPLETE` for the request) — older than
`_DISPOSITION_GRACE` (default: 1 digest period) and not
operator-dispositioned, with its derived/declared class policy.

Enforcement reuses the digest's **existing** teeth verbatim: the
non-skippable weekly ack, and the existing **≥2 consecutive unacked
weeks → `live_clearance` auto-de-escalation of live trading**. An
operator dispositioning the instance (the existing ack flow, extended
to record a disposition) clears it from the section. No new gate, no
new daemon, **the sacred `DATA_OPERATIONS_COMPLETE` 100%-green
invariant is untouched** (this never gates the data cycle — it rides
the operator-facing digest path, exactly like cutover/self-heal
near-miss reporting already does).

## 5. Honest scope / non-goals

- **Data lane only.** No engine/aar files touched; no cross-lane
  convention prescribed (operator's future cross-session call).
- **Does NOT auto-convert anything.** It forces the disposition
  decision to be *recorded and surfaced*; humans/PRs still do the
  converting (a new HealSpec, a structural fix, a RETIRE). Rung 3 is a
  forcing function, not an actor.
- **No new gate / daemon / table.** Class SoT is code (clockwork
  test); instance teeth ride the existing weekly-digest event reads.
  The 100%-green emit invariant is unchanged.
- **No duplicate SoT.** Rung-2 registries stay authoritative;
  disposition is derived from them where they exist.
- **Rung 5 (LLM/Epic E) is out** — operator-deferred, advisory-only,
  never auto-applied; not designed here.
- Operator interaction: unchanged in *kind* (the weekly digest already
  exists and is already non-skippable); this adds one section to it,
  not a new touchpoint.

## 6. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `tpcore/ladder/disposition.py`: `DispositionPolicy` model, `DISPOSITION_POLICIES` explicit registry (non-rung-2 classes), `data_lane_escalation_classes()` (derive rung-2 keys from `HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` + the explicit set), `policy_for(class)` (derived-or-declared), `disposition_drift()`. Clockwork drift-test == full known set; derivation-correctness tests (healable→AUTO_CONVERTED, unhealable→ESCALATE_OPERATOR). **Landed dark.** |
| 2 | `ops/weekly_digest.py`: the undispositioned-escalations section — open-escalation reads (one-terminal-liveness queries, mirroring the existing digest read patterns), each annotated with `policy_for`, age-bounded; integrate into the existing digest render + the existing ack/de-escalation path (extend, do not rebuild). Fake-pool tests: open vs resolved vs operator-dispositioned; age bound; ≥2-unacked still rides existing de-escalation. |
| 3 | `docs/ESCALATION_HARDENING_LADDER.md` (the canonical data-lane convention doc) + CLAUDE.md one-line pointer + `deterministic-agents-epic` memory update + this spec → BUILT with build record. |

## 7. Open questions for the plan phase (resolve by READING code, not guessing)

- **Exact enumeration sources.** The plan must read, not guess: the
  `HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` key accessors;
  the `audit_data_pipeline.py` known_knowns FAIL check-name set (the
  contract-sentinel spec already enumerated ~11 — re-derive from
  `run_known_knowns`); the canonical event-type constants
  (`ops/data_repair_service.ESCALATED_EVENT_TYPE`,
  `tpcore/datasupervisor/state.ESCALATED_EVENT`).
- **Operator-disposition record shape.** How the digest ack records a
  disposition (a new `data->>'disposition'` on the existing ack event
  vs a small dedicated event) — pick the lowest-blast-radius option
  consistent with how `ops/weekly_digest.py` already records acks;
  read that file first.
- **`audit_data_pipeline` FAIL classes that overlap rung-2**
  (e.g. `validation_status` mirrors selfheal): the plan must
  de-duplicate so a class with a rung-2 disposition is NOT also given
  a conflicting explicit one (derived wins; explicit only for the
  genuinely-uncovered remainder).
- **Grace period unit.** `_DISPOSITION_GRACE` in *digest periods* vs
  cycles vs wall-clock — align with how the digest already bounds its
  other sections (read `ops/weekly_digest.py`).
