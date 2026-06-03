# Claude Code workflow controls — alignment + failure-prevention audit (2026-06-03)

> **Type:** docs-only audit. **No code, no .claude, no workflow, no hook, no agent, no skill, no DB, no migration changes are included in this PR.** Implementation is explicitly out of scope; deferred decisions are enumerated in §13.
>
> **Authority order** (per the audit task spec):
>
> 1. Official Anthropic Claude Code documentation at `https://code.claude.com/docs/en/*` and `https://docs.anthropic.com/en/docs/claude-code/*`.
> 2. Official Anthropic public repositories: `anthropics/claude-code-action` (source, `examples/`, `docs/security.md`).
> 3. This repo's current Claude surfaces (`.claude/**`, `CLAUDE.md`, `.github/workflows/claude-*.yml`, `scripts/check_manifests.py`, sentinel tests).
> 4. STE lived practice only after it has been verified against the three above.
>
> **Authoritative external pointers** (`code.claude.com` redirects to the official documentation surface; URLs are unauthenticated public docs):
>
> - [Overview](https://code.claude.com/docs/en/overview)
> - [Memory (CLAUDE.md + auto memory + `.claude/rules/`)](https://code.claude.com/docs/en/memory)
> - [Hooks](https://code.claude.com/docs/en/hooks) and [hooks guide](https://code.claude.com/docs/en/hooks-guide)
> - [Sub-agents](https://code.claude.com/docs/en/sub-agents)
> - [Skills](https://code.claude.com/docs/en/skills)
> - [Settings](https://code.claude.com/docs/en/settings)
> - [Permissions](https://code.claude.com/docs/en/permissions)
> - [GitHub Actions integration](https://code.claude.com/docs/en/github-actions)
> - [`anthropics/claude-code-action` README + `docs/security.md` + `examples/`](https://github.com/anthropics/claude-code-action)

## 1. Verdict

STE's Claude Code harness is **structurally aligned** with Anthropic's documented mechanisms — every surface STE uses (CLAUDE.md + path-scoped `.claude/rules/`, `.claude/skills/<name>/SKILL.md`, `.claude/agents/<name>.md`, `.claude/hooks/*.sh` wired via `.claude/settings.json`, the `claude-code-action@v1` review workflow with a `paths:` filter and `--allowedTools`, the `worktree.bgIsolation: "worktree"` subagent pattern) is a documented Anthropic mechanism used in a documented way. The repo's path-registry SoT (`.claude/path_registry.yaml` + `scripts/check_manifests.py`) is an STE original that sits on top of the canonical mechanisms without breaking them.

**The failure case (2026-06-02 identity substrate audit, `docs/audits/2026-06-03-identity-substrate-data-flow.md`) was not caused by misuse of Claude Code's mechanisms.** It was caused by the absence of a **discovery-first enforcement layer**: the existing rules, skills, agents, and hooks all describe *what is correct in each path*, but none of them require a **system-wide trace** of writers/readers/consumers/tests before a targeted fix is proposed. Cleanup sidecars, evidence substrates, validators, and backfills were authored against narrow symptom views; the surrounding identity / FPFD / `ticker_history` model was never required as an upstream gate.

Two new controls — both implementable inside Anthropic's canonical mechanisms — close that gap:

- **System-Wide Verification (SWV) gate** — a path-scoped rule + a model-invocable skill + an optional `UserPromptSubmit` advisory hook that force a writer/reader/consumer/test/workflow trace before any targeted fix.
- **Change-Impact Classification (CIC) gate** — a path-scoped rule + a model-invocable skill that force the agent to classify the change type and prove the chosen layer is the correct one before proposing.

Both gates are designed in §8 and §9 of this doc. **Neither is implemented in this PR.** §13 lists the implementation decisions deferred to the operator.

## 2. Scope and posture

| Aspect | Statement |
| :--- | :--- |
| This is | A Claude Code / agent-workflow controls audit. An Anthropic-documentation alignment audit. A failure-prevention control-design pass. Read-only. |
| This is not | A database repair task. A validator repair task. A fundamentals backfill task. A table-cleanup task. An implementation task. |
| Case study (NOT scope) | The 2026-06-02 identity-substrate failure is the case study used to design the controls — not the audit subject. The data audit findings are durably tracked at `docs/audits/2026-06-03-identity-substrate-data-flow.md`; this doc references them as *failures of process discipline*, not data findings. |
| Implementation | Explicitly excluded from this PR. §13 enumerates the decisions the operator must make before any control becomes live. |

## 3. Repo surfaces inspected (current state)

All paths relative to repo root.

### 3.1 CLAUDE-system path registry (the H0 SoT)

| Path | Role |
| :--- | :--- |
| `.claude/path_registry.yaml` | Canonical SoT for `heavy_lane ∪ claude_system` paths. |
| `scripts/check_manifests.py` | Drift-sentinel: rule frontmatter ≡ workflow `paths:` ≡ registry. |
| `tests/test_path_registry_present.py` | Sentinel: registry schema + downstream consumer presence. |

### 3.2 Settings + hooks

| Surface | Role | Anthropic doc reference |
| :--- | :--- | :--- |
| `.claude/settings.json` | Project-scoped settings; wires the 5 hooks; sets `worktree.baseRef: head` + `worktree.bgIsolation: worktree`. | [Settings](https://code.claude.com/docs/en/settings) |
| `.claude/settings.local.json` | Per-machine state (gitignored). | [Settings precedence](https://code.claude.com/docs/en/settings) |
| `.claude/hooks/block-git-checkout.sh` | PreToolUse(Bash) → block `git checkout <branch>`; allow file-restore form. Exit 2 = block. | [Hooks PreToolUse](https://code.claude.com/docs/en/hooks) |
| `.claude/hooks/block-pytest-subset-when-ops.sh` | PreToolUse(Bash) → block subset pytest when `ops/` is dirty. Overridable via env. Exit 2 = block. | [Hooks PreToolUse](https://code.claude.com/docs/en/hooks) |
| `.claude/hooks/gate-ecr-dfcr-edits.sh` | PreToolUse(Edit\|Write\|MultiEdit) → force ECR / DFCR planner path for `tpcore/engine_profile.py` + `tpcore/providers.py`. Exit 2 = block. | [Hooks PreToolUse](https://code.claude.com/docs/en/hooks) |
| `.claude/hooks/risk-path-reminder.sh` | PostToolUse(Edit\|Write\|MultiEdit) → informational reminder on `tpcore/risk/`. Exit 0 (cannot block retroactively, by Anthropic's documented design). | [Hooks PostToolUse](https://code.claude.com/docs/en/hooks) |
| `.claude/hooks/session-start.sh` | SessionStart → one-line surface summary + TODO.md open-H2 extract (≤20 lines). | [Hooks SessionStart](https://code.claude.com/docs/en/hooks) |

### 3.3 Path-scoped rules (12 total)

`daemons.md`, `dashboard.md`, `data-adapter.md`, `data-feed-roster.md`, `engine-build.md`, `engine-roster.md`, `heavy-lane.md`, `migrations.md`, `risk-path.md`, `security-guidance.md`, `selfheal-auditheal.md`, `tests-and-ci.md`. Each uses the documented YAML-frontmatter `paths:` glob to limit when it is loaded into context, per [memory.md §Path-specific rules](https://code.claude.com/docs/en/memory).

### 3.4 Invocable skills (10 total)

`adapter-readiness/`, `audit-data-pipeline/`, `defect-register/`, `dfcr/`, `ecr/`, `engine-readiness/`, `lab-target-run/`, `run-data-ops/`, `security-review/`, `weekly-digest/`. Directory-named per [skills.md](https://code.claude.com/docs/en/skills) (command = directory name, except plugin roots).

### 3.5 Subagent profiles (6 total)

`adapter-implementer.md`, `code-quality-reviewer.md`, `db-architect.md`, `engine-implementer.md`, `lab-target-runner.md`, `spec-reviewer.md`. Tool-allowlist + isolation-mode pattern per [sub-agents.md](https://code.claude.com/docs/en/sub-agents).

### 3.6 GitHub workflows

| Workflow | Role |
| :--- | :--- |
| `.github/workflows/ci.yml` | Pytest + ruff + import sentinels. |
| `.github/workflows/secret-scan.yml` | `gitleaks` per [public-repo-secret-audit](../audits/2026-05-21-public-repo-secret-audit.md). |
| `.github/workflows/deploy-window.yml` | Deploy window guard. |
| `.github/workflows/claude-review-heavy-lane.yml` | `anthropics/claude-code-action@v1` review of heavy_lane ∪ claude_system PRs. `permissions: contents: read, pull-requests: write` only; `--allowedTools` restricts to inline-comment MCP + a few read-only `gh`/`grep`/`find`/`cat`/`ls` Bash patterns. |

### 3.7 Memory boundary (C0.1)

- `docs/MEMSTORE_HANDOFF.md` + `docs/MEMORY_MAINTENANCE.md` — the four-tier boundary.
- `tests/test_memory_boundary_present.py` — sentinel.
- `tests/test_memory_index_size.py` — caps `MEMORY.md` at 24 400 bytes (Anthropic-documented `MEMORY.md` loads only first 200 lines / 25 KB).

## 4. Anthropic-documented mechanisms (canonical baseline)

The following table summarises the canonical mechanisms Claude Code documents. Source pages are linked in §0. All citations were verified by WebFetch against `code.claude.com/docs/en/*` during this audit pass; doc surface as of 2026-06-03.

| Mechanism | Canonical role | Documented enforcement | STE uses it? |
| :--- | :--- | :--- | :--- |
| `CLAUDE.md` files | Persistent instructions; project / user / managed scope. | Context, **not enforced**. Doc: "Claude treats them as context, not enforced configuration. To block an action regardless of what Claude decides, use a PreToolUse hook instead." | Yes (slim project CLAUDE.md + path-scoped rules). |
| `.claude/rules/<name>.md` with `paths:` frontmatter | Path-scoped instructions, loaded when matching files are touched. | Context, not enforced. | Yes (12 rules). |
| `.claude/skills/<name>/SKILL.md` | Model-invocable or slash-only workflows; load on demand. Custom commands have been merged into skills (`.claude/commands/<x>.md` ≡ `.claude/skills/<x>/SKILL.md`). | Context, not enforced. `disable-model-invocation: true` makes it slash-only. | Yes (10 skills). |
| `.claude/agents/<name>.md` | Subagent profiles. Independent context window + tool allowlist + permission mode. `isolation: worktree` documented. | Tool allowlist IS enforced (subagent cannot call disallowed tools). | Yes (6 profiles). |
| `.claude/hooks/*.sh` | Shell hooks on lifecycle events: PreToolUse (can block via exit 2), PostToolUse (cannot block retroactively), SessionStart, UserPromptSubmit, PermissionRequest, SubagentStart/Stop, FileChanged, etc. | **PreToolUse exit 2 BLOCKS** the tool call. PostToolUse exit 2 only shows stderr (tool already ran). | Yes (5 hooks; PreToolUse, PostToolUse, SessionStart). |
| `.claude/settings.json` `permissions: { allow, ask, deny }` | Rule-based tool gating. Evaluation order **deny → ask → allow**, first match wins. Rules MERGE across scopes (deny from any scope blocks). | **Enforced by Claude Code, not the model.** Doc: "Permission rules are enforced by Claude Code, not by the model. Instructions in your prompt or CLAUDE.md shape what Claude tries to do, but they don't change what Claude Code allows." | **No** — STE relies entirely on PreToolUse hooks for tool gating. See §6 "Accidental drift". |
| `permissions.defaultMode` | Modes: `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`. | Enforced. | Not set (uses CLI default). |
| `worktree.bgIsolation: worktree` | Subagent isolation in a temp worktree. | Enforced (each background subagent gets its own worktree). | Yes. |
| `claude-code-action@v1` GitHub Action | PR / issue review; `prompt` + `claude_args` (passes `--allowedTools`, `--max-turns`, etc.). | Tool allowlist + repo permissions enforced. | Yes (`claude-review-heavy-lane.yml`). |
| `paths:` filter on `pull_request` workflow trigger | Standard GitHub Actions path filter. | Enforced by GitHub. | Yes (mirrors `path_registry.yaml`). |
| `--max-turns` CLI arg via `claude_args` | Cost control. | Enforced. | **Not set** — workflow uses the action default (10 turns per [v1 docs](https://code.claude.com/docs/en/github-actions)). See §6. |
| Auto memory (`MEMORY.md` + topic files) | Per-project memory directory at `~/.claude/projects/<project>/memory/`. First 200 lines / 25 KB of `MEMORY.md` loaded each session. | Loaded as context; not enforced. | Yes (size sentinel + boundary doc in C0.1). |

## 5. Alignment matrix

Status legend per audit-task spec:

- **ALIGNED** — STE uses the canonical mechanism in the canonical way.
- **INTENTIONAL_OVERRIDE** — STE deviates and the deviation is documented + justified.
- **ACCIDENTAL_DRIFT** — STE deviates and the deviation is not documented or is a missed canonical lever.
- **UNKNOWN** — needs operator clarification.

| Surface | Anthropic documented pattern | Anthropic example evidence | This repo current pattern | Alignment | Risk | Recommended control | Automatable? | Operator decision needed |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Slim `CLAUDE.md` + split into rules | `memory.md` §"Set up a project CLAUDE.md", §"Organize rules with `.claude/rules/`". 200-line target. | n/a | Slim `CLAUDE.md` + 12 `.claude/rules/` files. | **ALIGNED** | Low | None new — keep size sentinel. | Yes (already C0.1) | No |
| Path-scoped rules (`paths:` frontmatter) | `memory.md` §"Path-specific rules". | n/a | 12 rules, all use `paths:`. Sentinel: `tests/test_claude_rules_present.py`. | **ALIGNED** | Low | None new. | Yes | No |
| SKILL.md format + invocation control | `skills.md` §"Frontmatter reference". `disable-model-invocation: true` for manual-only. | n/a | 10 skills under `.claude/skills/<dir>/SKILL.md`. | **ALIGNED** | Low | None new. | Yes | No |
| Subagent profile + tool allowlist | `sub-agents.md` §"Available tools". | n/a | 6 agents; each declares `tools`. | **ALIGNED** | Low | None new. | Yes | No |
| Subagent isolation | `sub-agents.md` `isolation: worktree`. | `examples/` (no direct example; doc-only). | `worktree.bgIsolation: worktree` in `settings.json` + per-agent `isolation: worktree`. | **ALIGNED** | Low | None new. | Yes | No |
| PreToolUse exit-2 blocking | `hooks.md` §"Exit Code 2". | `hooks-guide.md` example "Block edits to protected files". | 3 PreToolUse hooks use exit 2 (`block-git-checkout`, `block-pytest-subset-when-ops`, `gate-ecr-dfcr-edits`). | **ALIGNED** | Low | None new. | Yes | No |
| PostToolUse as reminder only | `hooks.md` §"Blocking Capability by Event" — PostToolUse cannot block retroactively. | n/a | `risk-path-reminder.sh` exits 0 (correct). | **ALIGNED** | Low | None new. | Yes | No |
| SessionStart context injection | `hooks.md` §"Exit Code 0" + §"SessionStart". | n/a | `session-start.sh` extracts TODO.md open H2 sections (≤20). | **ALIGNED** | Low | None new. | Yes | No |
| GitHub Action: minimal permissions | `github-actions.md` §"Security considerations". `claude-code-action` `docs/security.md`. | `examples/pr-review-filtered-paths.yml`. | `contents: read, pull-requests: write, issues: read, id-token: write` only. | **ALIGNED** | Low | None new. | Yes | No |
| GitHub Action: `--allowedTools` restriction | `github-actions.md` §"Pass CLI arguments". | `examples/pr-review-filtered-paths.yml`. | Restricts to inline-comment MCP + read-only `gh`/`grep`/`find`/`cat`/`ls`. | **ALIGNED** | Low | None new. | Yes | No |
| GitHub Action: `paths:` filter | `github-actions.md` and the example name. | `examples/pr-review-filtered-paths.yml`. | Workflow `paths:` ≡ `path_registry.yaml`. | **ALIGNED** | Low | None new. | Yes | No |
| `permissions.allow/ask/deny` in settings | `permissions.md` §"Permission rule syntax". Documented as primary tool-gating layer; hooks are a secondary layer. | `permissions.md` example block. | **Not used.** All gating is via hooks. | **ACCIDENTAL_DRIFT** | Medium — STE misses the layered defence-in-depth pattern. A managed `deny` would survive even if a hook is bypassed via env var. | Add a `permissions.deny` block for never-allowed surfaces (e.g. `Read(./.env*)`, `Bash(rm -rf /*)`, `Bash(curl *)`). | Yes (settings.json edit; trivially testable). | **Yes** — operator must approve the deny list. |
| `permissions.defaultMode` | `permissions.md` §"Permission modes". | n/a | Unset (CLI default). | **UNKNOWN** | Low — depends on operator intent. Setting `plan` mode by default would force explore-before-edit. | Document the intent; consider `defaultMode: "default"` (explicit) or `plan` for the audit lane. | Yes | **Yes** — operator preference. |
| `--max-turns` on Claude review action | `github-actions.md` cost-optimization. Action default is 10. | n/a | Not set. | **ACCIDENTAL_DRIFT** | Low–medium — review action could iterate past the operator's intended cost budget. | Add `--max-turns 6` (heavy-lane review needs ≤6 turns: read diff, read rules, read affected files, post inline comments, post verdict, finalize). | Yes | **Yes** — operator picks the cap. |
| Docs-only PR Claude-review carve-out | Not documented as a feature; standard GitHub Actions `paths-ignore:` is the canonical lever. | `examples/pr-review-filtered-paths.yml` uses positive `paths:` only. | Workflow fires on every `.claude/**`, `.github/workflows/**`, `tpcore/quality/validation/**` etc. PR — including docs-only updates (e.g., comment changes to a hook script). | **ACCIDENTAL_DRIFT** | Medium — operator-flagged credit waste during recent arcs. | Add a `paths-ignore:` filter for pure-docs PRs (`**/*.md` AND `docs/**` AND no code paths), OR a job-level skip when only `.md` files changed. | Yes | **Yes** — operator confirms the carve-out criteria. |
| Claude review rerun on infra/billing failure | `github-actions.md` cost tips. | n/a | No documented rerun policy; recent session triggered reruns on transient failures. | **ACCIDENTAL_DRIFT** | Medium — credits burn on retry. | Document: "Claude review reruns require operator approval if the prior failure was billing/infrastructure (not VERDICT-rendered)." | No (policy doc only; not technically enforceable without a meta-workflow). | **Yes** — operator authors the rule. |
| Subagent branch base verification | Not Anthropic-documented as a feature. STE convention is `worktree.baseRef: head` per `.claude/settings.json`. Documented `worktree.baseRef` values: `fresh` (default, branches from `origin/<default-branch>`) and `head` (branches from current HEAD). | `sub-agents.md` §"Configure subagents → isolation". | `baseRef: head` is the chosen mode. During the recent arc, one subagent PR was on the wrong base (would have reverted unrelated work). | **INTENTIONAL_OVERRIDE → ACCIDENTAL_DRIFT in practice** | High — wrong base silently reverts merged work. | Set `worktree.baseRef: fresh` for implementer agents, OR add a CI sentinel that asserts subagent PRs branch from `origin/main`. | Yes (settings change OR a CI test) | **Yes** — operator picks `fresh` vs `head` per profile. |
| Auto memory size discipline | `memory.md`: first 200 lines / 25 KB of `MEMORY.md` is loaded. | n/a | `tests/test_memory_index_size.py` caps `MEMORY.md` at 24 400 bytes. | **ALIGNED** | Low | None new. | Yes | No |
| Discovery-before-fix discipline | **Not directly documented as a Claude Code mechanism.** Closest canonical surfaces: `plan` permission mode (read-only exploration), Plan subagent (read-only research). | `sub-agents.md` §"Built-in subagents → Plan". | **No SWV gate. No CIC gate.** Path-scoped rules describe *what is correct in each path* but not *what must be traced before fixing in that path*. | **GAP — not drift, but missing mechanism use** | **HIGH** — root cause of the 2026-06-02 failure. | New SWV rule + skill (§8); new CIC rule + skill (§9); optional `UserPromptSubmit` advisory hook. | Partly — rule + skill yes; UserPromptSubmit hook yes (but advisory only, see §11). | **Yes** — see §13. |

## 6. Divergences (intentional vs accidental)

### 6.1 Intentional overrides (documented and justified)

| Surface | Override | Justification (in repo) |
| :--- | :--- | :--- |
| `worktree.baseRef: head` | Anthropic default is `fresh` (branch from `origin/<default-branch>`). STE chose `head` to let subagents inherit the parent worktree's branch state. | Documented in `.claude/settings.json` `$comment`. **Trade-off documented:** allows subagents to share parent work-in-progress. **Risk surfaced this session:** a subagent dispatched on the wrong base produced a PR that would have reverted unrelated work — this risk was not previously enumerated. |
| Path-registry SoT | Not a documented Anthropic feature. STE adds `.claude/path_registry.yaml` as the cross-surface SoT for heavy-lane paths. | H0 hardening (PR #411). Documented + sentinel-tested. |
| ECR/DFCR planner gates | Beyond canonical examples but uses canonical PreToolUse mechanism. | `gate-ecr-dfcr-edits.sh` + `.claude/rules/{engine,data-feed}-roster.md`. Stops the 22-site drift incident (PR #170). |

### 6.2 Accidental drift (gaps the audit surfaces)

| Surface | Gap | Cost |
| :--- | :--- | :--- |
| `permissions.{allow,ask,deny}` block | Unused. STE relies entirely on hooks. | Loses the layered defence-in-depth Anthropic documents. A bypassed hook (env override or hook-disabled flag) leaves nothing behind. |
| `--max-turns` on heavy-lane review | Unset, uses default 10. | Operator-flagged credit burn during recent reruns. |
| Docs-only PR carve-out | Heavy-lane review fires on every claude-system path including pure-docs edits. | Operator-flagged credit burn on review of comment-only changes. |
| Subagent branch-base verification | `worktree.baseRef: head` choice gives no fence against wrong-base PRs. | One subagent PR this session was on the wrong base. |
| Discovery-before-fix mechanism | No canonical mechanism is used for this. Path-scoped rules describe correctness in each path; they do not require a writer/reader/consumer trace before a fix. | **Root cause of the 2026-06-02 failure.** Cleanup sidecars / evidence substrates / validators / backfills shipped without proving the surrounding identity model. |
| Claude-review rerun policy | No documented policy. | Credits burn on transient infra failure reruns. |

## 7. Failure case study — 2026-06-02 identity substrate audit

**Use only as control-design evidence.** Data findings are durably tracked at `docs/audits/2026-06-03-identity-substrate-data-flow.md` and the receipts at `docs/audits/data/2026-06-03/`. This section asks one question: *which controls would have blocked the failure pattern?*

### 7.1 Pattern timeline (process failures, not data findings)

| Arc | What happened | Control that was missing |
| :--- | :--- | :--- |
| Cleanup / archive / quarantine sidecars | Designed before proving the classifier-evidence substrate. | SWV gate (writer/reader/consumer trace) + CIC gate (is this a new abstraction when an existing one would do?). |
| Symbol-history populated from FMP symbol-change feed | Shipped before proving it covered the delisting-then-reuse case. | SWV gate (source-authority verification: SEC is authoritative for U.S. issuers, not FMP) + CIC gate (existing model check). |
| `excluded_confirmed_data_gap` evidence built | Built before verifying validator-inferred dates matched provider evidence dates. | SWV gate (validator vs source date map). |
| Bounded live runs populated `fundamentals_period_source_evidence` | Created polluted rows before the gate-check was in place. | Live-DB-write authorization gate (operator-explicit) + SWV gate (post-write row-touch acceptance check). |
| Fundamentals backfill ran | Ran before verifying `ticker_history` / CIK / internal-ID attribution. | Identity-substrate gate: "Any prices/fundamentals/lifecycle work must prove `ticker + date → classification_id → CIK` path." |
| Later audit revealed write-side triggers already existed | 15 BEFORE INSERT triggers auto-assigned `classification_id`. Sidecars duplicated logic. | CIC gate ("Does the existing system already have a trigger, function, table, hook, or workflow meant to handle this?"). |
| Later audit revealed read-side bypass | Engine callers of PricesRepo skipped `as_of` — cross-entity history contamination. | SWV gate (reader trace). |
| One subagent PR was on the wrong base | Would have reverted unrelated work. | Subagent branch-base verification control (§5 row). |
| Heavy-lane Claude review reruns on infra failures | Operator-flagged credit burn. | Claude-review rerun policy (§5 row). |

### 7.2 Why the existing path-scoped rules did not prevent it

The 12 existing rules describe *what is correct in each path* (heavy-lane process for risk; Alembic discipline for migrations; HealSpec coverage for self-heal; 6-stage contract for adapters). **None of them require a system-wide writer/reader/consumer trace before a fix.** The failure pattern was always a *narrow local edit that didn't trace to the surrounding system*; the rules said correct things about the narrow edit but never forced the trace.

A targeted-fix discipline is a separate axis from path-specific correctness rules. The audit recommends adding it as a separate path-scoped rule loaded on any data / validator / engine touch.

## 8. System-Wide Verification (SWV) gate — design (docs-only)

> Not implemented in this PR. Implementation decisions in §13.

### 8.1 Intent

Before *any* targeted fix that touches data, validators, ingestion, engines, identity, or the substrate, the agent must produce a **system-wide verification summary** that traces the affected behavior across:

1. **Writers** — what code path produces the current behavior?
2. **Readers** — what code path / engine / report / dashboard / operator consumes it?
3. **Source of truth** — what authority holds the canonical value? (SEC for U.S. CIK-backed issuers; FMP fallback only where SEC cannot cover.)
4. **Existing controls** — what triggers, validators, hooks, rules, tests, or workflows already enforce or detect this behavior, and why did they not prevent the current defect?
5. **Tests** — which tests already cover the behavior? Which ones should have caught the defect but did not?
6. **Workflows / hooks** — what CI gate or Claude hook would have caught this?
7. **Config / env** — what env var, feature flag, or setting affects it?
8. **Adjacent callers** — what other call sites use the same helper / table / function / migration?
9. **Blast radius** — what breaks if this fix is applied and turns out to be at the wrong layer?
10. **Rollback / no-op safety** — can the change be applied as a no-op first?

### 8.2 Mechanism (within Anthropic-documented canonicals)

Three Anthropic-mechanism implementations, each independently usable:

| Layer | Mechanism | Status when implemented |
| :--- | :--- | :--- |
| Documentation context | A new `.claude/rules/system-wide-verification.md` with `paths:` frontmatter covering the high-risk surfaces (initial scope: `tpcore/quality/validation/**`, `tpcore/ingestion/**`, `platform/migrations/**`, `tpcore/auditheal/**`, `tpcore/selfheal/**`, `scripts/ops.py`, plus `tpcore/engine_profile.py`'s data-dependencies). Loaded as context when these paths are touched. | Per `memory.md`, this is *context*, not enforced. |
| Model-invocable skill | A new `.claude/skills/system-wide-verification/SKILL.md` (model-invocable) that walks the 10 trace points above and produces a `verdict: PROCEED / DISCOVERY_REQUIRED / OPERATOR_DECISION_REQUIRED`. The skill is also slash-invokable as `/system-wide-verification`. | Skill content is *guidance*, not enforced; CIC gate (§9) is the structural enforcement complement. |
| Optional advisory hook | An optional `UserPromptSubmit` hook (e.g., `.claude/hooks/swv-advisory.sh`) that, when the prompt mentions a fix/patch/repair/backfill verb AND the working diff touches any SWV-scoped path, prepends a single advisory line: "SWV gate applies — invoke `/system-wide-verification` before any fix." Exit 0, never blocking — discovery-first must remain the agent's discipline, not a fragile hook. | Aligns with `hooks.md` `UserPromptSubmit` — *context injection*, not blocking. |

### 8.3 Required output (the skill's deliverable)

```text
VERDICT: PROCEED | DISCOVERY_REQUIRED | OPERATOR_DECISION_REQUIRED

1. Writer trace:    <file:line list>
2. Reader trace:    <file:line list>
3. Source authority: <SEC | FMP | … + why>
4. Existing controls inspected: <rule / trigger / hook / test / workflow + why they did/didn't catch this>
5. Test coverage:   <existing tests + named gap>
6. Workflow / hook: <relevant CI gate or Claude hook>
7. Config / env:    <env var or setting>
8. Adjacent callers: <other consumers of the same abstraction>
9. Blast radius:    <named callers/tables/engines at risk>
10. Rollback:        <no-op-safe plan or "non-no-op-safe — operator decision required">

Why this is the correct layer: <one-line reason + file:line evidence>
What not to touch:            <named callers/tables>
```

### 8.4 Blocking conditions (the skill returns `DISCOVERY_REQUIRED`)

- Only one file inspected.
- Only one table inspected.
- Callers not inspected.
- Downstream consumers not inspected.
- Shared helper usage not inspected.
- Tests not inspected.
- Runtime entrypoints not inspected.
- Config / env behavior not inspected.
- Source authority not verified.
- Existing controls not checked.
- The proposed fix creates a new table, helper, sidecar, hook, or workflow before checking existing models.
- The proposed fix is local but the defect is systemic.
- The explanation relies on "probably" or "likely" without evidence.

## 9. Change-Impact Classification (CIC) gate — design (docs-only)

> Not implemented in this PR. Implementation decisions in §13.

### 9.1 Intent

Before *any* targeted fix, the agent must classify the change type and prove the chosen layer is correct. The classification is required input to the SWV gate.

### 9.2 Change classifications

```text
documentation_only
workflow_control_change
claude_hook_or_agent_change
github_workflow_change
test_only_change
local_code_behavior_change
shared_abstraction_change
database_schema_change
database_data_repair
ingestion_or_backfill_change
validator_or_gate_change
engine_signal_change
broker_or_order_routing_change
risk_or_capital_gate_change
configuration_or_environment_change
unknown_requires_discovery
```

### 9.3 Required questions before fix

1. What kind of change is this exactly?
2. Is this local, shared, systemic, or unknown?
3. What behavior changes if this is implemented?
4. Who calls this code or uses this data?
5. What upstream component creates the state being changed?
6. What downstream component depends on the output?
7. Is this fixing the root cause or patching a symptom?
8. Could this break another caller that uses the same helper / table / hook / workflow / setting / abstraction?
9. Could this be solved by using an existing model instead of creating a new one?
10. Does the existing system already have a table, function, trigger, hook, rule, or workflow meant to handle this?
11. Why did the existing control not prevent the defect?
12. What evidence proves this is the correct layer to change?

### 9.4 Mechanism (within Anthropic-documented canonicals)

A new `.claude/skills/change-impact-classification/SKILL.md`, model-invocable AND slash-invokable as `/change-impact-classification`. The skill is short (one page), produces a structured output, and references SWV (§8) as the upstream trace.

### 9.5 Required output

```text
CHANGE_TYPE: <one of the 16 classifications>
SYSTEM_BOUNDARY: <local | shared | systemic | unknown>
AFFECTED_COMPONENTS: <named list>
ROOT_CAUSE_VS_SYMPTOM: <root_cause | symptom_patch | unknown>
WHY_THIS_LAYER: <one-line evidence>
WHAT_COULD_BREAK: <named list>
COLLATERAL_CHECKED: <named list>
DECISION: PROCEED | DISCOVERY_REQUIRED | OPERATOR_DECISION_REQUIRED
```

### 9.6 Blocking conditions

Change type is `unknown_requires_discovery`. Boundary is unmapped. Only the target file/table/hook was inspected. Shared callers not inspected. Downstream consumers not inspected. Existing system controls not checked. Tests not checked. The fix creates a new abstraction before proving existing mechanisms are insufficient. The proposed fix is local but the defect is systemic. The agent cannot explain why this is the correct layer.

## 10. Discovery-first controls catalog (design only)

| Control | Mechanism | Automatable? | Operator-decision-required? |
| :--- | :--- | :--- | :--- |
| SWV gate (§8) | `.claude/rules/system-wide-verification.md` + `.claude/skills/system-wide-verification/SKILL.md` + optional `UserPromptSubmit` advisory hook. | Rule + skill yes; hook yes (advisory). | Yes — operator picks scope paths and whether the hook is advisory or stronger. |
| CIC gate (§9) | `.claude/skills/change-impact-classification/SKILL.md`. | Yes (slash + model-invocable). | Yes — operator decides whether CIC is auto-loaded via a path-scoped rule. |
| Identity-path gate | A short rule scoped to `tpcore/{ingestion,quality/validation}/**` + `platform/migrations/**` that requires the trace `ticker + date → classification_id → CIK` for any new write to `prices_daily`, `fundamentals_quarterly`, or any lifecycle table. | Yes (rule). | Yes — operator approves the rule's `paths:` scope. |
| Source-authority gate | Add to the identity-path rule: "SEC-first authority for U.S. CIK-backed issuers. FMP fallback only where SEC cannot cover. FMP must not override SEC identity without divergence handling." | Yes (rule text). | No — already operator-confirmed policy. |
| "No new platform table without schema rationale" | `.claude/rules/migrations.md` already exists. Append: "No new platform table without an operator-approved schema rationale that names the readers, writers, and the existing-table alternative considered." | Yes (rule text). | Yes — operator approves the wording. |
| "No new sidecar/evidence/quarantine table before consolidation review" | Append to `.claude/rules/migrations.md`. | Yes (rule text). | Yes — operator approves the wording. |
| "Return DISCOVERY_REQUIRED on probably/likely without evidence" | A short rule + skill clause. | Partly (rule text + skill output convention; cannot be hard-blocked without a model-side hook). | Yes — operator approves the wording. |

## 11. Subagent branch-base verification control (design only)

| Option | Mechanism | Trade-off |
| :--- | :--- | :--- |
| A. Switch `worktree.baseRef` to `fresh` per implementer agent | Per-agent `baseRef: fresh` frontmatter (canonical Anthropic mechanism). | Subagents branch from `origin/main`. No silent revert risk. Loses the "inherit parent worktree state" property the operator chose `head` for. |
| B. CI sentinel test | A new test asserts every PR branch (other than `main`) has `origin/main` as its merge-base. | Catches the wrong-base PR before merge. Does not prevent the subagent from authoring it. |
| C. Both | `fresh` for implementer-class agents + CI sentinel as a backstop. | Strongest defence. |

**Recommendation:** Option C. Operator decision required (§13).

## 12. Claude review / credit-spend controls (design only)

| Control | Mechanism | Automatable? | Operator decision |
| :--- | :--- | :--- | :--- |
| `--max-turns 6` on heavy-lane review | One line in `claude-review-heavy-lane.yml`'s `claude_args`. | Yes. | Operator picks the cap. |
| Docs-only PR carve-out | `paths-ignore: ['**/*.md', 'docs/**', '*.md']` AND/OR a job-level `if:` that skips when only `.md` paths changed. Use GitHub's native filter; do not invent. | Yes. | Operator approves the carve-out criteria. |
| Rerun policy on infra/billing failures | Documentation in `docs/DEV_PIPELINE_STANDARD.md` + a checklist line on the heavy-lane rule. No technically-enforceable layer without a meta-workflow. | No (policy only). | Operator authors the rule. |
| Reserve Claude review for high-risk paths | Already done via the heavy-lane `paths:` filter. | n/a | No — already aligned. |
| Path-filter manifest test | Already done via `tests/test_path_registry_present.py`. | n/a | No — already aligned. |

## 13. Do-not-implement-yet — operator decisions required

Before any control in §8–§12 becomes live, the operator must decide:

1. **SWV gate scope.** Which paths get the SWV rule's `paths:` frontmatter? Audit recommends starting with `tpcore/quality/validation/**`, `tpcore/ingestion/**`, `tpcore/auditheal/**`, `tpcore/selfheal/**`, `platform/migrations/**`, and `scripts/ops.py`. Operator confirms or narrows.
2. **CIC gate auto-load.** Is the CIC skill purely model-invocable (Claude decides when to run it) or also paired with a path-scoped rule that auto-loads it as context on those same SWV paths?
3. **UserPromptSubmit advisory hook.** Add it (single advisory line on fix/patch/repair/backfill verbs against SWV-scoped diff) or skip it (rule + skill only)?
4. **Subagent branch base.** Switch implementer agents to `worktree.baseRef: fresh`? Add the CI merge-base sentinel? Both?
5. **`permissions.deny` block.** Add a `permissions.deny` block to `.claude/settings.json` for never-allowed surfaces (e.g., `Read(./.env*)`, `Bash(rm -rf /*)`, `Bash(curl *)`)? If yes, operator confirms the list.
6. **`permissions.defaultMode`.** Set an explicit default (`default`, `plan`, `acceptEdits`, `dontAsk`)? Or leave unset?
7. **Heavy-lane review `--max-turns`.** Pick a cap (audit recommends 6).
8. **Docs-only PR carve-out.** Approve the `paths-ignore` criteria for the heavy-lane review.
9. **Claude review rerun policy text.** Approve the policy wording for `docs/DEV_PIPELINE_STANDARD.md`.
10. **Identity-path gate wording.** Approve the rule text.
11. **"No new platform table without schema rationale" wording.** Approve the rule text appended to `.claude/rules/migrations.md`.

## 14. No-implementation statement

**No implementation is included in this PR.** No DB writes. No migrations. No table creation or drops. No code changes. No `.claude/` changes (no new rules, skills, agents, hooks). No `.github/workflows/` changes. No validator patches. No backfill. No cleanup, quarantine, or delete. No provider or API calls except WebFetch against Anthropic's public documentation surface and `gh api` against Anthropic's public `claude-code-action` repository. No PR was opened against any path outside `docs/audits/`, `TODO.md`, and `tests/`. The PR boundary is enforced by the sentinel test `tests/test_claude_code_workflow_controls_audit_documented.py`.

## 15. Anthropic surfaces inspected (for reproduction)

| Surface | Form | URL |
| :--- | :--- | :--- |
| Overview | WebFetch | `https://code.claude.com/docs/en/overview` |
| Memory | WebFetch | `https://code.claude.com/docs/en/memory` |
| Hooks | WebFetch | `https://code.claude.com/docs/en/hooks` |
| Sub-agents | WebFetch (persisted to disk; size > 50 KB) | `https://code.claude.com/docs/en/sub-agents` |
| Skills | WebFetch (persisted to disk; size > 50 KB) | `https://code.claude.com/docs/en/skills` |
| Settings | WebFetch | `https://code.claude.com/docs/en/settings` |
| Permissions | WebFetch | `https://code.claude.com/docs/en/permissions` |
| GitHub Actions | WebFetch | `https://code.claude.com/docs/en/github-actions` |
| `claude-code-action` repo root | `gh api repos/anthropics/claude-code-action/contents` | `https://github.com/anthropics/claude-code-action` |
| `examples/pr-review-filtered-paths.yml` | `gh api … --jq .content \| base64 -d` | `https://github.com/anthropics/claude-code-action/blob/main/examples/pr-review-filtered-paths.yml` |
| `examples/claude.yml` | `gh api … --jq .content \| base64 -d` | `https://github.com/anthropics/claude-code-action/blob/main/examples/claude.yml` |
| `docs/security.md` | `gh api … --jq .content \| base64 -d` | `https://github.com/anthropics/claude-code-action/blob/main/docs/security.md` |

## 16. References inside this repo

- `docs/DEV_PIPELINE_STANDARD.md` §0 §1 §2 — heavy-lane discipline, `gh pr checks` vs `gh run watch`, `statusCheckRollup` gate.
- `docs/MEMSTORE_HANDOFF.md` — four-tier memory boundary (C0.1, 2026-06-01).
- `docs/audits/2026-06-03-identity-substrate-data-flow.md` — the case-study failure (data findings; this audit references it as control-design evidence only).
- `docs/audits/data/2026-06-03/` — empirical receipts for the case study.
- `.claude/path_registry.yaml` — H0 path SoT.
- `.claude/settings.json` — current hook + worktree wiring.
- `tests/test_path_registry_present.py`, `tests/test_claude_rules_present.py`, `tests/test_claude_skills_present.py`, `tests/test_claude_agents_present.py`, `tests/test_claude_hooks_present.py`, `tests/test_claude_review_workflow_present.py`, `tests/test_memory_boundary_present.py`, `tests/test_memory_index_size.py` — sentinels that hold the current surface in place.
- `tests/test_claude_code_workflow_controls_audit_documented.py` (NEW, this PR) — sentinel that pins this audit's load-bearing claims and the "no implementation" boundary.
