---
name: always-use-wrapper-scripts
description: "Operator's terminal wraps long pasted commands and zsh splits args onto new lines. Any command with ≥1 flag goes in scripts/ first."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 6626da25-0752-45ca-99c0-beeb2f8af7bb
---

**Rule:** If a command needs more than one flag (or an env prefix plus flags), put it in `scripts/<name>.sh` before handing it to the operator. Tell them to run only `scripts/<name>.sh`.

**Why:** The operator's terminal (zsh on macOS) wraps long pasted lines, and depending on where the wrap lands, zsh interprets a continuation flag (e.g., `--force-rebalance`) as a separate command. The user has hit this three times:

1. The original 200-trial Sigma search hit `dquote>` because of paste-wrap.
2. The Reversion + Vector follow-up needed a script (`run_rev_vec.sh`) for the same reason.
3. The Momentum paper-trading kickoff failed when `--force-rebalance` got split off and zsh tried to execute it as a command: `zsh: command not found: --force-rebalance`. The user's response: "your shit never runs."

**How to apply:** Before suggesting any of the following to the operator, put it in a wrapper script first:

- Any `DATABASE_URL=... .venv/bin/python ...` invocation with ≥1 flag
- Any `nohup ... > log 2>&1 &` line
- Any heredoc-based file creation
- Any command longer than ~80 visible characters

**Anti-pattern:** Sending a fenced ``` block with `\` line continuations and expecting paste to survive. It doesn't.

**Good pattern:** Wrapper script in `scripts/`, chmod +x, committed. Operator runs `scripts/foo.sh` — one short token. The wrapper handles `source .env`, venv path, all flags.

The repo has many examples: `scripts/run_data_operations.sh`, `scripts/run_full_backfill.sh`, `scripts/run_all_engines.sh`, `scripts/git_hygiene.sh`, `scripts/run_audit_data_pipeline.sh`.
