#!/usr/bin/env bash
# The single canonical git-maintenance wrapper for this clone.
#
# Why this exists: this repo recurrently accumulates stale
# remote-tracking refs (needing manual `git remote prune`), leaked
# local branches whose upstream is [gone], and orphaned worktree admin
# entries from hard-crashed daemon cycles. The core repo config
# (`fetch.prune true`, `gc.worktreePruneExpire 3.days.ago`) was set
# once via a one-off `git config --local` on this clone — NOT
# reproducible across clones. `--init` makes that config reproducible
# and reviewed; `--dry-run`/`--apply` are the ONLY sanctioned cleanup
# (no ad-hoc destructive git anywhere else — see CLAUDE.md Session
# Rules / docs/STYLE_GUIDE.md "Git hygiene").
#
# HARD SAFETY (enforced in --apply): never deletes `main`, never the
# currently-checked-out branch, never an unmerged branch, never a
# branch whose upstream is NOT [gone]; uses `git branch -d` (refuses
# unmerged) — never `-D`. No `rm`, no force.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/git_hygiene.sh [--init | --dry-run | --apply]

  --init      Idempotently ensure the durable hygiene config:
                git config --local fetch.prune true
                git config --local gc.worktreePruneExpire 3.days.ago
              Prints before/after. Safe to run repeatedly.
  --dry-run   (DEFAULT) Show what --apply WOULD do, change nothing:
                * git remote prune origin --dry-run
                * local branches that are [gone] upstream AND merged
                  into main (the deletable set)
                * git worktree prune --dry-run -v
  --apply     Perform: git fetch --prune; delete ONLY local branches
              that are BOTH [gone] upstream AND merged into main
              (never main, never the current branch, never unmerged,
              `git branch -d` only); git worktree prune -v.

No args / unknown args => --dry-run (safe default).
EOF
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

MODE="dry-run"
case "${1:-}" in
    --init) MODE="init" ;;
    --apply) MODE="apply" ;;
    --dry-run) MODE="dry-run" ;;
    -h | --help)
        usage
        exit 0
        ;;
    "" ) MODE="dry-run" ;;
    *)
        echo "Unknown argument: ${1} — defaulting to --dry-run" >&2
        MODE="dry-run"
        ;;
esac

# Compute the deletable set: local branches whose upstream is [gone]
# AND that are merged into main. Emits one branch name per line.
# Excludes `main` and the current branch by construction. List-safe
# (NUL-free branch names; this repo never uses pathological names).
deletable_branches() {
    local current
    current="$(git branch --show-current)"
    local gone=()
    local line ref track
    while IFS= read -r line; do
        # `git branch -vv` line for a [gone] upstream looks like:
        #   "  feature  abc1234 [origin/feature: gone] msg"
        ref="$(awk '{print $1}' <<<"${line#\* }")"
        [ -z "$ref" ] && continue
        [ "$ref" = "main" ] && continue
        [ "$ref" = "$current" ] && continue
        track="$(echo "$line" | grep -oE '\[[^]]*: gone\]' || true)"
        [ -z "$track" ] && continue
        gone+=("$ref")
    done < <(git branch -vv)

    local b
    for b in "${gone[@]:-}"; do
        [ -z "$b" ] && continue
        # merged into main only (HARD SAFETY: never delete unmerged)
        if git branch --merged main --format='%(refname:short)' | grep -qxF "$b"; then
            echo "$b"
        fi
    done
}

case "$MODE" in
    init)
        echo "── git_hygiene --init ──────────────────────────────────────────────"
        echo "BEFORE:"
        echo "  fetch.prune            = $(git config --local --get fetch.prune || echo '<unset>')"
        echo "  gc.worktreePruneExpire = $(git config --local --get gc.worktreePruneExpire || echo '<unset>')"
        git config --local fetch.prune true
        git config --local gc.worktreePruneExpire 3.days.ago
        echo "AFTER:"
        echo "  fetch.prune            = $(git config --local --get fetch.prune)"
        echo "  gc.worktreePruneExpire = $(git config --local --get gc.worktreePruneExpire)"
        echo "Done (idempotent — safe to re-run)."
        ;;

    dry-run)
        echo "── git_hygiene --dry-run (NOTHING WILL BE CHANGED) ─────────────────"
        echo "▶ git remote prune origin --dry-run"
        git remote prune origin --dry-run || true
        echo ""
        echo "▶ deletable local branches ([gone] upstream AND merged into main):"
        _dels=()
        while IFS= read -r b; do
            [ -n "$b" ] && _dels+=("$b")
        done < <(deletable_branches)
        if [ "${#_dels[@]}" -eq 0 ]; then
            echo "  (none)"
        else
            for b in "${_dels[@]}"; do
                echo "  would delete: $b"
            done
        fi
        echo ""
        echo "▶ git worktree prune --dry-run -v"
        git worktree prune --dry-run -v || true
        echo "Dry-run complete. No changes made."
        ;;

    apply)
        echo "── git_hygiene --apply ─────────────────────────────────────────────"
        echo "▶ git fetch --prune"
        git fetch --prune
        echo ""
        echo "▶ deleting [gone]+merged local branches"
        _dels=()
        while IFS= read -r b; do
            [ -n "$b" ] && _dels+=("$b")
        done < <(deletable_branches)
        if [ "${#_dels[@]}" -eq 0 ]; then
            echo "  nothing to delete"
        else
            for b in "${_dels[@]}"; do
                echo "  git branch -d $b"
                git branch -d "$b"
            done
        fi
        echo ""
        echo "▶ git worktree prune -v"
        git worktree prune -v
        echo "Apply complete."
        ;;
esac
