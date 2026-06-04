---
name: check-ci-after-every-push
description: After every push (mine + subagents) check CI status before reporting completion. PR
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

After every push to a PR (mine OR a subagent's), check CI status before reporting "shipped." Run within 60s of push:

```bash
gh pr view <N> --json state,statusCheckRollup --jq '{state: .state, ci: [.statusCheckRollup[] | select(.conclusion == "FAILURE") | .name]}'
```

**Why:** 2026-05-22 — PRs #302 and #304 both shipped with ruff F541 + SLF001 red because subagents merged without verifying CI. Operator caught both. My report said "shipped + in flight" without the CI check.

**How to apply:**

1. When I push: run the query immediately after `git push`. If failures → fix-forward in the same turn before doing anything else.
2. When a subagent task-notification reports a PR URL: query CI before declaring success to the operator.
3. Subagent local-test pass is NECESSARY but NOT SUFFICIENT — CI runs ruff / vulture / check_imports / lab-isolation-db that may catch what local missed.
4. Pattern: most repeat failures are ruff F541 (f-strings without placeholders) or SLF001 (private member access in tests) — both have established fix patterns (`pyproject.toml` per-file-ignores for SLF, remove `f` prefix for F541).
