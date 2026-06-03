# Anthropic canonical-pattern alignment — COMPLETE audit (2026-06-04)

> **Type:** docs-only audit. No code / `.claude/**` / workflow / hook / agent / skill / DB / migration changes.
>
> **Why this exists:** the prior audit `docs/audits/2026-06-04-anthropic-canonical-pattern-alignment-audit.md` (#470) was a *partial, sampled* pass — it examined only 7 of the 13 `anthropics/claude-code` plugins and sampled (did not exhaustively enumerate) those 7. This audit is the **exhaustive** pass requested as "phase 3": every plugin, every `examples/` artifact, and the full `claude-code-action` repo, each compared against STE's current `.claude/**` surface and classified with no gaps. It **supersedes #470** for coverage; #470's verified conclusions are carried forward and re-confirmed here.
>
> **Authority order:** (1) `https://code.claude.com/docs/en/*`; (2) `anthropics/claude-code` (`plugins/`, `examples/`, `.claude/`); (3) `anthropics/claude-code-action`; (4) `anthropics/anthropic-cookbook` (pattern-level only); (5) this repo's `.claude/**` + `CLAUDE.md` + sentinels.

## 0. Method + complete surface inventory

Four parallel auditors swept the full Anthropic surface (read-only; `gh api` + STE `.claude/**` inspection):

- **Cluster A** — the 6 plugins #470 NEVER examined: `agent-sdk-dev`, `claude-opus-4-5-migration`, `explanatory-output-style`, `frontend-design`, `learning-output-style`, `ralph-wiggum`.
- **Cluster B** — the 2 richest plugins, every artifact: `security-guidance`, `pr-review-toolkit`.
- **Cluster C** — workflow/command/dev plugins: `feature-dev`, `code-review`, `commit-commands`, `plugin-dev`, `hookify`.
- **Cluster D** — `examples/{hooks,mdm,settings}` + the entire `claude-code-action` repo.

**Complete `anthropics/claude-code` plugin roster (13):** agent-sdk-dev, claude-opus-4-5-migration, code-review, commit-commands, explanatory-output-style, feature-dev, frontend-design, hookify, learning-output-style, plugin-dev, pr-review-toolkit, ralph-wiggum, security-guidance.
**`examples/`:** hooks, mdm, settings.
**`claude-code-action`:** separate repo (action.yml, examples/, .claude/, docs/security.md).

**Legend:** ALIGNED · STE_OVERRIDE (documented divergence) · UNKNOWING_DRIFT (undocumented divergence — candidate follow-up) · STE_ORIGINAL · NOT_APPLICABLE.

## 1. Cluster A — the 6 plugins #470 never examined

| Plugin | What it is | STE equivalent | Disposition | Pull-in? |
| :--- | :--- | :--- | :--- | :--- |
| **agent-sdk-dev** | Scaffolds + verifies Claude Agent SDK projects (cmd `/new-sdk-app` + 2 verifier agents) | none | NOT_APPLICABLE — STE is a trading monorepo, not an SDK-project generator | no |
| **claude-opus-4-5-migration** | Skill to migrate *other* codebases to Opus 4.5 | none | NOT_APPLICABLE — meta-upgrade tool; STE upgrades prompts directly from docs | no |
| **explanatory-output-style** | SessionStart hook injecting "★ Insight" educational commentary | `.claude/hooks/session-start.sh` (same *mechanism*; payload = TODO H2 extraction) | NOT_APPLICABLE — mechanism aligned (§1.9); payload is STE-original by design | no |
| **frontend-design** | Skill guiding distinctive frontend aesthetics | none (`dashboard.py` is an internal operator console) | NOT_APPLICABLE — no customer-facing UI | no |
| **learning-output-style** | SessionStart hook: "build and understand" learn-mode pauses | `.claude/agents/*` delegation model | NOT_APPLICABLE — STE uses subagent delegation + gated ceremonies, not learn-mode pauses | no |
| **ralph-wiggum** | Stop hook creating a self-referential completion loop (`--max-iterations`) | none | STE_OVERRIDE — Stop hooks are cost-deferred (§2.10); STE uses SWV+CIC gates for correctness-critical decisions | no |

**Cluster A net:** zero pull-ins. Four NOT_APPLICABLE (meta-tooling + output-styles for a domain STE doesn't serve), one mechanism-aligned-payload-original (session-start), one documented cost-driven override (ralph-wiggum/Stop hooks). No drift.

## 2. Cluster B — security-guidance + pr-review-toolkit (exhaustive)

Every artifact enumerated (security-guidance: 12 py + 1 sh + hooks.json; pr-review-toolkit: 6 agents + command).

**security-guidance:**

| Artifact | STE equivalent | Disposition | Pull-in? |
| :--- | :--- | :--- | :--- |
| Layer 1 pattern scan (`patterns.py`, ~25 rules) | `.claude/hooks/security_patterns_vendored.py` + `security_patterns_ste.py` (+5 STE rules) | ALIGNED (vendored + extended) | no (complete) |
| Layer 2 LLM diff review (Stop + `asyncRewake`) | none | STE_OVERRIDE (cost-deferred, vendor-audit §2.4) | no |
| Layer 3 agentic commit review | none | STE_OVERRIDE (cost-deferred) | no |
| `sg-python.sh` dispatcher | `.claude/hooks/security_pattern_scan.sh` | ALIGNED | no |
| SessionStart `ensure_agent_sdk.py` | none | NOT_APPLICABLE (L2/3 infra) | no |
| UserPromptSubmit advisory | none | STE_OVERRIDE (PostToolUse-only) | no |
| L2/3 support modules (`llm.py`, `review_api.py`, …) | none | NOT_APPLICABLE (L2/3 infra) | no |

**pr-review-toolkit (all 6 agents):**

| Agent | STE equivalent | Disposition | Pull-in? |
| :--- | :--- | :--- | :--- |
| silent-failure-hunter | `.claude/agents/silent-failure-hunter.md` | ALIGNED (vendored + adapted; tools restricted, model pinned, STE catalogue) | no |
| type-design-analyzer | merged into `.claude/skills/change-impact-classification/SKILL.md` | ALIGNED (merged per #468) | no |
| code-reviewer | `.claude/agents/code-quality-reviewer.md` | STE_OVERRIDE (STE's is more specific) | no |
| code-simplifier | none | STE_OVERRIDE ("correct" > "concise") | no |
| comment-analyzer | `.claude/rules/tests-and-ci.md` | STE_OVERRIDE (rule-governed) | no |
| pr-test-analyzer | whole-suite + order-flip gate | STE_OVERRIDE (structural) | no |

**Cluster B net:** every #470 conclusion (§1.11–1.13, §2.13) re-verified against current upstream — no drift. Every skip rationale still holds. Zero new pull-ins.

## 3. Cluster C — feature-dev / code-review / commit-commands / plugin-dev / hookify (exhaustive)

| Plugin | STE equivalent | Disposition | Pull-in? |
| :--- | :--- | :--- | :--- |
| **feature-dev** (7-phase + 3 agents) | `docs/DEV_PIPELINE_STANDARD.md` 13-step heavy lane (adds spec-PR + plan-PR gates) | STE_OVERRIDE | no |
| **code-review** (`/code-review` cmd) | spec-reviewer + code-quality-reviewer + silent-failure-hunter (manual gate-sequenced) | STE_OVERRIDE (paid CI review retired #458; sequential gates, not parallel) | no |
| **commit-commands** | `.claude/skills/{commit,commit-push-pr,clean-gone}` | ALIGNED (vendored #461 + STE enhancements: HEREDOC, Co-Authored-By, worktree-safe clean-gone) | no |
| **plugin-dev** (7 skills + 3 agents + cmd) | none (STE is a project, not a plugin) | NOT_APPLICABLE | **yes — but only on Trellis extraction (§3.4 plugin.json), which was closed today (Trellis PR #13)** |
| **hookify** (markdown-rule engine) | 6 hand-coded bash hooks | STE_OVERRIDE (semantic + override-flag semantics exceed the DSL; adopt for future *pattern-based* advisory hooks only) | deferred |

**Cluster C net:** all #470 conclusions (§1.10, §2.11, §2.13, §2.14, §3.3, §3.4) re-verified. feature-dev's 3 agents + plugin-dev's full suite + hookify's commands were newly enumerated (none vendored — correct). Zero new pull-ins; plugin-dev's manifest shape was the §3.4 item, now satisfied in Trellis.

## 4. Cluster D — examples/ + claude-code-action (exhaustive)

**examples/:**

| Artifact | STE equivalent | Disposition | Pull-in? |
| :--- | :--- | :--- | :--- |
| `settings/settings-strict.json` | `.claude/settings.json` (deny block, defaultMode set) | STE has the deny mechanism; lacked `disableBypassPermissionsMode` — **set #471, then REMOVED today per operator** → now STE_OVERRIDE | no (reversed by operator) |
| `settings/settings-lax.json` | `.claude/settings.json` (far deeper) | STE_OVERRIDE | no |
| `settings/settings-bash-sandbox.json` | none (deny rules instead of sandbox) | STE_OVERRIDE (deny block is STE's equivalent control) | no |
| `settings/README.md` | CLAUDE.md + audit docs | STE_ORIGINAL | no |
| `hooks/bash_command_validator_example.py` | `.claude/hooks/block-*.sh` (bash, same exit-2) | STE_OVERRIDE (Python shape = future optionality, §3.5) | conditional/future |
| `mdm/**` | none | NOT_APPLICABLE (enterprise fleet mgmt) | no |

**claude-code-action (full repo):**

| Artifact | STE equivalent | Disposition | Pull-in? |
| :--- | :--- | :--- | :--- |
| `action.yml` + workflow examples | `.github/workflows/` (paid review retired #458) | NOT_APPLICABLE (CI/CD GitHub-App model vs local CLI) | no |
| `docs/security.md` (bot/PR threat model) | deny block + PreToolUse hooks | DIFFERENT_THREAT_MODEL (bot/CI vs single-operator/data-integrity) | no |
| `.claude/agents/code-quality-reviewer.md` | `.claude/agents/code-quality-reviewer.md` (stricter, 8 STE defect classes) | STE_OVERRIDE | no |
| `.claude/agents/security-code-reviewer.md` | none (OWASP web-centric) | NOT_APPLICABLE (STE risks are data-integrity/identity, not OWASP) | no |
| `.claude/agents/{documentation-accuracy,performance,test-coverage}-reviewer.md` | none (test-coverage = CI gate) | NOT_APPLICABLE / structural | no |
| `.claude/commands/review-pr.md` (parallel 5-agent) | gate-sequenced manual agents | STE_OVERRIDE (sequential gates load-bearing) | no |
| `.claude/commands/{commit-and-pr,label-issue}.md` | `/commit-push-pr` (+ no labeling) | STE_OVERRIDE | no |

**Cluster D net:** the only flagged drift (`disableBypassPermissionsMode`) is the key the operator deliberately removed this session — now a documented STE_OVERRIDE, not a pull-in. The claude-code-action's 5-agent review suite is a CI/CD model orthogonal to STE's local workflow; the 3 STE-absent agents (security/performance/test-coverage) target a generic-web-app domain, not STE's. Zero new pull-ins.

## 5. Synthesis — delta vs #470 + complete pull-in list

### Coverage delta vs #470
- **#470 examined 7 of 13 plugins, and sampled them.** This audit enumerated **all 13 plugins + all `examples/` artifacts + the entire `claude-code-action` repo**, reading every command / agent / skill / hook / settings file.
- **Newly examined (not in #470):** the 6 Cluster-A plugins (`agent-sdk-dev`, `claude-opus-4-5-migration`, `explanatory-output-style`, `frontend-design`, `learning-output-style`, `ralph-wiggum`); feature-dev's 3 agents; plugin-dev's full 7-skill/3-agent suite; hookify's 4 commands + conversation-analyzer; `examples/settings/settings-bash-sandbox.json`; `examples/mdm/**`; and the full `claude-code-action` agent + command roster (10 artifact classes).
- **Re-verified (in #470, confirmed against current upstream):** all 14 §1 alignments + all §2 divergences. No upstream drift since #470; every prior conclusion holds.

### Complete disposition tally
| Classification | Count | Action |
| :--- | :--- | :--- |
| ALIGNED | 14 | none (matches canon) |
| STE_OVERRIDE (documented) | ~20 | none (intentional, documented) |
| STE_ORIGINAL | 6 | none (load-bearing; no Anthropic equivalent) |
| NOT_APPLICABLE | ~12 | none (out of STE's domain: SDK scaffolding, output-styles, MDM, CI/CD action, OWASP web agents) |
| UNKNOWING_DRIFT | 0 (net) | the lone candidate — `disableBypassPermissionsMode` — was set #471 then removed today per operator → now STE_OVERRIDE |

### Complete pull-in list (every candidate, final status)
1. `disableBypassPermissionsMode` (#470 §3.1) — **CLOSED-BY-OPERATOR-DECISION**: removed today; it blocked deliberate `--dangerously-skip-permissions`. Documented STE_OVERRIDE.
2. `permissions.defaultMode` (#470 §3.2) — **DONE**: set `"default"` in #471.
3. hookify markdown-rule shape (#470 §3.3) — **DEFERRED** (intentional): adopt for the next *pattern-based* advisory hook; existing hooks stay hand-coded.
4. `.claude-plugin/plugin.json` for the extraction (#470 §3.4) — **DONE TODAY**: added to the Trellis dev-system (PR #13) — the reusable-plugin manifest gap is closed.
5. `bash_command_validator` Python shape (#470 §3.5) — **DEFERRED** (intentional): adopt opportunistically for any future Python rewrite of a Bash PreToolUse hook.

### Conclusion
The exhaustive audit finds **zero net actionable gaps**. STE's `.claude/**` surface is fully aligned with the Anthropic canonical patterns across the *entire* published plugin + examples + action surface; every divergence is a documented STE_OVERRIDE or domain-driven NOT_APPLICABLE, and every #470 pull-in candidate is now either shipped, deferred-by-design, or closed-by-operator-decision. The dev-system (Trellis) now also carries the canonical `settings.json` hardening + `plugin.json` manifest (phase-3, PR #13). **The Anthropic-repo alignment arc is complete.**

## 6. References

- `docs/audits/2026-06-04-anthropic-canonical-pattern-alignment-audit.md` — #470, the partial pass this supersedes.
- `docs/audits/2026-06-03-claude-code-workflow-controls.md` — controls audit (§13 deferred deliverable = the alignment audit).
- `docs/audits/2026-06-03-vendor-vs-handrolled.md` — vendor-vs-hand-rolled companion.
- `.claude/path_registry.yaml`, `.claude/settings.json`, `tests/test_claude_*_present.py` — the surface + sentinels.
