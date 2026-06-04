---
name: feedback-pytest-hook-block-run-whole-suite-dont-retry
description: "When .claude/hooks/block-pytest-subset-when-ops.sh blocks — run the whole suite. Don't retry the same subset selector 3x with env-var prefixes — the hook reads CLAUDE_ALLOW_SUBSET from the PARENT shell env, not from VAR=1 cmd or env VAR=1 cmd prefixes."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

When `.claude/hooks/block-pytest-subset-when-ops.sh` blocks a Bash call,
the right action is to **run the whole suite** (`.venv/bin/python3 -m
pytest -p no:xdist -q`). Do NOT retry the same subset selector with
env-var workarounds — they don't work and waste the operator's time.

**Why:** The hook reads `CLAUDE_ALLOW_SUBSET` from the parent shell's
exported env (the env in which Claude Code itself was launched), not
from the command line. `VAR=1 cmd` and `env VAR=1 cmd` prefix the
COMMAND's env but the hook's env-read happens BEFORE the command runs.
The override is `export CLAUDE_ALLOW_SUBSET=1` in a SEPARATE Bash call
first (and even then, the next session loses it), but the safer move
is to honor the hook's intent — whole suite is the gate, subset is
unrepresentative when ops/*.py ↔ scripts/ops.py have uncommitted diff.

**How to apply:** First Bash error from this hook → next call is the
whole-suite command, not a retry of the subset. The operator caught
me retrying 3x on 2026-05-24 ("you should start learning from these
errors so you don't have to relearn it all the time"). One block = one
shift to whole-suite. See [[feedback_ops_package_shadow_full_suite_gate]]
for the underlying ops-shadow rationale and
[[feedback_no_shortcuts_100_pct]] for the broader no-corner-cutting rule.
