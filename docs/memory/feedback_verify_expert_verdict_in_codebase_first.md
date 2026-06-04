---
name: verify-expert-verdict-in-codebase-first
description: "STANDING RULE (operator 2026-05-23): when a dispatched expert recommends DROPPING / DELETING / DEPRECATING something, GREP THE ACTUAL CODEBASE for downstream dependencies BEFORE relaying the verdict as actionable. Experts speak in general terms and don't see the repo. Verify in-code before authorizing destructive action."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23):** *"you said it wasn't used... now it is"*.

When I dispatch a subject-matter expert (financial, db-architect, general-purpose) and they recommend a destructive action — DROP a table / DELETE rows / RETIRE a feed / REMOVE an indicator — I must **grep the actual codebase for downstream consumers BEFORE relaying the recommendation to the operator as actionable**.

Experts speak in general principles ("regional macro doesn't add edge to a national-universe daily system"). They have NO visibility into:
- Our specific `tpcore/` / `sentinel/` / `vector/` / `reversion/` / etc. modules
- Derived series / computed fields that depend on the candidate-for-drop
- Sacred byte-identical tests
- HealSpec / validation-check entries that reference the candidate
- ECR'd engine commitments

The codebase is the ground truth; the expert's verdict is the hypothesis. Validate the hypothesis against the ground truth BEFORE authorizing execution.

## Anti-pattern observed today

1. Financial expert recommended dropping 50 PHCI series + sos_state_diffusion as "pure storage cost; no engine uses state-of-incorporation as a signal".
2. I relayed this to operator with "drop PHCI + sos_state, add NFCI" as the actionable next move.
3. Operator authorized: "yes proceed".
4. THEN I went to execute and grepped — discovered `sentinel/backtest.py`, two byte-identical tests, the `diffusion.py` derivation, and validation checks all reference PHCI / sos_state.
5. Halted with "actually we can't drop this."
6. Operator: "you said it wasn't used... now it is".

The grep should have run BEFORE step 2, not after step 4.

## Pattern (corrected)

Before passing an expert's destructive recommendation to the operator:

1. **Grep all engine + tpcore + scripts + tests for any reference** to the candidate (table name, function name, series id, FK target).
2. **Check the HealSpec registry** for any health-check referencing it.
3. **Check sacred byte-identical tests** in `*/tests/test_*_byte_identical.py`.
4. **Check ECR-tracked engine commitments** in `tpcore/engine_profile.py` for data_dependencies.
5. Surface findings IN THE SAME response as the expert verdict: "Expert says drop; but our `sentinel/backtest.py` uses it via the `sos_state_diffusion` derivation. Pre-condition: retire bear-score Lab candidate first via ECR."

If the dependency check returns nothing, then surface the expert verdict + "verified no downstream consumers."

## What this overrides

The instinct to forward expert verdicts as authoritative. Experts are advisory; the codebase is dispositive.

## How this composes with other rules

- `[[ask-expert-then-execute]]` — still ASK experts; but VERIFY their drop recommendations against code before EXECUTE.
- `[[authorization-via-expert-keep-moving]]` — still applies for routine YES gates; only adds friction for destructive drop actions specifically.
- `[[investigate-dont-hand-wave-findings]]` — sibling rule; "verify before stating" is the same principle applied to expert relay.
- `[[no-shortcuts-100-pct]]` — the codebase grep IS the verification step that this rule mandates.
- `[[no-lazy-vendor-blame]]` — same shape: don't accept a generalized claim without per-item evidence.

## Related

- `[[ask-expert-then-execute]]`
- `[[investigate-dont-hand-wave-findings]]`
- `[[no-shortcuts-100-pct]]`
- `[[no-lazy-vendor-blame]]`
