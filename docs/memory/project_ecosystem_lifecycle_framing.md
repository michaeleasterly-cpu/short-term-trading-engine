---
name: ecosystem-lifecycle-framing
description: "Operator 2026-05-23: 'everything has a lifecycle... data.. feeds.. engines... its an ecosystem of technology'. Every artifact in the platform has a birth → maintain → audit → retire cycle. Designs that don't acknowledge lifecycle produce orphans/dormant/drift/zombie state."
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Standing read for EVERY spec/plan that touches data, feeds, engines, or schema.**

Operator 2026-05-23: *"everything has a lifecycle... data.. feeds.. engines... its an ecosystem of technology"*.

## The ecosystem layers (each with its own lifecycle)

### Data layer

Every ROW in `platform.*` has a lifecycle:
- **Born**: ingest from vendor (FMP/SEC/Alpaca/FRED/...) OR producer compute (derived tables)
- **Maintained**: daily/weekly refresh; corp-action adjustments; vendor revisions
- **Audited**: validation suite (11 checks), shrinkage detection, FK integrity
- **Archived**: per-feed CSV-first archives (vendor-truth) + db_snapshots (DB-truth) + pg_dump (DR)
- **Retired**: delisted ticker rows stay (historical truth); orphan parent gets `status='delisted_historical'`

Lifecycle violations observed today:
- 335K prices_daily rows for 166 tickers with NO ticker_classifications parent (orphan birth state)
- 6,083 ticker_classifications rows for tickers with no prices_daily (Alpaca-listed-no-bars zombie state)
- 101 tickers deleted today from classifications without lifecycle log entry (untracked retire)

### Feed layer

Each FEED (in `tpcore/feeds/profile.py` registry) has a lifecycle:
- **Evaluate**: vendor cost, coverage, freshness probe (`data_provider_evaluate.md` checklist)
- **Onboard**: adapter + handler + validation check + CSV archive + dashboard panel + scheduler entry (6-stage adapter contract)
- **Run**: cadence-driven daily/weekly per FeedProfile + skip_guard
- **Audit**: validation check per feed; freshness gates; cross-feed integrity (auditheal)
- **Retire**: vendor closure (Tradier 2026-05-10), provider cutover (Alpaca→FMP primary 2026-05-22), deprecate-forward migration

Lifecycle violations observed:
- `tradier_options_chains` table dormant since Tradier closed — no retire-or-revive decision recorded until 2026-05-23 (Tradier API still works — operator chose KEEP+DORMANT)
- `options_max_pain` table abandoned (1 row); no retire decision documented
- `insider_filings` (FMP) created PR #296 + dropped PR #320 same day — wasn't lifecycle-tracked

### Engine layer

Codified in `tpcore/engine_profile.py::LifecycleState`:
- `LAB` → `PAPER` → `LIVE` → `RETIRED`
- Gate transitions: DSR ≥ 0.95 + credibility ≥ 60 (LAB→PAPER); separate paper-stability gate (PAPER→LIVE — reserved per paper-only mandate)
- Today: 6 PAPER (reversion, vector, momentum, sentinel, canary, catalyst), 1 LAB (carver), 1 RETIRED (sigma)
- ECR (Engine Change Request) process for promote/modify/retire

This is the ONE lifecycle the platform got right early. Others should mirror it.

### Schema layer

Tables, indexes, FKs, constraints, views, columns all have lifecycles:
- **Designed**: spec + plan (this v2 work)
- **Created**: Alembic migration with UP+DOWN
- **Enforced**: NOT VALID → VALIDATE → CHECK / NOT NULL / FK
- **Audited**: data_quality_log + cross_table audits
- **Evolved**: rename, column add/drop, constraint tightening
- **Retired**: drop migration; data preserved or migrated forward

Lifecycle violations observed:
- 20260522_0200 migration (drop_insider_filings_add_sec_mspr) applied to live DB but lived only in killed-subagent worktree until 2026-05-23 commit — Alembic-vs-DB drift
- `platform.sec_insider_transactions` rename (Phase 1) + view drop (Phase 2) was sequenced without DATABASE_AND_DATAFLOW updates landing simultaneously
- FK additions before producer parent_resolver pattern — schema enforced what producers couldn't yet uphold

### Code/script layer

Producers, scripts, runbooks, agents:
- **Onboarded**: orphan-scripts test enforces every script is referenced
- **Maintained**: code-review, ruff, pytest
- **Archived**: orphan-scripts sweep moves to `archive/`
- **Retired**: explicit removal + commit message

Lifecycle violations:
- Today's `scripts/refresh_tradier_options.py` was dormant infrastructure (no consumer); allowlist + wrapper pattern correctly handled the dormancy
- `scripts/backfill_country_from_fmp.py` was one-shot — no retire trigger documented

## How to apply

Every spec/plan section that adds an artifact (table, column, feed, engine, script, view) must include:
- **Birth**: how is this created
- **Maintain**: who/what touches it on what cadence
- **Audit**: which check validates it
- **Archive/snapshot**: rollback substrate
- **Retire**: decommission protocol

The 12-item concern map (`feedback_complete_concern_map_first.md`) is the implementation; the lifecycle framing is the WHY.

## The ecosystem isn't lifecycles — it's INTERACTIONS

Operator 2026-05-23: *"the sun, the rain, the temperature... the plants, the animals how they interact... that is the part you are not doing you are not considering how all this interacts"*.

Lifecycle alone is single-component thinking. The system's behavior is **emergent from interactions** between components. Designs that don't model interactions silently produce cascades and timing bugs.

### Interaction patterns to map for every spec

For every change, walk the **dependency edges** in both directions:

1. **Upstream impact** — what does this change DEPEND ON for correctness? If those inputs degrade, what happens here?
2. **Downstream impact** — what depends ON this change? When this changes, what cascades?
3. **Timing contention** — does this change RUN AT THE SAME TIME as something else? Producer vs validator, snapshot vs ingest, dump vs daemon — what's the locking story?
4. **Resource contention** — shared rate-limit budget (FMP 300/min across daily_bars/profile/fundamentals), shared DB connection pool, shared disk, shared CPU.
5. **Failure propagation** — if this fails, what fails next? Sentinel reads fear_greed reads macro_indicators reads FRED — a FRED outage propagates 3 layers.

### Examples of interactions in the v2 plan I missed

- Phase 2 FKs landed → producers (plants) suddenly need FK-awareness → parent_resolver was needed BEFORE Phase 2, not after.
- classify_tickers DELETE → engines reading those tickers find them gone → engine roster needs to handle "ticker disappeared mid-trading-day".
- pg_dump at 22:00 UTC overlap with 21:30 UTC daemon → write contention; either dump waits for daemon OR daemon stalls.
- db_snapshots captures mid-ingest state → restore from that snapshot = torn read in the engine roster.
- parent_resolver hits FMP /profile → eats from the shared 300/min budget that daily_bars + fundamentals also draw from.
- prices_daily orphan backfill via parent_resolver → FMP /profile call per orphan × 166 tickers → ~30 seconds at 0.2s sleep → BUT what if those tickers are delisted-and-FMP-doesn't-have-them either? Cascade.

### How interactions show up in the concern map

Extend the 12-item concern map with interaction questions per change:

- **Inputs**: what data/feeds/state does this read? What happens if any input is stale/missing/wrong?
- **Outputs**: who consumes the output? When this output changes, what reacts?
- **Concurrency**: what else runs at the same time as this? On the same lock? On the same rate-limit?
- **Cascade**: trace the failure path. If this errors at midnight, what's broken at 09:00 in Manila?

### What this means for v2.1 (in flight)

The in-flight v2.1 subagent may not have this ecosystem-interactions framing baked in. If v2.1 lands with just per-component lifecycles, it's still half the picture. Need to iterate (v2.2 or amendment) to add interaction maps for each phase:
- Phase 0.5 (db_snapshots) timing relative to daily ingest
- Phase 0.6 (pg_dump) timing relative to daily ingest + db_snapshots
- Phase 3.5 (parent_resolver) shared FMP rate-limit accounting with daily_bars
- Phase 4 (backfill) impact on engine roster mid-trading-day

## Related

- `feedback_complete_concern_map_first.md` — the implementation of this framing
- `project_database_architecture_state_2026_05_23.md` — Tier 1 raw / Tier 2 derived ecosystem
- `project_engine_inventory_2026_05_23.md` — engine lifecycle state today
- `docs/superpowers/checklists/engine_readiness.md` — engine onboarding lifecycle
- `docs/superpowers/checklists/data_provider_evaluate.md` — feed onboarding lifecycle
