---
name: rebase-on-main-before-branch-work
description: "Always fetch + rebase on origin/main BEFORE doing substantive work on a feature branch — stale local main causes audits/research to run against state that's already been changed on main"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 6a2df8c2-f4fc-4267-b363-6d93871c6abf
---

Always run `git fetch origin main && git rebase origin/main` (or `git merge origin/main`) BEFORE doing substantive research/audit/implementation work on a feature branch — even when SessionStart hook context makes it look like the branch is current.

**Why:** When a session /clear's into a feature branch, the local `main` HEAD can be many commits behind `origin/main` without any visible signal. Dispatching an audit or research subagent against that state produces findings that are factually wrong by the time the work is committed — multiple "STE has no X" findings get invalidated because X already shipped on main while the local checkout slept. The "Branch base sentinel" CI check (`PR base is ancestor of HEAD`) catches the divergence but only AFTER the PR is opened, which means a force-push redo + a re-dispatched subagent run. Live failure: PR #470 first pass — audit ran against state 22 commits behind main, several "missing" findings (`permissions.deny`, SWV/CIC, silent-failure-hunter, identity-path rule, swv-advisory hook, `baseRef: fresh`) were already shipped via #460–#469. Required a full rebase + re-audit + force-push to fix. Operator pissed: "How the fuck can that happen when you've been merging the shit out of everything?"

**How to apply:**
- Any time you start substantive work on a non-main branch in a fresh session — even one minute of audit/research counts — run `git fetch origin main` first and check `git log --oneline HEAD..origin/main | head -5`. If anything is there, rebase or merge before dispatching the subagent.
- This is especially load-bearing for *audit / research / "what's the current state" tasks*, because they're the ones where stale findings are most expensive (the deliverable itself bakes the wrong state in).
- Sibling rule: [[never_touch_shared_main_checkout]] applies when 2 sessions are active; this rule applies always.
