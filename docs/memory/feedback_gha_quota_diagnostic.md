---
name: gha-quota-diagnostic
description: GitHub Actions quota-wall failures look like real CI failures but with a distinct shape — 1-second-empty-step jobs. Diagnose before chasing code-level regressions.
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

When GitHub Actions reports CI **FAILURE** on a personal-account private repo, **check the job step count FIRST** before assuming a code regression.

**Quota-wall diagnostic signature:**
- Job `started_at` → `completed_at` delta = **1–3 seconds**
- `steps_count: 0` (empty array)
- `system.txt` log shows runner allocated + "Job is about to start running" but NO step-by-step log
- Affected jobs report `conclusion: failure` with NO actionable output

**How to verify in <30s:**
```
gh api repos/<owner>/<repo>/actions/runs/<RUN_ID>/jobs \
  --jq '.jobs[] | {name, conclusion, started_at, completed_at, steps_count: (.steps | length)}'
```

If `steps_count` is 0 across all jobs that "failed" → quota wall, not code.

**Why this matters:**
- The empty-steps shape is INDISTINGUISHABLE from a real CI failure in `gh pr view`'s `statusCheckRollup` rendering.
- Re-running the workflow does NOT regenerate steps under quota wall (the rerun also gets blocked).
- Cost-tier options (recommended order): (a) **make repo public** = unlimited free minutes (verify no secrets in history first); (b) self-host runner on operator's Mac; (c) enable paid Actions billing ($0.008/min ubuntu-latest); (d) wait for monthly quota reset.

**Operator surface:** the billing summary at `github.com/settings/billing/summary` shows "Actions minutes used / included" exactly — the operator can confirm with a single page-load.

**Why this is a *feedback* memory not just a doc:**
On 2026-05-21 a string of PRs (#211, #212, #220) showed "CI FAILURE" rollups. I spent ~20 min trying to reproduce locally (everything green) + tried `gh run rerun` (silently no-op) + grep'd CI workflow YAML (unchanged for 17h) before reasoning to the job-step-count check that revealed the quota wall. Future-me should reach for the `steps_count: 0` check FIRST on a personal-account repo when CI fails inexplicably across multiple unrelated PRs in the same hour.

Related: [[ci-gate-on-check-conclusion]] (the existing rule about gating on conclusion not mergeStateStatus). The quota-wall failures DO produce a real `conclusion: failure` — so the standing gate fires, but the diagnosis is environmental, not code.
