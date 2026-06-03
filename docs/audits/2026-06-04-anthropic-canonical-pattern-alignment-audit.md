# Anthropic canonical pattern alignment audit (2026-06-04)

> **Type:** docs-only audit. **No code, no `.claude/**`, no workflow, no hook, no agent, no skill, no DB, no migration changes are produced by this PR.** This is the second-pass redo of an audit that was researched against state ~22 commits behind main and landed stale; this pass fixes the staleness against current main (post PR #469).
>
> **Authority order:**
>
> 1. Official Anthropic Claude Code documentation at `https://code.claude.com/docs/en/*`.
> 2. `anthropics/claude-code` (the open-source CLI repo; ships 14+ official plugins under `plugins/`).
> 3. `anthropics/claude-code-action` (the canonical PR-review GitHub Action).
> 4. `anthropics/anthropic-cookbook` — pattern-level guidance only (the cookbook's `skills/` are mostly notebooks, not `SKILL.md` format).
> 5. This repo's `.claude/**`, `CLAUDE.md`, sentinel tests, and `docs/audits/**`.
>
> **What changed since the prior (dropped) audit was authored:** PRs #460–#469 landed (subagent branch-base discipline, commit/clean-gone vendoring, silent-failure-hunter vendoring, permissions.deny block, SWV+CIC gates, security-guidance pattern scan, identity-path rule, SWV advisory hook, type-design merged into CIC). Anything the prior audit said about STE "missing" a permissions.deny, a silent-failure-hunter, a commit-commands flow, or a SWV/CIC gate is **invalidated by main**.

## 0. Sources fetched + receipts

| Surface | Form | Path / URL |
| :--- | :--- | :--- |
| Skills doc | WebFetch (51.6 KB, persisted) | `https://code.claude.com/docs/en/skills` |
| Sub-agents doc | WebFetch (63.5 KB, persisted) | `https://code.claude.com/docs/en/sub-agents` |
| Memory doc | WebFetch | `https://code.claude.com/docs/en/memory` |
| Permissions doc | WebFetch | `https://code.claude.com/docs/en/permissions` |
| `anthropics/claude-code` tree | `gh api repos/anthropics/claude-code/git/trees/main?recursive=1` | `plugins/`, `examples/`, `.claude/` |
| `examples/settings/{settings-strict.json, settings-lax.json, README.md}` | `gh api … --jq .content \| base64 -d` | `https://github.com/anthropics/claude-code/tree/main/examples/settings` |
| `examples/hooks/bash_command_validator_example.py` | same | `https://github.com/anthropics/claude-code/blob/main/examples/hooks/bash_command_validator_example.py` |
| `plugins/security-guidance/{README.md, hooks/hooks.json}` | same | `…/plugins/security-guidance/` |
| `plugins/pr-review-toolkit/agents/{silent-failure-hunter, code-reviewer, type-design-analyzer, …}` | same | `…/plugins/pr-review-toolkit/agents/` |
| `plugins/feature-dev/{agents/code-architect.md, commands/feature-dev.md, .claude-plugin/plugin.json}` | same | `…/plugins/feature-dev/` |
| `plugins/code-review/{commands/code-review.md, .claude-plugin/plugin.json}` | same | `…/plugins/code-review/` |
| `plugins/commit-commands/commands/commit.md` | same | `…/plugins/commit-commands/commands/commit.md` |
| `plugins/hookify/examples/sensitive-files-warning.local.md` | same | `…/plugins/hookify/examples/` |
| `anthropics/claude-code-action` tree | same | `https://github.com/anthropics/claude-code-action` |
| `claude-code-action/.claude/{agents/code-quality-reviewer.md, commands/review-pr.md, settings.json}` | same | `…/.claude/` |

**Could not read.** `repos/anthropics/claude-code/contents/CLAUDE.md` returned an empty payload — the upstream repo does not check in a top-level `CLAUDE.md` (the `.claude/commands/` directory is its entire dog-fooded surface). Noted in §4.

## 1. Alignments

For each row: STE side cited by repo path + line span where useful; Anthropic side cited by URL or repo path.

### 1.1 CLAUDE.md as slim project memory + `.claude/rules/` path-scoped split

- **STE:** `CLAUDE.md` (147 lines, well under the documented 200-line target) is identity + one-line architecture + universal invariants + pointers; `.claude/rules/` carries 14 path-scoped files (each ≤ 96 lines, all with `paths:` frontmatter).
- **Anthropic:** [`code.claude.com/docs/en/memory`](https://code.claude.com/docs/en/memory) §"Write effective instructions" recommends "target under 200 lines per CLAUDE.md file" and §"Organize rules with `.claude/rules/`" documents the same path-scoped split with `paths:` frontmatter. STE matches the documented shape verbatim.

### 1.2 Skill directory shape + `disable-model-invocation` for slash-only

- **STE:** 16 `.claude/skills/<name>/SKILL.md` files. Slash-only skills (`/commit`, `/commit-push-pr`, `/clean-gone`, `/ecr`, `/dfcr`, `/run-data-ops`, `/audit-data-pipeline`, `/weekly-digest`, `/defect-register`, `/lab-target-run`) carry `disable-model-invocation: true`. Model-invocable skills (`/system-wide-verification`, `/change-impact-classification`, `/security-review`, `/engine-readiness`, `/adapter-readiness`) omit it.
- **Anthropic:** [`code.claude.com/docs/en/skills`](https://code.claude.com/docs/en/skills) §"Control who invokes a skill" documents the `disable-model-invocation` boolean and the directory convention (command name = directory name). STE matches both verbatim.

### 1.3 Sub-agent profile shape (tools allowlist + model + color)

- **STE:** all 7 agents declare `name` + `description` + `tools` (allowlist) + `model` + `color`. Example: `.claude/agents/silent-failure-hunter.md` carries `tools: Bash, Read, Grep, Glob` + `model: opus` + `color: yellow`. STE adds `skills: [engine-readiness, adapter-readiness]` (auto-skill loading on dispatch) which is a documented frontmatter field per the sub-agents doc.
- **Anthropic:** [`code.claude.com/docs/en/sub-agents`](https://code.claude.com/docs/en/sub-agents) documents the same frontmatter set. Compare `anthropics/claude-code/plugins/pr-review-toolkit/agents/silent-failure-hunter.md` (frontmatter `tools` omitted → inherits all tools; `model: inherit; color: yellow`). STE's stricter `tools:` allowlist is more restrictive than Anthropic's published version of the same agent — STE override is intentional (read-only by construction).

### 1.4 PreToolUse exit-2 blocking + PostToolUse advisory hooks

- **STE:** `.claude/hooks/block-git-checkout.sh`, `block-pytest-subset-when-ops.sh`, `gate-ecr-dfcr-edits.sh` use exit 2 to block via PreToolUse. `.claude/hooks/risk-path-reminder.sh` + `.claude/hooks/security_pattern_scan.sh` are PostToolUse advisory (exit 0).
- **Anthropic:** [`code.claude.com/docs/en/hooks`](https://code.claude.com/docs/en/hooks) §"Blocking Capability by Event" — PreToolUse exit 2 blocks; PostToolUse cannot block retroactively. Canonical example: `anthropics/claude-code/examples/hooks/bash_command_validator_example.py` uses identical exit-2 pattern (`# Exit code 2 blocks tool call and shows stderr to Claude`). STE matches verbatim.

### 1.5 `permissions.deny` block in `settings.json`

- **STE (NEW 2026-06-04, PR #463):** `.claude/settings.json` carries a `permissions.deny` block with 24 rules: `Read(./.env)`, `Read(./.env.*)`, `Read(~/.ssh/**)`, `Read(~/.aws/**)`, `Read(~/.gnupg/**)`, `Read(~/.netrc)`, `Read(~/.config/gh/**)`, `Bash(curl *)`, `Bash(wget *)`, `Bash(rm -rf /)`, `Bash(rm -rf ~)`, `Bash(dd if=*)`, `Bash(chmod -R 777 *)`, etc.
- **Anthropic:** [`code.claude.com/docs/en/permissions`](https://code.claude.com/docs/en/permissions) §"Permission rule syntax" documents the `deny → ask → allow` evaluation order and the exact rule shapes STE uses (gitignore-style path semantics including `//absolute`, `~/home`, project-relative `./`). `examples/settings/settings-strict.json` ships `deny: ["WebSearch", "WebFetch"]` as a starting point; STE's deny list extends that pattern to secrets + destructive ops. STE matches the documented mechanism.

### 1.6 `worktree.bgIsolation` + `worktree.baseRef`

- **STE:** `.claude/settings.json` sets `worktree.baseRef: "fresh"` (flipped from `head` in PR #460 per controls-audit §13 #4) and `worktree.bgIsolation: "worktree"`.
- **Anthropic:** [`code.claude.com/docs/en/worktrees`](https://code.claude.com/docs/en/worktrees) + [`code.claude.com/docs/en/sub-agents`](https://code.claude.com/docs/en/sub-agents) §"Configure subagents → isolation" document both fields and the `fresh` vs `head` semantics. STE matches; the flip-to-fresh is the intentional alignment with Anthropic's documented default.

### 1.7 `claude-code-action@v1` workflow with `paths:` filter + `--allowedTools` was the pattern; STE retired it

- **STE (HISTORY):** `claude-review-heavy-lane.yml` carried `paths: <heavy_lane ∪ claude_system>` + `--allowedTools` restriction. **Retired in PR #458** (`3bed0f8 chore(claude): retire paid heavy-lane Claude review workflow`). The path registry's note documents this retirement (`.claude/path_registry.yaml` line 14–17).
- **Anthropic:** `anthropics/claude-code-action/examples/pr-review-filtered-paths.yml` ships the canonical path-filtered shape. STE used it correctly while it ran; the retirement was a cost-control decision (operator-flagged credit burn on docs-only PRs — controls-audit §5 row "Docs-only PR Claude-review carve-out").

### 1.8 Auto-memory size discipline (`MEMORY.md` ≤ 200 lines / 25 KB)

- **STE:** `tests/test_memory_index_size.py` caps `MEMORY.md` at 24,400 bytes; `docs/MEMSTORE_HANDOFF.md` documents the four-tier boundary.
- **Anthropic:** memory doc §"Auto memory → How it works": *"The first 200 lines of `MEMORY.md`, or the first 25 KB, whichever comes first, are loaded at the start of every conversation."* STE matches verbatim.

### 1.9 SessionStart context-injection hook

- **STE:** `.claude/hooks/session-start.sh` extracts open `TODO.md` H2 sections into context (≤ 20 lines).
- **Anthropic:** memory doc §"Troubleshoot memory issues" + hooks doc both describe SessionStart context injection; `anthropics/claude-code/plugins/explanatory-output-style/hooks-handlers/session-start.sh` is the canonical analogue (the published plugins use it for output-style switching; STE uses it for TODO surfacing). Mechanism matches.

### 1.10 Commit + clean-gone slash-only skills

- **STE (NEW 2026-06-04, PR #461):** `/commit`, `/commit-push-pr`, `/clean-gone` are vendored from `anthropics/claude-code/plugins/commit-commands/`. STE's versions carry `disable-model-invocation: true` + `allowed-tools: Bash(git status:*), Bash(git diff:*), …` matching Anthropic's frontmatter shape, plus STE-specific additions (HEREDOC formatting, Co-Authored-By footer, the worktree-aware safety invariants in `/clean-gone`).
- **Anthropic:** `plugins/commit-commands/commands/commit.md` ships the same allowed-tools shape (`Bash(git add:*), Bash(git status:*), Bash(git commit:*)`). STE's frontmatter matches the canonical shape; the body is adapted (acknowledged in each skill's preamble: "Vendored 2026-06-04 from `anthropics/claude-code` …").

### 1.11 Silent-failure-hunter sub-agent vendored + adapted

- **STE (NEW 2026-06-04, PR #462):** `.claude/agents/silent-failure-hunter.md` is vendored from `anthropics/claude-code/plugins/pr-review-toolkit/agents/silent-failure-hunter.md` and adapted to STE's silent-skip catalogue (HealSpec `healable=False`, `DATA_OPERATIONS_COMPLETE` bypass, hardcoded `ExitReason` literals, PricesRepo `as_of` bypass, banned-data-source fallbacks). The mission + core-principles + output-format structure are preserved verbatim.
- **Anthropic:** `plugins/pr-review-toolkit/agents/silent-failure-hunter.md` is the source. STE's vendor acknowledgment is explicit ("This agent's prompt structure (mission → core principles → review process → output format) is adapted from Anthropic's `silent-failure-hunter`. The principle set is preserved; the catalogue + the project-specific patterns are STE-original.") This is the documented vendor-then-adapt pattern.

### 1.12 Security-guidance Layer 1 (pattern rules) vendored

- **STE (NEW 2026-06-04, PR #466):** `.claude/hooks/security_pattern_scan.{sh,py}` + `.claude/hooks/security_patterns_vendored.py` (~17 KB of regex patterns) + `.claude/hooks/security_patterns_ste.py` vendored from `anthropics/claude-code/plugins/security-guidance/`. Wired as a `PostToolUse(Edit|Write|MultiEdit|NotebookEdit)` advisory hook with `STE_SECURITY_PATTERN_SCAN_DISABLE=1` kill-switch.
- **Anthropic:** `plugins/security-guidance/hooks/hooks.json` wires the same hook events for the same matchers (`Edit|Write|MultiEdit|NotebookEdit`). STE deliberately skipped Layer 2 (Stop-hook LLM diff review with `asyncRewake`) and Layer 3 (agentic commit/push review) per vendor-audit §2.4 + operator decision §9 #1 (Layer 1 only — defer until cost is measured). The pattern-data file (`security_patterns_vendored.py`) is structurally the upstream `patterns.py` content.

### 1.13 Type-design-analyzer merged into CIC skill

- **STE (NEW 2026-06-04, PR #468):** `.claude/skills/change-impact-classification/SKILL.md` lines 126–209 fold the type-design 5-dimension framework from `anthropics/claude-code/plugins/pr-review-toolkit/agents/type-design-analyzer.md` into the CIC skill rather than vendor it as a standalone agent. STE-specific anti-patterns (`from __future__ import annotations` missing, untyped `Callable`, `dict[str, Any]` return type, missing `as_of` field, missing `classification_id`) are added.
- **Anthropic:** type-design-analyzer is published as a separate pr-review-toolkit agent. STE's merge decision is documented in vendor-audit §3 ("merge with the morning audit's CIC gate") + §9 #3 (operator approval).

### 1.14 Path-scoped rule autoload — exact `paths:` frontmatter shape

- **STE:** all 14 rules in `.claude/rules/` use `paths:` lists; `.claude/rules/identity-path.md` has the largest scope (13 globs covering ingestion, validators, auditheal, selfheal, data, migrations, scripts/ops.py, and the 7 engine lanes).
- **Anthropic:** memory doc §"Path-specific rules" documents the exact YAML shape STE uses including the brace-expansion example STE could borrow but hasn't needed. STE matches verbatim.

## 2. Divergences (classified)

Legend:
- **STE_OVERRIDE** — STE deviates with a documented rationale in repo (CLAUDE.md / docs / audits / git log / PR description).
- **UNKNOWING_DRIFT** — STE deviates without a documented rationale; candidate follow-up.
- **STE_ORIGINAL** — STE invented this; Anthropic has no equivalent. Not a drift; not a follow-up.

### 2.1 Permission `defaultMode` unset → **UNKNOWING_DRIFT** (low risk)

- **STE:** `.claude/settings.json` does not set `permissions.defaultMode`.
- **Anthropic:** [`code.claude.com/docs/en/permissions`](https://code.claude.com/docs/en/permissions) §"Permission modes" documents `default | acceptEdits | plan | auto | dontAsk | bypassPermissions`; setting one is recommended for organizational policy.
- **Rationale search:** controls-audit §13 #6 explicitly flagged this as an open operator decision ("Set an explicit default (`default`, `plan`, `acceptEdits`, `dontAsk`)? Or leave unset?"). Operator deferred the decision; no rationale was committed.
- **Why low risk:** the CLI default behaves correctly for single-operator paper-only use; the operator has not reported a `--dangerously-skip-permissions` incident. Worth resolving but not urgent.

### 2.2 `disableBypassPermissionsMode` unset → **UNKNOWING_DRIFT** (low risk)

- **STE:** the field is not present in `.claude/settings.json`.
- **Anthropic:** `examples/settings/settings-strict.json` ships `disableBypassPermissionsMode: "disable"` as the first key; `examples/settings/settings-lax.json` also sets it.
- **Rationale search:** not mentioned in either audit; not in CLAUDE.md.
- **Why low risk:** STE is single-operator personal-use; the protection guards against an operator passing `--dangerously-skip-permissions` accidentally. Belongs in §3 as a small, free pull-in.

### 2.3 `.claude/path_registry.yaml` + `scripts/check_manifests.py` → **STE_ORIGINAL**

- **STE:** the canonical SoT for `heavy_lane ∪ claude_system` paths (PR #411, H0 hardening). `scripts/check_manifests.py` validates drift across `.claude/rules/heavy-lane.md` frontmatter, `.github/workflows/**` `paths:` filters, `docs/DEV_PIPELINE_STANDARD.md` §0, the PR template, and `session-start.sh`.
- **Anthropic:** no equivalent in the published canon. The plugin marketplace + the `.claude-plugin/plugin.json` manifest cover plugin-level metadata but there is no Anthropic-published cross-file path-list SoT.
- **Not a follow-up.** This is STE's structural answer to "many surfaces, one path list" and the morning audit §6.1 documents the trade-off.

### 2.4 Discovery-first rule + SWV + CIC gates → **STE_ORIGINAL** (with hookify alignment in §3)

- **STE:** `.claude/rules/discovery-first.md` + `.claude/skills/system-wide-verification/SKILL.md` + `.claude/skills/change-impact-classification/SKILL.md` + `.claude/hooks/swv-advisory.sh` form a 4-surface gate covering 10 trace points (writers / readers / authority / existing controls / tests / workflows / config / adjacent callers / blast radius / rollback) + 16-class change classification.
- **Anthropic:** the canonical-pattern-equivalent is `permissions.defaultMode: plan` (read-only exploration mode) + the built-in `Plan` subagent. Neither encodes a 10-point trace or a 16-class taxonomy.
- **Rationale:** `docs/audits/2026-06-03-claude-code-workflow-controls.md` §8 + §9 design these as a response to the 2026-06-02 identity-substrate failure (`docs/audits/2026-06-03-identity-substrate-data-flow.md`). The audit explicitly documents that no Anthropic canonical mechanism encodes this discipline.
- **Not a follow-up.** This is STE's load-bearing failure-prevention layer.

### 2.5 Identity-path rule → **STE_ORIGINAL**

- **STE:** `.claude/rules/identity-path.md` encodes the SCD-2 `ticker + date → classification_id → CIK` invariant + the SEC-first authority order + the engine-reader `as_of` discipline. Auto-loads on 13 path globs covering data writers + 7 engine readers.
- **Anthropic:** no equivalent (this is domain-specific to STE's trading-platform identity model).
- **Not a follow-up.**

### 2.6 ECR/DFCR planner-path gating via `gate-ecr-dfcr-edits.sh` → **STE_ORIGINAL** (within canonical mechanism)

- **STE:** PreToolUse hook blocks `Edit|Write|MultiEdit` against `tpcore/engine_profile.py` + `tpcore/providers.py` unless `CLAUDE_ECR_RUN=1` / `CLAUDE_DFCR_RUN=1` is set. Documented in `.claude/rules/engine-roster.md` + `data-feed-roster.md`.
- **Anthropic:** the hooks-guide §"Block edits to protected files" example shows the same mechanism (PreToolUse exit-2 block on a file path); STE's use is a faithful instance of the documented pattern applied to STE-domain SoT files. Hook mechanism is canonical; the path + override-flag semantics are STE-original.

### 2.7 Sentinel-test surface (`tests/test_claude_*_present.py`) → **STE_ORIGINAL** (within memory mechanism)

- **STE:** `tests/test_claude_rules_present.py`, `test_claude_skills_present.py`, `test_claude_agents_present.py`, `test_claude_hooks_present.py`, `test_claude_review_workflow_present.py`, `test_claude_surface_contract.py`, `test_memory_boundary_present.py`, `test_memory_index_size.py`, `test_path_registry_present.py`, `test_permissions_deny_present.py` red CI if the surface drifts.
- **Anthropic:** the published canon ships sentinel-test patterns for plugin authors (`plugins/plugin-dev/skills/hook-development/scripts/{hook-linter.sh, validate-hook-schema.sh}`) but no project-level pinning of `.claude/**` artifact presence.
- **Not a follow-up.** STE's sentinel surface is the structural complement to "memory is not enforced" — the audit-board mechanism is STE-original but uses standard pytest.

### 2.8 No top-level `.claude/CLAUDE.md` (uses `./CLAUDE.md` only) → **STE_OVERRIDE** (canonical, both supported)

- **STE:** project memory is at `/Users/michael/short-term-trading-engine/CLAUDE.md`. No `.claude/CLAUDE.md` exists.
- **Anthropic:** memory doc §"Choose where to put CLAUDE.md files" says both `./CLAUDE.md` and `./.claude/CLAUDE.md` are project-instructions locations; either is canonical. STE's pick is documented in CLAUDE.md preamble ("This file is the slim project memory…").
- **Not a follow-up.** Both Anthropic-documented locations are honored; STE picked one.

### 2.9 Bundled-skill `references/` + `scripts/` subdirs not used → **STE_OVERRIDE** (intentional simplicity)

- **STE:** every `SKILL.md` is single-file. Skills don't bundle helper scripts or reference docs.
- **Anthropic:** the skills doc §"Progressive disclosure" + `plugins/plugin-dev/skills/hook-development/{examples/, references/, scripts/}` show the canonical bundle shape (`SKILL.md` + helper assets loaded on demand).
- **Rationale:** STE's skills are short enough (≤ 209 lines for the largest, `change-impact-classification`) that progressive disclosure is unneeded. The SoT lives in `docs/audits/**` + `docs/superpowers/checklists/**`; the skill's job is to point at it, not duplicate it. No documented "we considered and rejected" note for this, but the small skill sizes are evidence the simpler shape is intentional.
- **Not a follow-up unless** a skill grows past ~250 lines.

### 2.10 No `Stop` hook → **STE_OVERRIDE** (cost-driven)

- **STE:** the hook surface uses `PreToolUse`, `PostToolUse`, `SessionStart`, `UserPromptSubmit` only.
- **Anthropic:** `Stop` is a documented hook event; `plugins/security-guidance/hooks/hooks.json` uses it with `asyncRewake` for the LLM diff review pattern.
- **Rationale:** vendor-audit §2.4 + operator decision §9 #1 explicitly defer Stop-hook Layer 2 + 3 ("enable Layer 1 only, defer Layers 2 + 3 until the operator knows the real per-day cost"). PR #458 retired the paid heavy-lane review workflow for the same cost reason. The override is documented.
- **Not a follow-up.** Cost-driven.

### 2.11 `feature-dev` 7-phase workflow not vendored → **STE_OVERRIDE**

- **STE:** the heavy-lane 13-step pipeline (`docs/DEV_PIPELINE_STANDARD.md` §1) covers the same surface as `/feature-dev` but adds two operator-approval gates (spec PR + plan PR) before any implementation.
- **Anthropic:** `plugins/feature-dev/commands/feature-dev.md` ships the 7-phase Discovery → Exploration → Clarification → Architecture → Confirmation → Implementation → Review flow with a single user-confirmation gate.
- **Rationale:** vendor-audit §4.4 documents the divergence ("STE's discipline optimizes for `paper-only trading platform that cannot afford silent regressions`; vendor optimizes for `single developer with quick approval`").
- **Not a follow-up.** Documented intentional override.

### 2.12 Path-registry-based workflow filter (vs Anthropic `paths-ignore:`) → **STE_OVERRIDE** (now mostly moot)

- **STE:** the retired heavy-lane review workflow used a positive `paths:` filter derived from `path_registry.yaml`. The vendor canon (`pr-review-filtered-paths.yml`) uses the same positive filter shape, so STE matched.
- **Anthropic:** `paths-ignore:` is the canonical lever for docs-only carve-outs; the controls-audit §5 row "Docs-only PR Claude-review carve-out" flags STE as not having used it (drift at the time).
- **Resolved:** PR #458 retired the paid workflow, so the docs-only carve-out is moot. The path registry remains, consumed only by rules + docs + sentinel tests — controls-audit §3.1 documents this.

### 2.13 `pr-review-toolkit` `code-reviewer` + `comment-analyzer` + `code-simplifier` + `pr-test-analyzer` not vendored → **STE_OVERRIDE**

- **STE:** vendored only `silent-failure-hunter` (PR #462) and merged `type-design-analyzer` into CIC (PR #468). The other four pr-review-toolkit agents are not present.
- **Anthropic:** `anthropics/claude-code/plugins/pr-review-toolkit/agents/{code-reviewer, code-simplifier, comment-analyzer, pr-test-analyzer}.md` are all published.
- **Rationale:** vendor-audit §3.4 documents the four skips: `code-reviewer` (STE's `code-quality-reviewer` is more specific), `code-simplifier` (out of scope — "make it correct" > "make it concise"), `comment-analyzer` (covered by `tests-and-ci.md`), `pr-test-analyzer` (covered by whole-suite + order-flip gate).
- **Not a follow-up.** Documented operator decision.

### 2.14 `hookify` plugin not vendored → **STE_OVERRIDE**

- **STE:** `.claude/hooks/*.sh` are 6 hand-coded bash scripts (each tied to a specific STE failure incident).
- **Anthropic:** `plugins/hookify/` ships a Python rule engine + markdown rule files + a `/hookify "<description>"` self-generation command.
- **Rationale:** vendor-audit §5.4 documents the "stay diverged for existing 5 hooks" decision — STE's hooks read JSON input, parse `file_path`, and emit structured deny messages with override semantics the regex engine can't replicate. The audit recommends *adopting hookify for future SWV/CIC advisory hooks*; that has not been actioned. Open follow-up (operator decision §9 #5 of vendor-audit).
- **Why kept as STE_OVERRIDE not UNKNOWING_DRIFT:** explicitly deferred, not overlooked.

## 3. Canonical surface worth pulling in

Only items that close a real STE gap. Each: what it is, why STE benefits, one-line "would slot into X" placement.

### 3.1 `permissions.disableBypassPermissionsMode: "disable"` in `.claude/settings.json`

- **What:** one-line entry in `.claude/settings.json` that prevents `--dangerously-skip-permissions` and the `bypassPermissions` permission mode from being used.
- **Why STE benefits:** Anthropic's `examples/settings/settings-strict.json` ships this as the first key. It is the documented lock against a single accidental `--dangerously-skip-permissions` invocation. STE already chose strict semantics elsewhere (deny block, PreToolUse hooks); this is the cheapest matching-discipline addition.
- **Would slot into:** `.claude/settings.json` next to the existing `permissions.deny` block. One added line; sentinel test addition trivial.

### 3.2 `permissions.defaultMode: "default"` (explicit) or `"plan"` (for advisory lanes)

- **What:** explicit setting of `permissions.defaultMode`. Two options: `"default"` (current behavior, but now committed-to-explicit), or `"plan"` for the read-only exploration lane.
- **Why STE benefits:** controls-audit §13 #6 already flagged this open. STE's `discovery-first` rule is the policy equivalent of "explore before edit"; making the default mode `"plan"` would be the structural enforcement complement (Claude reads but does not edit until the operator explicitly approves). The trade-off is friction on the fast/default-lane.
- **Would slot into:** `.claude/settings.json` next to `permissions.deny`. Operator pick required.

### 3.3 Adopt `hookify` markdown-rule pattern for *future* SWV/CIC-class advisory hooks only

- **What:** the `hookify` plugin's markdown-rule shape (YAML frontmatter `event:` + `action:` + `conditions:`, body = the reminder text). Example: `plugins/hookify/examples/sensitive-files-warning.local.md`.
- **Why STE benefits:** vendor-audit §5.4 + §9 #5 documented the deferred adoption. The existing 6 hooks should stay hand-coded (they encode STE incident-specific behavior the regex engine can't model), but a *new* SWV-class advisory hook for a different lane (e.g., a "warn when migration touches `ticker_*` tables without `identity-path` rule in context") fits the hookify shape exactly.
- **Would slot into:** when the next SWV-class advisory hook is needed, author it as a hookify rule rather than another `.sh` file. Don't vendor hookify proactively; vendor on first reach.

### 3.4 `plugin.json` manifest if STE ever extracts `.claude/**` to a plugin

- **What:** `.claude-plugin/plugin.json` (the upstream `plugins/code-review/.claude-plugin/plugin.json` is the minimal example — `{name, description, version, author}`).
- **Why STE benefits:** the deferred `packetvoid-dev-system` extraction (TODO.md D0 + project memory) is exactly the use case. When the .claude surface is lifted into a reusable plugin, the manifest is mandatory for marketplace discoverability.
- **Would slot into:** D0 extraction work, not before. Listed here only so D0 inherits the canonical shape.

### 3.5 Adopt `examples/hooks/bash_command_validator_example.py` shape for any *future* Bash PreToolUse hook

- **What:** the canonical example uses a tuple list of `(regex, message)` + `validate_command(command)` + `sys.exit(2)` on match. Compact (~50 lines).
- **Why STE benefits:** STE's existing Bash PreToolUse hooks (`block-git-checkout.sh`, `block-pytest-subset-when-ops.sh`) are bash, but a future Python rewrite of either could adopt the upstream Python shape — same structural pattern, easier to extend with multiple rules.
- **Would slot into:** opportunistic when the bash form starts feeling brittle. Not urgent.

### 3.6 Pull-ins explicitly NOT recommended

For completeness — these are publicly-available patterns the prior audit might have flagged, but which would not close a real STE gap:

- **`plugins/feature-dev`** — vendor-audit §4.4 documented the stay-diverged decision.
- **`plugins/code-review`** — STE's spec-reviewer + code-quality-reviewer + silent-failure-hunter triplet already covers what `/code-review` does, and the paid heavy-lane review was retired for cost reasons (PR #458).
- **`plugins/security-guidance` Layers 2 + 3** — paid LLM in the loop; operator deferred (vendor-audit §2.4).
- **`anthropics/financial-services` managed-agent cookbooks** — wrong domain (sell-side finance); STE's auto-trading scope doesn't overlap. Vendor-audit §6.4 documented "study + cherry-pick shape only when rebuilding."
- **`anthropic-cookbook`** — pattern-level guidance, mostly Jupyter notebooks; no `SKILL.md` format to compare against.

## 4. Methodology + caveats

### 4.1 URLs / repos fetched

All listed in §0. WebFetch on docs pages succeeded (skills + sub-agents pages were each > 50 KB, persisted to disk by the tool harness). `gh api` against `anthropics/claude-code` succeeded for every path attempted.

### 4.2 What couldn't be read

- `repos/anthropics/claude-code/contents/CLAUDE.md` — returned empty payload. The repo's `.claude/` dir contains only `commands/`, so the upstream project does not check in a root `CLAUDE.md`. The published memory doc is the authoritative shape source instead; STE comparison is unaffected.

### 4.3 Where inference was applied (named explicitly)

- **§2.9 (`references/` + `scripts/` subdirs not used).** No explicit "we considered and rejected" doc; inferred from skill sizes (largest = 209 lines, well below the threshold where progressive disclosure pays off). Flagged as STE_OVERRIDE not UNKNOWING_DRIFT on the strength of small-size evidence.
- **§2.7 (sentinel-test surface as STE_ORIGINAL).** Upstream plugin-dev skill ships validation scripts for plugin *authors*; STE applies the equivalent discipline at the project level. The pattern (pytest tests asserting filesystem artifacts) is generic, but the wiring is STE-original.

### 4.4 Reproducibility of the prior (dropped) audit's findings

The prior audit was researched against state ~22 commits behind main. By comparing the audit's likely conclusions against current main:

- **Prior finding "STE has no `permissions.deny`"** — INVALIDATED by PR #463 (2026-06-03). The current `.claude/settings.json` has 24 deny rules. Confirmed by file inspection above.
- **Prior finding "STE has no SWV/CIC gate"** — INVALIDATED by PR #464 + #468 (2026-06-03/04). The `discovery-first` rule + both skills exist. Confirmed by file inspection.
- **Prior finding "STE has no silent-failure-hunter"** — INVALIDATED by PR #462 (2026-06-03). `.claude/agents/silent-failure-hunter.md` exists. Confirmed.
- **Prior finding "STE has no commit / clean-gone slash commands"** — INVALIDATED by PR #461 (2026-06-03). All three skills exist.
- **Prior finding "STE has no security-guidance Layer 1"** — INVALIDATED by PR #466 (2026-06-03). The Python pattern scan hook + 17 KB of patterns exists.
- **Prior finding "STE has no identity-path rule"** — INVALIDATED by PR #467 (2026-06-04). `.claude/rules/identity-path.md` exists.
- **Prior finding "STE has no SWV advisory hook"** — INVALIDATED by PR #469 (2026-06-04). `.claude/hooks/swv-advisory.sh` exists.
- **Prior finding "STE has `worktree.baseRef: head`"** — INVALIDATED by PR #460 (2026-06-03). Now `"fresh"`.

**Net.** Of the major surface deltas the prior audit likely flagged, every single one has shipped on main between when the prior audit was researched and now. The current audit's §1 ("Alignments") is the corrected view; only §2.1 (`defaultMode` unset) and §2.2 (`disableBypassPermissionsMode` unset) remain as small open drifts, both flagged as low-risk.

### 4.5 Audit boundary

This is a docs-only audit. No `.claude/**` files modified; no settings, hooks, agents, skills, rules edits; no test changes; no migration; no DB. The only artifact produced is this markdown.

## 5. References inside this repo

- `docs/audits/2026-06-03-claude-code-workflow-controls.md` — controls audit; this audit's predecessor.
- `docs/audits/2026-06-03-vendor-vs-handrolled.md` — vendor audit; companion that documented the vendoring decisions implemented in PRs #461 / #462 / #466 / #468.
- `docs/audits/2026-06-03-identity-substrate-data-flow.md` — the 2026-06-02 case study that motivated SWV + CIC + identity-path.
- `.claude/path_registry.yaml` — H0 path SoT.
- `.claude/settings.json` — current hook + worktree + permissions wiring.
- `tests/test_claude_*_present.py`, `tests/test_path_registry_present.py`, `tests/test_memory_boundary_present.py`, `tests/test_memory_index_size.py`, `tests/test_permissions_deny_present.py` — sentinels holding the surface in place.

## 6. Anthropic surfaces inspected (for reproduction)

| Path | Form |
| :--- | :--- |
| `https://code.claude.com/docs/en/memory` | WebFetch |
| `https://code.claude.com/docs/en/skills` | WebFetch (persisted, > 50 KB) |
| `https://code.claude.com/docs/en/sub-agents` | WebFetch (persisted, > 50 KB) |
| `https://code.claude.com/docs/en/permissions` | WebFetch |
| `gh api repos/anthropics/claude-code/git/trees/main?recursive=1` | tree listing |
| `anthropics/claude-code/examples/settings/{settings-strict, settings-lax, README}.json|.md` | `gh api … --jq .content \| base64 -d` |
| `anthropics/claude-code/examples/hooks/bash_command_validator_example.py` | same |
| `anthropics/claude-code/plugins/{security-guidance, pr-review-toolkit, feature-dev, code-review, commit-commands, hookify, plugin-dev}/**` | same |
| `gh api repos/anthropics/claude-code-action/git/trees/main?recursive=1` | tree listing |
| `anthropics/claude-code-action/.claude/{agents, commands}/**, settings.json` | same |
