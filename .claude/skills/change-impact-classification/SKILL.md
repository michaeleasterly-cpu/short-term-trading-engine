---
name: change-impact-classification
description: "Model-invocable change-impact classification (CIC) gate. Companion to /system-wide-verification. Before any targeted fix in the discovery-first rule's scoped paths, name the change type (one of 16 classifications), the system boundary (local / shared / systemic / unknown), answer the 12 mandatory questions, and prove the chosen layer is correct. Returns PROCEED / DISCOVERY_REQUIRED / OPERATOR_DECISION_REQUIRED. Designed in docs/audits/2026-06-03-claude-code-workflow-controls.md §9."
---

# /change-impact-classification

Authoritative external: <https://code.claude.com/docs/en/skills>.
Canonical SoT: `docs/audits/2026-06-03-claude-code-workflow-controls.md` §9.
Auto-loaded via: `.claude/rules/discovery-first.md` (path-scoped).

## When to invoke

- **After SWV passes** — the SWV trace is input to CIC's "why this layer" answer.
- **Auto** — when the user asks for a fix / patch / repair / backfill / cleanup AND the diff would touch a `discovery-first`-scoped path.
- **Slash** — `/change-impact-classification` manually at any time.

## What this skill does

Names the change type, names the boundary, answers 12 mandatory questions, and produces a verdict. The pair (SWV + CIC) gates any fix on the scoped paths. SWV says "here's what the system looks like." CIC says "here's what kind of change this is and why this layer is correct."

## The 16 change classifications

Pick exactly one:

```text
documentation_only
workflow_control_change
claude_hook_or_agent_change
github_workflow_change
test_only_change
local_code_behavior_change
shared_abstraction_change
database_schema_change
database_data_repair
ingestion_or_backfill_change
validator_or_gate_change
engine_signal_change
broker_or_order_routing_change
risk_or_capital_gate_change
configuration_or_environment_change
unknown_requires_discovery
```

`unknown_requires_discovery` is always a DISCOVERY_REQUIRED verdict. Use it honestly when the classification isn't clear.

## The 4 system boundaries

Pick exactly one:

- `local` — only this file's behavior changes; no readers, helpers, or downstream consumers affected.
- `shared` — a helper / class / table used by multiple callers; this change affects all of them.
- `systemic` — the defect spans multiple paths and the proposed fix addresses only one; this is the failure pattern the discovery-first rule was built to catch.
- `unknown` — the trace from SWV didn't conclusively place the change. DISCOVERY_REQUIRED.

## The 12 mandatory questions

Answer each one — vague answers count as DISCOVERY_REQUIRED.

1. What kind of change is this exactly? (one of the 16 classifications)
2. Is this local, shared, systemic, or unknown?
3. What behavior changes if this is implemented?
4. Who calls this code or uses this data? (cite SWV reader trace)
5. What upstream component creates the state being changed? (cite SWV writer trace)
6. What downstream component depends on the output? (cite SWV blast radius)
7. Is this fixing the root cause or patching a symptom?
8. Could this break another caller that uses the same helper / table / hook / workflow / setting / abstraction?
9. Could this be solved by using an existing model instead of creating a new one?
10. Does the existing system already have a table / function / trigger / hook / rule / workflow meant to handle this?
11. Why did the existing control not prevent the defect? (cite SWV "existing controls" point)
12. What evidence proves this is the correct layer to change?

## Required output format

```text
CIC-GATE for <one-line scope description>

CHANGE_TYPE:           <one of the 16 classifications>
SYSTEM_BOUNDARY:       <local | shared | systemic | unknown>
AFFECTED_COMPONENTS:   <named list with file:line>
ROOT_CAUSE_VS_SYMPTOM: <root_cause | symptom_patch | unknown>
WHY_THIS_LAYER:        <one-line evidence — typically cites SWV result>
WHAT_COULD_BREAK:      <named list>
COLLATERAL_CHECKED:    <named list of adjacent callers verified>

Mandatory questions (one-line each):
 1. <kind of change>
 2. <boundary>
 3. <behavior change>
 4. <readers>
 5. <upstream writer>
 6. <downstream consumer>
 7. <root cause / symptom>
 8. <breakage risk>
 9. <existing model alternative>
10. <existing infra for this>
11. <why existing control didn't prevent>
12. <evidence this is correct layer>

VERDICT: PROCEED | DISCOVERY_REQUIRED | OPERATOR_DECISION_REQUIRED
```

## When to return DISCOVERY_REQUIRED

- `CHANGE_TYPE` is `unknown_requires_discovery`.
- `SYSTEM_BOUNDARY` is `unknown`.
- Only the target file/table/hook was inspected; collateral not checked.
- Shared callers not inspected.
- Downstream consumers not inspected.
- Existing system controls not checked (see SWV's #4 trace point).
- Tests not checked (SWV's #5).
- The fix creates a new abstraction / table / hook / workflow before proving existing mechanisms are insufficient (question #9 / #10 unanswered or evasive).
- The proposed fix is `local` but the defect is `systemic`.
- The agent cannot explain why this is the correct layer (question #12 evasive).

## When to return OPERATOR_DECISION_REQUIRED

- `CHANGE_TYPE` is in the policy-gated set (`database_schema_change`, `risk_or_capital_gate_change`, `broker_or_order_routing_change`) AND the operator hasn't authorized this specific change yet.
- A new platform table is proposed (per audit §13 #11 — "no new platform table without operator-approved schema rationale"). DISCOVERY can complete; OPERATOR_DECISION is the gate-on-action.
- The change crosses two `discovery-first`-scoped paths and the right ordering is contested.

## When to return PROCEED

All 12 questions answered concretely; SWV passed; the chosen layer is named and defended; the boundary is `local` or `shared` with all collateral verified; no policy gate fires.

## Type-design analysis (merged from anthropics/claude-code pr-review-toolkit, vendor-audit §3)

Vendored 2026-06-04 per `docs/audits/2026-06-03-vendor-vs-handrolled.md` §3 + operator decision §9 #3 ("merge with the CIC gate rather than vendor as a standalone agent"). Anthropic ships `type-design-analyzer` as a separate pr-review-toolkit agent; STE folds its analysis here so a single CIC pass covers both classification + type-design — one gate, one report.

### When to run the type-design pass

Run the type-design analysis (in addition to the 12 mandatory questions) when the diff matches **any** of:

- `CHANGE_TYPE` is `shared_abstraction_change` — a new helper class / mixin / abstraction is introduced.
- `CHANGE_TYPE` is `database_schema_change` — a new table or column is a new row-type with implicit invariants.
- `CHANGE_TYPE` is `local_code_behavior_change` AND the diff introduces a new `class` / `dataclass` / Pydantic v2 model / Protocol / TypedDict / Enum.
- The diff modifies an existing type's invariants (frozen=True flip, mutable default added, validator removed, required field made optional).

**Skip** the type-design pass for `documentation_only`, `test_only_change`, `configuration_or_environment_change`, `workflow_control_change`, `github_workflow_change`, `claude_hook_or_agent_change` — those don't carry type invariants in the relevant sense.

### What to evaluate (5 dimensions)

For each new or modified type, evaluate:

1. **Identify invariants.** Data-consistency requirements, valid state transitions, relationships between fields, business-logic rules encoded in the type, preconditions / postconditions. List each one with a one-line description.
2. **Encapsulation (1–10).** Are internals hidden? Can invariants be violated from outside? Is the interface minimal and complete? STE convention: never expose tpcore-private attrs (`._store`, `._pool`); extend the class with a public accessor (see `docs/STYLE_GUIDE.md`).
3. **Invariant expression (1–10).** How clearly do the invariants come through the type's structure? STE convention: Pydantic v2 `Field(...)` constraints, `Literal[...]`, `Annotated[...]` for explicit invariants; type hints required everywhere (`from __future__ import annotations`).
4. **Invariant usefulness (1–10).** Do the invariants prevent real bugs? Aligned with STE's risk model (capital-gate, 100%-green data invariant, paper-only mandate)? Neither too restrictive nor too permissive.
5. **Invariant enforcement (1–10).** Are invariants checked at construction (`__init__` / Pydantic validator)? All mutation points guarded? Is it impossible to create an invalid instance? STE convention: prefer `frozen=True` Pydantic models for value objects; constructor validation is required, not advisory.

### STE-specific anti-patterns to flag

In addition to Anthropic's catalogue (anemic models, exposed mutable internals, invariants only in docs, too many responsibilities, missing constructor validation, inconsistent enforcement) — flag these STE-specific ones:

- **No `from __future__ import annotations`** at the top of a module that defines new types.
- **Untyped `Callable`** (`Callable` without parameters/return) — required to be `Callable[[...], ...]` per code-quality-reviewer.
- **`dict[str, Any]` as a return type** when a Pydantic model would be more specific (one of STE's recurring defect classes).
- **Missing `tpcore.aar.classify_exit_reason` call** when a new `ExitReason` literal is added — hardcoded literals can't carry the invariant.
- **A new ticker-bearing class without an `as_of` field** in the constructor — per `.claude/rules/identity-path.md`, the identity chain must be provable.
- **A new platform-table-row dataclass without `classification_id`** — same identity-path discipline.

### Required output (added to the CIC report when type-design runs)

```text
## Type-design analysis

For each new / modified type:

### <TypeName>

Invariants identified:
  - <invariant 1>
  - <invariant 2>

Ratings:
  - Encapsulation:        X/10 — <one-line justification>
  - Invariant expression: X/10 — <one-line justification>
  - Invariant usefulness: X/10 — <one-line justification>
  - Invariant enforcement: X/10 — <one-line justification>

Strengths: <what's done well>
Concerns: <specific issues>
Recommended improvements: <concrete, pragmatic suggestions — perfect is the enemy of good>
```

Add this section to the CIC report; the overall CIC verdict (`PROCEED` / `DISCOVERY_REQUIRED` / `OPERATOR_DECISION_REQUIRED`) still applies. A type with average rating < 6/10 across the four dimensions, AND no clear improvement path, should usually return `DISCOVERY_REQUIRED` — the existing type model needs more thought before fixing the symptom.

## What this skill does NOT do

- Never proposes the actual fix. CIC is classification + justification, not implementation.
- Never modifies files. Read / Grep / Glob only.
- Never auto-runs the fix even after PROCEED. The implementer (engine-implementer / adapter-implementer / db-architect) consumes the SWV + CIC output as input.
- Never writes to memory.
- Never invokes destructive operations.

## Adjacent SoT

- `.claude/rules/discovery-first.md` — the path-scoped rule that loads this skill.
- `.claude/skills/system-wide-verification/SKILL.md` — the prerequisite gate.
- `docs/audits/2026-06-03-claude-code-workflow-controls.md` §9 — the design.
- `docs/audits/2026-06-03-vendor-vs-handrolled.md` §3 — the type-design merge decision (§9 #3).
- `.claude/rules/migrations.md` — for `database_schema_change` classifications: also check the "no new platform table without schema rationale" wording (audit §13 #11, implemented PR #467).
- `.claude/rules/identity-path.md` — for any ticker-bearing type / classification_id-bearing schema, the identity-path discipline applies.
- `.claude/rules/data-adapter.md` — for `ingestion_or_backfill_change` classifications: the 6-stage contract is the existing infrastructure question #10 must check.
- `docs/STYLE_GUIDE.md` — the type-hint + Pydantic v2 + private-attr conventions the type-design pass enforces.

## Acknowledgement of vendor source

The "Type-design analysis" section above adapts the prompt structure (5-dimension framework, output format, anti-pattern catalogue, key principles) from `anthropics/claude-code` `plugins/pr-review-toolkit/agents/type-design-analyzer.md`. STE-specific anti-patterns and the trigger conditions are STE-original.
