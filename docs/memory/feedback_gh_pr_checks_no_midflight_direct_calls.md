---
name: gh-pr-checks-no-midflight-direct-calls
description: "Don't run direct one-shot `gh pr checks <n>` calls while CI is still in-flight — wait for the background poll loop to settle instead. Mid-flight direct calls surface noisy \"Error: Exit code 8\" in the UI."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

Don't run direct one-shot `gh pr checks <n>` calls while CI is still in-flight. Only check after the background poll loop has settled (the `until ! gh pr checks ... | grep -q "pending"; do sleep N; done` pattern).

**Why:** `gh pr checks` exit codes by design: 0 = all green, 1 = at least one failed, **8 = at least one still pending**. The wrapper UI surfaces exit-8 as "Error" even though it's the canonical "still polling" code. The operator flagged the resulting noise on PR #463 (2026-06-04). The polling loop ignores the exit code and greps the text, so it's the right pattern for waiting.

**How to apply:** When I push and want to gate on CI, kick off a single background poll loop and wait for the notification. Resist the urge to do impatient mid-flight `gh pr checks <n>` calls — they add noise without adding signal. If a one-shot status read is genuinely needed (e.g., the background loop errored after the worktree was removed), append `|| true` to suppress the exit-8 noise, but accept that this also loses the exit-0/exit-1 distinction.

**Polling-loop early-exit gotcha:** The naive `until ! gh pr checks <n> 2>&1 | grep -q "pending"; do sleep N; done` exits prematurely when gh's first response shows ONLY the fast checks (Vercel, etc.) that already passed — pytest/gitleaks/lab-isolation may not have been registered with the GitHub API yet. Observed 3+ times on 2026-06-04. Two robust fixes: (a) add an initial `sleep 30` before the loop so all checks have time to register; OR (b) wait until a specific known-slow check appears in the output before considering the run "started" — e.g., `until gh pr checks <n> 2>&1 | grep -q "pytest"; do sleep 30; done` first, then the pending-grep loop.

Related: [[feedback_check_ci_after_every_push]] — the timing rule (check CI within 60s of every push) is unchanged; that's about *when* to start polling. This rule is about *how* to poll cleanly.
