#!/usr/bin/env bash
# One-way sync: live Claude auto-memory dir → in-repo snapshot at docs/memory/.
#
# The canonical location Claude Code reads from is
#   ~/.claude/projects/-Users-michael-short-term-trading-engine/memory/
# That is machine-local and NOT in git. This script copies it into
# docs/memory/ in the repo so memory changes are tracked by git and
# recoverable from history if a memory file is lost or corrupted.
#
# Run manually after writing/editing memories, then commit + push:
#   bash scripts/sync_claude_memory.sh && git add docs/memory && git commit -m "..."
#
# Live → repo is one-way. To restore a memory from the repo back into
# the live dir (e.g. on a fresh machine), copy in the opposite direction:
#   cp docs/memory/<file>.md ~/.claude/projects/.../memory/<file>.md
set -euo pipefail

LIVE_DIR="$HOME/.claude/projects/-Users-michael-short-term-trading-engine/memory"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)/docs/memory"

if [[ ! -d "$LIVE_DIR" ]]; then
  echo "ERROR: live memory directory not found: $LIVE_DIR" >&2
  exit 1
fi

mkdir -p "$REPO_DIR"

# Remove repo files that no longer exist in live (memories deleted via /forget)
for f in "$REPO_DIR"/*.md; do
  [[ -e "$f" ]] || continue
  base="$(basename "$f")"
  if [[ ! -e "$LIVE_DIR/$base" ]]; then
    echo "  - $base (removed from live; deleting from repo)"
    rm "$f"
  fi
done

# Copy every live .md into the repo snapshot
for f in "$LIVE_DIR"/*.md; do
  [[ -e "$f" ]] || continue
  cp "$f" "$REPO_DIR/"
done

count=$(find "$REPO_DIR" -name '*.md' -type f | wc -l | tr -d ' ')
echo "Synced $count memory file(s) from $LIVE_DIR → $REPO_DIR"
echo "Next: review with 'git diff docs/memory/' and commit if changed."
