# Referential-Integrity Implementation Plan v2 — `platform.*` schema (NOT-VALID-FIRST sequence)

**Status:** PLAN v2 — no migrations, no code, no tests written. Executable phase-by-phase by a future subagent or the operator without re-investigation.

**Supersedes:** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md` (v1, 11 phases). v1's phase sequence is REPLACED by this doc's 5-phase NOT-VALID-FIRST sequence. v1 stays on disk as the historical record; do not delete it. Where v2 and v1 conflict on sequence, gating, or migration shape, **v2 wins**. The contract (scope, invariants, acceptance criteria) is unchanged — only the *how* and the *order* change.

**Spec basis (read before executing any phase):**
1. `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.md` — the v2 spec this plan implements (5-phase NOT-VALID-FIRST sequence, §5 NOT-VALID pattern, §6 index audit, §9 verification gates, §10 compatibility view, §11 cleanup-template `ctid` fix).
2. `docs/superpowers/specs/2026-05-23-referential-integrity-design.md` — v1 of the spec; §2 invariants, §3 scope, §4 non-goals inherited verbatim.
3. `.claude/agents/db-architect.md` — operating contract: audit-before-alter (§1), source-named tables (§2), Alembic conventions (§3), FK defaults `ON UPDATE CASCADE ON DELETE RESTRICT` (§4), index discipline (§6), heavy-lane gates (§7).
4. `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md` — v1 plan (sequence superseded; tactical migration templates still readable for cross-reference).
5. Postgres canonical `NOT VALID` + `VALIDATE CONSTRAINT` pattern: <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>.
6. Memory `project_database_architecture_state_2026_05_23.md` — schema state + `ticker_classifications.ticker ⊆ prices_daily.ticker` operator invariant.
7. Memory `project_supabase_pro_tier.md` — Supabase Pro tier statement_timeout posture.

**Goal (unchanged from v1):** every `ticker`-bearing table in `platform.*` has a real FK to `platform.ticker_classifications(ticker)` with `ON UPDATE CASCADE ON DELETE RESTRICT`. Drift becomes a constraint violation at INSERT time, not an audit-after-the-fact print line.

**Non-goals (unchanged from v1 §1):** composite `(ticker, date)` FK chains, Tier 2 freshness constraints, RLS policies, macro-table consolidation (Task #18), Phase 2 denormalization (Task #17), per-country insider adapters (Task #15 — depends on this plan's Phase 1 + Phase 2).

---

## 1. v2 phase summary + wall-clock budget

| Phase | Topic | Migrations | Est. wall-clock | Riskiest? |
|---|---|---|---|---|
| **0** | Pre-flight audit — orphan counts, FK-column-index audit, statement_timeout verification, `pg_locks` baseline, `pg_constraint` baseline | 0–N CREATE INDEX CONCURRENTLY (one per child with FK-column-index gap; expected: at least 1 for `universe_candidates`) | 1 hr (incl. CONCURRENT index build) | low |
| **1** | Rename `sec_insider_transactions` → `insider_transactions` + compatibility view + `source` column + CHECK; add `country char(2)` on `ticker_classifications` (nullable, no CHECK yet) | 2 migrations + 1 producer-code change PR | 1.5 hr (incl. Alpaca country backfill) | low–medium (rename grep sweep) |
| **2** | **NOT-VALID-FIRST bulk** — `ADD CONSTRAINT … NOT VALID` on all 15 child tables in ONE migration; fast lock, no row scan; producers cannot create new orphans from this commit onward | 1 migration (15 `op.execute` ops) | 1 hr (sub-second per `ADD CONSTRAINT NOT VALID`; verification dominates) | medium (brief ACCESS EXCLUSIVE × 15) |
| **3** | classify_tickers producer fix — DELETE-source-tracking + `⊆ prices_daily` filter; runs against FK-protected parent so any orphan-creating DELETE fails ON DELETE RESTRICT loud-and-immediate | 0 schema; 1 producer-code PR | 2 hr | medium (producer logic + ON DELETE RESTRICT test) |
| **4** | Per-table cleanup + per-table VALIDATE — rolling, one PR per table (cleanup migration then separate VALIDATE migration), ordered light → heavy; PHASES MUST BE SPLIT (per spec §5.2: NOT VALID and VALIDATE in DIFFERENT migrations to avoid forfeiting the SHARE UPDATE EXCLUSIVE benefit) | 2 migrations × 15 tables = up to 30 migrations total; operator may batch some VALIDATEs per PR | 3–6 hr total spread across many PRs; `prices_daily` VALIDATE dominates (5–30 min wall) | **HIGHEST** (prices_daily 20.6M-row VALIDATE) |
| **5** | Post-FK verification + cleanup — `pg_constraint.convalidated = true` on all 15; replica check; drop `sec_insider_transactions` compatibility view; add `country` CHECK constraint once null tolerance measured | 1 verification PR + 1 cleanup migration (drop view + add country CHECK) | 1 hr | low |

**Total v2 estimated wall-clock budget: 9–13 hours of focused work** spread across ~20 PRs (one Phase 0 PR, two Phase 1 PRs, one Phase 2 PR, one Phase 3 PR, up to 15 Phase 4 PRs operator-batchable to fewer, one Phase 5 PR). **Each phase is gated** — Phase N+1 cannot start until Phase N's verification gate is green.

**v2 vs v1 budget:** v1 = 12–20 hours / 10 sequential PRs. v2 = 9–13 hours / ~20 PRs (smaller per-PR, more PRs because per-table Phase 4 split). v2 saves wall-clock by eliminating v1's Phase 5 cleanup-precondition window and by letting Phase 4 VALIDATEs run on each table's own clock. The 20.6M-row `prices_daily` VALIDATE still dominates; v2 doesn't make it faster, but it makes it the LAST event with the safest blast radius (FKs already enforcing on new rows for weeks by then if the operator chooses to space Phase 4 out).

---

## 2. Phase 0 — Pre-flight audit + index audit + statement_timeout verification (READ-ONLY + 0–N CREATE INDEX CONCURRENTLY)

**Goal:** capture orphan baselines, identify FK-column-index gaps, verify Supabase Pro statement_timeout ceiling, and ship any missing FK-column indexes before Phase 2's `NOT VALID` bulk add lands.

**Deliverables:**
- `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md` — per-table orphan counts using the **MVCC-safe** `WHERE NOT EXISTS` template (NEVER `ctid`; per spec §11).
- `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md` — FK-column-index coverage report per spec §6.2.
- `docs/superpowers/audits/2026-05-23-referential-integrity-timeout-locks-baseline.md` — `statement_timeout` ceiling for the migration role, `pg_locks` baseline snapshot during a nightly ingest, `pg_constraint` baseline snapshot.
- 0–N `CREATE INDEX CONCURRENTLY` migration files (one per child table with no leading-column index on its FK column). Expected: at least one, for `universe_candidates(ticker)` (per spec §6.1 table; PK `(as_of_date, engine, ticker)` does not lead with `ticker`).

### 2.1 Orphan-count audit (MVCC-safe template — per spec §11)

For every in-scope child table T from spec v1 §3.1 (column is `ticker` on every table except `insider_sentiment` where it's `symbol`):

```sql
SELECT '<T>' AS child_table,
       COUNT(*) AS orphan_count,
       COUNT(DISTINCT c.ticker) AS distinct_orphan_tickers
FROM platform.<T> c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.ticker
);
```

For `insider_sentiment` (Finnhub; column is `symbol`):

```sql
SELECT 'insider_sentiment' AS child_table,
       COUNT(*) AS orphan_count,
       COUNT(DISTINCT c.symbol) AS distinct_orphan_tickers
FROM platform.insider_sentiment c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.symbol
);
```

Cross-check counts against any existing `data_quality_log.cross_table_audit.<T>.orphan_no_prices` rows (PR #167 / `tpcore/auditheal`). Discrepancies indicate stale audit rows or a different predicate — investigate before Phase 4 ordering.

### 2.2 FK-column-index audit (per spec §6.2)

```sql
SELECT c.relname AS table_name,
       i.relname AS index_name,
       pg_get_indexdef(i.oid) AS index_def
FROM pg_class c
JOIN pg_index ix ON c.oid = ix.indrelid
JOIN pg_class i ON ix.indexrelid = i.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'platform'
  AND c.relname = ANY(ARRAY[
      'prices_daily','insider_transactions','sec_material_events',
      'corporate_actions','earnings_events','fundamentals_quarterly',
      'short_interest','borrow_rates','social_sentiment',
      'options_max_pain','insider_sentiment','liquidity_tiers',
      'spread_observations','universe_candidates','insider_mspr_daily'
  ])
ORDER BY c.relname, i.relname;
```

For each in-scope child, parse `pg_get_indexdef` and confirm at least one B-tree index has the FK column (`ticker` or, for `insider_sentiment`, `symbol`) as its **leading** column. Spec §6.1 already pre-confirms:
- `spread_observations` — covered by `(ticker, observed_at)` from `20260512_2100_spread_observations_and_liquidity_tiers.py`.
- `universe_candidates` — **GAP.** PK `(as_of_date, engine, ticker)` does not lead with `ticker`; `idx_uc_engine_date` covers `(engine, as_of_date)`. Needs `CREATE INDEX CONCURRENTLY idx_universe_candidates_ticker ON platform.universe_candidates (ticker);`.
- Every other table — verify; expected covered by existing PK first-column index. If any other gap surfaces, add a CONCURRENTLY index migration in Phase 0.

### 2.3 CREATE INDEX CONCURRENTLY migration template (one file per gap)

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_idx_concurrently_<table>_ticker.py`
**revision:** new revision ID
**down_revision:** prior alembic head at time-of-write (run `alembic heads` to confirm)

```text
def upgrade():
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_<table>_ticker
            ON platform.<table> (<fk_column>)
        """)

def downgrade():
    with op.get_context().autocommit_block():
        op.execute("""
            DROP INDEX CONCURRENTLY IF EXISTS platform.idx_<table>_ticker
        """)
```

**Why `autocommit_block`:** `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. Per Alembic docs, `op.get_context().autocommit_block()` is the canonical pattern. Lock is `SHARE UPDATE EXCLUSIVE` — concurrent reads + writes proceed.

### 2.4 Statement-timeout verification (per spec §9.1)

```sql
-- Current session
SHOW statement_timeout;
-- Defaults
SELECT name, setting, source, context
FROM pg_settings
WHERE name IN ('statement_timeout','lock_timeout','idle_in_transaction_session_timeout');
-- Role-level cap
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = current_user;
```

**Required budgets** (capture in audit doc; raise via Supabase dashboard if role cap < budget):
- Phase 2 bulk: `SET LOCAL statement_timeout = '5min'` (15 sub-second `ADD CONSTRAINT NOT VALID` ops in series + per-table ACCESS EXCLUSIVE).
- Phase 4 `prices_daily` VALIDATE: `SET LOCAL statement_timeout = '30min'` (20.6M-row scan under SHARE UPDATE EXCLUSIVE).
- Per spec §9.1: v2 does NOT assume `SET LOCAL` overrides the role cap on Supabase Pro — that's a dashboard setting at the role level. **Document the dashboard-override path** in the audit doc; operator raises the cap before Phase 2 if needed.

### 2.5 `pg_locks` baseline snapshot

During the nightly ingest cycle (UTC 21:30 = local 05:30 Manila, per memory `project_manila_utc_everything.md`), capture a typical lock state on the 15 in-scope tables:

```sql
SELECT l.pid, l.locktype, l.mode, l.granted,
       c.relname AS table_name, a.application_name,
       a.query_start, a.state,
       LEFT(a.query, 100) AS query_snippet
FROM pg_locks l
LEFT JOIN pg_class c ON l.relation = c.oid
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
LEFT JOIN pg_stat_activity a ON l.pid = a.pid
WHERE n.nspname = 'platform'
  AND c.relname = ANY(ARRAY[<in-scope tables>])
ORDER BY l.granted, l.pid;
```

Save the snapshot. Phase 2 / Phase 4 monitoring queries will compare against this baseline to detect lock escalation (per spec §9.2).

### 2.6 `pg_constraint` baseline snapshot

```sql
SELECT conname, conrelid::regclass AS table_name, contype, convalidated
FROM pg_constraint
WHERE connamespace = 'platform'::regnamespace
  AND contype = 'f'
ORDER BY conname;
```

Expected pre-Phase-2: zero FK rows referencing `platform.ticker_classifications`. The post-Phase-2 check (§3.4 below) compares against this baseline.

### 2.7 Exit gate (Phase 0)

- Per-table orphan counts + distinct-orphan-ticker counts captured in `2026-05-23-referential-integrity-baseline.md`.
- FK-column-index coverage report complete; every gap closed via `CREATE INDEX CONCURRENTLY` migration.
- Statement-timeout ceiling documented; dashboard override path confirmed.
- `pg_locks` + `pg_constraint` baseline snapshots captured.
- All Phase 0 CONCURRENTLY index migrations round-trip green per spec §9.5.

---

## 3. Phase 1 — Rename + compatibility view + `country` column (NO FKs yet)

**Goal:** ship the two pre-FK schema preconditions (rename + country) so Phase 2's NOT-VALID bulk has its target shape. **classify_tickers producer change is deliberately deferred to Phase 3** — v2 sequence inversion (per spec §10 and the spec's §7 Phase 3 reasoning).

### 3.1 Migration 1 — rename `sec_insider_transactions` → `insider_transactions` + compatibility view + `source` column + CHECK

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_rename_sec_insider_transactions_with_view.py`
**revision:** new
**down_revision:** Phase 0 final head (any CONCURRENTLY index added)

```text
def upgrade():
    # 1. Rename the table.
    op.execute("ALTER TABLE platform.sec_insider_transactions RENAME TO insider_transactions")
    # 2. Rename indexes that embed the old name (verify exact names with \di in Phase 0).
    op.execute("ALTER INDEX IF EXISTS platform.sec_insider_transactions_pkey RENAME TO insider_transactions_pkey")
    op.execute("ALTER INDEX IF EXISTS platform.idx_sec_insider_ticker_date RENAME TO idx_insider_transactions_ticker_date")
    # 3. Add source column with default 'sec' (current sole producer); NOT NULL after backfill.
    op.add_column("insider_transactions",
        sa.Column("source", sa.Text(), nullable=True, server_default="sec"),
        schema="platform")
    op.execute("UPDATE platform.insider_transactions SET source = 'sec' WHERE source IS NULL")
    op.alter_column("insider_transactions", "source", nullable=False, schema="platform")
    op.create_check_constraint("ck_insider_transactions_source",
        "insider_transactions", "source IN ('sec', 'fmp')",
        schema="platform")
    # 4. Compatibility view at old name (SELECT-only). Per spec §10.1: missed read
    # consumers continue to work; missed INSERT/UPDATE/DELETE consumers fail
    # loud (`cannot insert into view`).
    op.execute("""
        CREATE VIEW platform.sec_insider_transactions AS
            SELECT * FROM platform.insider_transactions WHERE source = 'sec'
    """)

def downgrade():
    op.execute("DROP VIEW IF EXISTS platform.sec_insider_transactions")
    op.drop_constraint("ck_insider_transactions_source", "insider_transactions", schema="platform")
    op.drop_column("insider_transactions", "source", schema="platform")
    op.execute("ALTER INDEX IF EXISTS platform.insider_transactions_pkey RENAME TO sec_insider_transactions_pkey")
    op.execute("ALTER INDEX IF EXISTS platform.idx_insider_transactions_ticker_date RENAME TO idx_sec_insider_ticker_date")
    op.execute("ALTER TABLE platform.insider_transactions RENAME TO sec_insider_transactions")
```

**Same-PR producer-code change (not a migration):**

- Update all known INSERT/UPDATE/DELETE paths to write to `platform.insider_transactions` with `source='sec'`:
  - `tpcore/sec/edgar_adapter.py`
  - `tpcore/ingestion/handlers.py` (`handle_sec_filings`)
  - `tpcore/audit/cross_table.py` (any `CROSS_TABLE_CHECKS` table-name reference)
  - `scripts/ops.py`
  - Run `grep -rn 'sec_insider_transactions' --include='*.py' --include='*.sql' --include='*.md' --include='*.sh'` and triage every hit by call-site (write vs read).
- READ consumers (dashboards, ad-hoc queries) are NOT required to migrate in this PR — they flow through the compatibility view.
- `docs/DATABASE_AND_DATAFLOW.md` §2.2 — rename section heading + body to `insider_transactions`.

### 3.2 Migration 2 — add `country char(2)` on `ticker_classifications` (NULLABLE; CHECK deferred to Phase 5)

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_ticker_classifications_country_column.py`
**revision:** new
**down_revision:** Phase 1 Migration 1

```text
def upgrade():
    op.add_column("ticker_classifications",
        sa.Column("country", sa.CHAR(length=2), nullable=True),
        schema="platform")
    op.create_index("idx_ticker_classifications_country",
        "ticker_classifications", ["country"], schema="platform",
        postgresql_where=sa.text("country IS NOT NULL"))
    # CHECK constraint deferred to Phase 5 (per spec §12 — measure null tolerance first).

def downgrade():
    op.drop_index("idx_ticker_classifications_country",
        table_name="ticker_classifications", schema="platform")
    op.drop_column("ticker_classifications", "country", schema="platform")
```

**Same-PR producer-code change (not a migration):**

- `scripts/classify_tickers.py` — pull `country` from Alpaca `/v2/assets` response (`asset['country']` ISO2 if present).
- Run a one-shot backfill stage invocation (`--stage classify_tickers`) to populate the column.
- Per spec §12: log the per-asset-class null-rate breakdown in the PR body. **No hard threshold.** Expected: ~0–2% null for common-stock-US, ~30–50% null for ETFs, ~40–60% null for closed-end funds (Alpaca documentation, 2026).
- PR body must enumerate Phase 1.5 follow-up backfill sources (Polygon `/v3/reference/tickers`, OpenFIGI, FMP company-profile, manual ETF curation) as TODO — operator picks one or defers Task #15.

### 3.3 Verification queries (capture in PR bodies)

```sql
-- Rename verification: row count via the view matches the source.
SELECT (SELECT COUNT(*) FROM platform.insider_transactions WHERE source='sec') AS table_count,
       (SELECT COUNT(*) FROM platform.sec_insider_transactions) AS view_count;
-- Expected: equal.

-- Source-CHECK verification:
SELECT source, COUNT(*) FROM platform.insider_transactions GROUP BY source;
-- Expected: 'sec' only at this point.

-- Compatibility-view loud-fail on writes (smoke test in a transaction):
BEGIN;
INSERT INTO platform.sec_insider_transactions (ticker, filing_date, ...) VALUES (...);
-- Expected: ERROR  cannot insert into view "sec_insider_transactions"
ROLLBACK;

-- Country backfill distribution:
SELECT country, asset_class, COUNT(*)
FROM platform.ticker_classifications
GROUP BY country, asset_class
ORDER BY 3 DESC LIMIT 30;
SELECT asset_class, COUNT(*) FILTER (WHERE country IS NULL) AS nulls,
       COUNT(*) AS total,
       ROUND(100.0 * COUNT(*) FILTER (WHERE country IS NULL) / COUNT(*), 1) AS null_pct
FROM platform.ticker_classifications
GROUP BY asset_class
ORDER BY null_pct DESC;
```

### 3.4 Rollback (downgrade) path

- Migration 1: `DROP VIEW` → drop CHECK → drop `source` column → revert index renames → `ALTER TABLE … RENAME TO sec_insider_transactions`. Producer-code rollback = `git revert` the producer PR.
- Migration 2: drop index → drop column. `country` data lost on downgrade — acceptable since the data is rederivable from Alpaca within minutes.

### 3.5 Test contracts (already pinned in spec §8.5, §8.6)

- `ck_insider_transactions_source` CHECK rejects rows with `source NOT IN ('sec','fmp')` — INSERT-violation smoke test in `tpcore/tests/test_referential_integrity.py`.
- `country` column accepts NULL and `'^[A-Z]{2}$'` values (CHECK lands in Phase 5; for now, test the column type and the partial index).
- Compatibility-view loud-fail-on-INSERT test.

### 3.6 Exit gate (Phase 1)

- Both migrations round-trip green per spec §9.5 (`upgrade head` → `downgrade -1` → `upgrade head`).
- Rename grep sweep complete; every write-side hit triaged + migrated.
- View read-count matches table read-count (Phase 1 verification query 1).
- Country backfill landed; per-asset-class null-rate documented in PR body.
- Heavy-lane gates green per `.claude/agents/db-architect.md` §7.

---

## 4. Phase 2 — NOT-VALID-FIRST bulk FK add (ALL 15 FKs in ONE migration)

**The v2 inversion.** One migration adds `FOREIGN KEY … NOT VALID` to every in-scope child from spec v1 §3.1. From the moment this migration commits, producers cannot create new orphans. Per spec §5.1: `NOT VALID` constraints enforce on INSERT/UPDATE immediately; only existing rows remain unvalidated.

### 4.1 Migration template

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_fk_not_valid_all_15_tables.py`
**revision:** new
**down_revision:** Phase 1 Migration 2

```text
def upgrade():
    op.execute("SET LOCAL statement_timeout = '5min'")  # per spec §9.1
    # 15 ADD CONSTRAINT NOT VALID ops, one per in-scope child.
    # Each acquires ACCESS EXCLUSIVE briefly (sub-second per table per Postgres docs).
    op.execute("""
        ALTER TABLE platform.prices_daily
            ADD CONSTRAINT fk_prices_daily_ticker
            FOREIGN KEY (ticker)
            REFERENCES platform.ticker_classifications(ticker)
            ON UPDATE CASCADE ON DELETE RESTRICT
            NOT VALID
    """)
    op.execute("""
        ALTER TABLE platform.insider_transactions
            ADD CONSTRAINT fk_insider_transactions_ticker
            FOREIGN KEY (ticker)
            REFERENCES platform.ticker_classifications(ticker)
            ON UPDATE CASCADE ON DELETE RESTRICT
            NOT VALID
    """)
    # ... repeat for the remaining 13:
    #   sec_material_events, corporate_actions, earnings_events,
    #   fundamentals_quarterly, short_interest, borrow_rates,
    #   social_sentiment, options_max_pain, liquidity_tiers,
    #   spread_observations, universe_candidates, insider_mspr_daily.
    # And insider_sentiment uses the symbol column:
    op.execute("""
        ALTER TABLE platform.insider_sentiment
            ADD CONSTRAINT fk_insider_sentiment_symbol
            FOREIGN KEY (symbol)
            REFERENCES platform.ticker_classifications(ticker)
            ON UPDATE CASCADE ON DELETE RESTRICT
            NOT VALID
    """)

def downgrade():
    # Fast: pure DDL DROP per constraint.
    op.execute("ALTER TABLE platform.insider_sentiment DROP CONSTRAINT IF EXISTS fk_insider_sentiment_symbol")
    op.execute("ALTER TABLE platform.insider_mspr_daily DROP CONSTRAINT IF EXISTS fk_insider_mspr_daily_ticker")
    # ... reverse order.
    op.execute("ALTER TABLE platform.prices_daily DROP CONSTRAINT IF EXISTS fk_prices_daily_ticker")
```

**Note on Alembic API:** `op.create_foreign_key` does NOT support `NOT VALID` as a kwarg as of Alembic 1.18. Use raw `op.execute("ALTER TABLE … ADD CONSTRAINT … NOT VALID")` — verify the kwarg state at write-time and only fall back to `op.create_foreign_key` if a future Alembic version adds it (cleaner round-trip).

### 4.2 Pre-migration gates

- Phase 0 deliverables green (all 15 FK columns indexed; statement_timeout cap verified).
- Phase 1 migrations landed on main (rename + view + country backfilled).
- `alembic heads` against live DB returns single head (no divergence).
- `pg_constraint` snapshot confirms no `fk_*_ticker` constraints exist yet on `ticker_classifications`.
- In a separate session, prepare the `pg_locks` monitor query (per spec §9.2) — run it during migration commit.

### 4.3 Verification queries (post-migration, capture in PR body)

```sql
-- 4.3.1 Confirm all 15 FKs added with correct semantics, all in NOT VALID state.
SELECT conname,
       conrelid::regclass AS child_table,
       confrelid::regclass AS parent_table,
       convalidated,
       confupdtype,   -- expect 'c' (CASCADE)
       confdeltype    -- expect 'r' (RESTRICT)
FROM pg_constraint
WHERE contype = 'f'
  AND confrelid = 'platform.ticker_classifications'::regclass
ORDER BY conname;
-- Expected: 15 rows, all with convalidated=false, confupdtype='c', confdeltype='r'.

-- 4.3.2 NOT-VALID-still-enforces smoke test (the load-bearing v2 assertion).
BEGIN;
INSERT INTO platform.universe_candidates (as_of_date, engine, ticker)
VALUES (CURRENT_DATE, 'momentum', 'NEVER_EXISTED_XYZ');
-- Expected: ERROR  insert or update on table "universe_candidates" violates
--          foreign key constraint "fk_universe_candidates_ticker"
ROLLBACK;

-- 4.3.3 pg_locks post-snapshot — confirm no lingering AccessExclusiveLock on
-- any in-scope table after migration commit.
SELECT l.pid, l.locktype, l.mode, l.granted, c.relname
FROM pg_locks l JOIN pg_class c ON l.relation = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'platform'
  AND c.relname = ANY(ARRAY[<in-scope tables>])
  AND l.mode LIKE '%Exclusive%';
-- Expected: empty result (migration session lock released cleanly).

-- 4.3.4 Read-replica propagation (per spec §9.4) — if replica provisioned,
-- query the same pg_constraint snapshot from each replica connection and
-- confirm convergence within seconds.
SELECT COUNT(*) FROM pg_constraint
WHERE contype = 'f'
  AND confrelid = 'platform.ticker_classifications'::regclass;
-- Expected on each replica: 15 (within seconds of primary commit).
```

### 4.4 Rollback (downgrade) path

- `DROP CONSTRAINT` × 15 in `downgrade()` — fast, ms-scale per drop, ACCESS EXCLUSIVE briefly per table.
- Forward-recoverable: if a producer breaks post-Phase-2 due to a missed orphan-producing path (which Phase 0 audit should have caught), revert the Phase 2 PR and re-investigate the offending producer.

### 4.5 Test contracts pinned in this phase

- **NOT-VALID-still-enforces test** (spec §8.7) — added to `tpcore/tests/test_referential_integrity.py`. Asserts that for at least one in-scope FK in `convalidated = false` state, INSERT of an orphan ticker raises `asyncpg.ForeignKeyViolationError`. **This test is load-bearing for v2 correctness** — proves `NOT VALID` ≠ "inactive constraint".
- **Constraint-presence test** (spec §8.1) — `SELECT COUNT(*) FROM pg_constraint WHERE contype='f' AND confrelid='platform.ticker_classifications'::regclass` returns ≥15.
- **DELETE-RESTRICT smoke test** (spec §8.2) — try to delete a ticker that has at least one child row; expect `ForeignKeyViolationError`. Works even with `convalidated=false` because RESTRICT is enforced by constraint definition, not by validation state.

### 4.6 Exit gate (Phase 2)

- All 15 FKs present in `pg_constraint` with `convalidated=false`, `confupdtype='c'`, `confdeltype='r'`.
- §4.3.2 NOT-VALID-still-enforces smoke test passes against at least one table.
- §4.3.3 pg_locks post-snapshot confirms no lingering AccessExclusiveLock.
- §4.3.4 replica propagation check captured (or documented as no-op if no replica provisioned).
- Alembic round-trip green.
- Heavy-lane gates green.
- Producer regression smoke: a single nightly ingest cycle runs clean (no producer creates an orphan, since classify_tickers hasn't been migrated yet — every ticker producers write was already in ticker_classifications pre-Phase-2).

**Critical post-Phase-2 invariant:** from this commit forward, producers physically cannot create new orphans. The Phase 4 cleanup-then-VALIDATE rolling sequence is now bounded by the existing-orphan set captured in Phase 0 — that set can only shrink, never grow.

---

## 5. Phase 3 — classify_tickers DELETE-source-tracking + `⊆ prices_daily` filter (producer-code PR; no schema migration)

**Why AFTER Phase 2 (v2's critical sequence reversal):** v1 puts the producer change BEFORE any FK (so DELETEs don't trip `ON DELETE RESTRICT` during the inevitable producer-race window between Phase 5 cleanup and Phase 8 VALIDATE). v2 has no such window — Phase 2's NOT-VALID FKs already enforce `ON DELETE RESTRICT` (RESTRICT is enforced by constraint definition, NOT by validation state — per Postgres docs and spec §7 Phase 3). So in v2 the producer change runs against a FK-protected parent, and any orphan-creating DELETE fails loud-and-immediate rather than silently producing an orphan that VALIDATE catches 5–30 minutes later.

### 5.1 Producer change (file-level scope)

- `scripts/classify_tickers.py` (or wherever the producer body lives; locate during exec).
  1. Pull Alpaca `/v2/assets` set (source-of-truth universe `A`).
  2. Pull distinct `prices_daily.ticker` set (`P`).
  3. Compute upsert set `U = A ∩ P` (operator invariant: ticker not in daily-bars → not in classification).
  4. Compute delete set `D = {existing ticker_classifications.ticker} - U`.
  5. Single transaction: `INSERT … ON CONFLICT DO UPDATE` for `U`; then `DELETE FROM platform.ticker_classifications WHERE ticker = ANY($D)`.
  6. Log `+inserts / ~updates / -deletes` counts to `application_log` for visibility.
- Risk mitigation: first-run dry-mode (`--param dry_run=true`) surfaces `|D|` size. If `|D| > 1%` of universe, halt + operator review.
- Persist the delete-set to `application_log.data` JSON before the live DELETE executes (forensics recoverable).

### 5.2 The new v2 loud-fail behaviour (critical to test)

In v2, any `XYZ` in the delete-set that still has live children fails with `ForeignKeyViolationError` on commit. This is **strictly better than v1's silent ordering** — the failure surfaces immediately and the operator chooses path A (BACKFILL ticker_classifications with `XYZ`) or path B/C (whitelist `XYZ` for explicit cleanup in Phase 4).

### 5.3 Test contracts pinned in this phase

- **ON DELETE RESTRICT blocks delete with live children** — explicit pytest:

```text
async def test_classify_tickers_delete_blocked_by_restrict(pool):
    """A classify_tickers DELETE of a ticker with live prices_daily rows
    must fail with ForeignKeyViolationError under the v2 NOT VALID FK."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "DELETE FROM platform.ticker_classifications WHERE ticker = 'AAPL'"
                )
```

- **Producer-side `A ∩ P` filter** — unit test on the producer body confirms a ticker present in Alpaca but absent from `prices_daily` is NOT upserted.
- **Producer-side delete-set computation** — unit test on the `D` computation confirms it equals `{existing} - U`.

### 5.4 Verification queries (post-deploy, capture in PR body)

```sql
-- 5.4.1 Drift check post-producer-run.
SELECT COUNT(*) AS classifications_not_in_prices
FROM platform.ticker_classifications tc
WHERE NOT EXISTS (SELECT 1 FROM platform.prices_daily p WHERE p.ticker = tc.ticker);
-- Expected: 0.

-- 5.4.2 Dry-run audit row for the live run.
SELECT data->>'inserts' AS inserts, data->>'updates' AS updates, data->>'deletes' AS deletes,
       data->'delete_set' AS delete_set
FROM platform.application_log
WHERE event_type = 'classify_tickers.summary'
ORDER BY ts DESC LIMIT 1;

-- 5.4.3 Any ForeignKeyViolation logged during the live run.
SELECT COUNT(*) FROM platform.application_log
WHERE event_type = 'classify_tickers.fk_violation' AND ts > now() - interval '1 hour';
-- Expected: 0 if cleanup is well-ordered; >0 means Phase 4 cleanup is urgent
-- for the offending child table.
```

### 5.5 Rollback (downgrade) path

- Producer-code revert (`git revert` the PR). No schema rollback needed.
- If the live run produced unintended `ForeignKeyViolation`s, the parent DELETE was aborted by Postgres — no schema state changed. Re-investigate and re-run.

### 5.6 Exit gate (Phase 3)

- Producer unit tests green.
- Dry-run produces `|D| < 100` (expected: today's accumulated +46 drift + ~200 historical FMP-coverable-only ADRs not in Alpaca's asset list).
- Operator approves the delete-set; live run executes; row-count delta matches dry-run forecast.
- §5.4.1 drift = 0.
- §5.4.3 no `ForeignKeyViolation` rows (or, if any, Phase 4 cleanup ordering re-prioritises affected children).

---

## 6. Phase 4 — Per-table cleanup + per-table VALIDATE (rolling, at operator's leisure)

The core of v2's at-leisure approach. **Per-table, one PR per table** (operator may batch multiple light tables per PR). For each in-scope table T with `orphan_count > 0` from Phase 0:

1. **Per-table operator decision** — A (BACKFILL), B (DELETE), or C (ARCHIVE-then-DELETE).
2. **Cleanup migration** using the `WHERE NOT EXISTS` template (per spec §11 — NEVER `ctid`).
3. **Separate VALIDATE CONSTRAINT migration** in the NEXT PR (per spec §5.2: cleanup and VALIDATE MUST live in different migrations).
4. **Verification gates per spec §9** (pg_locks during VALIDATE, pg_constraint post-VALIDATE, replica propagation).

### 6.1 Per-table operator decision matrix (filled from Phase 0 baseline)

| Table | Orphan count (Phase 0) | Recommended path | Operator sign-off |
|---|---|---|---|
| `liquidity_tiers` | TBD | A (small derived; backfill from prices_daily) | — |
| `options_max_pain` | TBD | A (single SPY symbol; must be in classifications) | — |
| `borrow_rates` | TBD | B (iborrowdesk transient) | — |
| `insider_sentiment` | TBD | B (Finnhub returns broader universe than US-listed) | — |
| `social_sentiment` | TBD | B (ApeWisdom returns random crypto tickers) | — |
| `short_interest` | TBD | B (FINRA bi-monthly stale) | — |
| `spread_observations` | TBD | B (derived; rebuildable) | — |
| `universe_candidates` | TBD | C (historical engine outputs; archive then delete) | — |
| `insider_mspr_daily` | TBD | derived; rebuild after parent cleaned | — |
| `corporate_actions` | TBD | A (Alpaca canonical; backfill ticker_classifications) | required |
| `earnings_events` | TBD | likely B for delisted, A for active | required |
| `sec_material_events` | TBD | A (SEC canonical) | — |
| `fundamentals_quarterly` | TBD | B for delisted, A for active | required |
| `insider_transactions` | TBD | A (foreign ADRs — backfill with country='XX' via Phase 1.5 source) | required |
| `prices_daily` | TBD | likely A (these tickers SHOULD be in classifications) | required |

### 6.2 Cleanup migration template — Path B (DELETE; MVCC-safe per spec §11)

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_cleanup_orphans_<T>.py`
**revision:** new
**down_revision:** prior alembic head

```text
def upgrade():
    op.execute("""
        DELETE FROM platform.<T> c
        WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_classifications p
            WHERE p.ticker = c.ticker
        )
    """)
    # For insider_sentiment (column is symbol):
    # DELETE FROM platform.insider_sentiment c
    # WHERE NOT EXISTS (SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.symbol)

def downgrade():
    # Orphan cleanup is forward-only by design; downgrade is documentation:
    op.execute("-- forward-only; restore from prices_daily archive if needed")
```

**Why NOT `ctid` (spec §11 mandate):** `ctid` is row-version-volatile. A concurrent producer UPDATE between CTE materialization and DELETE invalidates the captured `ctid` — the DELETE then targets a different row or no row at all. v1's plan §7.1 template used `ctid` as the default; v2 mandates `WHERE NOT EXISTS` (Postgres MVCC handles concurrency correctly, statement-start snapshot semantics).

### 6.3 Cleanup migration template — Path A (BACKFILL)

```text
def upgrade():
    op.execute("""
        INSERT INTO platform.ticker_classifications (ticker, source, last_updated)
        SELECT DISTINCT p.ticker, 'backfill', now()
        FROM platform.prices_daily p
        WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_classifications c WHERE c.ticker = p.ticker
        )
        ON CONFLICT (ticker) DO NOTHING
    """)
```

### 6.4 Cleanup migration template — Path C (ARCHIVE-then-DELETE)

```text
def upgrade():
    op.execute("""
        INSERT INTO platform.<T>_archive
        SELECT * FROM platform.<T> c
        WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.ticker
        )
    """)
    op.execute("""
        DELETE FROM platform.<T> c
        WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.ticker
        )
    """)
```

### 6.5 VALIDATE migration template — **SEPARATE MIGRATION, SEPARATE PR**

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_validate_fk_<T>.py`
**revision:** new
**down_revision:** the cleanup migration for the same table

```text
def upgrade():
    # SHARE UPDATE EXCLUSIVE — concurrent reads + writes proceed.
    # For prices_daily (20.6M rows): set 30min budget; off-window scheduling.
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute("ALTER TABLE platform.<T> VALIDATE CONSTRAINT fk_<T>_ticker")

def downgrade():
    # No-op: cannot un-validate a constraint in Postgres without dropping it.
    # Documentation-only downgrade per spec §9.5.
    op.execute("-- VALIDATE is forward-only; drop the constraint via Phase 2 downgrade to fully revert")
```

**Why split from cleanup (spec §5.2):** wrapping `ADD CONSTRAINT NOT VALID` and `VALIDATE CONSTRAINT` in one transaction forces Postgres to hold the stronger lock for the entire compound operation, forfeiting the SHARE UPDATE EXCLUSIVE benefit. Same principle applies to cleanup + VALIDATE: a cleanup DELETE on `prices_daily` (20.6M rows) is itself a long operation; bundling it with the VALIDATE forces a single mega-transaction whose lock holds longer than necessary. SEPARATE PRs let the cleanup commit + VACUUM before VALIDATE runs.

### 6.6 Ordering recommendation (light → heavy)

Operator may re-order based on which tables they care about validating fastest:

1. `liquidity_tiers` (~7K) — Tier-2 derived, small.
2. `options_max_pain` (~5K) — single SPY.
3. `borrow_rates` (~20K).
4. `insider_sentiment` (~10K) — column is `symbol`.
5. `social_sentiment` (~50K).
6. `short_interest` (~30K).
7. `spread_observations` (~30K).
8. `universe_candidates` (~20K) — note new ticker index from Phase 0.
9. `insider_mspr_daily` (TBD — verify shape in Phase 0).
10. `corporate_actions` (~50K).
11. `earnings_events` (~80K).
12. `sec_material_events` (~200K).
13. `fundamentals_quarterly` (~178K).
14. `insider_transactions` (~1M).
15. **`prices_daily` (~20.6M) — THE BIG ONE; dedicated off-window maintenance slot.**

Phase 4 can pause indefinitely between tables — the FKs are already enforcing for new rows. Operator may merge multiple per-table VALIDATEs into a single PR if convenient; one PR per table is the safe default. **Operator may stop the rollout after Phase 2 + a partial Phase 4** without compromising new-orphan protection.

### 6.7 Per-table verification gates (per spec §9)

For each PR (cleanup + VALIDATE):

- **Cleanup PR exit gate:**
  - `SELECT COUNT(*) FROM platform.<T> c WHERE NOT EXISTS (SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.<fk>) ` returns 0.
  - Alembic round-trip green for the cleanup migration.
  - Heavy-lane gates green.
- **VALIDATE PR exit gate:**
  - During VALIDATE (separate session), `pg_locks` monitor (spec §9.2) shows ONLY `ShareUpdateExclusiveLock` on the target table; no AccessExclusiveLock appears (other than the migration's own brief grab); no blocked process accumulates > 60s wait.
  - Post-VALIDATE: `SELECT convalidated FROM pg_constraint WHERE conname = 'fk_<T>_ticker'` returns `true`.
  - Replica propagation (per spec §9.4): query each replica connection; `convalidated = true` converges within seconds.
  - `pg_stat_user_constraints` (or equivalent fallback query against `pg_constraint`) confirms the constraint is active and enforcing.
  - Producer regression smoke for any producer that writes to T: one ingest cycle clean.

### 6.8 prices_daily special handling

- **Maintenance window:** schedule during local 04:00–08:00 UTC (off-window per memory `project_manila_utc_everything.md` — US market closed; engine cron quiet).
- **No `daily_bars` stage in-flight**; **no engine cron in next 60 min**.
- Operator present to monitor + abort if statement_timeout approaches.
- `VALIDATE CONSTRAINT` may take 5–30 minutes on 20.6M rows; SHARE UPDATE EXCLUSIVE means concurrent reads + writes proceed (engines unaffected for SELECT).
- If `VALIDATE` fails on an orphan slipping through: the constraint stays `NOT VALID` (new rows still protected). Re-run the cleanup for `prices_daily` (find the missed orphan via the spec §11 query), then re-submit a VALIDATE PR. No downgrade needed.

### 6.9 Rollback path (per-table)

- Cleanup migration: forward-only by design (deleted/archived rows). Downgrade = documentation only. If a cleanup PR proves wrong, re-ingest the missing data via the relevant producer.
- VALIDATE migration: downgrade is a no-op (cannot un-validate in Postgres). To fully revert, downgrade Phase 2 (drops the constraint), then re-execute Phase 2 + Phase 4 from a fresh baseline. Per spec §9.5: this is acceptable because VALIDATE is forward-only-safe.

### 6.10 Exit gate (Phase 4 overall)

Phase 4 is "done" when every in-scope table has `pg_constraint.convalidated = true`. The exit gate fires Phase 5.

---

## 7. Phase 5 — Post-FK verification + cleanup (drop view + add country CHECK)

### 7.1 Verification PR (no migration)

- **All 15 FKs validated** (per spec acceptance criterion 1):

```sql
SELECT COUNT(*) FROM pg_constraint
WHERE contype='f' AND confrelid='platform.ticker_classifications'::regclass
  AND convalidated = true;
-- Expected: ≥15.
```

- **DELETE-RESTRICT smoke test** for 3 sample tickers (AAPL = heavy / SPY = derived-dependency / a newly-listed low-row ticker):

```sql
BEGIN;
DELETE FROM platform.ticker_classifications WHERE ticker = 'AAPL';
-- Expected: ERROR  update or delete on table "ticker_classifications" violates
--          foreign key constraint "fk_prices_daily_ticker" on table "prices_daily"
ROLLBACK;
```

- **Replica propagation final check** — all replicas converge to all-validated state.
- **Producer regression sweep** — run each `--stage` once, verify no FK violations: `daily_bars`, `corporate_actions`, `fundamentals_refresh`, `compute_fundamental_ratios`, `earnings_events`, `sec_filings`, `finra_short_interest`, `iborrowdesk_borrow_rates`, `apewisdom_social_sentiment`, `finnhub_insider_sentiment`, `greeks_max_pain`, `classify_tickers`, `assign_liquidity_tiers`, `universe_candidates_refresh`, `compute_insider_mspr_daily`.
- **`tpcore/auditheal` cross-table audit reads all-green** (the audit checks become assertion-style sentinels, per v1 Task #10).
- **ERD + docs update:** `docs/DATABASE_AND_DATAFLOW.md` §2.1 ERD adds FK arrows from every ticker-bearing entity to `TICKER_CLASSIFICATIONS`; §2.2 note: *"Every `ticker` column FK → `platform.ticker_classifications(ticker)` with `ON UPDATE CASCADE ON DELETE RESTRICT`; landed as NOT VALID in Phase 2 then per-table VALIDATED in Phase 4."*
- **Memory update:** mark `project_database_architecture_state_2026_05_23.md` outstanding-debt item DONE.

### 7.2 Cleanup migration — drop compatibility view + add country CHECK

**File:** `platform/migrations/versions/<YYYYMMDD_HHMM>_drop_sec_insider_view_add_country_check.py`
**revision:** new
**down_revision:** last Phase 4 VALIDATE

```text
def upgrade():
    # Drop the compatibility view once grep against the view returns zero hits.
    # Operator may defer this for a follow-up PR if any consumer still uses it.
    op.execute("DROP VIEW IF EXISTS platform.sec_insider_transactions")
    # Add country CHECK constraint now that Phase 1 null-rate is measured.
    op.create_check_constraint("ck_ticker_classifications_country_iso",
        "ticker_classifications",
        "country IS NULL OR country ~ '^[A-Z]{2}$'",
        schema="platform")

def downgrade():
    op.drop_constraint("ck_ticker_classifications_country_iso",
        "ticker_classifications", schema="platform")
    op.execute("""
        CREATE VIEW platform.sec_insider_transactions AS
            SELECT * FROM platform.insider_transactions WHERE source = 'sec'
    """)
```

**Pre-PR check (operator-gated):**
- `grep -rn 'sec_insider_transactions' --include='*.py' --include='*.sql'` returns zero hits (or only the historical migration files and v1/v2 docs).
- Phase 1 country null-rate report: confirm the operator has accepted the measured tolerance (per spec §12). If a Phase 1.5 backfill source landed, the null-rate has tightened; if not, the CHECK still accepts NULLs.

### 7.3 Verification (capture in PR body)

```sql
-- View dropped:
SELECT 1 FROM pg_views WHERE schemaname='platform' AND viewname='sec_insider_transactions';
-- Expected: empty.

-- Country CHECK present:
SELECT conname FROM pg_constraint
WHERE conrelid = 'platform.ticker_classifications'::regclass
  AND contype = 'c';
-- Expected: includes 'ck_ticker_classifications_country_iso'.
```

### 7.4 Test contracts pinned in this phase

- **Cross-table-orphan sentinel** (spec §8.4) — every `cross_table_audit.<T>.orphan_no_prices` row in `data_quality_log` reads 0.
- **Source-tag CHECK** (spec §8.5) — `insider_transactions.source` rejects values other than `'sec'`/`'fmp'`.
- **Country-format CHECK** (spec §8.6) — `ticker_classifications.country` rejects non-ISO2 values.
- **NOT-VALID-still-enforces test** (spec §8.7) — still passes (now in the post-VALIDATED state, the FK enforces on BOTH new and old rows).

### 7.5 Rollback path

- Recreate the view + drop the CHECK via downgrade.
- Producer + audit state is unchanged by this PR.

### 7.6 Exit gate (Phase 5 / spec acceptance criteria 1–13)

Maps directly to spec v2 §15:
1. All 15 in-scope tables have a VALIDATED FK to `ticker_classifications`.
2. Phase 2's NOT-VALID-FIRST guarantee was verified mid-rollout (captured in Phase 2 PR body).
3. Pre-FK prep work complete (rename + view dropped, country column + CHECK, classify_tickers producer filter live).
4. Zero orphan rows post-VALIDATE per table.
5. Drift invariant holds (`|ticker_classifications - prices_daily.ticker| = 0`).
6. DELETE-RESTRICT smoke test passes for three sample tickers.
7. All test contracts (§8.1–§8.7) pass.
8. Producer regression sweep clean.
9. `tpcore/auditheal` cross-table audit all-green.
10. Alembic round-trip verified for every migration shipped in Phases 0–4 (Phase 4 VALIDATE downgrades are documentation-only).
11. Heavy-lane gates green on every PR.
12. Documentation reconciled (ERD + DATABASE_AND_DATAFLOW.md + memory).
13. Verification gates captured in PR bodies per spec §9.

---

## 8. Gates between phases — strict dependency contract

| From → To | Gate condition |
|---|---|
| Phase 0 → 1 | Orphan baseline captured; index audit complete; statement_timeout cap verified; all CONCURRENTLY index migrations merged. |
| Phase 1 → 2 | Both Phase 1 migrations + producer-code PR merged; rename grep sweep complete; country backfill landed; null-rate documented. |
| Phase 2 → 3 | All 15 FKs present with `convalidated=false`; NOT-VALID-still-enforces smoke test green; pg_locks post-snapshot clean; replica propagation confirmed. |
| Phase 3 → 4 | Producer drift = 0; classify_tickers dry-run + live run match; no `ForeignKeyViolation` logged (or, if any, Phase 4 cleanup re-prioritised). |
| Phase 4 → 5 | All 15 tables have `convalidated = true`; per-table cleanup PRs + VALIDATE PRs merged; replica propagation confirmed for each. |

**No phase skips.** No bundling Phase 2 with Phase 3 — Phase 2 must commit and bake before classify_tickers DELETEs run against the FK-protected parent. No bundling Phase 4 cleanup with Phase 4 VALIDATE per table — spec §5.2 mandates separate migrations.

---

## 9. Risk register (mirrors spec v2 §14; ordered by phase)

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| R1 | classify_tickers producer change inadvertently DELETEs valid rows | 3 | Dry-run + 1% halt; **v2 bonus: any orphan-creating DELETE fails ON DELETE RESTRICT loud-and-immediate** |
| R2 | Phase 4 cleanup deletes rows operator wanted to keep | 4 | Per-table operator sign-off; path C default for uncertain tables |
| R3 | Phase 4 VALIDATE on `prices_daily` hits statement timeout | 4 | Phase 0 statement_timeout verify; off-window schedule; orphan_count=0 pre-check; SHARE UPDATE EXCLUSIVE allows concurrent reads+writes; failure leaves constraint NOT VALID (still enforced for new rows) — re-run after cleanup, no downgrade |
| R4 | Locks block engine queries during VALIDATE | 4 | SHARE UPDATE EXCLUSIVE by design; Phase 2 ACCESS EXCLUSIVE is brief (sub-second per table) |
| R6 | Derived-table refresh breakage post-FK | 2, 3 | Producer ordering: classify_tickers FIRST in daily cycle; derived populators run after; **v2 bonus: ON DELETE RESTRICT surfaces stale-ticker bugs loud-and-immediate** |
| R7 | Phase 1 rename breaks external consumer | 1 | **v2: compatibility view** (§3.1) — read consumers continue to work; write consumers fail loud (`cannot insert into view`); drop view in Phase 5 once grep is zero |
| R8 | Raw-SQL INSERT bypasses producer + violates FK | post-2 | **Acceptable — this is the whole point**, earlier in v2 than in v1 |
| R9 | Alembic head divergence during rollout | all | Re-base each phase against fresh main; verify `alembic heads` before each push; pg_locks monitor catches concurrent migration sessions |
| R11 | Edge-case operator decision drift (`open_orders` / `tradier_options_chains`) | 2, 4 | Operator decision recorded in Phase 0 before Phase 2 bulk |
| R12 | JSONB-payload ticker refs (aar_events / application_log / data_quality_log / risk_state) out of scope | post-5 | Documented as out-of-scope per spec v1 §3.2; forensics not schema |
| R13 | New Tier 2 derived table emerges between spec and Phase 4 | 4 | Phase 0 includes `\dt platform.*` enumeration vs spec §3.1; reconcile before Phase 4 final |
| R14-v2 | `pg_constraint.convalidated` mis-reads via tooling | 4 | Use `pg_constraint.convalidated` directly per spec §9.3, NOT `\d+` human-readable output |
| R15-v2 | Read-replica drift | 2, 4 | Per spec §9.4: query each replica post-each-phase; capture lag in PR body |
| R16-v2 | Compatibility view discipline lapses | 1, 5 | Drop-view step explicit in Phase 5; operator may drop earlier once grep is zero |
| R17-v2 | Country-backfill null-rate > 50% for asset class operator cares about | 1, 5 | Honest null-rate logged per spec §12; Phase 1.5 follow-up backfill source enumerated in Phase 1 PR; Task #15 unblocks on Phase 1.5 if needed |

**Risks removed vs v1:** R5 (producer-race in cleanup window) and R10 (Phase 8 cleanup-precondition fragility) — both eliminated by v2's NOT-VALID-FIRST inversion. The producer-race window does not exist in v2.

---

## 10. Execution-time checklist (for the subagent or operator running this plan)

For each phase:

1. Branch off fresh `main`: `git fetch origin && git checkout -b feat/refint-v2-p<N>-<topic> origin/main`.
2. Read the relevant phase section above; do NOT skip the read.
3. Run the orphan-audit query (Phase 0 baseline if not done; per-table re-audit per Phase 4 PR).
4. Implement the migration per the template.
5. Run alembic round-trip (`upgrade head` → `downgrade -1` → `upgrade head`).
6. Run heavy-lane gates per `.claude/agents/db-architect.md` §7.
7. For Phase 2 + Phase 4: open the pg_locks monitor (spec §9.2) in a separate session during migration commit.
8. Open PR with audit numbers + verification-query outputs in the body.
9. Squash-merge on green CI.
10. Pull main locally; verify exit gate; only THEN proceed to next phase.

**Never run two phases in a single PR.** Per spec §5.2: **never bundle `ADD CONSTRAINT NOT VALID` with `VALIDATE CONSTRAINT` in the same migration** (forfeits the SHARE UPDATE EXCLUSIVE benefit). Never skip the round-trip. Never skip the orphan re-audit before adding the FK. Never skip the replica check.

---

## 11. v2 vs v1 — concrete sequence diff

| Aspect | v1 (superseded) | v2 (this plan) |
|---|---|---|
| Phase count | 11 (Phase 0–10) | 5 (Phase 0–5; Phase 4 is rolling per-table) |
| Wall-clock total | 12–20 hr | 9–13 hr |
| PR count | 10 | ~20 (more, smaller; Phase 4 split per-table) |
| FK rollout order | cleanup-then-FK (light → medium → heavy) | **FK NOT VALID first (all 15 at once) → cleanup-then-VALIDATE rolling per-table** |
| classify_tickers producer fix | Phase 3 (BEFORE any FK) | Phase 3 (AFTER NOT-VALID FKs land; producer DELETEs now hit ON DELETE RESTRICT loud-fail) |
| Rename strategy | Forward-only, no view; relies on grep sweep | **Rename + compatibility view** (loud-fail on missed writes; quiet-success on missed reads) |
| Cleanup template | `ctid`-based DELETE (concurrency-unsafe) | **`WHERE NOT EXISTS`** (MVCC-safe) |
| `prices_daily` strategy | Two-phase `NOT VALID` + `VALIDATE` in ONE migration | **`NOT VALID` (Phase 2 bulk) + `VALIDATE` (Phase 4 final) in TWO separate migrations** to preserve SHARE UPDATE EXCLUSIVE benefit |
| Producer-race window | Phase 5 (cleanup-complete) → Phase 8 (VALIDATE) — entire migration runtime ≈ 5–30 min | **Eliminated** — FKs enforce on new rows continuously from Phase 2 commit |
| Risk count | 10 risks | 13 risks (R5 + R10 removed; R14-v2 + R15-v2 + R16-v2 + R17-v2 added; R1/R6/R7 strengthened) |
| Operator can stop mid-rollout? | No — partial cleanup leaves orphans + no FK protection | **Yes** — after Phase 2 commits, new-orphan protection is fully shipped; Phase 4 can run on operator's clock |

**Riskiest phase v1:** Phase 8 (`prices_daily` FK addition with VALIDATE bundled in same migration as ADD CONSTRAINT — 20.6M-row scan under ACCESS EXCLUSIVE if the bundling mistake stayed in).
**Riskiest phase v2:** Phase 4's `prices_daily` VALIDATE PR — still 20.6M rows, still 5–30 min wall-clock, but now under SHARE UPDATE EXCLUSIVE (concurrent reads + writes proceed) and bounded by an orphan set that can only have shrunk since Phase 0 (no new orphans possible after Phase 2). Less risky than v1's equivalent, but still the largest blast radius in the rollout.

---

## 12. Out-of-scope (unchanged from v1 §16; restated for completeness)

1. Composite `(ticker, date)` FK chains to `prices_daily` for time-series children.
2. Tier 2 freshness constraints.
3. `source` column policy on multi-source tables beyond `prices_daily` + `insider_transactions`.
4. RLS / Supabase row-level policies.
5. Engine-keyed FK additions (`open_orders.engine`, `risk_state.engine`, `allocations.engine`).
6. Macro consolidation (Task #18).
7. Phase 2 denormalization (Task #17).
8. Per-country insider adapters (Task #15) — depends on this plan's Phase 1 + Phase 2.
9. SCD-2 history for `ticker_classifications`.
10. Columnar / partitioned storage for `prices_daily`.

---

## 13. Open questions (resolve in Phase 0; per spec v2 §16)

1. Read-replica provisioning state — enumerate connection URLs for replica checks, or document the no-op state.
2. Role-level `statement_timeout` cap — confirm ceiling; raise via Supabase dashboard if needed.
3. Country-backfill follow-up source for Phase 1.5 (Polygon `/v3/reference/tickers`, OpenFIGI, FMP company-profile, manual ETF curation) — operator picks or defers Task #15.
4. Edge-case tables `open_orders` and `tradier_options_chains` — include in Phase 2 NOT-VALID bulk or defer? (Recommendation per v1: include `open_orders` in the bulk; skip `tradier_options_chains` until table is unfrozen.)

---

**END OF PLAN v2.**
