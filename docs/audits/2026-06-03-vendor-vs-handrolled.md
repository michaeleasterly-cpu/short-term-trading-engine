# Vendor-vs-hand-rolled audit (2026-06-03)

> **Type:** docs-only audit. **No code, no `.claude/**`, no `.github/workflows/**`, no DB, no migration changes are included in this PR.** Implementation is explicitly out of scope; deferred decisions are enumerated in §9.
>
> **Follow-up to:** `docs/audits/2026-06-03-claude-code-workflow-controls.md` §"What the original audit got wrong" — the morning audit verified STE's *mechanism* alignment with Anthropic's documented Claude Code surfaces but did not verify *pattern* alignment against Anthropic's published reference plugins. Operator: "do the audit."
>
> **Authority order** (same as the morning audit):
>
> 1. Official Anthropic Claude Code documentation at `https://code.claude.com/docs/en/*`.
> 2. `anthropics/claude-code` (the open-source CLI repo; eats its own dog food in `.claude/` + ships 14 official plugins under `plugins/`).
> 3. `anthropics/claude-code-action` (already covered in the morning audit).
> 4. `anthropics/financial-services` (10 managed-agent cookbooks + LSEG/S&P Global partner plugins).
> 5. `anthropics/claude-plugins-official` (the master marketplace).
> 6. `anthropics/skills` (the Agent Skills open-standard reference repo).
> 7. This repo's `.claude/**` + sentinel tests + lived practice.

## 1. Verdict

For each of the 6 STE-hand-rolled surfaces the morning audit named, Anthropic publishes an official equivalent that uses the same documented mechanisms but encodes more capability or a more battle-tested pattern. None of the equivalents is a 1-for-1 drop-in — every one is opinionated in ways STE may or may not want to inherit. The net recommendation per surface is in §8; the per-surface deep dive is §2–§7.

**TL;DR:**

| STE surface | Anthropic equivalent | Recommendation |
| :--- | :--- | :--- |
| `.claude/rules/security-guidance.md` + `.claude/skills/security-review/` | `plugins/security-guidance` (Python + LLM diff + agentic commit review) | **Vendor (with kill-switches).** Major capability uplift; STE's version is advisory text only. |
| `code-quality-reviewer.md` + `spec-reviewer.md` | `plugins/pr-review-toolkit` (6 specialized agents + `/review-pr` coordinator) | **Hybrid: vendor 4 of 6 agents.** Keep STE's spec-reviewer as-is (STE-specific spec discipline); adopt `silent-failure-hunter`, `pr-test-analyzer`, `type-design-analyzer`, `comment-analyzer`. |
| Spec/plan/implement pipeline + heavy-lane discipline | `plugins/feature-dev` (`/feature-dev` 7-phase command) | **Stay diverged.** STE's discipline is tied to `path_registry.yaml` + heavy-lane invariants; vendoring would dilute the operator gate. |
| 5 bash hooks under `.claude/hooks/` | `plugins/hookify` (Python rule engine + markdown rule files) | **Stay diverged.** STE's hooks are mature, individually exit-2-blocking, and tied to specific STE failure modes; hookify is a general-purpose engine. |
| Manual subagents for research/discovery | `financial-services/managed-agent-cookbooks/{market-researcher,earnings-reviewer,statement-auditor}` | **Study + cherry-pick.** The `agent.yaml` + `callable_agents` shape is the canonical multi-subagent pattern; adopt the shape when STE next builds a lab/catalyst/auditheal multi-agent workflow. Don't pre-build. |
| Manual `git commit` + `gh pr create` workflow | `plugins/commit-commands` (`/commit`, `/commit-push-pr`, `/clean_gone`) | **Vendor.** Low-risk quality-of-life uplift. |

## 2. Surface 1 — security-guidance

### 2.1 What Anthropic ships

`plugins/security-guidance/` is a Python-backed plugin with **three independently-toggleable enforcement layers**:

| Layer | Mechanism | Trigger | What it does |
| :--- | :--- | :--- | :--- |
| 1. Pattern rules | `PostToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` regex scan | Every file edit | ~25 known-dangerous patterns (`yaml.load`, `torch.load(weights_only=False)`, `pickle.load` on untrusted data, raw `innerHTML`, hardcoded secrets, SQL/command injection, path traversal, etc.). Injects `additionalContext` warnings inline. |
| 2. Stop-hook LLM diff review | `Stop` hook with `asyncRewake: true` | When Claude finishes a turn | `git stash create` captures a baseline SHA at `UserPromptSubmit`; on `Stop`, runs `git diff <baseline>` and sends it to **Claude Opus 4.7 by default** (configurable via `SECURITY_REVIEW_MODEL`). Returns severity-rated findings via `asyncRewake` so Claude is re-invoked to address them. |
| 3. Agentic commit/push review | `PostToolUse(Bash)` with `if: Bash(git commit:*)` + `if: Bash(git push:*)` + `asyncRewake: true` | On commit / push | SDK-driven reviewer reads related files via `Read`/`Grep`/`Glob` to trace data flow across the codebase — catches multi-file vulnerabilities (IDOR, auth bypass, cross-file SSRF) that pattern matching misses. |

Kill-switches: `SECURITY_GUIDANCE_DISABLE=1`, `ENABLE_SECURITY_REMINDER=0`, `ENABLE_PATTERN_RULES=0`, `ENABLE_CODE_SECURITY_REVIEW=0`, `ENABLE_COMMIT_REVIEW=0`.

Source files: `hooks/hooks.json`, `security_reminder_hook.py`, `review_api.py`, `_base.py`, `patterns.py`, `diffstate.py`, `ensure_agent_sdk.py`, `extensibility.py`, `gitutil.py`, `llm.py`, `session_state.py`, `sg-python.sh`.

### 2.2 What STE has

`.claude/rules/security-guidance.md` (path-scoped rule, advisory text) + `.claude/skills/security-review/SKILL.md` (model-invocable skill). After PR #458, the manual `/security-review` skill is the model-driven Layer-2 assist; Layer 1 is gitleaks + manifest-checker + sentinels.

### 2.3 Gap

| Capability | Anthropic | STE | Gap |
| :--- | :--- | :--- | :--- |
| Pattern-based per-edit reminder | Yes (PostToolUse regex; ~25 patterns) | No | **Major.** STE has no per-edit security pattern check. |
| LLM diff review on Stop | Yes (Opus 4.7 by default) | No | **Major.** Operator burns the model's main-session context to invoke `/security-review`; Anthropic runs it in a separate Stop-hook call. |
| Agentic commit-time review | Yes (multi-file trace via Read/Grep/Glob) | No | **Major.** STE's `gitleaks` catches secret strings; nothing catches IDOR / auth bypass / SSRF. |
| Cost model | LLM credits per turn / per commit (configurable model) | Free (text-only rule) | Vendor version has real per-PR cost. |
| Operator control | Per-feature env-var toggles | n/a | Vendor version's kill-switches make it trivially disable-able. |

### 2.4 Recommendation

**Vendor the plugin, with operator-set kill-switches.** The capability uplift is large; the per-feature toggles let the operator decide which of the 3 layers actually run. Initial recommendation: enable Layer 1 (pattern rules — zero LLM cost), defer Layers 2 + 3 until STE knows the real per-day cost. Keep STE's `.claude/rules/security-guidance.md` (path scope + cross-link) as a thin shim pointing at the plugin.

Cost note: this is exactly the kind of paid-LLM-in-the-loop pattern PR #458 retired for code review. The justification for keeping it on the security path is different — security defects have asymmetric blast radius (a single SSRF / auth bypass costs more than a year of API spend). Operator decision §9 #1.

## 3. Surface 2 — pr-review-toolkit (vs STE's 2 review agents)

### 3.1 What Anthropic ships

`plugins/pr-review-toolkit/` — a coordinator command (`/review-pr`) plus 6 specialized review agents:

| Agent | Focus |
| :--- | :--- |
| `code-reviewer` | General code quality |
| `code-simplifier` | Identify simplifications without changing behavior |
| `comment-analyzer` | Comment accuracy + technical debt |
| `pr-test-analyzer` | Test coverage + edge-case completeness |
| `silent-failure-hunter` | Error handling that swallows failures silently |
| `type-design-analyzer` | Type invariants + design coherence |

The `/review-pr` command is a router: it reads `git diff --name-only`, decides which agents apply (e.g., `pr-test-analyzer` only when test files changed; `type-design-analyzer` only when types were added/modified), and runs them sequentially.

### 3.2 What STE has

Two agents:
- `.claude/agents/spec-reviewer.md` — reads spec + diff, returns PASS/FAIL on STE-specific spec discipline.
- `.claude/agents/code-quality-reviewer.md` — scans for STE-specific code-quality defects (tpcore-private access, missing type hints, missing FilterDiagnostics, missing classify_exit_reason, etc.).

### 3.3 Gap

STE's two agents are **STE-specific** in a way Anthropic's six are not:
- `spec-reviewer` references STE's spec/plan discipline tied to `docs/superpowers/specs/**` — no direct vendor equivalent.
- `code-quality-reviewer` references STE-specific anti-patterns (private-attribute access on `tpcore.*`, sentinel-fenced regions, FilterDiagnostics) — no direct vendor equivalent.

But STE has **no equivalent** for:
- `silent-failure-hunter` — error-handling that swallows failures silently. **Highly relevant to STE** (PR #319 "silent_skip vs hard-fail" debate; the heavy-lane `daemons` rule lists "swallow + log + exit 0" as a recurring defect).
- `pr-test-analyzer` — behavioral vs line coverage. **Relevant to STE** but STE has its own `tests-and-ci.md` rule that covers most of the same ground.
- `type-design-analyzer` — type invariants. **Relevant** since STE's `engine_profile.py` + `providers.py` carry SoT type invariants. Anthropic's version is generic; STE's CIC gate (the morning audit's §9) overlaps.
- `comment-analyzer` — comment accuracy. **Low priority** for STE.

### 3.4 Recommendation

**Hybrid: keep STE's 2 agents, vendor 2 of Anthropic's 6 as new STE-specific files.** Priority order:

1. **Vendor `silent-failure-hunter`** — high overlap with STE's recurring failure mode (silent skips in self-heal / validators / engines). Wire it into the heavy-lane subagent dispatch.
2. **Vendor `type-design-analyzer`** — overlaps with the morning audit's CIC gate; useful when ECR/DFCR-gated SoT files are edited.
3. **Skip `comment-analyzer`** — STE's `tests-and-ci.md` rule already covers comment hygiene.
4. **Skip `pr-test-analyzer`** — STE's whole-suite + order-flip gate already covers test discipline.
5. **Skip `code-simplifier`** — out of scope; STE prefers "make it correct" over "make it concise."
6. **Skip `code-reviewer`** — STE's `code-quality-reviewer` is more specific.

The `/review-pr` coordinator command is a separate question (§5).

## 4. Surface 3 — feature-dev (vs STE's spec/plan/implement pipeline)

### 4.1 What Anthropic ships

`plugins/feature-dev/` — a single command `/feature-dev <description>` + 3 agents:

- `code-architect` — designs the implementation
- `code-explorer` — read-only codebase exploration
- `code-reviewer` — quality review at the end

`/feature-dev` runs a **7-phase workflow**:

1. **Discovery** — understand what needs to be built
2. **Exploration** — code-explorer reads the relevant codebase
3. **Clarification** — ask the user about ambiguities
4. **Architecture design** — code-architect produces a design
5. **User confirmation** — present the design
6. **Implementation** — build it
7. **Review** — code-reviewer checks quality

### 4.2 What STE has

Codified discipline rather than a single command:

- `docs/DEV_PIPELINE_STANDARD.md` §0 (lane decision) + §1 (heavy-lane 13-step pipeline: brainstorm → expert-harden → spec → plan → subagent execution → split-review → operator gate → CI → squash-merge → sync).
- `.claude/agents/engine-implementer.md`, `adapter-implementer.md`, `db-architect.md` — domain-specific implementers, each with `isolation: worktree`.
- `.claude/skills/engine-readiness/SKILL.md`, `adapter-readiness/SKILL.md` — readiness checklists.

### 4.3 Gap

The vendor 7-phase workflow and STE's 13-step pipeline are **shape-compatible** — both front-load discovery + design before implementation, and both finish with review. But they're not 1-for-1:

- STE's pipeline requires **operator approval** at two distinct gates (spec PR + plan PR) before any implementation begins. The vendor's "user confirmation" is a single approval after architecture design.
- STE's discipline is **path-scoped** (heavy lane fires on specific paths). The vendor command is invocation-scoped (operator runs `/feature-dev`).
- STE has **separate spec + plan documents** that get committed via dedicated docs-only PRs. The vendor produces in-memory design that gets implemented in the same session.

### 4.4 Recommendation

**Stay diverged.** Vendoring `/feature-dev` would dilute the operator-gate-at-spec-and-plan pattern that STE relies on for the heavy-lane paths. The vendor command optimizes for "single developer with quick approval"; STE's discipline optimizes for "paper-only trading platform that cannot afford silent regressions." Different objective functions.

If STE wants something `/feature-dev`-like for **non-heavy-lane** work, write a thin `.claude/skills/quick-feature/SKILL.md` that wraps the brainstorming → implement → verify flow. Operator decision §9 #4.

## 5. Surface 4 — hookify (vs STE's 5 bash hooks)

### 5.1 What Anthropic ships

`plugins/hookify/` — a Python rule engine that loads `.claude/hookify.*.local.md` markdown rule files. Each rule file declares (via YAML frontmatter) an `event` (`bash` / `file` / `prompt` / `stop`) and a regex matcher. Rule engine evaluates each rule against the hook input and decides allow / warn / block.

Hooks shipped: `pretooluse.py`, `posttooluse.py`, `userpromptsubmit.py`, `stop.py`. Core: `config_loader.py`, `rule_engine.py`. Example rules: `console-log-warning`, `dangerous-rm`, `require-tests-stop`, `sensitive-files-warning`.

Also ships a `/hookify "<plain English description>"` command that **generates** rule files automatically (`/hookify Warn me when I use rm -rf` creates `.claude/hookify.warn-rm.local.md`).

### 5.2 What STE has

5 hand-coded bash hooks, each targeting a specific failure mode:

- `block-git-checkout.sh` — PreToolUse(Bash) — blocks `git checkout <branch>`, allows file-restore form
- `block-pytest-subset-when-ops.sh` — PreToolUse(Bash) — blocks subset pytest when ops/ is dirty
- `gate-ecr-dfcr-edits.sh` — PreToolUse(Edit\|Write\|MultiEdit) — forces ECR/DFCR planner path for `tpcore/engine_profile.py` + `tpcore/providers.py`
- `risk-path-reminder.sh` — PostToolUse(Edit\|Write\|MultiEdit) — informational reminder on `tpcore/risk/`
- `session-start.sh` — SessionStart — TODO.md open-H2 extract

Wired in `.claude/settings.json`. Sentinel: `tests/test_claude_hooks_present.py`.

### 5.3 Gap

The vendor engine is **more general** but **less domain-specific**:

- Vendor strengths: easy to add new rules (write a markdown file); centralized rule engine; covers UserPromptSubmit + Stop events STE doesn't currently use.
- Vendor weaknesses: rules are regex-based — STE's `gate-ecr-dfcr-edits.sh` reads JSON input, parses `file_path`, and emits structured deny messages with `CLAUDE_ECR_RUN=1` override semantics that the regex engine can't replicate; same for `block-pytest-subset-when-ops.sh` which checks `git diff` for `ops/` changes.

STE's hooks each **encode a specific STE failure incident** (the 22-site engine roster drift, the ops-package-shadow gotcha, the checkout-detach incident). Vendoring would lose that encoded knowledge.

### 5.4 Recommendation

**Stay diverged for the existing 5 hooks.** They're each tightly tied to an STE-specific failure mode that the generic rule engine can't capture.

**But adopt hookify's UserPromptSubmit + Stop hook patterns** when implementing the morning audit's SWV/CIC gates (§13 items #1–#3). Those gates are exactly the "advisory pattern matching at prompt time" use case hookify was built for. Specifically:

- A markdown rule `Whenever prompt mentions fix/patch/repair/backfill AND diff touches SWV paths, prepend the SWV gate reminder` is much easier to maintain than a hand-coded bash hook.
- The `/hookify <description>` self-generation is operator-friendly.

Operator decision §9 #5 — adopt `hookify` *just* for the new gate-class hooks, or hand-code those too?

## 6. Surface 5 — financial-services managed-agent cookbooks

### 6.1 What Anthropic ships

10 multi-subagent cookbooks under `managed-agent-cookbooks/`, each with:

- `agent.yaml` — the orchestrator definition (model, tools, MCP servers, **`callable_agents:` list**)
- `steering-examples.json` — example invocations + expected outputs
- `subagents/*.yaml` — leaf subagent definitions
- A matching `plugins/agent-plugins/<name>/` package with `agents/<name>.md` + `skills/<skill>/SKILL.md` for each capability

Cookbooks relevant to STE:

- **market-researcher** — `sector-reader` + `comps-spreader` + `note-writer` (leaf with Write); MCP toolsets for capiq + factset
- **earnings-reviewer** — `transcript-reader` + `model-updater` + `note-writer`
- **statement-auditor** — `flagger` + `reconciler` + `statement-reader`

The `agent.yaml` format is **not the same** as a Claude Code subagent profile (`.claude/agents/<name>.md`). It's a managed-agent definition for the Anthropic Managed Agent platform (`agent_toolset_20260401`), used outside Claude Code. The shape is still useful as a reference.

### 6.2 What STE has

6 subagent profiles in `.claude/agents/`, each with `tools` + `isolation: worktree`. They're invoked individually via the Agent tool — not orchestrated as multi-agent workflows.

### 6.3 Gap

STE's `lab/` discovery workflow, `catalyst/` event detection, and `auditheal/` cross-table audit are all candidates for the **multi-subagent orchestration pattern** the vendor cookbooks demonstrate. The current STE pattern is "operator dispatches one subagent at a time" — the vendor pattern is "one orchestrator delegates to N leaf subagents in parallel."

But:
- STE is paper-only and doesn't currently need a managed-agent platform.
- The `agent.yaml` format is for the Managed Agent platform, not Claude Code subagents.
- STE's discovery + research lanes use deterministic primitives + the Lab gate — the multi-subagent shape would be additional rather than replacement.

### 6.4 Recommendation

**Study + cherry-pick the shape, don't pre-build.** When STE next builds a new multi-agent workflow (e.g., the deferred §13 #5 of the morning audit's SWV gate, or a future lab-discovery v2), inherit the `callable_agents:` orchestrator + leaf-subagent shape — one orchestrator that does the routing, leaves that each have a single responsibility. **Do not vendor the financial-services cookbooks themselves** — they're built for sell-side finance use cases (earnings transcripts, GL recon, KYC, IB pitches) that don't map to STE's automated-trading scope.

Specific cookbook patterns worth borrowing if STE rebuilds:

- **statement-auditor's `flagger` + `reconciler` + `statement-reader` pattern** — directly analogous to STE's `auditheal` Step-3 cross-table audit. The flagger generates suspicion candidates; the reconciler tries to remediate; the statement-reader is read-only. STE's `auditheal` currently bundles these.
- **market-researcher's `sector-reader` + `comps-spreader` + `note-writer` pattern** (where only `note-writer` has Write) — exactly the discovery-then-emit pattern STE's lab workflow could use.

Operator decision §9 #6.

## 7. Surface 6 — commit-commands

### 7.1 What Anthropic ships

`plugins/commit-commands/` — 3 slash commands:

- `/commit` — analyzes git status, drafts a commit message in the repo's existing style, stages relevant files, commits.
- `/commit-push-pr` — same as `/commit` + push + open PR.
- `/clean_gone` — deletes local branches whose remote has been deleted.

Each command is a markdown file with YAML frontmatter declaring `allowed-tools` (`Bash(git status:*)`, `Bash(git diff:*)`, etc.).

### 7.2 What STE has

Manual git commit messages drafted ad-hoc per the standing rules: "commit message ends with `Co-Authored-By: …`" and "pass via HEREDOC for formatting." `gh pr create --title …--body "$(cat <<'EOF' …EOF)"` for PRs.

### 7.3 Gap

Low: STE's commit + PR flow works. But:

- `/commit` would save ~10–15 seconds per commit by automating message drafting.
- `/commit-push-pr` would replace the 3-step `git commit` → `git push` → `gh pr create` ritual.
- `/clean_gone` is genuinely useful for STE's worktree-heavy workflow (deleted branches accumulate).

### 7.4 Recommendation

**Vendor.** Low-risk quality-of-life. Operator's existing standing rules about commit messages (HEREDOC, Co-Authored-By footer) would need to be encoded in the plugin's command bodies — that's a 30-line edit per command. Defer until §9 #7 is decided.

## 8. Net recommendations matrix

| # | Surface | Recommendation | Effort | Risk | Cost impact |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | security-guidance | **Vendor, kill-switches on** | Medium (env-var wiring + STE-rule shim) | Low if kill-switches enabled per layer | Layer 1: free. Layer 2 + 3: per-turn LLM credits |
| 2 | pr-review-toolkit | **Vendor 2 of 6 agents** (`silent-failure-hunter`, `type-design-analyzer`); keep STE's 2 | Low (copy agent prompts; wire into heavy-lane subagent dispatch) | Low | None — subagents run in main session |
| 3 | feature-dev | **Stay diverged** (consider thin `/quick-feature` skill for non-heavy-lane work) | n/a | n/a | n/a |
| 4 | hookify | **Stay diverged for existing 5 hooks; adopt for new SWV/CIC gate hooks** | Medium (plugin install + rule generation for SWV/CIC) | Low (advisory hooks only, no blocking) | None |
| 5 | financial-services cookbooks | **Study + cherry-pick shape when rebuilding lab/auditheal** | n/a (deferred) | n/a | n/a |
| 6 | commit-commands | **Vendor** | Low (install plugin; encode HEREDOC + Co-Authored-By in command bodies) | Low | None |

## 9. Operator decisions required

Before any control above becomes live, the operator must decide:

1. **security-guidance vendor** — vendor the plugin? Which of the 3 layers (pattern rules / Stop-hook LLM review / agentic commit review) are enabled? What's the LLM budget? Suggest: enable Layer 1 only, defer Layers 2 + 3 until cost is measured.
2. **`silent-failure-hunter` vendor** — copy the agent prompt verbatim, or adapt to reference STE's specific silent-skip vocabulary (HealSpec, FilterDiagnostics, `swallow + log + exit 0`)? Suggest: adapt.
3. **`type-design-analyzer` vendor** — copy verbatim, or merge with the morning audit's CIC gate design (§9 of the controls audit)? Suggest: merge — they're solving the same problem.
4. **`/quick-feature` thin skill** — write one for non-heavy-lane work, or leave default-lane discipline as-is? Suggest: leave as-is. The default lane (Explore → Plan → Implement → Commit) already covers this.
5. **hookify for SWV/CIC gate hooks** — use hookify for the new SWV/CIC advisory hooks (§13 #3 of the controls audit), or hand-code? Suggest: hand-code the first one to validate the design, then migrate to hookify if a second hook of similar shape lands.
6. **financial-services cookbook study** — schedule a future "rebuild auditheal as orchestrator + leaf subagents" arc, or leave as a one-line pointer? Suggest: leave as a pointer.
7. **commit-commands vendor** — vendor the plugin? Encode the HEREDOC + Co-Authored-By footer in the command bodies?

## 10. No-implementation statement

**No implementation is included in this PR.** No code changes. No `.claude/` changes (no new rules, skills, agents, hooks). No `.github/workflows/` changes. No DB writes. No migrations. No schema changes. No validator patches. No backfill. No cleanup, quarantine, or delete. No PR was opened against any path outside `docs/audits/`, `TODO.md`, and `tests/`. The PR boundary is enforced by the sentinel test `tests/test_vendor_vs_handrolled_audit_documented.py`.

## 11. Anthropic surfaces inspected (for reproduction)

| Surface | How fetched |
| :--- | :--- |
| `anthropics/claude-code` full tree (302 paths) | `gh api 'repos/anthropics/claude-code/git/trees/main?recursive=1'` |
| `plugins/security-guidance/{README.md,hooks/hooks.json,hooks/security_reminder_hook.py,hooks/review_api.py}` | `gh api … --jq .content \| base64 -d` |
| `plugins/pr-review-toolkit/{README.md,agents/*.md,commands/review-pr.md}` | same |
| `plugins/feature-dev/{README.md,agents/code-architect.md,commands/feature-dev.md}` | same |
| `plugins/hookify/{README.md,hooks/{pretooluse.py,posttooluse.py,userpromptsubmit.py,stop.py},core/{config_loader.py,rule_engine.py}}` | same |
| `plugins/commit-commands/{README.md,commands/{commit.md,commit-push-pr.md,clean_gone.md}}` | same |
| `anthropics/financial-services` full tree (625 paths) | `gh api 'repos/anthropics/financial-services/git/trees/main?recursive=1'` |
| `managed-agent-cookbooks/market-researcher/agent.yaml` | `gh api … --jq .content \| base64 -d` |
| `anthropics/claude-plugins-official` full tree (587 paths) | recursive tree fetch |
| `anthropics/skills` full tree (479 paths) | recursive tree fetch |

## 12. References inside this repo

- `docs/audits/2026-06-03-claude-code-workflow-controls.md` — the morning audit; this doc is its follow-up.
- `docs/audits/2026-06-03-identity-substrate-data-flow.md` — the 2026-06-02 failure case study.
- `.claude/rules/security-guidance.md`, `.claude/skills/security-review/`, `.claude/agents/{spec-reviewer,code-quality-reviewer,engine-implementer,adapter-implementer,db-architect}.md`, `.claude/hooks/*.sh` — the 6 hand-rolled surfaces.
- `tests/test_vendor_vs_handrolled_audit_documented.py` (NEW, this PR) — sentinel pinning this audit's load-bearing claims + the no-implementation boundary.
