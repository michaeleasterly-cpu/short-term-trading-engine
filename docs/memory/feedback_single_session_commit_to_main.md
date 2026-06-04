---
name: single-session-commit-to-main
description: "STANDING RULE (operator 2026-05-23, single-session mode): during single-session mode, commit + push directly to main. No feature branches + PRs for routine work. Local-gates discipline still applies. Heavy-lane reviewers (spec-reviewer / code-quality-reviewer) can still be dispatched on the post-push diff if needed."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23, applies during `[[single-session-until-db-done]]` mode):** *"why are you using branches and not main because you are the only session right now"*.

When only one session is active (per `single-session-until-db-done`), commit + push directly to `main`. NO feature branches + PRs for routine work. The branch-and-PR ceremony exists to coordinate parallel sessions; with only one session, it adds friction without benefit.

## What this changes

- **Default workflow:** `git add <files> && git commit && git push origin main`. NOT `git switch -c feat/foo && ... && gh pr create`.
- **CI signal:** runs on every push to main; if red, fix-forward (next commit) or revert.
- **Reviewer cadence:** heavy-lane split-review (spec-reviewer + code-quality-reviewer) can still be dispatched on the post-push diff. They run against the merged state; their findings become follow-up commits.
- **PR count:** stops mattering as a session metric. The operator's earlier "≤3 PRs without checking" is replaced by "ship local-green commits to main".

## What STAYS the same

- **Local gates before EVERY push** per `[[run-gates-locally-on-commit]]` — gitleaks + ruff + pytest + check_imports for the changed scope. The standing rule is even more important without the PR gate.
- **Major-deliverable batching** per `[[push-when-tangible-batch-prs]]` — commit often, push when a deliverable is complete. Direct-to-main doesn't mean per-line pushes; it means no PR-and-merge layer between local-green and live.
- **Heavy-lane discipline** per `.claude/rules/heavy-lane.md` — the migrations / risk / selfheal paths still warrant split-review. Just run the reviewers BEFORE pushing to main rather than gating a PR.
- **Standards anchoring + ISO + memory updates** — unchanged.

## When this rule does NOT apply

- When `[[single-session-until-db-done]]` expires (Carver or another session resumes).
- When the operator explicitly says "use a PR for this".
- When the change is genuinely high-risk and benefits from the GitHub PR review UI (rare; usually heavy-lane reviewers cover this).

## How to apply

For routine code commits:
```bash
.venv/bin/ruff check <files>
.venv/bin/python -m pytest <targeted> -p no:xdist -q
gitleaks detect --no-git --redact --source <files>
git add <specific files>
git commit -m "..."
git push origin main
```

For heavy-lane (migrations, risk, selfheal):
```bash
# Same local-gates AS ABOVE, then:
git push origin main
# Dispatch spec-reviewer + code-quality-reviewer on the head commit
# Fold findings via follow-up commits if they flag anything
```

## Related

- `[[single-session-until-db-done]]` — the parent state this rule operates under
- `[[run-gates-locally-on-commit]]` — the load-bearing gate when there's no PR layer
- `[[push-when-tangible-batch-prs]]` — major-deliverable batching still applies
- `[[keep-building-dont-pause-for-breaks]]` — no pause for branch-and-PR ceremony
- `[[i-do-that-too-not-operator-action]]` — sibling rule on execution responsibility
