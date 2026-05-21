---
name: railway-hobby-tier
description: "Railway paused 2026-05-12; local Mac is the active execution environment. Railway Hobby $5/mo still subscribed but paused — auto-deploys disabled, cron schedules unset. Canonical $52/mo platform stack. platform.ingestion_jobs FROZEN — application_log INGESTION_COMPLETE wins. railway.json is stale (pre-DA-3 5→3 topology); fix at re-enable."
metadata:
  node_type: memory
  type: project
  originSessionId: 246bce25-f322-4daa-80a2-4db8cacc183e
---
**Active state (as of 2026-05-12):** Railway is **paused**. The subscription is still Hobby ($5/mo) and four services exist in the project (`ingestion-engine`, `sigma-scheduler`, `reversion-scheduler`, `vector-scheduler`), but **auto-deploys are disabled** and the engine schedulers have **no cron schedule set** on their service instances. Effective execution environment is the operator's **local Mac**, invoking the scripts in `scripts/` directly. Daily ops use `scripts/ops.py`; engine paper-trading is on hold pending the pipeline smoke test passing.

**Why pause:** The trade-monitor refactor (Phase 1.5) landed in `railway.json` but never got applied to live Railway (the GraphQL `serviceInstanceUpdate` step was skipped). Rather than partial-apply, the operator paused Railway entirely and put execution local. The architectural decision on whether to re-enable Railway, move to a different host, or stay local long-term is deferred until an engine proves an edge (credibility ≥ 60).

**Live Railway state (verified 2026-05-12 via GraphQL):**
- `ingestion-engine`: persistent, **restartPolicyType=NEVER** (railway.json says ALWAYS — drift).
- `sigma-scheduler`, `reversion-scheduler`, `vector-scheduler`: **cronSchedule=null**.
- `trade-monitor`: defined in railway.json but **never deployed** to Railway.
- The Sunday-cron services (`fundamentals-refresh-scheduler`, `corporate-actions-scheduler`, `validation-scheduler`) listed in earlier doc revisions **do not exist** — consolidated into the persistent `ingestion-engine`.

**railway.json is stale (sweep finding 2026-05-19):** the file describes the retired 5→3-service pre-DA-3 topology, would mis-deploy the current 2-daemon+cron architecture. Must be fixed at Railway re-enable (not now).

**How to apply:**
- When totalling platform costs, the fixed monthly stack is: Alpaca free + FMP Starter $22 + Railway $5 + Supabase Pro $25 = **$52/mo** (see [[supabase-pro-tier]]). Railway $5 is paid even while paused; subscription was not cancelled.
- Don't suggest building or deploying new Railway services until the user explicitly re-enables Railway. Any infra work goes into `scripts/` and runs locally for now.
- The trade-monitor `railway.json` block is intentionally kept committed — when Railway re-enables, the deploy already has its source.
- When advising on engine submissions or paper trading, treat the active path as: scheduler invoked locally → broker → trade_monitor (also local). No automatic cron firing.
- **`platform.ingestion_jobs` is the Railway daemon's bookkeeping table and is FROZEN at the 2026-05-12 pause state.** Its `last_status` / `last_run_at` / `last_error` reflect the last Railway run, NOT current health. Do not treat a `last_status='failed'` row as an active failure — the operator confirmed "everything runs from this Mac" (2026-05-15). The authoritative ingestion-health signal is `platform.application_log` `INGESTION_COMPLETE` / `INGESTION_FAILED` events written by the local `scripts/ops.py` pipeline. Any Railway-sourced state table is historical; local `application_log` wins.
- **Pre-Railway-re-enable blockers** ([[railway-archive-substrate-migration]]): vendor-truncation detection→durable Postgres (D2); CSV archive→S3-compatible object-storage bucket (R3); asyncpg `statement_cache_size=0` (already fixed in `tpcore/db.py`); railway.json topology fix.
