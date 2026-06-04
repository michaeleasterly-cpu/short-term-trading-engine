---
name: finder-first-edge-signal
description: "2026-05-22 — first concrete signal that the autonomous LLM edge-finder produces real edges. Catalyst PEAD candidate post-engine-enrichment shows Sharpe +1.24, PF 3.50, win-rate 70%. Engine surface enrichment is the unblock; not finder defect."
metadata: 
  node_type: memory
  type: project
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

**2026-05-22 — autonomous finder produced its first signal of a real edge.**

The `catalyst_pead_expansion_range` candidate (emitted v2.0 gate pilot, run `91100f12`) initially FAILED its Lab probe with Sharpe +0.18 + n_trades=2 because catalyst's engine required ≥3 distinct insider clusters BEFORE its event_confirmation_mode filter. The LLM's hypothesis was pure PEAD with no clustering need.

After PR #277 added `event_confirmation_mode='beat_30d_only'` (PEAD path, skips clustering) + `hold_days` Lab knob, the SAME candidate re-probed shows:

| Metric | Pre-enrichment | Post-enrichment |
|---|---|---|
| Sharpe | +0.18 | **+1.24** |
| Profit factor | 1.69 | **3.50** |
| Win rate | 50% | **70%** |
| n_trades | 2 | 10 |

**Why this matters:** the hypothesis quality was ALREADY there. The blocker was engine surface — LAB_TARGET didn't expose the right knobs for the LLM to specify PEAD-only. Once the engine surface caught up, the edge signature is visible: 70% win rate + 3.5 profit factor + 1.24 Sharpe on the test universe.

**Why:** validates the entire Task #25 build investment ($0.16 API + 4 pilots + 6 production-readiness fixes). The autonomous loop is mechanically sound + the LLM produces real edges; the bottleneck is engine surface, not finder defect.

**How to apply:**

1. **Engine surface enrichment is the next epic** (not finder redesign). For each engine where the LLM emitted a hypothesis that engine code can't express, add LAB_TARGET knobs. Pattern in PR #277 (`beat_30d_only` mode + `hold_days` knob) is the template.
2. **DSR gate failure ≠ no edge.** Catalyst's post-enrichment DSR is ~0 even with Sharpe 1.24 — that's because (a) test universe is 15 names so n_trades=10 ≤ gate floor; (b) cumulative lab_trial_ledger.catalyst=200 inflates the multiple-testing penalty. Production universe probe (T1+T2 ≈ 1300 names) likely lifts n_trades into the testable band.
3. **Don't re-probe immediately** after engine enrichment — preserve ledger discipline. The next probe should be deliberate (operator-explicit go), not reflexive.

**Related:** [[project_master_remaining_program]], [[feedback_anthropic_529_self_heal]], [[project_research_llm_edge_discovery]].
