# Referential-Integrity Implementation Plan v2.1 — `platform.*` schema (complete-concern-map amendment)

**Status:** PLAN v2.1 — amendment to v2 incorporating the concern-map gaps surfaced mid-execution. Phases 0/1/2 already merged; Phase 3 PR #320 in flight; Phases 0.5/0.6/3.5 NEW; Phase 4 REVISED.

**Supersedes:** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan-v2.md` (v2, 5 phases). v2's phase sequence is REPLACED by this doc's 9-phase sequence: Phase 0 → 0.5 → 0.6 → 1 → 2 → 3 → 3.5 → 4 → 5. v2 stays on disk as the historical record; do not delete it. Where v2.1 and v2 conflict on phase order, scope, or orphan-handling protocol, **v2.1 wins**. v1 stays on disk via the v2 inheritance chain.

**Spec basis (read before executing any phase):**
1. `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.1.md` — the v2.1 spec this plan implements. **Read first.** §2 concern-map checklist, §3 revised scope (14 tables), §7 phase sequence, §1.11 orphan-protocol-per-table matrix.
2. `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.md` — v2 spec; §5 NOT-VALID pattern, §6 index audit, §8 test contracts, §9 verification gates, §11 cleanup-template `ctid` fix — all inherited unchanged into v2.1.
3. `.claude/agents/db-architect.md` — operating contract.
4. Memory `feedback_complete_concern_map_first.md` — the 12-item concern map.
5. Memory `project_database_architecture_state_2026_05_23.md` — schema state.
6. Memory `project_railway_archive_substrate_migration.md` — D2/R3 substrate decisions; Phase 0.5 + 0.6 build on these.
7. Phase 0 audits (DONE PR #317): orphan baseline, index audit, timeout audit.

**Goal (unchanged):** every `ticker`-bearing table in `platform.*` has a real FK to `platform.ticker_classifications(ticker)` with `ON UPDATE CASCADE ON DELETE RESTRICT`. Drift becomes a constraint violation at INSERT time, not an audit-after-the-fact print line. v2.1 adds the surrounding substrate (snapshots + backups + parent_resolver) so the FK-rollout is recoverable, auditable, and self-healing.

**Non-goals (unchanged from v2):** composite `(ticker, date)` FK chains, Tier 2 freshness constraints, RLS policies, macro-table consolidation (Task #18), Phase 2 denormalization (Task #17), per-country insider adapters (Task #15).

---

## 1. v2.1 phase summary + wall-clock budget

| Phase | Topic | Status | Migrations | Est. wall-clock |
|---|---|---|---|---|
| **0** | Pre-flight audit — orphan counts, FK-column-index audit, timeout audit | **DONE PR #317** | 1 (CONCURRENTLY index on universe_candidates(ticker)) | shipped |
| **0.5** | `db_snapshots/` build (Task #22) — per-table COPY-to-CSV.gz daily + manifest + retention | **NEW** | 0 schema; 1 producer script PR + launchd plist | 4–6 hr |
| **0.6** | `pg_dump` daily backup regimen — full schema dump + S3/R3 upload + restore protocol + quarterly test | **NEW** | 0 schema; 1 script PR + launchd plist + runbook | 3–4 hr |
| **1** | Rename `sec_insider_transactions` → `insider_transactions` + compat view + `source` column + CHECK + `country char(2)` + FMP backfill | **DONE PR #318** | 2 + 1 producer-code change | shipped |
| **2** | 14 FKs NOT VALID + drop sec_insider_transactions compat view | **DONE PR #319** | 1 (14 `op.execute` `ADD CONSTRAINT NOT VALID` ops + DROP VIEW) | shipped |
| **3** | classify_tickers DELETE-source-tracking + `A ∩ P` filter (dry_run=True default) | **IN FLIGHT PR #320** | 0 schema; 1 producer-code PR | shipped pending review |
| **3.5** | `parent_resolver` build (Task #24) — pre-INSERT sentinel + vendor fallback chain (FMP → Alpaca → SEC) | **NEW** | 0 schema; 1 new module PR (`tpcore/ingestion/parent_resolver.py`) + handler integration | 6–8 hr |
| **4** | Per-table BACKFILL-then-VALIDATE — REVISED orphan-protocol (BACKFILL for prices_daily / corporate_actions / fundamentals_quarterly / earnings_events / short_interest; DELETE for spread_observations / liquidity_tiers / universe_candidates) | **REVISED** | 2 migrations × 8 needing-cleanup tables (cleanup + VALIDATE) + 6 zero-orphan VALIDATEs = up to 22 migrations; operator may batch | 4–8 hr |
| **5** | Verification + ongoing-ops sentinel + country CHECK constraint | unchanged | 1 (add country CHECK) | 1 hr |

**Total v2.1 remaining wall-clock: 18–27 hours** of focused work (Phase 0/1/2 already shipped). The NEW phases (0.5, 0.6, 3.5) add ~13–18 hr to v2's original 9–13 hr budget — the trade-off for shipping with the concern map closed.

**v2.1 vs v2 budget:** v2 = 9–13 hr / ~20 PRs. v2.1 = 18–27 hr / ~25 PRs. v2.1 is heavier because three NEW phases land before Phase 4 unblocks. Net win: zero mid-execution surprises; full rollback substrate; auto-resolution of FK violations going forward.

---

## 2. Phase 0 — DONE PR #317

Shipped 2026-05-23. Deliverables in `docs/superpowers/audits/`:
- `2026-05-23-referential-integrity-baseline.md` — 336,857 total orphans (99.5% in `prices_daily`).
- `2026-05-23-referential-integrity-index-audit.md` — 15 → 14 in-scope (`insider_mspr_daily` is a VIEW); `universe_candidates(ticker)` index landed.
- `2026-05-23-referential-integrity-timeout-locks-baseline.md` — `statement_timeout=120s` (must raise to 30min before Phase 4 prices_daily VALIDATE).

Migration: `platform/migrations/versions/20260523_0500_idx_concurrently_universe_candidates_ticker.py`.

---

## 2.5. Phase 0.5 — `db_snapshots/` build (NEW; Task #22)

**Goal:** ship the per-table snapshot substrate before any Phase 4 cleanup runs. Provides pre-cleanup rollback baseline.

### 2.5.1 Deliverables

- **New module: `scripts/db_snapshots.py`** (or stage `db_snapshots` in `scripts/ops.py`).
  - For each table in the snapshot set (14 in-scope + `ticker_classifications` + `application_log` + `data_quality_log` = ~17 tables):
    - `COPY (SELECT * FROM platform.<T>) TO STDOUT WITH (FORMAT csv, HEADER true)` piped to `gzip > data/db_snapshots/<T>/<utc_stamp>.csv.gz`.
    - Compute sha256 of the .csv.gz file.
    - Capture row count via `SELECT count(*) FROM platform.<T>`.
    - Capture current alembic revision via `SELECT version_num FROM platform.alembic_version`.
  - Write manifest: `data/db_snapshots/<utc_stamp>_manifest.json` with `{table, rows, sha256, alembic_revision, completed_at}` per table.
  - Age-based prune: delete any `.csv.gz` + manifest older than 30 days.
- **launchd plist: `~/Library/LaunchAgents/com.short-term-trading-engine.db-snapshots.plist`** running at 22:00 UTC = 06:00 Manila next-day.
- **Disk budget verification:** before first run, estimate `pg_total_relation_size` for each in-scope table × ~10% CSV.gz compression ratio; budget should land at 1–2.5 GB cumulative for the 30-day retention. Alert if budget exceeds 5 GB.

### 2.5.2 Pre-migration gates

- Phase 0 deliverables green (already DONE per PR #317).
- Operator confirms `data/db_snapshots/` parent path is writeable + has ≥10 GB headroom on the operator's Mac.

### 2.5.3 Verification queries (capture in PR body)

```bash
# Successful daily run produces:
ls -la data/db_snapshots/
# Expected: <table>/<utc_stamp>.csv.gz for each in-scope table + manifest.

# Manifest is well-formed:
python -c "import json; json.load(open('data/db_snapshots/<utc_stamp>_manifest.json'))"

# Row count from manifest matches live DB:
psql "$DATABASE_URL" -c "SELECT count(*) FROM platform.prices_daily;"
# Compare against manifest's prices_daily.rows entry.

# Restore-test: load latest snapshot into a temp table; row count matches manifest.
gunzip -c data/db_snapshots/prices_daily/<utc_stamp>.csv.gz | head -5
# Then COPY into a temp table and verify count.
```

### 2.5.4 Rollback path

- launchd plist `unload`; remove `scripts/db_snapshots.py`. Snapshots themselves are static files; rolling back the producer doesn't delete past snapshots (`data/db_snapshots/` stays).

### 2.5.5 Test contracts

- Unit test: `scripts/db_snapshots.py` handles a table-missing case (returns error, doesn't crash mid-run).
- Unit test: manifest schema validates against an expected JSON shape.
- Integration test: one full run against a small (`liquidity_tiers`-sized) table; verify sha256 + row count agreement.

### 2.5.6 Exit gate (Phase 0.5)

- One full successful daily snapshot run captured in `data/db_snapshots/`.
- Restore-test passed: latest `prices_daily` CSV.gz loaded into temp table; row count matches manifest; sha256 matches manifest.
- launchd plist installed + running (next-day's run is queued).
- Heavy-lane gates green.

**Blocks:** Phase 4 (any cleanup).

---

## 2.6. Phase 0.6 — `pg_dump` daily backup regimen (NEW)

**Goal:** ship the tenant-loss disaster-recovery path. Complements Supabase Pro's 7-day PITR with longer-horizon + Supabase-account-independent backup.

### 2.6.1 Deliverables

- **New script: `scripts/pg_dump_daily.sh`** (bash, since pg_dump is a CLI tool).
  - Run:
    ```bash
    pg_dump --format=custom --compress=9 --schema=platform \
            --no-owner --no-privileges \
            --file="/tmp/short-term-trading-engine-$(date -u +%Y%m%d).dump" \
            "$DATABASE_URL"
    ```
  - Upload to S3 OR R3 (operator decision per v2.1 spec §16 open question 1):
    - S3: `aws s3 cp /tmp/<file>.dump s3://<bucket>/short-term-trading-engine/pg_dumps/<utc_date>.dump`
    - R3 (recommended for reuse with `project_railway_archive_substrate_migration.md`): same path under the existing R3 bucket.
  - Verify upload via checksum comparison.
  - Delete local `/tmp/` file.
  - Write `BACKUP_COMPLETE` event to `application_log`:
    ```sql
    INSERT INTO platform.application_log (event_type, data)
    VALUES ('BACKUP_COMPLETE', jsonb_build_object('s3_path', '<path>', 'size_bytes', <size>, 'sha256', '<hash>'));
    ```
- **launchd plist: `~/Library/LaunchAgents/com.short-term-trading-engine.pg-dump.plist`** running at 22:00 UTC.
- **S3 lifecycle policy (or R3 equivalent):** 30-day transition to delete.
- **Restore-protocol runbook: `docs/runbooks/database_disaster_recovery.md`** (NEW):
  ```bash
  # 1. Provision throwaway Supabase project; capture DATABASE_URL.
  # 2. aws s3 cp s3://<bucket>/short-term-trading-engine/pg_dumps/<date>.dump /tmp/
  # 3. pg_restore --schema=platform --no-owner --no-privileges \
  #               --dbname="<new-url>" /tmp/<date>.dump
  # 4. Smoke: psql "<new-url>" -c "SELECT count(*) FROM platform.prices_daily;"
  # 5. Drop the throwaway project.
  ```
- **Quarterly recovery-test calendar entry** added to operator's calendar (manual; not enforced by code).

### 2.6.2 Pre-migration gates

- Operator confirms S3 (or R3) bucket exists + IAM creds present in `~/.aws/credentials` or env (`AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`).
- Operator picks S3 vs R3 per v2.1 spec §16 open question 1.

### 2.6.3 Verification queries (capture in PR body)

```bash
# Successful daily run uploads to S3:
aws s3 ls s3://<bucket>/short-term-trading-engine/pg_dumps/
# Expected: <utc_date>.dump (~600 MB compressed; varies with prices_daily growth).

# pg_restore --list inspects the dump:
aws s3 cp s3://<bucket>/<file>.dump /tmp/
pg_restore --list /tmp/<file>.dump | head -50
# Expected: enumerates expected platform.* tables + indexes + constraints.

# application_log captures the event:
psql "$DATABASE_URL" -c "SELECT data FROM platform.application_log WHERE event_type='BACKUP_COMPLETE' ORDER BY ts DESC LIMIT 1;"
```

### 2.6.4 Rollback path

- launchd plist `unload`; remove `scripts/pg_dump_daily.sh`. Past dumps stay in S3 until lifecycle policy expires them (operator can manually delete if needed).

### 2.6.5 Test contracts

- Unit test: script handles `aws s3 cp` failure (retry once; log failure; exit non-zero).
- Integration test (manual, one-time): full run; restore the dump into a throwaway Supabase project; smoke-query `prices_daily` row count.

### 2.6.6 Exit gate (Phase 0.6)

- One full successful daily dump uploaded to S3/R3.
- Restore-test passed: dump downloaded; `pg_restore --list` enumerates expected tables.
- launchd plist installed + running.
- `docs/runbooks/database_disaster_recovery.md` landed.
- Quarterly recovery-test calendar entry added (manual action acknowledged).
- Heavy-lane gates green.

**Independent of Phase 0.5** — operator may land 0.5 and 0.6 in parallel (separate PRs, no shared files).

---

## 3. Phase 1 — DONE PR #318

Shipped 2026-05-23. Deliverables:
- Migration `<stamp>_rename_sec_insider_transactions_with_view.py` — rename + compat view + `source` column + CHECK.
- Migration `<stamp>_ticker_classifications_country_column.py` — `country char(2)` + partial index.
- Producer changes for the rename grep sweep.
- FMP `/stable/profile` country backfill running (88% complete; null-rates per v2.1 spec §1.3).

---

## 4. Phase 2 — DONE PR #319

Shipped 2026-05-23. Deliverables:
- Migration `<stamp>_fk_not_valid_14_tables.py` — `ADD CONSTRAINT … NOT VALID` on the 14 in-scope tables (15 minus `insider_mspr_daily` — VIEW).
- DROP of `platform.sec_insider_transactions` compat view (per v2.1 spec §1.2 — auto-rewrite finding meant the view provided no detection benefit).

---

## 5. Phase 3 — IN FLIGHT PR #320

Branch: `feat/phase-3-classify-tickers-delete-source-tracking`. Held per operator instruction 2026-05-23 pending v2.1 amendment. Will resume post-v2.1 merge.

### 5.1 Producer change scope (current state of PR #320)

- `scripts/classify_tickers.py` updated to:
  - Compute `A = Alpaca /v2/assets`, `P = SELECT DISTINCT ticker FROM prices_daily`, `U = A ∩ P`.
  - Compute delete-set `D = {existing ticker_classifications.ticker} - U`.
  - Default `dry_run=True` — surfaces `|D|` via `application_log` event; does NOT commit DELETEs.
  - With `dry_run=False`, executes DELETEs in same transaction as UPSERT.
- asyncpg imported at module level (NOT TYPE_CHECKING — per v2.1 spec §1.6 / §8.8).
- Unit tests cover the `A ∩ P` filter, delete-set computation, and the FK-violation-catch path.

### 5.2 Critical state captured today

Today's dry-run delete-set is 6,083 tickers (43.7% of universe) per v2.1 spec §1.7. Live DELETE mode is BLOCKED until Phase 3.5 (`parent_resolver`) lands so the resolver can auto-resolve the recoverable subset BEFORE any DELETE commits.

### 5.3 Exit gate (REVISED per v2.1)

- Producer unit tests green.
- Dry-run produces full `application_log` audit row with delete-set captured.
- Operator reviews the delete-set + Phase 3.5 capabilities → decides which fraction needs DELETE vs which gets auto-resolved.
- **Live DELETE mode DOES NOT execute** until Phase 3.5 lands.

---

## 5.5. Phase 3.5 — `parent_resolver` build (NEW; Task #24)

**Goal:** ship the auto-resolution layer for handler-side FK-violation handling. Required before Phase 4 backfill protocol (which uses `parent_resolver` as its engine).

### 5.5.1 Deliverables

- **New module: `tpcore/ingestion/parent_resolver.py`**.
  - Public API:
    ```python
    async def resolve(
        unknown_tickers: set[str],
        *,
        vendor_priority: list[str] = ["fmp", "alpaca", "sec"],
        timeout_s: float = 30.0,
    ) -> ResolveResult:
        """
        For each ticker in unknown_tickers, attempt vendor lookup.
        Successful resolutions auto-INSERTed into ticker_classifications
        with source='auto_resolver'.
        Unresolvable tickers logged as INGEST_ORPHAN_BLOCKED.
        Returns ResolveResult{resolved: set[str], unresolvable: set[str]}.
        """
    ```
- **Vendor fallback chain:**
  - FMP `/stable/profile/<symbol>` — primary; returns `country, asset_class, exchange, name`.
  - Alpaca `/v2/assets/<symbol>` — fallback; active US-listed only.
  - SEC company-tickers JSON (cached) — fallback; CIK-based resolution.
- **Integration:** every handler in `tpcore/ingestion/handlers.py` adds a pre-INSERT sentinel:
  ```python
  unknown = set(incoming.ticker) - existing_ticker_set
  if unknown:
      result = await parent_resolver.resolve(unknown)
      # Drop unresolvable rows from incoming batch
      incoming = [row for row in incoming if row.ticker not in result.unresolvable]
  # Then proceed with the INSERT; FK is now satisfied.
  ```
- **Unresolvable-ticker logging:**
  ```sql
  INSERT INTO platform.application_log (event_type, data)
  VALUES ('INGEST_ORPHAN_BLOCKED', jsonb_build_object(
      'handler', '<handler_name>',
      'tickers', <array>,
      'vendor_failures', <vendor_failure_map>
  ));
  ```
- **Unit tests** (`tpcore/tests/test_parent_resolver.py`):
  - FMP-success path.
  - FMP-fail → Alpaca-success path.
  - FMP-fail → Alpaca-fail → SEC-success path.
  - All-fail (unresolvable) path → INGEST_ORPHAN_BLOCKED log + ticker dropped.
  - Rate-limit handling (HTTP 429 → exponential backoff, then move to next vendor).
  - Vendor-returns-wrong-data sentinel (FMP returns row but ticker symbol doesn't match query — discard; try next vendor).

### 5.5.2 Pre-migration gates

- Phase 2 (DONE PR #319) — FKs in NOT VALID state so the resolver actually has an FK to satisfy.
- FMP `/stable/profile` access verified (already in use for Phase 1 country backfill).
- Alpaca `/v2/assets` access verified (already in use).
- SEC company-tickers JSON access verified (no auth required).

### 5.5.3 Verification queries (capture in PR body)

```sql
-- 5.5.3.1 Resolver test: synthesize an unknown ticker; verify resolution.
-- (Manual test in PR body: pick a known-delisted-ticker like 'OMOT' that FMP
-- still indexes; verify resolver returns it; verify ticker_classifications row
-- gets created with source='auto_resolver'.)

-- 5.5.3.2 INGEST_ORPHAN_BLOCKED events post-deploy:
SELECT data->>'handler' AS handler,
       jsonb_array_length(data->'tickers') AS unresolvable_count,
       data->'vendor_failures' AS failures
FROM platform.application_log
WHERE event_type = 'INGEST_ORPHAN_BLOCKED'
  AND ts > now() - interval '24 hours'
ORDER BY ts DESC LIMIT 20;
-- Expected after one normal ingest cycle: 0 or near-0 (most tickers should auto-resolve).
```

### 5.5.4 Test contracts (per v2.1 spec §8.8)

Every handler in `tpcore/ingestion/handlers.py` that catches `asyncpg.ForeignKeyViolationError` MUST:
- Import `asyncpg` at module level (NOT TYPE_CHECKING).
- Have a unit test simulating an orphan-ticker INSERT through the handler.
- Test asserts: `parent_resolver.resolve(...)` is invoked OR `INGEST_ORPHAN_BLOCKED` event lands; no NameError/AttributeError surfaces.

### 5.5.5 Rollback path

- Producer-code revert (`git revert` the PR).
- No schema rollback needed.
- Any `ticker_classifications` rows created by the resolver with `source='auto_resolver'` stay — they're real ticker data; no harm in retaining.

### 5.5.6 Exit gate (Phase 3.5)

- All `parent_resolver` unit tests green.
- Integration smoke: one ingest cycle (`daily_bars` or `corporate_actions`) runs cleanly with a synthesized unknown-ticker; resolver auto-resolves; row lands; no FK violation surfaces.
- `application_log` shows the `auto_resolver` event for the synthesized ticker.
- Handler FK-violation-path tests (v2.1 spec §8.8) green for every handler.
- Operator triages the Phase 3 classify_tickers 6,083-ticker delete-set through the resolver — expected outcome: large fraction (preferred shares with FMP profile, foreign ADRs, etc.) auto-resolves; small fraction remains for explicit Path-B DELETE.
- Heavy-lane gates green.

**Blocks:** Phase 4 (per-table backfill cleanup).

---

## 6. Phase 4 — Per-table BACKFILL-then-VALIDATE (REVISED per v2.1 §1.11)

**Goal:** clean orphans per the v2.1 per-table protocol matrix, then VALIDATE per table. The protocol DEFAULTS to BACKFILL via `parent_resolver` for real-history tables; DELETE for derived/junk tables.

### 6.1 Per-table operator-decision matrix (FROM v2.1 spec §1.11)

| Table | Orphan rows | Orphan tickers | Path | Rationale | Sign-off |
|---|---:|---:|---|---|---|
| `universe_candidates` | 1 | 1 | **B (DELETE)** | Engine output rebuildable | — |
| `short_interest` | 3 | 1 | **A (BACKFILL via resolver)** | FINRA canonical | — |
| `liquidity_tiers` | 8 | 8 | **B (DELETE)** | Derived rebuildable | — |
| `earnings_events` | 12 | 1 | **A (BACKFILL via resolver)** | Real earnings | — |
| `spread_observations` | 33 | 8 | **B (DELETE)** | Derived rebuildable | — |
| `fundamentals_quarterly` | 135 | 8 | **A (BACKFILL via resolver)** | FMP canonical | required |
| `corporate_actions` | 1,506 | 69 | **A (BACKFILL via resolver)** | Alpaca canonical | required |
| `prices_daily` | 335,159 | 166 | **A (BACKFILL via resolver)** | Real market history | required |
| `insider_transactions` | 0 | 0 | — | already clean (renamed) | — |
| `sec_material_events` | 0 | 0 | — | already clean | — |
| `borrow_rates` | 0 | 0 | — | already clean | — |
| `social_sentiment` | 0 | 0 | — | already clean | — |
| `options_max_pain` | 0 | 0 | — | already clean | — |
| `insider_sentiment` | 0 | 0 | — | already clean | — |

### 6.2 Pre-Phase-4 hard gates

- ✅ Phase 0 DONE (PR #317).
- ✅ Phase 1 DONE (PR #318).
- ✅ Phase 2 DONE (PR #319).
- ⏳ Phase 3 PR #320 (dry_run=True default; merge after operator review of the 6,083-delete-set in conjunction with Phase 3.5).
- ⏳ **Phase 0.5 LIVE** with one daily snapshot captured + restore-test passed.
- ⏳ **Phase 0.6 LIVE** with one daily dump captured + restore-test passed.
- ⏳ **Phase 3.5 LIVE** with `parent_resolver` integrated into every handler.
- ⏳ **`statement_timeout` raised to 30min via Supabase dashboard** (operator action).
- ⏳ **Pre-Phase-4 snapshot**: confirm yesterday's `db_snapshots` for prices_daily + ticker_classifications + every-Path-B table exists. This is the recovery baseline if cleanup goes wrong.

### 6.3 Path-A BACKFILL template (uses `parent_resolver` from Phase 3.5)

For each Path-A table T with orphan-ticker-set `O`:

**Cleanup PR** (one per table; operator may batch the small ones):

```python
# scripts/phase4_backfill_<T>.py — one-shot script, NOT a migration.
import asyncio
from tpcore.ingestion.parent_resolver import resolve

async def backfill_orphans_for_table(table: str, fk_col: str = "ticker"):
    async with pool.acquire() as conn:
        # 1. Capture orphan-ticker set.
        rows = await conn.fetch(f"""
            SELECT DISTINCT c.{fk_col} AS ticker
            FROM platform.{table} c
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.{fk_col}
            )
        """)
        orphan_tickers = {r["ticker"] for r in rows}

        # 2. Resolve via parent_resolver (FMP → Alpaca → SEC).
        result = await resolve(orphan_tickers)

        # 3. For unresolvable tickers: log + leave the orphan rows in place.
        #    Operator decides Path-B/C in a follow-up PR with per-ticker sign-off.
        if result.unresolvable:
            await conn.execute(f"""
                INSERT INTO platform.application_log (event_type, data)
                VALUES ('PHASE4_UNRESOLVABLE_ORPHANS', $1::jsonb)
            """, json.dumps({"table": table, "tickers": list(result.unresolvable)}))

        # 4. Re-check orphan_count post-resolution.
        post_count = await conn.fetchval(f"""
            SELECT count(*) FROM platform.{table} c
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.{fk_col}
            )
        """)
        return result, post_count
```

If `post_count == 0`, the table is ready for VALIDATE in the next PR. If `post_count > 0`, the unresolvable tickers need explicit Path-B follow-up.

**Per-ticker resolver fallback for prices_daily**: for any of the 166 orphan tickers `parent_resolver` cannot resolve via any vendor, write minimal classification with `source='phase4_backfill', status='delisted_historical_unresolvable'`:

```sql
INSERT INTO platform.ticker_classifications (ticker, source, asset_class, country, status, last_updated)
SELECT DISTINCT c.ticker, 'phase4_backfill', 'unknown', NULL, 'delisted_historical_unresolvable', now()
FROM platform.prices_daily c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.ticker
)
ON CONFLICT (ticker) DO NOTHING;
```

This requires explicit operator sign-off per the matrix — only operator approves the minimal-row fallback. Audit row in `application_log` captures the count of tickers added this way.

### 6.4 Path-B DELETE template (MVCC-safe per v2 spec §11)

For derived/junk tables (`universe_candidates`, `liquidity_tiers`, `spread_observations`):

**Cleanup migration**: `platform/migrations/versions/<stamp>_cleanup_orphans_<T>.py`

```python
def upgrade():
    op.execute("""
        DELETE FROM platform.<T> c
        WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_classifications p
            WHERE p.ticker = c.<fk_col>
        )
    """)

def downgrade():
    op.execute("-- forward-only; restore from db_snapshots if needed (Phase 0.5)")
```

### 6.5 VALIDATE migration template (UNCHANGED from v2 plan §6.5)

```python
def upgrade():
    op.execute("SET LOCAL statement_timeout = '30min'")  # for prices_daily; lower for small tables
    op.execute("ALTER TABLE platform.<T> VALIDATE CONSTRAINT fk_<T>_ticker")

def downgrade():
    op.execute("-- VALIDATE is forward-only; drop the constraint via Phase 2 downgrade to fully revert")
```

### 6.6 Ordering recommendation (light → heavy)

Per v2.1 spec §7 Phase 4. The 6 zero-orphan tables can VALIDATE immediately (no cleanup migration needed) — `insider_transactions`, `sec_material_events`, `borrow_rates`, `social_sentiment`, `options_max_pain`, `insider_sentiment`. Operator may batch these into one VALIDATE PR.

The 8 needing-cleanup tables follow the ordering matrix in §6.1, finishing with `prices_daily`.

### 6.7 prices_daily special handling (REVISED from v2 §6.8)

- **Pre-flight:** confirm yesterday's `db_snapshots/prices_daily/` snapshot exists; confirm `pg_dump` from Phase 0.6 captured prices_daily.
- **Resolver pre-run:** run `parent_resolver.resolve({166 orphan tickers})` BEFORE the migration. Capture the resolved/unresolvable split in `application_log`.
- **Path-A backfill** of all 166 orphan tickers (resolved-via-resolver + unresolvable-via-minimal-row). Goal: post-backfill `prices_daily.orphan_count = 0`.
- **VALIDATE window:** 04:00–08:00 UTC. No engine cron in the next 60 min. Operator present.
- **`statement_timeout=30min`** (raised via dashboard pre-Phase-4).
- **`pg_locks` monitor** in a separate session per v2 spec §9.2.

If VALIDATE fails (unexpected orphan slipped through): constraint stays NOT VALID; new rows still protected; investigate via spec §11 query; re-run cleanup; re-submit VALIDATE PR.

### 6.8 Per-table verification gates (per v2 spec §9; UNCHANGED)

- **Cleanup PR exit gate:** orphan_count = 0; Alembic round-trip green; heavy-lane gates green.
- **VALIDATE PR exit gate:** `pg_locks` shows only `ShareUpdateExclusiveLock`; `pg_constraint.convalidated = true` post-commit; replica propagation; producer regression smoke clean.

### 6.9 Rollback path (per-table)

- **Cleanup migration:** forward-only by design.
- **If wrong:** restore the affected table from `data/db_snapshots/<table>/<latest_pre_cleanup>.csv.gz` (Phase 0.5). For full-tenant-loss: restore via `pg_restore` from Phase 0.6.
- **VALIDATE migration:** no-op downgrade.

### 6.10 Exit gate (Phase 4 overall)

Every in-scope table has `pg_constraint.convalidated = true`. Triggers Phase 5.

---

## 7. Phase 5 — Verification + ongoing-ops sentinel + country CHECK

### 7.1 Verification PR (no migration)

Per v2 plan §7.1 (unchanged). Acceptance criteria 1–13 from v2 spec §15 + criteria 14–18 from v2.1 spec §15.

### 7.2 Cleanup migration — add country CHECK

REVISED from v2 plan §7.2: the compat view drop already happened in Phase 2 (per PR #319). Phase 5 only adds the country CHECK.

**File:** `platform/migrations/versions/<stamp>_add_country_check.py`

```python
def upgrade():
    op.create_check_constraint(
        "ck_ticker_classifications_country_iso",
        "ticker_classifications",
        "country IS NULL OR country ~ '^[A-Z]{2}$'",
        schema="platform"
    )

def downgrade():
    op.drop_constraint(
        "ck_ticker_classifications_country_iso",
        "ticker_classifications", schema="platform"
    )
```

**Pre-PR check:**
- Phase 1 country null-rate report measured; operator has accepted the tolerance.
- `SELECT count(*) FROM platform.ticker_classifications WHERE country IS NOT NULL AND country !~ '^[A-Z]{2}$'` returns 0.

### 7.3 Ongoing-ops sentinel (NEW per v2.1 spec §7 Phase 5)

- New stage or event-driven hook: nightly `tpcore/auditheal` cross-table-audit runs; any new orphan in any table fires `DATA_INTEGRITY_BREACH` event in `application_log`.
- Implementation: add a check inside the existing `auditheal` cross-table sweep that triggers on `orphan_count > 0` for any in-scope table.
- Alerting: `DATA_INTEGRITY_BREACH` events surface in operator's daily summary (existing channel).

### 7.4 Test contracts pinned in this phase

Per v2 §8 + v2.1 §8.8.

### 7.5 Exit gate (Phase 5 — final)

Maps to v2.1 spec §15 acceptance criteria 1–18.

---

## 8. Gates between phases — strict dependency contract (REVISED per v2.1)

| From → To | Gate condition |
|---|---|
| Phase 0 → 0.5 + 0.6 + 1 | Phase 0 PR #317 merged; audit baselines captured. |
| Phase 0.5 → Phase 4 | One daily snapshot + restore-test passed. |
| Phase 0.6 → Phase 4 | One daily dump + restore-test passed; runbook landed. |
| Phase 1 → 2 | Phase 1 PR #318 merged. |
| Phase 2 → 3 | Phase 2 PR #319 merged; 14 FKs in NOT VALID state. |
| Phase 3 → 3.5 | Phase 3 PR #320 merged in dry_run=True mode; operator reviews 6,083-delete-set. |
| Phase 3.5 → 4 | `parent_resolver` unit tests + integration smoke green; INGEST_ORPHAN_BLOCKED events surfacing; Phase 3 delete-set re-evaluated through resolver; `statement_timeout` raised to 30min via dashboard. |
| Phase 4 → 5 | All 14 tables have `convalidated = true`. |

**Hard parallelism:** Phase 0.5 + Phase 0.6 may run in parallel (separate PRs, separate files). Everything else is strictly sequential.

**No phase skips.** Per v2 spec §5.2: never bundle `ADD CONSTRAINT NOT VALID` with `VALIDATE CONSTRAINT` in the same migration (already enforced by Phase 2's separation from Phase 4).

---

## 9. Risk register (REVISED per v2.1 spec §14 — adds R18-v2.1 through R22-v2.1)

Inherits v2 plan §9 verbatim. v2.1 additions:

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| R18-v2.1 | parent_resolver vendor lookup hits rate limit / returns wrong data | 3.5, 4 | Vendor fallback chain (FMP → Alpaca → SEC); unresolvable tickers log INGEST_ORPHAN_BLOCKED and stay orphan rather than getting bogus parents; quarterly review of `phase4_backfill`-sourced rows |
| R19-v2.1 | db_snapshots/ disk consumption grows beyond expected ~2.5 GB | 0.5 | Daily size monitoring; 30-day retention prune; alert if exceeds 5 GB |
| R20-v2.1 | pg_dump upload fails silently | 0.6 | Upload returns checksum confirmation; BACKUP_COMPLETE event in application_log; missing event = alert |
| R21-v2.1 | prices_daily VALIDATE crosses 30min ceiling even after backfill | 4 | If VALIDATE fails: constraint stays NOT VALID (new rows still protected); re-run cleanup; raise dashboard ceiling further if needed; no downgrade required |
| R22-v2.1 | New in-scope table emerges between v2.1 and Phase 4 final | 4, 5 | Phase 5 reconciles `\dt platform.*` against §3.1; new table gets follow-up PR |

**Risks removed in v2 (still removed in v2.1):** R5 (producer-race window), R10 (cleanup-precondition fragility).

---

## 10. Execution-time checklist (REVISED per v2.1)

For each NEW phase (0.5, 0.6, 3.5) or REVISED phase (4):

1. Branch off fresh `main`: `git fetch origin && git checkout -b feat/refint-v21-p<N>-<topic> origin/main`.
2. Read the relevant v2.1 plan section AND v2.1 spec §7 / §8 / §1.x findings.
3. Run any required Phase 0 / Phase 0.5 / Phase 0.6 dependency checks (e.g. Phase 4 needs db_snapshots + pg_dump + parent_resolver live).
4. Implement per the templates above.
5. Run alembic round-trip for any schema migrations (Phase 4 + Phase 5 only — 0.5/0.6/3.5 are pure producer-code).
6. Run heavy-lane gates per `.claude/agents/db-architect.md` §7.
7. For Phase 4 cleanup PRs: confirm db_snapshots row for the target table from the prior day; this is the recovery baseline.
8. For Phase 4 VALIDATE PRs: open the pg_locks monitor (v2 spec §9.2) in a separate session during migration commit.
9. Open PR with audit numbers + verification-query outputs in the body.
10. Squash-merge on green CI.
11. Pull main locally; verify exit gate; only THEN proceed to next phase.

**Never run two phases in a single PR.** Per v2 spec §5.2: never bundle `ADD CONSTRAINT NOT VALID` with `VALIDATE CONSTRAINT` in the same migration.

---

## 11. v2.1 vs v2 — concrete sequence diff

| Aspect | v2 (superseded) | v2.1 (this plan) |
|---|---|---|
| Phase count | 5 (Phase 0–5; Phase 4 rolling) | **9** (Phase 0, 0.5, 0.6, 1, 2, 3, 3.5, 4, 5) |
| In-scope tables | 15 | **14** (`insider_mspr_daily` removed — VIEW) |
| Wall-clock total | 9–13 hr | **18–27 hr** (3 new phases add ~13–18 hr) |
| Snapshot substrate | none | **Phase 0.5 db_snapshots/** — per-table COPY.gz daily; 30-day retention |
| Disaster recovery | implicit Supabase PITR (7 days) | **Phase 0.6 pg_dump** to S3/R3; 30-day retention; restore runbook; quarterly recovery test |
| Producer-side FK handling | ON DELETE RESTRICT loud-fail only | **Phase 3.5 parent_resolver** — FMP → Alpaca → SEC fallback; auto-resolves unknown tickers; INGEST_ORPHAN_BLOCKED for unresolvable |
| prices_daily orphan protocol | Path B (DELETE 335K bars) | **Path A (BACKFILL ticker_classifications via resolver; preserve bars)** |
| Country source | Alpaca `/v2/assets` (assumed) | **FMP `/stable/profile`** per-ticker (real source) |
| Compat view loud-fail | claimed `cannot insert into view` | **wrong claim** — Postgres auto-rewrites simple views; v2 retroactively chose drop-without-replacement (PR #319) |
| Statement_timeout action | "5min for Phase 2" | **Operator dashboard action: raise to 30min before Phase 4 prices_daily VALIDATE** |
| classify_tickers halt threshold | 1% drift | **Operator review mandatory** (today's drift is 43.7%); dry_run=True default; live mode unblocks after Phase 3.5 + resolver triage |
| Risk count | 16 (R1–R4, R6–R9, R11–R17-v2) | **21** (adds R18-v2.1 through R22-v2.1) |
| Concern-map coverage | informal | **Formal 12-item checklist in spec §2** |

**Riskiest phase v2.1:** Phase 4's `prices_daily` BACKFILL-then-VALIDATE — still 20.6M rows, still 5–30 min wall-clock VALIDATE. NOW operates under:
- Pre-state snapshot from Phase 0.5 (rollback baseline).
- Pre-state pg_dump from Phase 0.6 (tenant-loss baseline).
- Resolver-driven backfill from Phase 3.5 (166 orphan tickers auto-resolve to minimal classifications instead of bars being deleted).
- Statement_timeout raised to 30min via dashboard.
- The constraint stays NOT VALID if VALIDATE fails — new rows still protected; re-run.

Less risky than v2's equivalent (which had no rollback substrate and prescribed deleting real history), but still the largest blast radius.

---

## 12. Out-of-scope (UNCHANGED from v2 plan §12)

Items 1–10 in v2 plan §12 stand.

---

## 13. Open questions (REVISED per v2.1 spec §16)

v2 plan §13 questions 1–3 RESOLVED per v2.1 spec §16:
- Read-replica: no replica today; §9.4 check is a no-op.
- statement_timeout: 120s today; raise to 30min via dashboard before Phase 4 prices_daily.
- Country source: FMP `/stable/profile`; 88% backfilled.

v2.1 questions:
1. Phase 0.6 S3 vs R3 bucket choice — recommendation: R3 reuse.
2. Phase 3.5 resolver triage of Phase 3's 6,083-ticker delete-set — what fraction auto-resolves?
3. Phase 4 prices_daily 166-orphan-ticker resolution rate — FMP recognizability rate.
4. Edge-case tables (`open_orders`, `tradier_options_chains`) — defer per v1 §3.3.

---

**END OF PLAN v2.1.**
