---
name: project_data_layer_rebuild_arc
description: "Active arc — full data-layer REBUILD via audit→spec→plan→implement. SPEC APPROVED v1.4 (2026-06-04); moratoria LIFTED; Plan 1 (4) authored. Clean re-ingest, source-rebuilt universe, identity-first."
metadata: 
  node_type: memory
  type: project
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

Operator directed (2026-06-03) a **full data-layer rebuild**, done through the heavy-lane pipeline — **audit → spec → plan → implement**, NOT ad-hoc. Decisions locked:

- **Clean re-ingest** (not preserve-in-place): new clean schema, re-pull all data from source.
- **Universe rebuilt from source** (SEC full company list + FMP symbol/delisting history), survivorship-free.
- **Identity-first** build order: `ticker_classifications` + `ticker_history` built correct FIRST, then prices/fundamentals on top so the **14** SCD-2 `BEFORE INSERT` triggers (rebuild count: 15 live − options_max_pain dropped) attribute `classification_id` correctly (no re-attribution debt).
- **Target ~20 tables** (from identity audit §3 KEEP/MERGE/DROP); drop the ~7 empty speculative sidecars; merge corporate_actions/lifecycle→corporate_events, evidence/ledger/parity/forensics→data_quality_log.
- **Supabase mechanics:** `TRUNCATE` (immediate disk reclaim, no bloat) + session-mode `:5432` for DDL/COPY + `COPY`-from-CSV bulk load + drop/recreate indexes + ANALYZE.
- Tradier/options OUT (see [[project_tradier_closed_no_options]]).

**STATUS 2026-06-04: SPEC APPROVED → IMPLEMENT phase.** Spec `docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md` reached **v1.4, operator-approved 2026-06-04**; the 7 moratoria are **LIFTED** for the implementation plan + its gated execution. Spec history: finance+db SME review (v1.1), key-consistency re-review (v1.2 — child tables KEEP natural PK + nullable TKR-14 FK, NOT identity-PK), ops-layer expansion (v1.3 — per-table PRESERVE/RESET disposition; aar_events FK gap; data_quality_log fold narrowed so failed_alpha_ledger + ingest_quarantine stay standalone), sign-off review (v1.4 — reconciled 3 layered-edit contradictions; counts corrected: triggers 15→14, sentinel 100%→72.8%, failed_alpha 28→30). Plan **decomposed into 4 sequenced plans** (`docs/superpowers/plans/2026-06-04-data-layer-rebuild-*.md`): **Plan 1 (identity-predicate half-open fix in 14 triggers + dispatcher + resolver, + aar_events FK — NON-destructive, ships first) AUTHORED**; Plan 2 clean-schema cutover (the wipe); Plan 3 identity-first re-ingest; Plan 4 validation green-gate + DATA_OPERATIONS_COMPLETE + doc refresh. Execution-time: pause engine/lane/trade-monitor + the cleared data-operations cron ([[reference_railway_access]]); finish before momentum's next monthly rebalance (~late June 2026 — momentum is LIVE, ~160 paper positions). The agent must consult [[reference_data_layer_index]] (DATABASE_AND_DATAFLOW.md §0) on every data task and keep that doc current.

**Why:** substrate polluted (read-side `as_of` bypass in 20/20 engine readers; ~92k stale attributions) + schema sprawl (49 tables, ~half empty); operator judged incremental repair insufficient.

**How to apply:** spec (approved) → 4 plans → execute Plan N task-by-task via superpowers:subagent-driven-development ([[feedback_always_subagent_driven]]), each plan's whole-suite + order-flip gate authoritative; column-level target schema rewrites DATABASE_AND_DATAFLOW.md §2/§3; SWV+CIC gates before any code/DB change. Plan 1 is non-destructive (trigger/reader correctness + additive FK); Plans 2-3 are the destructive wipe+reingest (snapshot + Supabase PITR are the rollback). Supersedes the deferred-DB note in [[project_single_session_until_db_done]].
