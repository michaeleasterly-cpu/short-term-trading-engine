#!/usr/bin/env bash
# PreToolUse(Bash) — block `git checkout <branch|sha>` and `git checkout -b`.
# Operator standing rule (git-hygiene memo) + .claude/rules/heavy-lane.md:
#   use `git switch <branch>` / `git switch -c <new>` because `git switch`
#   refuses to silently detach HEAD. `git checkout -- <path>` (file restore)
#   stays allowed.
# Authoritative external: https://code.claude.com/docs/en/hooks-guide
set -euo pipefail

input="$(cat)"
cmd="$(echo "$input" | jq -r '.tool_input.command // empty')"

# Quick gate: does the command contain `git checkout`?
if ! echo "$cmd" | grep -qE '(^|[^a-zA-Z0-9_-])git[[:space:]]+checkout([[:space:]]|$)'; then
  exit 0
fi

# Extract the token immediately after `git checkout`.
rest="$(echo "$cmd" | sed -E 's/.*git[[:space:]]+checkout[[:space:]]+//; s/&&.*//; s/;.*//; s/\|.*//' | head -n1)"
first_token="$(echo "$rest" | awk '{print $1}')"
second_token="$(echo "$rest" | awk '{print $2}')"

# Allow file-restore forms:
#   git checkout -- <path>
#   git checkout HEAD -- <path>
#   git checkout HEAD~N -- <path>
if [ "$first_token" = "--" ]; then
  exit 0
fi
if echo "$first_token" | grep -qE '^HEAD(~[0-9]+|\^+)?$'; then
  if [ "$second_token" = "--" ]; then
    exit 0
  fi
fi

# Otherwise block.
echo "BLOCK: \`git checkout\` is forbidden by the operator's standing git-hygiene rule." >&2
echo "  Use:  git switch <branch>           # switch to existing branch" >&2
echo "        git switch -c <new-branch>     # create + switch (replaces \`git checkout -b\`)" >&2
echo "        git checkout -- <path>         # restore a file (still allowed)" >&2
echo "Reason: \`git switch\` refuses to silently detach HEAD; the checkout-detach incident is why." >&2
echo "See .claude/rules/heavy-lane.md + docs/DEV_PIPELINE_STANDARD.md." >&2
exit 2
