---
name: system-wide-verification
description: "Model-invocable system-wide verification (SWV) gate. Before any targeted fix in the discovery-first rule's scoped paths (validators, ingestion, audit-heal, self-heal, migrations, scripts/ops.py), trace 10 points across writers, readers, source authority, existing controls, tests, workflows, config, adjacent callers, blast radius, and rollback. Returns PROCEED / DISCOVERY_REQUIRED / OPERATOR_DECISION_REQUIRED. Designed in docs/audits/2026-06-03-claude-code-workflow-controls.md §8."
---

# /system-wide-verification

Authoritative external: <https://code.claude.com/docs/en/skills>.
Canonical SoT: `docs/audits/2026-06-03-claude-code-workflow-controls.md` §8.
Auto-loaded via: `.claude/rules/discovery-first.md` (path-scoped).

## When to invoke

- **Auto** — when the user asks for a fix / patch / repair / backfill / cleanup AND the diff would touch a `discovery-first`-scoped path.
- **Slash** — `/system-wide-verification` manually at any time.

## What this skill does

Produces a 10-point trace of the affected behavior + a verdict. The output is **the input** to any fix on the path; without it, the fix is blocked by the `discovery-first` rule.

## The 10 trace points

For the behavior you intend to change, produce:

1. **Writer trace** — what code path produces the current behavior? Pasted `grep` / `rg` evidence with `file:line`.
2. **Reader trace** — what code path / engine / report / dashboard / operator consumes it? Same evidence shape.
3. **Source of truth** — who holds the canonical value? For US-equity identity: SEC is authoritative for CIK-backed issuers; FMP is fallback only. For prices: ALPACA > IEX > SIP > TRADIER > FMP (per `tpcore/upsert_bars_provenance_guard`). Named with `file:line`.
4. **Existing controls** — what triggers, validators, hooks, rules, tests, or workflows already enforce or detect this behavior? **Why did they not prevent the current defect?** Cite each.
5. **Tests** — which tests already cover the behavior? Which ones should have caught the defect but did not? Name the missing test, not just "tests are missing."
6. **Workflows / hooks** — what CI gate or Claude hook would have caught this? Why didn't it?
7. **Config / env** — what env var, feature flag, or setting affects it? Default value, and whether changing the default would solve this without code change.
8. **Adjacent callers** — what other call sites use the same helper / table / function / migration? Each one is a place this fix could break.
9. **Blast radius** — what breaks if this fix is applied and turns out to be at the wrong layer? Named callers / tables / engines.
10. **Rollback** — can the change be applied as a no-op first (e.g., a column that defaults to NULL; a feature flag default-off)? If yes, name the no-op step; if no, the change requires explicit operator approval.

## Required output format

```text
SWV-GATE for <one-line scope description>

1. Writer trace:
   - <file:line> — <what>
2. Reader trace:
   - <file:line> — <what>
3. Source authority: <SEC | ALPACA | FMP | …>
   - Evidence: <file:line>
4. Existing controls inspected:
   - <rule / trigger / hook / test / workflow> — <why it did/didn't catch this>
5. Test coverage:
   - Existing: <named tests + file:line>
   - Named gap: <the test that should have caught this but didn't>
6. Workflow / hook: <relevant CI gate or Claude hook> — <why it didn't catch>
7. Config / env: <env var / setting / default>
8. Adjacent callers: <named list with file:line>
9. Blast radius: <named callers / tables / engines at risk>
10. Rollback: <no-op-safe plan, OR "non-no-op-safe — operator decision required">

Why this is the correct layer: <one-line evidence>
What not to touch: <named list>

VERDICT: PROCEED | DISCOVERY_REQUIRED | OPERATOR_DECISION_REQUIRED
```

## When to return DISCOVERY_REQUIRED

Return `DISCOVERY_REQUIRED` if any of these is true:

- Only one file was inspected.
- Only one table was inspected (data-lane changes).
- Callers were not inspected.
- Downstream consumers were not inspected.
- Shared helper usage was not inspected.
- Tests were not inspected.
- Runtime entrypoints were not inspected.
- Config / env behavior was not inspected.
- Source authority was not verified.
- Existing controls were not checked.
- The proposed fix creates a new table, helper, sidecar, hook, or workflow before checking existing models.
- The proposed fix is local but the defect is systemic.
- The explanation relies on "probably" or "likely" without evidence.

These are the same blocking conditions the `discovery-first` rule enumerates.

## When to return OPERATOR_DECISION_REQUIRED

- The trace surfaces a policy question only the operator can answer (e.g., "this is a known schema-rationale gap; do you authorize the new sidecar table?").
- The proposed fix is non-no-op-safe AND the operator hasn't approved it.
- The fix layer is genuinely contested between two reasonable options.

## What this skill does NOT do

- Never proposes code or schema. SWV is a trace, not a fix.
- Never modifies files. The skill calls only Read / Grep / Glob / Bash(grep:*) / Bash(rg:*) tools.
- Never invokes `gh pr merge`, `git push --force`, or any destructive action.
- Never writes to memory.

## Companion gate

After SWV passes, run `/change-impact-classification` (CIC). Both gates must pass before any targeted fix on `discovery-first`-scoped paths.

## Adjacent SoT

- `.claude/rules/discovery-first.md` — the path-scoped rule that loads this skill.
- `.claude/skills/change-impact-classification/SKILL.md` — the companion gate.
- `docs/audits/2026-06-03-claude-code-workflow-controls.md` §8 — the design.
- `docs/audits/2026-06-03-identity-substrate-data-flow.md` — the failure case study.
- `.claude/agents/silent-failure-hunter.md` — the reviewer that hunts the failure modes SWV is designed to prevent.
