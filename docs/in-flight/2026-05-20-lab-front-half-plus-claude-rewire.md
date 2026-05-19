# Session checkpoint — Lab front-half + Claude-Code extension rewire

**Date:** 2026-05-20 (session paused at this point, fresh session to resume).

## What was the session doing

Single-session execution of two interleaved programs: (1) the **Lab front-half epic** (SP-A pre-existing; SP-B through SP-F shipped this session — roster-driven Lab targeting, Lab Candidate Readiness checklist, pluggable per-engine scoring, Sentinel validation case, Catalyst new-engine via SDLC ADD), and (2) the **Claude-Code extension-surface rewire** (5-PR program landing path-scoped rules, invocable skills, named subagent profiles, enforcement hooks, slim CLAUDE.md). Plus the operator's `DEV_PIPELINE_STANDARD.md` 3-lane refactor (fast/default/heavy) and the `docs/MEMORY_MAINTENANCE.md` Claude-spec structural-checks update. The session pivoted mid-flight to lean cadence after operator feedback ("~10% coding, too much review spiral"); the new lane standard + the lean cadence memo (`feedback_cut_process_overhead_ship`) is now the durable default.

## Pending list as of this moment (priority order)

| # | Item | Status | Next action |
|---|------|--------|-------------|
| 1 | **PR #144 — docs(memory-audit): add Claude-spec structural checks** | in-progress | Just pushed the reviewer-flagged backtick fix (commit `400f54a`). Wait for CI conclusion==SUCCESS on `pytest + ruff + check_imports` (`gh pr view 144 --json statusCheckRollup`); then `gh pr merge 144 --squash --delete-branch`. ONE review already passed (approved pending the now-shipped fix). |
| 2 | **Operator-triggered memory audit** (`clean up your memories`) | blocked-on-#1 | Run AFTER PR144 merges, per operator standing rule ("operator will trigger that separately"). The updated procedure in `docs/MEMORY_MAINTENANCE.md` is what to follow (structural checks → repo-shadow deletion → MEMORY.md mechanical invariant). |
| 3 | **Throughput close-out** (operator's DONE criterion #5 for the 5-PR rewire) | not-started | Run a representative small fix (single-file engine plug tweak, fast lane) and a representative heavy change (new adapter, heavy lane) under the new mechanism; report wall-clock + token deltas. Qualitative deltas captured in PR #143's body; empirical follow-up deferred per the operator's "stop after the memory audit" directive. |
| 4 | **Lab front-half SP-G** (#242 thin advisory LLM spec-emitter) | not-started | Operator decided 2026-05-20: **thin emitter now + autonomous quant edge finder as a follow-on epic** (task #25). SP-G is gated on SP-A–SP-F (all shipped). Resume: brainstorm-light → spec → plan → subagent build per the lane standard; see `[[research-llm-edge-discovery]]` for the autonomous-quant ambition that the operator wants surfaced again at SP-G's design point. |
| 5 | **Master step 4 — engine-lane follow-ups** | not-started | #148 ntrials-ledger eviction, `lab-isolation-db` CI job (5 DB-gated suites skip), `[[momentum-aar-plug-finding]]`, 13 orphan scripts, `DBLogHandler.run_id` public accessor (repoint `scripts/ops.py:1939/2108/3350`), the pre-existing `test_lab_ntrials_ledger.py` ↔ oracle `test_amain_*` cross-file isolation fragility found in SP-B T4 review. |
| 6 | **Master step 4b — engine improvements** | not-started | Edge-config Lab candidates (subsumed by Lab front-half flow) + future engines `s2/`, `catalyst/` (note: catalyst engine code shipped via #137 SP-F; the operator's ECR `python -m ops.engine_sdlc --ecr ecr_catalyst.txt` activation is the deferred step). |
| 7 | **Task #24 — build `carver` engine** (Carver Systematic Trading) | not-started | New engine via SDLC ADD/new_scaffold; design captured in task #24; refs `[[ref-carver-systematic-trading]]` + `[[ref-chan-algorithmic-trading]]` (standing cross-engine improvement toolkit). Sequenced at master step 4b unless operator says "prioritize carver". |
| 8 | **Task #25 — autonomous LLM+quant edge finder epic** | not-started | Dedicated follow-on epic after SP-G; operator-approved 2026-05-20. Standalone brainstorm→spec→plan→build. |
| 9 | **Master step 5 — #189 dashboard refactor** | not-started (DEAD LAST) | Input = `design_handoff_trading_console/` (Claude-design handoff, untracked locally — note in step 10 below). Full brainstorm→spec→plan→build per the lane standard (UX rewrite). |
| 10 | **Master step 6 — #252 docs-to-reality reconciliation** | not-started (FINAL) | Reconcile CLAUDE.md / specs / docs / TODO / memory vs everything shipped. CLAUDE.md just slimmed to 53 NBNC lines in PR #143; the durable `tests/test_claudemd_under_200_lines.py` enforces the cap. |

## What's already committed (merged + open + branches)

### Merged this session (in order)
- `477b380` `refactor(lean-p5.5c)` — momentum assert_can_graduate dedup (P5 epic 7/7 closed) — PR #127
- `a9af35c` `docs(spec2-pillar-a)` — Agent-Teams adoption decision: SKIP single-session — PR #128
- `e1eefac` `docs(lab-sp-b)` — SP-B hardened design spec — PR #129
- `d67529d` `docs(lab-sp-b)` — SP-B implementation plan — PR #130
- `bcbd98a` `feat(lab-sp-b)` — roster-driven plug-and-play Lab targeting — PR #131
- `1724dbf` `feat(lab-sp-c)` — Lab Candidate Readiness checklist — PR #132
- `4bf4557` `docs(lab-sp-d)` — SP-D hardened design spec — PR #133
- `d5795cc` `docs(lab-sp-d)` — SP-D implementation plan — PR #134
- `0cb64b0` `feat(lab-sp-d)` — pluggable per-engine success scoring + richer dossier — PR #135
- `64975f7` `feat(lab-sp-e)` — Sentinel validation case (MAXDD_REDUCTION non-Sharpe Lab target) — PR #136
- `1236a2a` `feat(lab-sp-f)` — Catalyst engine scaffold + ECR-ADD prepared — PR #137
- `9d30371` `docs(dev-pipeline)` — scope-gated fast lane + single-review default — PR #138
- `537ff67` `feat(claude-rules)` — 11 path-scoped invariants in `.claude/rules/` — PR #139
- `9d298b5` `feat(claude-skills)` — 9 invocable wrappers in `.claude/skills/` — PR #140
- `2b69a44` `feat(claude-agents)` — 5 named subagent profiles — PR #141
- `e02e9eb` `feat(claude-hooks)` — 5 enforcement hooks + project `settings.json` — PR #142
- `377c516` `docs(claude-md)` — slim CLAUDE.md + durable ≤200-line test — PR #143

### Open PRs
- **PR #144** `docs(memory-audit): add Claude-spec structural checks` — branch `docs/memory-audit-spec-checks`, NOT draft, mergeable, CI re-running after the backtick fix (`400f54a`).

### Pushed branches (other than `main`)
- `docs/memory-audit-spec-checks` (PR #144 head)
- A handful of older local branches from prior sessions: `backlog-deep-research-spike`, `chore-todo-handoff-sync`, `feat/daily-bars-repair`, `fix-allocator-sample-stdev`, `fix-asyncpg-pooler-safety`, `fix-credibility-rubric-dsr-threshold`, `fix-engine-llm-triage-async-anthropic`, `hotfix-sp3-scope-gate-skip`, `lab-candidates-rollthrough` (Vector pilot, the SP-C reference, **not yet merged** — `lab_candidate_readiness.md` cites it with a post-merge-repoint follow-up).

## Anything in-flight that ISN'T in git yet

- `design_handoff_trading_console/` — untracked, pre-existing from prior session. The input for #189 dashboard refactor (master step 5, DEAD LAST). **Not discarded**; left in place for the future #189 cycle. No changes proposed this session.
- `docs/MEMORY_MAINTENANCE.md` — WAS untracked before this session; PR #144 now adds it to git history with the structural-checks edits. Disclosed transparently in #144's body.
- No stashed work. `git stash list` empty (operator's no-stash rule).
- No scratch notes outside of the in-flight checkpoint doc itself.

## Doc + memory references this session loaded (re-load on resume)

### Docs (in repo)
- `docs/DEV_PIPELINE_STANDARD.md` — the three lanes (fast/default/heavy). Just shipped + extended by PR #138.
- `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md` — Lab front-half epic (SP-A…SP-G).
- `docs/superpowers/specs/2026-05-19-lab-sp-b-roster-driven-targeting-design.md` (PR #129)
- `docs/superpowers/specs/2026-05-20-lab-sp-d-pluggable-scoring-design.md` (PR #133)
- `docs/superpowers/specs/2026-05-20-sentinel-maxdd-lab-candidate.md` (PR #136)
- `docs/superpowers/checklists/engine_readiness.md` (10 non-optional sections; `/engine-readiness` skill loads it)
- `docs/superpowers/checklists/lab_candidate_readiness.md` (Lab-candidate gate; PR #132)
- `docs/superpowers/checklists/engine_change_request.md` (ECR structured touchpoint; `/ecr` skill)
- `docs/superpowers/checklists/data_feed_change_request.md` (DFCR; `/dfcr` skill)
- `docs/superpowers/pipelines/data_adapter_pipeline.md` (6-stage contract)
- `docs/MEMORY_MAINTENANCE.md` (now in git via PR #144)
- `docs/ESCALATION_HARDENING_LADDER.md` + `docs/ENGINE_ESCALATION_HARDENING_LADDER.md`
- `CLAUDE.md` (slim form, PR #143)
- `.claude/rules/` (12 rules), `.claude/skills/` (9 skills), `.claude/agents/` (5 profiles), `.claude/hooks/` (5 hooks) + `.claude/settings.json`

### Memory (`~/.claude/projects/-Users-michael-short-term-trading-engine/memory/`)
- `MEMORY.md` (the durable index — load first)
- `project_master_remaining_program.md` — **the authoritative full remaining work sequence**; STATUS block updated through SP-E (SP-F update pending in the next session if not done before the checkpoint).
- `project_lab_front_half_epic.md` — SP-A/SP-A2 status; resume notes for SP-G.
- `project_research_llm_edge_discovery.md` — #242/SP-G; the operator's 2026-05-20 autonomous-quant ambition recorded here for surfacing at SP-G design point.
- `feedback_cut_process_overhead_ship.md` — 2026-05-20 lean-cadence operating contract; overrides the heavy uniform pipeline.
- `feedback_ci_gate_on_check_conclusion.md` — wait on `statusCheckRollup` conclusion, never `mergeStateStatus`.
- `ref_carver_systematic_trading.md` + `ref_chan_algorithmic_trading.md` — cross-engine improvement reference toolkit; ambition framing for SP-G.

### Authoritative external (operator's "authoritative docs override CLAUDE.md")
- <https://code.claude.com/docs/en/extend> (extension layers)
- <https://code.claude.com/docs/en/memory> (CLAUDE.md guidance — keep short)
- <https://code.claude.com/docs/en/skills> (skills loading)
- <https://code.claude.com/docs/en/sub-agents> (agent profiles)
- <https://code.claude.com/docs/en/hooks-guide> (hook events)
- <https://code.claude.com/docs/en/best-practices> (Explore→Plan→Implement→Commit; one-sentence-diff → skip the plan)

## Where the session ended (one-line summary)

5-PR Claude-Code extension-surface rewire merged (PRs #138–#143); SP-A→SP-F of the Lab front-half merged; PR #144 (memory-audit doc) open with CI re-running on the reviewer-flagged backtick fix — fresh session: merge #144 on green, then per operator standing rule run `clean up your memories` to apply the new structural checks, then proceed to SP-G.

## Resume command (for the operator)

```bash
claude --resume        # interactive picker — choose this session from the list
# or
claude -c              # continue the most recent session
```
