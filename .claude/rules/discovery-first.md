---
name: discovery-first
paths:
  - "tpcore/quality/validation/**"
  - "tpcore/ingestion/**"
  - "tpcore/auditheal/**"
  - "tpcore/selfheal/**"
  - "platform/migrations/**"
  - "scripts/ops.py"
description: "Path-scoped rule: discovery-first discipline on the paths where the 2026-06-02 identity-substrate failure happened. Two gates: System-Wide Verification (SWV) — trace writers/readers/consumers/tests/workflows — and Change-Impact Classification (CIC) — name the change type + prove the chosen layer. Both must pass before any targeted fix."
---

# Discovery-first (SWV + CIC gates)

Canonical SoT: `docs/audits/2026-06-03-claude-code-workflow-controls.md` §8 + §9 + §13 #1 + #2.
Authoritative external: <https://code.claude.com/docs/en/memory>.

## Why this rule loads

This rule is auto-loaded when a diff touches any path in the frontmatter `paths:` glob (per Claude Code's path-scoped rule mechanism). The path list covers the failure surface of the 2026-06-02 identity-substrate audit: validators, ingestion handlers, audit-heal, self-heal, migrations, and the `scripts/ops.py` stage registry. These are the lanes where a narrow local fix has historically masked a systemic defect.

## What this rule requires

Before any targeted fix to a file in scope, **both gates** must pass:

1. **System-Wide Verification (SWV) gate** — invoke `/system-wide-verification`. Produces a 10-point trace: writers, readers, source authority, existing controls, test coverage, workflows/hooks, config/env, adjacent callers, blast radius, rollback. Verdict: `PROCEED` / `DISCOVERY_REQUIRED` / `OPERATOR_DECISION_REQUIRED`.
2. **Change-Impact Classification (CIC) gate** — invoke `/change-impact-classification`. Names the change type (one of 16 classifications), the system boundary (local / shared / systemic / unknown), and answers the 12 mandatory questions. Verdict: same three values.

**The two gates compose.** SWV says "here's what the system looks like." CIC says "here's what kind of change this is and why this layer is correct." A fix that passes SWV but fails CIC (e.g., a local edit when the defect is systemic) is blocked. A fix that passes CIC but fails SWV (e.g., a "well-classified" change without a reader trace) is blocked.

## When the gates can be skipped (the narrow carve-out)

- **Pure documentation edits** (`docs/**`, `*.md` files under the rule's scope, comments-only edits inside a tracked file). The gates are about behavior change; doc-only edits don't change behavior.
- **Test-only additions** that add coverage *without* modifying production code. (Modifying a test to mask a real failure is a behavior change — gates apply.)
- **A docs-only audit doc** that codifies findings. (The previous two audits this year — controls-audit, vendor-audit — were docs-only and exempt.)

For everything else in scope: both gates run.

## What this rule blocks (DISCOVERY_REQUIRED conditions)

Either gate returning `DISCOVERY_REQUIRED` blocks the fix. The blocking conditions (per the audit §8.4 + §9.6):

- Only one file was inspected; callers / downstream consumers / shared helpers not traced.
- Tests were not inspected; the gap that allowed the defect to ship is unnamed.
- Source authority not verified (SEC vs FMP for identity; canonical source for prices / fundamentals).
- Existing controls (rules, triggers, hooks, tests, workflows) not checked.
- The proposed fix creates a new table, helper, sidecar, hook, or workflow before checking existing mechanisms.
- The proposed fix is local but the defect is systemic.
- The explanation relies on "probably" / "likely" without evidence.
- Change type is `unknown_requires_discovery`.
- The agent cannot explain why this layer is the correct one.

## Cross-links

- `/system-wide-verification` — `.claude/skills/system-wide-verification/SKILL.md`
- `/change-impact-classification` — `.claude/skills/change-impact-classification/SKILL.md`
- The morning audit's §8 (SWV design) + §9 (CIC design).
- `docs/audits/2026-06-03-identity-substrate-data-flow.md` — the failure case study that motivated this rule.
- `.claude/rules/selfheal-auditheal.md` — the 100%-green-or-don't-trade invariant; one of the things SWV's "existing controls" question must check.
- `.claude/rules/data-adapter.md` — the 6-stage contract; one of the things CIC's "is this a shared abstraction" question must check.
