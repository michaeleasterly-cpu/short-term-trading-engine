# CLAUDE.md — Short-Term Trading Engine

This file is the **slim** project memory. It carries (a) project identity, (b) one-line architecture, (c) hard universal invariants that hold EVERY session regardless of path, and (d) pointers to the canonical SoT for everything else. Path-scoped rules, invocable workflows, named subagent profiles, and enforcement hooks live in `.claude/`. Per the Anthropic memory guidance (<https://code.claude.com/docs/en/memory>): keep CLAUDE.md short; load detail on demand.

## Project Identity
Multi-engine automated trading platform. US equities, daily timeframe, fully automated execution via Alpaca API. Personal-use only.

## Architecture (one line per module — load `.claude/rules/<name>.md` for invariants on path touch)

- `tpcore/` — shared library: `risk/` (RiskGovernor + batch_gate; `.claude/rules/risk-path.md`), `selfheal/` + `auditheal/` (generic-engine self-heal + cross-table audit; `.claude/rules/selfheal-auditheal.md`), `quality/validation/` (13-check data-acceptance gate), `aar/`, `parity/`, `backtest/`, `lab/`, `order_management/`, `engine_profile.py` (engine roster SoT; ECR-only — `.claude/rules/engine-roster.md`), `providers.py` (data-feed ProviderBinding SoT; DFCR-only — `.claude/rules/data-feed-roster.md`), `forensics/`, `indicators/`, `templates/{engine,adapter}_template/`.
- `platform/migrations/` — Alembic schema substrate; `.claude/rules/migrations.md`.
- `reversion/`, `vector/`, `momentum/`, `sentinel/`, `canary/`, `catalyst/` — 5-plug engines (`setup_detection`/`lifecycle_analysis`/`execution_risk`/`aar_logging`/`capital_gate`); `.claude/rules/engine-build.md`.
- `sigma/` — **ARCHIVED 2026-05-16** → `archive/sigma/` (EULOGY.md).
- `ops/` — daemons + SDLC: `engine_service.py` (consolidated DA-3; `.claude/rules/daemons.md`), `lane_service.py` (deployed deterministic-only data-repair daemon — 2026-05-22, no LLM at runtime), `data_repair_service.py` (library source), `llm_aar_critic*.py` (operator-local LLM AAR critic — NEVER deployed; the only LLM caller left in the repo), `engine_sdlc/` (ECR), `lab/` (on-demand search), `weekly_digest.py`, `defect_register.py`, `platform_pipeline.py`. **LLM-triage stack REMOVED 2026-05-22** + **LLM lab/finder/monitor REMOVED 2026-05-25** (operator directives "we aren't going to use the llm triage… take it out" → "it is out", Railway-readiness retirement) — `llm_data_recovery`, `llm_data_triage`, `engine_llm_triage`, `llm_triage_service`, `llm_lab_emitter`, `llm_edge_finder`, `llm_edge_finder_sdk`, `llm_finder_outcome_monitor` + `tpcore.lab.llm_emitter` + `tpcore.lab.llm_finder` + `/lab-spec-emit` + `/lab-edge-find` skills all deleted. Deterministic cascade catalog is the COMPLETE self-heal layer with no LLM backstop.
- `dashboard.py` + `dashboard_components/` — Streamlit operator console (`scripts/run_dashboard.sh`); `.claude/rules/dashboard.md`.
- `scripts/` — canonical workflows: `run_data_operations.sh`, `run_full_backfill.sh`, `run_all_engines.sh`, `install_all_daemons.sh`, `ops.py` (parameterised stages — `.claude/rules/data-adapter.md`), `gen_engine_manifest.py` (sentinel-fenced shadow regen).
- `tests/`, `**/tests/` — full-suite + order-flip is authoritative; `.claude/rules/tests-and-ci.md`.

**Engine lifecycle:** `LAB → PAPER → LIVE → RETIRED` (`tpcore.engine_profile.LifecycleState`). All live engines are PAPER. `LIVE` is reserved (paper-only mandate). Spec: `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`.

**Engine credibility status (accuracy guard):** six PAPER engines (reversion, vector, momentum, sentinel, canary, catalyst) — the autonomous Lab criteria gate (`docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md`) replaces the absolute DSR ≥ 0.95 ∧ credibility ≥ 60 absolute gate for ADD/promote. carver is in LAB (PR #154); sigma is RETIRED (`archive/sigma/EULOGY.md`); `canary` is the documented non-graduating heartbeat (never calls `write_credibility_score`, spec §4b). PAPER→LIVE remains a separate, future-reserved gate per the paper-only mandate.

## Universal invariants (hold every session, regardless of path)

- **All timestamps UTC.** Market hours via `tpcore.calendar` (XNYS via `exchange_calendars`).
- **No yfinance. No Discord. No manual execution.** All orders via Alpaca API. Default daily-bars feed is **FMP** (full CTA consolidated tape via `/stable/historical-price-eod/full` on the operator's $200/year Starter tier; Alpaca IEX/SIP available via `--param feed=iex|sip` for fallback/diagnostics). Paper-then-live.
- **All code type-hinted.** Pydantic v2 for data models. structlog for logging. `from __future__ import annotations`.
- **Backtest with the self-built survivorship-free database** before any live trading (`prices_daily` is the substrate; partial survivorship-clean — known caveat in `momentum/backtest.py`).
- **Never access private attributes** (`._store`, `._pool`, etc.) on `tpcore.*` classes. Use the public accessor; extend the class with one if missing; never add `# noqa: SLF001`. See `docs/STYLE_GUIDE.md`.
- **Order semantics by engine:** reversion/vector use Alpaca bracket orders (TP + SL together); momentum uses day-market orders only (no per-name stops between monthly rebalances; risk via diversification + rotation); sentinel uses day-market batch orders for the defensive ETF basket (no per-name stops, lifecycle-driven exits).
- **Engine-build compliance shortlist** (the recurring gaps — full detail in `.claude/rules/engine-build.md`):
  - Every plug subclasses `BaseEnginePlug` with `validate_dependencies` + `healthcheck`.
  - Backtest calls `write_credibility_score` (EXCEPT `canary`, spec §4b).
  - Scheduler checks `tpcore.calendar.is_trading_day()` and returns early on non-trading days.
  - AAR uses `tpcore.aar.classify_exit_reason` (no hardcoded `ExitReason` literals).
  - `setup_detection` populates `tpcore.backtest.filter_diagnostics.FilterDiagnostics`.
  - Stale-order cancel via shared `tpcore.order_management.stale_order_cancel`.
  - Scheduler `await db_log.startup()` after `try:` + `await db_log.shutdown(...)` in `finally:`.
  - Register required tickers in `CRITICAL_TICKERS` (`tpcore/quality/validation/checks/prices_daily_freshness.py`).
  - Smoke loop + `run_all_engines.sh` + `ops/platform_pipeline.py` docstrings are **sentinel-fenced** (regenerated by `scripts/gen_engine_manifest.py`; do NOT hand-edit inside a fence).
- **Hard safety invariant:** `DATA_OPERATIONS_COMPLETE` is NEVER emitted unless self-heal returns 100% green ("100% data or don't trade", structural).
- **`prices_daily_completeness`** is the ungameable zero-tolerance invariant (every liquid currently-trading common stock has a bar for every NYSE session in the recent 30-session window within the ticker's active range — ANY miss fails).
- **Public repo as of 2026-05-21.** `gitleaks` runs on every PR (`.github/workflows/secret-scan.yml`) — any committed secret (API key, SSH/RSA key, Postgres URL with creds) fails the gate. The `.gitleaks.toml` allowlist documents legitimate placeholders (`u:p@h/d` test strings, the operator's public repo identifier). Pre-commit hook in `.pre-commit-config.yaml` for local-side protection. Baseline audit at `docs/audits/2026-05-21-public-repo-secret-audit.md`. **Never paste an API key or credential into a tracked file** — only `.env` (gitignored) holds secrets.

## Pointers (load on demand)

- **`docs/DEV_PIPELINE_STANDARD.md`** — the three lanes (fast / default / heavy), the standing discipline rules (incl. `git stash` ban, `gh pr checks` not `gh run watch`, whole-suite + order-flip authoritative gate, ops-package-shadow `xdist_group("ops_shadow")` discipline), and the lean-integration accelerator-vs-gate. Heavy-lane triggers enumerated in §0.
- **`.claude/rules/`** — path-scoped invariants (auto-load on matching path edits): `heavy-lane`, `engine-build`, `data-adapter`, `risk-path`, `selfheal-auditheal`, `migrations`, `daemons`, `engine-roster`, `data-feed-roster`, `dashboard`, `tests-and-ci`. The `llm-triage` rule was REMOVED 2026-05-22 alongside the deleted LLM-triage stack. Sentinel: `tests/test_claude_rules_present.py`.
- **`.claude/skills/`** — invocable wrappers: `/engine-readiness`, `/adapter-readiness` (model-invocable, auto-trigger on engine/adapter work), `/lab-target-run`, `/ecr`, `/dfcr`, `/audit-data-pipeline`, `/run-data-ops`, `/weekly-digest`, `/defect-register` (slash-only). Sentinel: `tests/test_claude_skills_present.py`.
- **`.claude/agents/`** — named subagent profiles: `spec-reviewer`, `code-quality-reviewer`, `engine-implementer`, `adapter-implementer`, `lab-target-runner`. Sentinel: `tests/test_claude_agents_present.py`.
- **`.claude/hooks/`** + `.claude/settings.json` — enforcement guarantees: `git checkout` block, subset-pytest-when-ops block, ECR/DFCR-gated `_PROFILE`/`providers.py` edits, risk-path reminder, session-start summary. Sentinel: `tests/test_claude_hooks_present.py`.
- **Canonical phrase triggers** (operator → action):
  - *"audit data pipeline" / "audit pipeline" / "run pipeline audit"* → `/audit-data-pipeline` skill (`scripts/run_audit_data_pipeline.sh`). Do NOT re-audit manually.
- **Defect register:** `ops/defect_register.py` (derived read-model; `/defect-register` skill). TODO.md `[defect_ref: X]` rows must have a matching open `REVIEW_DEFECT_LOGGED` (CI forcing-test).
- **Weekly digest:** `ops/weekly_digest.py` (`/weekly-digest` skill). Non-skippable state-comprehension floor; ≥2 unacked weeks ⇒ `live_clearance` auto-de-escalates live trading.
- **Engine roster changes** → `/ecr` skill (`docs/superpowers/checklists/engine_change_request.md` + `python -m ops.engine_sdlc --ecr <file>`); NEVER hand-edit `_PROFILE` (the hook blocks it).
- **Data-feed roster changes** → `/dfcr` skill (`docs/superpowers/checklists/data_feed_change_request.md`); NEVER hand-edit `tpcore/providers.py` (the hook blocks it).
- **Engine readiness** (10 non-optional sections) → `docs/superpowers/checklists/engine_readiness.md` / `/engine-readiness` skill.
- **Adapter readiness** (6-stage contract) → `docs/superpowers/checklists/adapter_readiness.md` / `/adapter-readiness` skill.
- **Lab candidate readiness** (feature-flag-variant; single pre-registered hypothesis; n_trials-ledger acknowledgement) → `docs/superpowers/checklists/lab_candidate_readiness.md`.
- **Escalation & Hardening Ladders:** `docs/ESCALATION_HARDENING_LADDER.md` (data lane), `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (engine lane). Every escalation class has a disposition; clockwork-enforced.

## Parallel sessions = worktrees (per `code.claude.com/docs/en/worktrees`)

Two-session work (one Claude window per task) is the lock-in. Each session gets its own worktree under `.claude/worktrees/<name>/`. New session: `claude --worktree <name>`. Mid-session: `EnterWorktree`. Background implementer subagents auto-isolate via `worktree.bgIsolation: "worktree"` in `.claude/settings.json` + `isolation: worktree` on the engine/adapter implementer profiles. `.worktreeinclude` carries `.env` into each new worktree so DB-touching work has credentials.

**The shared `/Users/michael/short-term-trading-engine/` working tree belongs to whichever parallel session is currently using it.** Never `cd`, `git switch`, `git pull`, or otherwise mutate working-tree state in the bare repo path — that's the OTHER session's checkout. Both subagent dispatches AND your own git/file operations must happen in a dedicated worktree. Read-only inspections that don't touch the working tree (`gh pr`, `git log`, `git show <ref>:<path>`) are safe anywhere because they read from the `.git` object store.

**Cleanup is mandatory, not optional.** When a worktree's PR merges (the task is done), remove the worktree the same turn: from inside the worktree session, `ExitWorktree action: "remove"`; from another session or the main checkout, `git worktree remove .claude/worktrees/<name>` + `git branch -d <branch>`. On session close, Claude prompts keep/remove if the worktree has changes — don't accumulate stale worktrees, the next session's `EnterWorktree` should start from a clean roster.

**Subagent worktrees: same-turn cleanup from the parent.** Background subagents dispatched with `isolation: "worktree"` cannot call `ExitWorktree` themselves — that's a parent-only tool. **The moment a subagent's task notification reports its PR is merged, the parent session MUST run `git worktree remove -f -f .claude/worktrees/<agent-worktree>` + `git branch -D <branch>` the same turn.** Do NOT defer to "the harness will GC eventually" — that leaves locked dirs the operator has to delete manually, which violates the no-direct-deletion principle (engine retirement goes through the SDLC; worktree retirement goes through this convention).

## Work-tracking source of truth

- **`TODO.md`** (git-tracked) is the canonical task list. **ALWAYS consult it before any "what's next" decision** — never drive next-work choices from memory alone. Memory entries describe rationale and constraints; task state lives in TODO.md.
- **`docs/MASTER_PLAN.md §9 Build Order`** is the sequenced rollout plan.
- The `.claude/hooks/session-start.sh` SessionStart hook auto-extracts open TODO.md H2 sections and injects them at session open — that summary is the trustworthy in-context view; consult `TODO.md` directly for sub-item detail.

## Memory boundary (C0.1, 2026-06-01)
- **Memory is context, not source of truth.** Code, tests, schemas, migrations, and `docs/**` override every memory tier.
- **Four-tier boundary** (`CLAUDE.md` → local `MEMORY.md` → Anthropic API memstores → repo docs/tests/hooks): full model + forbidden-content list in `docs/MEMSTORE_HANDOFF.md`. Sentinel `tests/test_memory_boundary_present.py` reds CI if the policy goes missing; `tests/test_memory_index_size.py` reds CI if a tracked `MEMORY.md` exceeds 24 400 bytes.
- **If a rule must be enforced, it belongs in tests / hooks / CI** — never memory.

## Style + glossary
- Read `docs/STYLE_GUIDE.md` before writing any code.
- Read `docs/glossary.md` if present.
