---
name: project_data_layer_rebuild_arc
description: "Active arc (2026-06-03) — full data-layer REBUILD via audit→spec→plan→implement; clean re-ingest, source-rebuilt universe, identity-first; behind the 7 moratoria."
metadata: 
  node_type: memory
  type: project
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

Operator directed (2026-06-03) a **full data-layer rebuild**, done through the heavy-lane pipeline — **audit → spec → plan → implement**, NOT ad-hoc. Decisions locked:

- **Clean re-ingest** (not preserve-in-place): new clean schema, re-pull all data from source.
- **Universe rebuilt from source** (SEC full company list + FMP symbol/delisting history), survivorship-free.
- **Identity-first** build order: `ticker_classifications` + `ticker_history` built correct FIRST, then prices/fundamentals on top so the 15 SCD-2 `BEFORE INSERT` triggers attribute `classification_id` correctly (no re-attribution debt).
- **Target ~20 tables** (from identity audit §3 KEEP/MERGE/DROP); drop the ~7 empty speculative sidecars; merge corporate_actions/lifecycle→corporate_events, evidence/ledger/parity/forensics→data_quality_log.
- **Supabase mechanics:** `TRUNCATE` (immediate disk reclaim, no bloat) + session-mode `:5432` for DDL/COPY + `COPY`-from-CSV bulk load + drop/recreate indexes + ANALYZE.
- Tradier/options OUT (see [[project_tradier_closed_no_options]]).

Gated by the **7 moratoria** in `docs/audits/2026-06-03-identity-substrate-data-flow.md` §4 — **no DB mutation until the spec is approved**. The agent must consult [[reference_data_layer_index]] (DATABASE_AND_DATAFLOW.md §0) on every data task and keep that doc current.

**Why:** substrate polluted (read-side `as_of` bypass in 20/20 engine readers; ~92k stale attributions) + schema sprawl (49 tables, ~half empty); operator judged incremental repair insufficient.

**How to apply:** spec → `docs/superpowers/specs/<date>-data-layer-rebuild-design.md`; column-level target schema rewrites DATABASE_AND_DATAFLOW.md §2/§3; SWV+CIC gates before any code/DB change. Supersedes the deferred-DB note in [[project_single_session_until_db_done]].
