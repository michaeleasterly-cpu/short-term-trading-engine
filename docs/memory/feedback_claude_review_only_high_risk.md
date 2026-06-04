---
name: feedback-claude-review-only-high-risk
description: GitHub Claude Review consumes Anthropic credits — do not run it on every heavy-lane PR; reserve for security/architecture/destructive-data PRs and treat billing failures as non-dispositive
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

GitHub Claude Review (`.github/workflows/claude-review-heavy-lane.yml`) is **advisory** — its absence does NOT block merge.

**Why:** the Claude-review workflow charges Anthropic credits for every review comment. The operator's standing principle is that mechanical CI gates + local full-suite/ruff/vulture/gitleaks + diff-scope-matches-plan + operator review are already the authoritative merge basis. Spending credit on a routine docs/spec/plan/small-impl PR is wasted spend.

**How to apply:**

- **Default = do NOT trigger Claude review.** Routine docs/spec/plan PRs, small tested implementation PRs, anything where local gates already give adequate confidence — skip the credit spend.
- **Use Claude review ONLY when:** operator explicitly requests it · security-sensitive workflow/auth/secret-handling diff · large unfamiliar architecture change · high-risk migration or destructive data operation implementation · local evidence is genuinely ambiguous and a second-opinion is worth the credit.
- **Billing failure ("Credit balance is too low") is non-dispositive.** Do NOT rerun the failed check to "make CI green"; do NOT block merge on it. Report the failure as infrastructure-side, then merge on the mechanical-checks-green basis.
- **Merge basis = required mechanical checks green + local gates green + diff scope matches plan + operator review approves.** The Claude-review check fits in only as one optional input among those — never alone, never as a gate.

Related: [[feedback_keep_building_dont_pause_for_breaks]] · [[feedback_authorization_via_expert_keep_moving]] · [[feedback_cut_process_overhead_ship]]. Workflow file: `.github/workflows/claude-review-heavy-lane.yml`. Heavy-lane discipline doc: `.claude/rules/heavy-lane.md` §"Automated first-pass reviewer (advisory)" — which already says "advisory / review-only" but this entry hardens the operator's spend-discipline interpretation.

Established PR #444 (symbol_history_evidence_backfill impl), 2026-06-02: the workflow failed with "Credit balance is too low"; operator decision was NOT to rerun and NOT to treat the failure as a merge blocker.
