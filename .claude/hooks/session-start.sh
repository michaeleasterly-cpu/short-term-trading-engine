#!/usr/bin/env bash
# SessionStart — one-line summary of the active extension surface + the
# canonical-work-tracking directive + an auto-extracted list of open
# TODO.md H2 sections so the operator NEVER has to remind Claude which
# work source to read.
# Authoritative external: https://code.claude.com/docs/en/hooks-guide
# Project SoT: docs/DEV_PIPELINE_STANDARD.md §0; TODO.md is the canonical
# work-tracking surface (git-tracked, survives memory audits).
set -e

# Count from repo root; tolerate missing dirs (e.g. on a worktree).
rules_count="$(find .claude/rules -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
skills_count="$(find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md 2>/dev/null | wc -l | tr -d ' ')"
agents_count="$(find .claude/agents -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
hooks_count="$(find .claude/hooks -maxdepth 1 -type f -name '*.sh' 2>/dev/null | wc -l | tr -d ' ')"

# Extract open H2 sections from TODO.md (root) — those lacking ✅ / ⚰️ /
# DONE / ARCHIVED / CLOSED markers. Capped at 20 lines so this stays
# well under the SessionStart context budget (per
# https://code.claude.com/docs/en/memory — first 200 lines / 25KB).
todo_open=""
if [ -f TODO.md ]; then
  todo_open="$(grep -nE '^## ' TODO.md | grep -viE '✅|⚰️|DONE|ARCHIVED|CLOSED' | head -20)"
fi

cat <<EOF
Claude-Code extension surface: ${rules_count} path-scoped rules + ${skills_count} skills + ${agents_count} subagent profiles + ${hooks_count} hooks loaded.
Heavy-lane triggers (full §1 pipeline): tpcore/risk/, tpcore/selfheal/, tpcore/auditheal/, tpcore/quality/validation/, ops/engine_service.py, ops/engine_sdlc/, ops/llm_*triage.py, platform/migrations/, new engine (5-plug), new data adapter, tpcore/engine_profile.py, tpcore/providers.py.
Default = Anthropic Explore → Plan → Implement → Commit (one review). Fast = single-file/doc-only one-sentence diff. See docs/DEV_PIPELINE_STANDARD.md §0.

CANONICAL WORK-TRACKING: TODO.md is the source of truth for "what's left to do" (git-tracked; survives memory audits). Memory entries are rationale/constraints, NOT task state. ALWAYS consult TODO.md before any next-work decision; never drive next-work choices from memory alone.

TODO.md open H2 sections (auto-extracted; cap 20; ✅ / ⚰️ / DONE / ARCHIVED / CLOSED filtered out):
${todo_open}
EOF
