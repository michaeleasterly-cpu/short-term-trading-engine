---
name: autonomous-lab-criteria-replaces-absolute-gate
description: The absolute DSR ≥ 0.95 ∧ credibility ≥ 60 gate was superseded for ADD/promote by the autonomous Lab criteria framework (PR
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**The Lab→PAPER graduation gate is now the autonomous Lab criteria set, NOT the absolute DSR/cred threshold.**

**Why this changed (2026-05-20, PR #158):**

The original gate `DSR ≥ 0.95 ∧ credibility ≥ 60` was applied to BOTH "is this a new engine worth adding" (ADD) AND "is this an improvement worth folding" (MODIFY). It was over-constrained on both:

- **For new engines:** DSR's denominator depends on `n_trials`. A sparse-but-real-edge engine (catalyst: 24 trades / 6 years → DSR=0.754) can never clear DSR ≥ 0.95 *no matter how clean the signal is*. The 5 grandfathered PAPER engines (reversion, vector, momentum, sentinel, canary) also failed this absolute gate — they were activated before the SDLC framework existed.
- **For improvements:** an absolute threshold rejects real wins. A change that takes Sharpe from 0.4 to 0.7 is a real improvement but doesn't clear 0.95 absolute.

**What replaced it (autonomous criteria sets):**

`ops/engine_sdlc/lab_criteria.py` ships two pure-function gates:

- `_assess_new_engine_signal(dossier) → (passed, reason)`: signal-presence floor. `sharpe > 0` AND `trades >= 10` AND `max_drawdown >= -0.50` AND `ruin_probability <= 0.30` AND `profit_factor >= 1.0` AND `min_btl_gap <= 365`. Calibrated to accept catalyst's empirical numbers (sharpe 2.27, 24 trades, max_dd -0.41, ruin_prob 0.087, PF 1.36, min_btl_gap 109). None are subjective; all are read directly off the dossier.

- `_assess_improvement(candidate_dossier, incumbent_dossier, primary_metric) → (passed, reason)`: comparative gate. Candidate must beat incumbent on the declared `primary_metric` (strict, not tie) AND pass the new-engine criteria above (the floor) AND have trade-count within 50% of the incumbent (so "better Sharpe via 90% fewer trades" doesn't qualify as an improvement).

**Three-gate semantic separation:**

1. **LAB → PAPER** (ADD/promote) — autonomous Lab criteria (PR #158).
2. **PAPER (improvement)** — `_assess_improvement` for `fold_existing` MODIFY.
3. **PAPER → LIVE** — RESERVED. Paper-only mandate stays binding (CLAUDE.md). Future-reserved gate; no code path opens it today. The drift sentinel `test_clause_d_claude_fail_the_gate_honesty_substring` pins the LIVE-reserved phrase in CLAUDE.md.

**How to apply (future sessions):**
- Don't enforce `DSR ≥ 0.95 ∧ credibility ≥ 60` as the gate in new code paths — that's the SUPERSEDED gate. Use the criteria functions from `ops/engine_sdlc/lab_criteria.py`.
- The dossier still computes DSR + credibility — they are diagnostics + the gate's underlying floor inputs (sharpe, trades, etc.) — but they are NOT the binary gate.
- ECR-ADD with `source: existing_code` reads the engine's most-recent backtest dossier and applies `_assess_new_engine_signal` before allowing the lifecycle transition. Same path catalyst used to land PAPER 2026-05-20.
- ECR-MODIFY with `intent: fold_existing` reads the Lab dossier and applies `_assess_improvement` against the current incumbent. The PCA-residual sweep 2026-05-21 would have used this path had it cleared (it didn't — see [[pca-residual-falsified-2026-05-21]]).

**Why this matters / vital context:**
- Old specs/plans / older parts of TODO.md / older memory entries reference `DSR ≥ 0.95 ∧ cred ≥ 60` as the canonical gate. Those references are HISTORICAL post 2026-05-20.
- The 5 grandfathered PAPER engines stay PAPER — they're not being re-tested against the autonomous criteria retroactively. The criteria gate forward-only.
- catalyst's activation to PAPER (PR #159 via the new `source: existing_code` path + the autonomous criteria) is the canonical example of the new gate's use.

**Related:**
- `docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md` — the design spec
- PR #158 implementation
- [[project-engine-tables-sot-migration]] — sibling SoT migration
- [[project-ml-research-track]] — the commissioned-expert verdict that informed both gate reforms
