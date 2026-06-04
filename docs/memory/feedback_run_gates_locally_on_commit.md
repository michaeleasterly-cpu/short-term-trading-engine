---
name: run-gates-locally-on-commit
description: "Operator 2026-05-23 + reinforced 2026-05-24: 'you dont have to push all the time just run the checks gitleas, pytest, ruff, etc when you commit'. Run EVERY CI-equivalent gate LOCALLY on commit, not just the obvious ones. The full set: gitleaks + ruff + targeted pytest + check_imports + VULTURE (dead-code) + lab-isolation-db tests when DB-touching. Push only when the batch is complete AND local-green. GitHub Actions is the secondary redundant check, not the primary signal — pushing-to-find-out burns Actions minutes (forced repo public 2026-05-21; vulture-miss on P5 cutover 2026-05-24)."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Rule (operator 2026-05-23):** *"you dont have to push all the time just run the checks gitleas, pytest, ruff, etc when you commit"*.

On commit, run the same gates the CI runs — LOCALLY. Don't push to find out if it would pass. Push only when the batch is complete AND the local gates are all green.

## Why (failure-derived 2026-05-23)

Session 87291947 pushed 3 PRs in one session (#322, #323, #324), watching CI for each. Each burned ~5min of GitHub Actions on jobs (pytest + lab-isolation-db) that could have been run locally in seconds. The repo was forced public 2026-05-21 because operator burned Actions quota — that incident is exactly the pattern this rule prevents.

Push when the batch is a tangible deliverable (per `feedback_push_when_tangible_batch_prs`). The CI gates are a secondary check, not the way you learn whether your code is green.

## The local-gate equivalents to run on commit

**The CI workflow runs ALL of these. So must I, locally, before push.**

For any commit touching code:

```bash
# 1. Secret scan (matches the gitleaks CI step)
gitleaks detect --no-git --redact 2>&1 | tail -5

# 2. Ruff lint (matches the ruff CI step)
.venv/bin/ruff check <changed Python files>

# 3. Vulture dead-code (matches CI's "vulture (fail on new dead code)" step)
#    INCLUDE THIS — missed it on P5 cutover 2026-05-24 (autouse fixture +
#    underscore-prefix-unused-arg flagged).
.venv/bin/vulture --min-confidence 60 tpcore ops reversion vector momentum sentinel canary catalyst dashboard_components vulture_allowlist.py

# 4. Orphan-scripts gate (for any new scripts/*.py — matches CI's test_no_orphan_scripts.py)
.venv/bin/python -m pytest scripts/tests/test_no_orphan_scripts.py -p no:xdist -q

# 5. Targeted pytest (matches the relevant CI test slice)
.venv/bin/python -m pytest -p no:xdist -q -k "<topic-keyword>"

# 6. For commits touching engines/feeds/adapters (matches CI's check_imports)
.venv/bin/python -m tpcore.scripts.check_imports

# 7. For commits touching DB / migrations / handlers — DB-gated tests
#    (matches CI's lab-isolation-db job). Hit live Supabase via DATABASE_URL_IPV4.
set -a && source .env && set +a && DATABASE_URL="$DATABASE_URL_IPV4" \
  RUN_DB_INTEGRATION_TESTS=1 .venv/bin/python -m pytest \
  <relevant-DB-gated-test-files> -p no:xdist -q
```

For docs-only commits (markdown changes only):
- `gitleaks detect --no-git --redact` is sufficient — no Python gates needed
- Read through the diff once for typos / broken links / stale references

**The decision question per gate:** "Could this commit possibly trip this CI gate?" If yes, run it locally. Vulture is the easy one to skip and the easy one to miss — it doesn't care about behavior, only about static dead-code-ness, so renaming a function or adding an autouse fixture can red it even when nothing else changes.

## How to apply

1. **Commit when work is logically done** (per `feedback_git_workflow_commit_push_ci`).
2. **Run the local-gate sequence** above (pick the gates that match what the commit touched).
3. **Fix in-thread if any gate is red** — don't commit a known-red change "to fix later".
4. **Push when the batch is a tangible deliverable** AND all local gates are green.
5. **CI after push is verification** — quick `gh pr view <N> --json statusCheckRollup` confirms the local green held up. If CI catches something local missed, that's a signal to upgrade the local-gate sequence for next time.

## What this changes (perpetual, every session)

- ≤3 PRs/session budget per `feedback_push_when_tangible_batch_prs` — still applies, but now I'm running the gates BEFORE I push, so each push is more likely to land first-try green.
- No more pytest-pending-let's-monitor cycles for docs-only PRs.
- No more "push, watch CI, see red, fix-forward, push again, watch CI" loops — local gates catch the issue before the first push.
- Commit cadence is FREE (cheap); push cadence is BOUNDED (only on major-deliverable batches that are local-green).

## Related

- [[git-workflow-commit-push-ci]] — § 8 already mandates "Run pytest LOCALLY before push, not just ruff"; this rule sharpens it to "run the FULL gate set locally on commit, not push-to-find-out"
- [[push-when-tangible-batch-prs]] — batching discipline; this rule complements it (run gates per commit, push per batch)
- [[check-ci-after-every-push]] — still applies; the CI check is verification of the local green, not the discovery mechanism
- [[gha-quota-diagnostic]] — 2026-05-21 incident where Actions quota burn forced the repo public; this rule prevents recurrence
