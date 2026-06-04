# MEMORY.md — index for /Users/michael/short-term-trading-engine

**Session-start trigger:** if the operator's first message is `open session` (case-insensitive), execute `memstore_01P5DiJJgau4NhMMekaZDQEN:/agent-context/open-session-protocol.md` — 6 steps, no work before the report.

Memory boundary + size discipline: `docs/MEMSTORE_HANDOFF.md` (C0.1, 2026-06-01).
Memory is context, not source of truth — code/tests/migrations/schemas/`docs/**` dispositive.
Cleanup procedure: `docs/MEMORY_MAINTENANCE.md`. Sentinels: `tests/test_memory_index_size.py` (≤ 24 400 bytes), `tests/test_memory_boundary_present.py`.

## Current project status (2026-06-01)

- PRs shipped 2026-06-01: #408 F0 parity gate · #409 F1 failed-alpha ledger · #410 greeks_max_pain retirement · #411 H0 canonical path registry + drift sentinels · #412 C0.1 memory boundary + size sentinel.
- Phase: **C0 Claude hardening**. C0.1 SHIPPED; C0.2 (this prune) IN PROGRESS.
- Mode: single-session (Carver session closed per [project_single_session_until_db_done](project_single_session_until_db_done.md)).
- Always consult `TODO.md` before "what's next" decisions — memory is context, not task state.

## Canonical source-of-truth pointers

- Path registry (H0): `.claude/path_registry.yaml` + `scripts/check_manifests.py` + `tests/test_path_registry_present.py`.
- Memory boundary (C0.1): `docs/MEMSTORE_HANDOFF.md` + `docs/MEMORY_MAINTENANCE.md` + `tests/test_memory_boundary_present.py` + `tests/test_memory_index_size.py`.
- Failed-alpha ledger (F1): `platform.failed_alpha_ledger` migration `20260601_0100` + `tpcore/forensics/alpha_ledger.py` (PR #409).
- Parity gate (F0): `tpcore/parity/data_parity.py` + `ops/cutover_agent.py` + `scripts/ops.py` `evaluate_provider_parity` stage (PR #408).
- Claude review workflow: `.github/workflows/claude-review-heavy-lane.yml` (path filter = registry `heavy_lane ∪ claude_system`).
- Cross-session memstores: see `docs/MEMSTORE_HANDOFF.md` §6 + [reference_anthropic_memstores](reference_anthropic_memstores.md).

## Current backlog

- **C0.2 local Claude memory prune** — IN PROGRESS (this pass).
- **C0.3+** — remaining C0 Claude hardening items per operator queue.
- **F2 / F3** — refint follow-ups per operator sequence.
- **D0** — packetvoid-dev-system extraction (deferred).
- Refint follow-ups: P1b CIK long-tail · 8-K Item 3.01 extractor · metadata coverage gate — see [project_refint_arc_state_2026_05_31](project_refint_arc_state_2026_05_31.md).
- Engine defect tracked-not-auto-fix: [momentum AAR/lifecycle plugs not instantiated](project_momentum_aar_plug_finding.md).

## Operating rules (standing)

Persona + identity:
- [Research Builder Hat v2.1](feedback_research_builder_persona.md) — 11 stop-rules; pre-commit gate.
- [Operating identity — Connor rule](feedback_operating_identity_for_this_system.md) — failure-derived lens; stay on reservation.
- [Authoritative docs override CLAUDE.md](feedback_authoritative_docs_override_claudemd.md) — Python/Claude/Railway/Supabase docs win on technical conflicts.
- [ISO/industry standards over custom](feedback_always_use_iso_standards.md) — identifiers, codes, protocols.

Process + discipline:
- [Cut process overhead — ship](feedback_cut_process_overhead_ship.md) — ONE review/task default.
- [No shortcuts; 100% verified](feedback_no_shortcuts_100_pct.md) — no chained pipes masking failures.
- [Ask expert THEN execute](feedback_ask_expert_then_execute.md) — every tech-choice via subagent first.
- [Authorization via expert verdict](feedback_authorization_via_expert_keep_moving.md) — operator reserved for scope/priority/blockers.
- [Stop over-asking; delegate to expert](feedback_stop_over_asking_use_expert.md) — kill per-gate AskUser cadence.
- [Keep building; don't pause](feedback_keep_building_dont_pause_for_breaks.md) — drive to completion or true blocker.
- [Visible progress around subagents](feedback_visible_progress_not_opaque_subagents.md) — crisp status.
- [Workflow style](feedback_workflow_style.md) — pivot reporting; no premature A/B/C options.
- [I do that too](feedback_i_do_that_too_not_operator_action.md) — DFCR/migrations/live-DB are mine when creds in .env.
- [Apply own documented constraints to next thing built](feedback_apply_my_own_documented_constraints.md).
- [Verify expert DROP verdicts in codebase FIRST](feedback_verify_expert_verdict_in_codebase_first.md).
- [Sanitize operator cursing in logs/commits/PRs](feedback_sanitize_operator_cursing_in_logs.md).
- [Always subagent-driven execution](feedback_always_subagent_driven.md).

Git / PR / CI:
- [Run gates LOCALLY on commit; push after major completion](feedback_run_gates_locally_on_commit.md).
- [Check CI within 60s of every push](feedback_check_ci_after_every_push.md).
- [No mid-flight direct `gh pr checks` calls — wait for the background poll](feedback_gh_pr_checks_no_midflight_direct_calls.md) — exit 8 ≡ pending; noisy in UI; the poll loop greps text instead.
- [Stop burning GitHub with per-task PRs](feedback_stop_burning_github_with_per_task_prs.md) — batch related work.
- [Push when tangible; batch related changes](feedback_push_when_tangible_batch_prs.md).
- [Local/subset pytest green ≠ CI green](feedback_ops_package_shadow_full_suite_gate.md) — whole-suite + order-flip authoritative.
- [Pytest hook block = run whole suite](feedback_pytest_hook_block_run_whole_suite_dont_retry.md).
- [Multi-session = PR workflow](feedback_multi_session_github_flow_resumed.md) — supersedes single-session direct-to-main while 2 sessions active.
- [Direct-to-main during single-session](feedback_single_session_commit_to_main.md) — current mode per project_single_session_until_db_done.
- [Never touch shared main checkout](feedback_never_touch_shared_main_checkout.md) — worktree only when 2 sessions.
- [Worktree isolation ≠ shared infra](feedback_worktree_isolation_doesnt_cover_shared_infra.md) — live DB / .venv / FMP / Alembic shared.
- [Git workflow: commit-often, push-batched, check-CI](feedback_git_workflow_commit_push_ci.md).
- [Rebase on origin/main BEFORE substantive branch work](feedback_rebase_on_main_before_branch_work.md) — stale local main caused PR #470 audit to research against state 22 commits behind; required full redo.
- [Claude Review = advisory, not for routine PRs](feedback_claude_review_only_high_risk.md) — billing failures are non-dispositive; reserve credit for security/architecture/destructive-data PRs.

Engineering practice:
- [Always use tpcore for shared concerns](feedback_tpcore_reuse.md).
- [Symmetry, not copy — engine↔data](feedback_symmetry_not_copy.md).
- [Event-driven on application_log bus, not scheduled](feedback_event_driven_not_scheduled.md).
- [Use official docs, not assumed knowledge](feedback_use_official_docs.md).
- [Investigate findings; don't hand-wave](feedback_investigate_dont_hand_wave_findings.md).
- [Stream long-running output](feedback_stream_long_running_output.md).
- [Wrap multi-flag commands in scripts/](feedback_always_use_wrapper_scripts.md).
- [Python 3.11 f-strings can't contain backslashes](feedback_python_fstring_no_backslashes.md).

Data / ETL:
- [Bulk file BEFORE API crawl — REINFORCED](feedback_bulk_before_api_crawl_REINFORCED.md) — SEC submissions.zip; FMP /historical-price-eod/full.
- [ETL: bulk-file precedent](feedback_etl_bulk_before_api_crawl.md) — sibling of REINFORCED entry.
- [Daily data update runs FIRST](feedback_data_update_first.md).
- [NEVER Alpaca for daily prices backfill](feedback_no_alpaca_for_daily_prices_backfill.md) — FMP primary; Tradier secondary.
- [SEC primary for INSIDER lane US; FMP fallback non-US](feedback_sec_authoritative_fmp_fallback_non_us.md).
- [No lazy vendor-blame](feedback_no_lazy_vendor_blame.md).
- [DB is research substrate first; engine inputs second](feedback_db_is_substrate_not_engine_inputs.md).
- [Self-heal end-to-end autonomous](feedback_self_heal_autonomous_no_operator_task.md).
- [Engines session DB scope = VIEWS ONLY](feedback_other_session_db_views_only.md).
- [OPS-table changes require FULL system rewiring](feedback_ops_table_changes_require_system_rewiring.md).
- [Coverage-gate denominator = engine universe](feedback_coverage_gate_denominator_matches_engine_universe.md).
- [Autonomous Lab criteria supersedes absolute DSR/cred gate](feedback_autonomous_lab_criteria_replaces_absolute_gate.md).
- [Heavy Lab probes need final-holdout chunking](feedback_lab_heavy_probe_needs_chunking.md).
- [Anthropic 529 transient — retry with long backoff](feedback_anthropic_529_self_heal.md).

Public/console dashboard:
- [Dashboard does the work, NOT the user](feedback_dashboard_does_the_work_not_the_user.md).
- [Fact-check operator claims too](feedback_fact_check_operator_claims.md).
- [No judgment headlines](feedback_no_judgment_headlines_on_public_dashboards.md).
- [DIMENSION_SCOPE_LIMIT — composite-score scope gaps](feedback_dimension_scope_limit_when_composite_partial.md).
- [NextAuth middleware public-route exclude](reference_nextauth_public_route_pattern.md) — add new slugs to negative-lookahead.

Memory:
- [Memory cleanup command trigger](feedback_memory_cleanup_command.md) — "clean up your memories".

## Project state (durable context)

- **[ACTIVE ARC — Data-layer REBUILD 2026-06-03](project_data_layer_rebuild_arc.md)** — clean re-ingest, source-rebuilt universe, identity-first; audit→spec→plan; behind the 7 moratoria. **On ANY DB work read `docs/DATABASE_AND_DATAFLOW.md` §0 index FIRST** ([reference_data_layer_index](reference_data_layer_index.md)).
- [Tradier CLOSED — no options](project_tradier_closed_no_options.md) — decommission adapter, drop tradier_options_chains, FMP/Alpaca prices only.
- [Refint arc P0→P2c SHIPPED 2026-05-30/31](project_refint_arc_state_2026_05_31.md) — TODO.md canonical work log.
- [LWA dashboards standardized 2026-05-29](project_lwa_report_standardization_2026_05_29.md) — LWA-23 + LWA-25 19-section.
- [R2 archive substrate live 2026-05-26](project_r2_archive_substrate_2026_05_26.md) — Railway+Supabase+R2+Vercel cloud stack.
- [LLM lab/finder/monitor RETIRED 2026-05-25](project_llm_lab_finder_monitor_retired_2026_05_25.md) — AAR critic only LLM caller left.
- [Single-session mode until DB v2.1 done](project_single_session_until_db_done.md) — current mode.
- [TKR-14 smart-key PK for ticker_classifications (v2.2)](project_figi_primary_stable_id.md).
- [Database architecture state 2026-05-23](project_database_architecture_state_2026_05_23.md) — no FKs yet (real defect).
- [Engine inventory 2026-05-23](project_engine_inventory_2026_05_23.md) — 7 production engines; sentinel inverse-only.
- [Three-service architecture](project_three_service_architecture.md) — data/engine/aar + platform overlays.
- [Macro consumer audit 2026-05-23](project_macro_consumer_audit_2026_05_23.md) — vector/reversion/catalyst all consume.
- [Publishing intent stelib](project_publishing_intent_stelib.md) — outbound; zero internal callers by design.
- [SACRED: hy_spread series — never re-derive](project_hy_spread_sacred.md).
- [Supabase constraints + chunked-DML mandate](project_supabase_constraints_2026_05_23.md).
- [Master remaining program + sequence](project_master_remaining_program.md).
- [Finder first edge signal 2026-05-22](project_finder_first_edge_signal.md) — catalyst PEAD candidate.
- [Canary = end-to-end heartbeat, not hypothesis host](project_canary_is_end_to_end_heartbeat.md).
- [PCA-residual FALSIFIED 2026-05-21](project_pca_residual_falsified_2026_05_21.md).
- [ENGINE_TABLES → EngineProfile.data_dependencies migration done](project_engine_tables_sot_migration.md).
- [Deterministic-cascade architecture](project_deterministic_cascade_architecture.md).
- [Railway archive substrate migration design LOCKED](project_railway_archive_substrate_migration.md).
- [Railway paused 2026-05-12; local Mac active](project_railway_hobby_tier.md).
- [UTC-everything; operator currently SF (was Manila)](project_manila_utc_everything.md).
- [ML research track — expert verdict; not now](project_ml_research_track.md).
- [Research-LLM edge-discovery #242](project_research_llm_edge_discovery.md) — gated on Lab front-half.
- [Supabase Pro tier $25/mo 8GB](project_supabase_pro_tier.md).
- [FMP primary daily-bars feed 2026-05-22](project_fmp_primary_daily_bars_2026_05_22.md) — $200/yr Starter; per-ticker calls.

## References

- [Data-layer START-HERE index](reference_data_layer_index.md) — `docs/DATABASE_AND_DATAFLOW.md` §0; read FIRST on any DB/ingest/validation/identity/repair task.
- [Two Anthropic memstores in use](reference_anthropic_memstores.md) — dev + finder; full curl pattern; see `docs/MEMSTORE_HANDOFF.md` §6.
- [NextAuth middleware public-route pattern](reference_nextauth_public_route_pattern.md).
- [Carver — Systematic Trading (2015)](ref_carver_systematic_trading.md).
- [Chan — Algorithmic Trading (2013)](ref_chan_algorithmic_trading.md).

## Known local caveats (active)

- Pre-C0.2, this index was 26 649 bytes — over the C0.1 ceiling. This pass is the first remediation under the new policy.
- ~~Provenance-guard tests fail full-suite-only when `DATABASE_URL` leaks into the test process.~~ **Fixed PR #465 (2026-06-04)** — consumer-side defense (placeholder URL substring detection + try/except OSError with 2s timeout); 8 failures became 8 skips, whole-suite is now 3534 passed / 0 failed.
- TODO.md is the canonical work tracker. Consult before any "what's next" decision; never drive next-work from memory alone.
