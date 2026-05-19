#!/usr/bin/env bash
# SessionStart — one-line summary of the active extension surface so the
# operator SEES the lane configuration at session open.
# Authoritative external: https://code.claude.com/docs/en/hooks-guide
# Project SoT: docs/DEV_PIPELINE_STANDARD.md §0.
set -e

# Count from repo root; tolerate missing dirs (e.g. on a worktree).
rules_count="$(find .claude/rules -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
skills_count="$(find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md 2>/dev/null | wc -l | tr -d ' ')"
agents_count="$(find .claude/agents -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
hooks_count="$(find .claude/hooks -maxdepth 1 -type f -name '*.sh' 2>/dev/null | wc -l | tr -d ' ')"

cat <<EOF
Claude-Code extension surface: ${rules_count} path-scoped rules + ${skills_count} skills + ${agents_count} subagent profiles + ${hooks_count} hooks loaded.
Heavy-lane triggers (full §1 pipeline): tpcore/risk/, tpcore/selfheal/, tpcore/auditheal/, tpcore/quality/validation/, ops/engine_service.py, ops/engine_sdlc/, ops/llm_*triage.py, platform/migrations/, new engine (5-plug), new data adapter, tpcore/engine_profile.py, tpcore/providers.py.
Default = Anthropic Explore → Plan → Implement → Commit (one review). Fast = single-file/doc-only one-sentence diff. See docs/DEV_PIPELINE_STANDARD.md §0.
EOF
