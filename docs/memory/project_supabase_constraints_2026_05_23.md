---
name: supabase-constraints-2026-05-23
description: "Supabase Pro tier hard constraints + the 2026-05-23 disk-blowup incident lessons. Disk auto-resized from 8 GB to 18 GB after the 97% incident (verified via Supabase UI 2026-05-24). 95% of provisioned triggers auto read-only; 4-hour resize cooldown; no superuser CHECKPOINT; large single-transaction UPDATEs accumulate WAL faster than auto-checkpoint can free it. Mandatory pattern: chunked DML for >100K-row writes. ALWAYS check current provisioned disk via Supabase UI before assuming a cap — auto-resize means historical limits are stale."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Hard constraints (Supabase Pro tier, refreshed 2026-05-24):**

1. **Disk: provisioned size is 18 GB as of 2026-05-24** (auto-resized up from 8 GB after the 2026-05-23 97% blow-up triggered the cap). The 8 GB baseline in older docs/memory entries is STALE — always re-check via the Supabase UI Database Size panel before assuming. `pg_database_size()` measures live data only; the UI shows provisioned vs used. As of 2026-05-24: 6.09 GB used / 18 GB = ~34%.
2. **Auto read-only at ~95% disk usage.** Supabase protects the project by setting `default_transaction_read_only = on` and refusing writes. Surfaces as `asyncpg.exceptions.CannotConnectNowError: the database system is not accepting connections / DETAIL: Hot standby mode is disabled.`
3. **4-hour disk-resize cooldown after any resize.** Cannot grow the plan back-to-back during recovery; have to wait it out. (The auto-resize that brought us from 8 → 18 GB consumed this cooldown 2026-05-23.)
4. **No superuser** — `CHECKPOINT` requires `pg_checkpoint` role we don't have. Cannot manually flush WAL. Must wait for natural auto-checkpoint (usually every 5 min or 1 GB).
5. **Connection pool**: 60 connections via the IPv4 pooler URL (`DATABASE_URL_IPV4`). 19 in active use at idle gives ~30% of capacity.
6. **`statement_timeout` default 120s.** Per-session override via `SET LOCAL statement_timeout = '30min'` works (verified). Project-wide override via Supabase dashboard requires operator action.

## The 2026-05-23 incident

**Trigger:** ran a single-transaction `UPDATE platform.prices_daily SET classification_id = tc.id FROM platform.ticker_classifications tc WHERE pd.ticker = tc.current_ticker` against 21M rows. The migration's `op.execute(...)` ran in one alembic transaction.

**What happened:**
- UPDATE rewrote up to 21M rows. Each row UPDATE = WAL record. ~1.5 GB WAL generated.
- Migration failed mid-run (transient connection / timeout issue), rolled back the row changes.
- BUT WAL records persisted until next checkpoint.
- WAL ballooned to 1.95 GB (was 0.16 GB before).
- Disk hit 7.78 GB / 8 GB = 97% → Supabase auto-flipped to read-only.
- 4-hour resize cooldown locked us out of plan upgrade.
- All writes failed for ~45 min until VACUUM + ALTER DATABASE override recovered.

## Mandatory pattern A: chunked DML for >100K-row writes

Single-transaction `UPDATE`/`DELETE` against a large table is FORBIDDEN. Use the chunked-stage pattern:

```python
async with pool.acquire() as conn, conn.transaction():
    await conn.execute("SET LOCAL statement_timeout = '5min'")
    r = await conn.execute("""
        WITH batch AS (
            SELECT pd.ctid FROM platform.prices_daily pd
            WHERE pd.classification_id IS NULL
            LIMIT $1
        )
        UPDATE platform.prices_daily pd
        SET classification_id = ...
        FROM ...
        WHERE pd.ctid IN (SELECT ctid FROM batch) AND ...
    """, chunk_size)
# Sleep BETWEEN transactions so WAL has time to checkpoint
await asyncio.sleep(0.5)
```

Per-chunk transaction → COMMIT releases WAL to checkpoint → sleep gives Supabase headroom → next chunk. The reference implementation is `scripts/ops.py::_stage_prices_daily_backfill_classification_id` (added 2026-05-23 post-incident).

**Chunk sizing rule of thumb:**
- 100K rows per chunk = ~14 MB WAL per chunk (manageable)
- 1M rows per chunk = ~140 MB WAL — risky, can fill checkpoint headroom
- 100K + 500ms sleep × 210 chunks = ~2 min total wall-clock per 21M rows

## Mandatory pattern B: STREAMING COMMITS for any stage that gathers data BEFORE writing

**FAILURE MODE 2026-05-23 (the second time I tripped this):** the FMP profile
backfill stage made 13K HTTP calls (~45 min wall-clock at 5 req/s rate limit),
buffered all responses in memory, then bulk-UPDATEd at the END. Stage hit the
1-hour `HEAVY_STAGE_TIMEOUT_SEC` cap, was force-killed, **zero rows committed**.
This is the second-class failure that pattern A (chunked DML) doesn't catch —
the writes themselves were small, but the buffer-then-flush-at-end shape meant
all progress evaporated on timeout.

**Rule:** any stage that does "loop over N rows fetching from external API +
buffering + writing at end" MUST flush the buffer EVERY N rows (typically every
100-500) so progress survives a crash, timeout, or kill. Pattern:

```python
BATCH = 500
pending: list[dict] = []
async def _flush(buf):
    if not buf: return 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE ... FROM unnest($1::text[]) ...", ...)
    return len(buf)

for i, r in enumerate(rows):
    result = await external_api_call(r["ticker"])  # the slow per-row op
    if result: pending.append(result)
    if len(pending) >= BATCH:           # <-- streaming flush, not at-end
        n_committed += await _flush(pending)
        pending.clear()

# Final flush for the tail
if pending:
    n_committed += await _flush(pending)
```

When `HEAVY_STAGE_TIMEOUT_SEC` (3600s) kills mid-loop: the last `_flush` ran
within the last BATCH iterations, so the loss is bounded to a few hundred rows
of staged work, not the whole 13K.

**Where this applies — anywhere these two conditions co-occur:**
1. Stage runs LONG (>~30 min wall-clock; near `HEAVY_STAGE_TIMEOUT_SEC = 3600`)
2. Stage gathers data BEFORE the DB write (external API in the loop body)

Reference impl: `scripts/ops.py::_tkr14_backfill_fmp_profile` (streaming version
landed 2026-05-23 after the first attempt buffered-then-died).

## Recovery procedure when disk hits read-only

Per Supabase docs (`https://supabase.com/docs/guides/platform/database-size#disabling-read-only-mode`), execute in a fresh session:

```sql
SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE;
VACUUM;   -- reclaim dead-tuple bloat (the rolled-back UPDATE leaves tons of these)
SET default_transaction_read_only = off;   -- session-scoped
ALTER DATABASE postgres SET default_transaction_read_only = off;   -- persistent
ALTER ROLE postgres SET default_transaction_read_only = off;
ALTER ROLE authenticator SET default_transaction_read_only = off;
```

Then wait for WAL to checkpoint naturally (5-15 min of no large writes).

## Recovery worked example (2026-05-23)

| Step | Before | After |
|---|---|---|
| Failed UPDATE rolled back | DB: 5,144 MB / WAL: ~1.95 GB | unchanged |
| VACUUM 16 touched tables | DB: 5,144 MB | DB: 4,985 MB (-159 MB dead-tuple reclaim) |
| ALTER DATABASE override | RO blocked all writes | RW restored, persistent |
| ~30 min idle for WAL checkpoint | WAL: 1.95 GB | WAL: ~0 GB (auto-checkpointed) |

## What does NOT work as a recovery step

- `CHECKPOINT` — permission denied (no pg_checkpoint role)
- `pg_switch_wal()` — same permission issue
- Dashboard "Upgrade plan" — locked by 4-hour cooldown after the trigger
- Dropping our P6 indexes (~200 MB) — small relative to the 1.95 GB WAL

## Where this gets enforced

- **`scripts/ops.py` for any new stage that does large DML (pattern A)** — must use chunked pattern; copy from `_stage_prices_daily_backfill_classification_id`
- **`scripts/ops.py` for any new stage that loops over rows + external API (pattern B)** — must use streaming flush; copy from `_tkr14_backfill_fmp_profile`
- **`platform/migrations/` for any UPDATE/DELETE on a >100K-row table** — split into (a) DDL-only migration, (b) ops.py stage backfill, (c) DDL-only follow-up migration for FK/INDEX
- **Future test that exercises this** — `tests/test_no_unbounded_dml_in_migrations.py` (TODO)
- **Mental checklist before kicking off any background ops.py stage**:
  1. Does the stage involve >100K-row writes? → must be chunked (pattern A)
  2. Does the stage loop over rows + external API? → must stream-flush (pattern B)
  3. Will wall-clock be >30 min? → both patterns A AND B mandatory

## Related

- [[run-gates-locally-on-commit]] — local-gate sequence should now include "if migration contains UPDATE/DELETE on a >100K table, REJECT"
- [[i-do-that-too-not-operator-action]] — disk recovery IS my responsibility (I have credentials)
- [[supabase-pro-tier]] — older entry; this supersedes the disk-cap framing
- [[worktree-isolation-doesnt-cover-shared-infra]] — sibling: live DB is shared infra; large-impact ops have project-wide blast radius
