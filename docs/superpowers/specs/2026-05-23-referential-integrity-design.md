# Referential-Integrity Design — `platform.*` Schema, Phase 1 (FK Enforcement)

**Status:** SPEC (this doc). Brainstorm → **spec** → plan (already drafted; see §11) → phased subagent build. Authored by the `db-architect` Postgres role per `.claude/agents/db-architect.md`.

**Author / role:** `db-architect` (Postgres + Supabase Pro tier; `platform.*` schema).
**Spec basis (read in this order):**
1. `.claude/agents/db-architect.md` — operating contract (audit-before-alter, FK defaults `ON UPDATE CASCADE ON DELETE RESTRICT`, source-named tables, no redundant `source` column on single-source tables, heavy-lane gates).
2. Memory `project_database_architecture_state_2026_05_23.md` — current schema state, two-tier (raw + derived) dependency tree, `ticker_classifications.ticker ⊆ prices_daily.ticker` operator invariant.
3. `docs/DATABASE_AND_DATAFLOW.md` §2 — current schema tables (`prices_daily`, `fundamentals_quarterly`, `corporate_actions`, `earnings_events`, `tradier_options_chains`, `universe_candidates`, `aar_events`, `data_quality_log`, `parity_drift_log`, `open_orders`, …).
4. `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md` — the prior referential work (`tpcore/auditheal` + `tpcore/audit/cross_table.py`) that **detects + remediates** orphans after the fact via `data_quality_log` rows. This spec is the **schema-layer enforcement** that audit-remediation never delivered.
5. `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md` — the 11-phase Alembic-migration plan that **implements** this spec. This SPEC defines the *contract* (invariants, scope, safety, acceptance); the PLAN owns the per-phase migration templates, ordering, and `NOT VALID` + concurrent `VALIDATE CONSTRAINT` mechanics. Do NOT duplicate that detail here.

**Operator triggers:**
- 2026-05-22 *"how the fuck do you design a database with no referential integrity"*
- 2026-05-23 *"we need referential integrity on these tables"*
- 2026-05-23 *"if the ticker isn't in daily bars then the ticker doesn't need to be in the ticker classification"*

---

## 1. Problem

The `platform.*` schema has **zero foreign keys**. Every ticker-bearing table is implicit-join only. Three concrete defects, all observed:

1. **`ticker_classifications` drift accumulates silently.** The `classify_tickers` producer UPSERTs Alpaca's current asset list but never DELETEs rows the upstream source dropped. Drift was +41 yesterday, +46 today (2026-05-23). Nothing in the schema catches this; only an ad-hoc audit count surfaces it.

2. **Orphans persist across every child table.** A ticker can live in `prices_daily` (or `corporate_actions` / `earnings_events` / `sec_insider_transactions`) without a corresponding row in `ticker_classifications`. `tpcore/auditheal` (PR #167) emits `cross_table_audit.<table>.orphan_no_prices` rows to `data_quality_log` after the fact, but only one orphan class (`tradier_options_chains` via `cross_ref_cleanup`) is auto-remediated. The rest sit red and escalate-only.

3. **Cross-source mixing isn't caught.** `prices_daily.source ∈ {alpaca, tradier, fmp}` is operator policy, not a constraint. `insider_transactions` (planned rename of `sec_insider_transactions`) will hold both SEC and FMP data; without a CHECK constraint a producer bug can silently write the wrong tag.

The downstream cost: every engine that joins on `ticker` is silently joining against a potentially-stale or partially-orphaned universe. `tpcore/auditheal` is detection theatre — it logs, it doesn't enforce.

**The right fix (this spec):** every `ticker`-bearing child table gets `FOREIGN KEY (ticker) REFERENCES platform.ticker_classifications(ticker) ON UPDATE CASCADE ON DELETE RESTRICT`. Drift becomes a constraint violation at INSERT time, not an audit-after-the-fact print line.

---

## 2. Goal — Invariants the Schema Will Enforce

After Phase 1 ships, the following invariants hold **at the database layer** (not at producer-discretion):

- **I-1 (universe FK).** For every ticker-bearing child table `T` in §3 scope-in: every row in `T` has a matching row in `platform.ticker_classifications`. SQL: `SELECT COUNT(*) FROM platform.T c WHERE NOT EXISTS (SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.ticker)` returns `0` and will continue to return `0` because INSERT/UPDATE fails the FK otherwise.
- **I-2 (no silent classification drop).** Attempting to `DELETE FROM platform.ticker_classifications WHERE ticker = X` while any child row references `X` raises `ForeignKeyViolation`. Producers must explicitly clean dependents first (forced-acknowledgement; no silent cascade).
- **I-3 (rename safety).** Updating `ticker_classifications.ticker` propagates to all children via `ON UPDATE CASCADE`. (Used rarely, only for ticker-change corporate events.)
- **I-4 (classifications ⊆ prices_daily).** Producer-side invariant (`classify_tickers` filters Alpaca's asset list to `A ∩ {distinct prices_daily.ticker}` before upserting). Not enforced as a FK — `ticker_classifications` is the parent and `prices_daily` is its child; reversing the direction would create a cycle. Enforced by producer code (Phase 3 of the plan) and verified by a sentinel test.
- **I-5 (cross-source tagging).** `insider_transactions.source IN ('sec', 'fmp')` is a `CHECK` constraint after the Phase 1 rename. Symmetric to the existing `prices_daily.source` policy (today policy-only; opportunistically tighten in a later phase but NOT in this spec's scope).
- **I-6 (country dimension).** `ticker_classifications.country` is a 2-char ISO country code (nullable initially, NOT-NULL deferred), enforced by a `CHECK` `country IS NULL OR country ~ '^[A-Z]{2}$'`. Required for the country-partitioned coverage metric (Task #15).

A new FK violation MUST fail loud — see §8 (test contracts).

---

## 3. Scope

### 3.1 In-scope tables (15 child tables → `ticker_classifications.ticker`)

**Tier 1 — RAW (vendor-fed):**

| Table | Approx rows | FK column | Notes |
|---|---|---|---|
| `prices_daily` | ~20.6M | `ticker` | THE hardest migration. Uses `NOT VALID` + concurrent `VALIDATE CONSTRAINT`. |
| `insider_transactions` | ~1M | `ticker` | **Post-rename** from `sec_insider_transactions` in Phase 1. New `source IN ('sec','fmp')` CHECK. |
| `sec_material_events` | ~200K | `ticker` | SEC 8-K. |
| `corporate_actions` | ~50K | `ticker` | Alpaca splits/dividends. |
| `earnings_events` | ~80K | `ticker` | FMP earnings. |
| `fundamentals_quarterly` | ~178K | `ticker` | FMP. |
| `short_interest` | ~30K | `ticker` | FINRA bi-monthly. |
| `borrow_rates` | ~20K | `ticker` | iborrowdesk. |
| `social_sentiment` | ~50K | `ticker` | ApeWisdom. |
| `options_max_pain` | ~5K | `ticker` | Greeks Pro; single SPY symbol. |
| `insider_sentiment` | ~10K | `symbol` | Finnhub monthly. **Column name is `symbol` not `ticker`.** |

**Tier 2 — DERIVED (computed):**

| Table | Approx rows | FK column | Notes |
|---|---|---|---|
| `liquidity_tiers` | ~7K | `ticker` | Per-ticker tier 1–5. |
| `spread_observations` | ~30K | `ticker` | Corwin-Schultz from OHLC. |
| `universe_candidates` | ~20K | `ticker` | Per-engine prescreens. |
| `insider_mspr_daily` | TBD | `ticker` (verify shape in Phase 0 audit) | Derived from `insider_transactions`. |

### 3.2 Out-of-scope tables (no `ticker` column or behavioral edge case)

| Table | Reason out-of-scope |
|---|---|
| `aaii_sentiment` | Date-only, no ticker. |
| `macro_indicators` | `(indicator, date)` keyed; no ticker. |
| `aar_events` | Ticker lives inside `aar_data` JSONB payload, not a column. Forensics question, not schema. |
| `application_log` | Event bus; no ticker column. |
| `data_quality_log` | Audit log; no ticker column. |
| `risk_state` | Engine-keyed, not ticker-keyed. |
| `allocations` | Engine-keyed. |
| `forensics_triggers` | No ticker column. |
| `execution_quality_log` | No ticker column. |
| `parity_drift_log` | Order-keyed, not ticker-keyed. |
| `open_orders` | Ticker column exists BUT short-lived transactional state. **Open question for plan §17** — FK adds correctness but introduces a hard-fail mode if a classification row drops between order-submit and order-fill (rare race). Default recommendation: include in Phase 7 medium-table batch; operator may override. |
| `tradier_options_chains` | FROZEN per `DATABASE_AND_DATAFLOW.md` §2; currently cleaned by `cross_ref_cleanup`. FK would render `cross_ref_cleanup` redundant. Default recommendation: SKIP until table is unfrozen. |
| `ingestion_jobs` (platform.) | Job-tracking, FROZEN per memory `project_railway_hobby_tier.md`. |
| `provider_binding_state` | Source-keyed, not ticker-keyed. |

**Counts:** 15 in-scope (11 Tier-1 + 4 Tier-2), 12 out-of-scope, 2 edge-case operator-decision (`open_orders`, `tradier_options_chains`).

### 3.3 Pre-FK prep work (must land BEFORE Phase 6+ FK additions)

These are not FK additions themselves; they are preconditions without which the FK rollout will fail or be wrong-shaped:

1. **Rename `sec_insider_transactions` → `insider_transactions`** (Phase 1 of plan). The FMP non-US insider adapter (Task #15) lands in the same table with `source='fmp'`; renaming once now is cheaper than dual-name FK reorganisation later. Add `source TEXT NOT NULL` column (backfilled to `'sec'`) + `CHECK (source IN ('sec','fmp'))`. Update all 68 grep-hits across `tpcore/` and `scripts/` in the same PR.
2. **Add `country char(2)` column to `ticker_classifications`** (Phase 2 of plan). Required for Task #15 country-partitioned coverage metric. Populate from Alpaca `/v2/assets` response. Add `CHECK (country IS NULL OR country ~ '^[A-Z]{2}$')`. Partial index `WHERE country IS NOT NULL`. Initially nullable; NOT-NULL tightening deferred to a follow-up.
3. **`classify_tickers` DELETE-source-tracking + `⊆ prices_daily` filter** (Phase 3 of plan). Producer-code change, no schema migration. Computes `A = Alpaca asset list`, `P = distinct prices_daily.ticker`, `U = A ∩ P`, `D = existing - U`; single transaction UPSERT-then-DELETE. Without this, `ON DELETE RESTRICT` FK additions will break every subsequent `classify_tickers` run that tries to prune drift.
4. **Invariant verification gate** (Phase 4 of plan). Read-only confirmation that `|ticker_classifications - prices_daily.ticker| = 0` before Phase 5 cleanup begins.
5. **Per-table orphan cleanup** (Phase 5 of plan). Per-table operator decision: BACKFILL (add missing rows to `ticker_classifications`), DELETE (remove orphan rows from child), or ARCHIVE-then-DELETE (preserve historical data first).

**Why prep work is load-bearing for the FK rollout:** without (1), Task #15 can't deliver country-partitioned coverage. Without (2), classifying tickers can't surface non-US ADRs. Without (3), the first FK addition deadlocks the producer. Without (4)+(5), `ALTER TABLE … ADD FOREIGN KEY` fails loudly mid-migration on the first orphan it hits.

---

## 4. Non-goals

This spec **does not** cover:

1. **Composite `(ticker, date)` FK chains to `prices_daily`** for time-series children (`spread_observations`, `liquidity_tiers`, `insider_mspr_daily`). Would enforce "no derived row without a parent bar". Separate later spec; depends on `prices_daily` being clean post-Phase 8.
2. **Phase 2 denormalization** per operator task #17 (de-redundantize the schema). Deferred.
3. **SCD-2 history table** for `ticker_classifications` (slowly-changing-dimension type-2 trails for ticker renames / delistings). Deferred. Operator may revisit after this Phase 1 ships.
4. **Columnar / partitioned storage** for `prices_daily` (e.g. native Postgres partitioning by year, or a `pg_partman`/`citus` migration). Deferred. Operator may revisit when row count crosses ~50M.
5. **Tier 2 freshness constraints.** Memory `project_database_architecture_state_2026_05_23.md`: *"There is NO enforced freshness constraint between source and derived tables. A stale upstream silently produces a stale downstream."* This spec fixes the orphan dimension only. Freshness enforcement is a separate plan (DB-level CHECK on `recorded_at` deltas, or producer-level invariant).
6. **RLS / Supabase row-level security policies.** Out of scope for the integrity work; orthogonal concern.
7. **`source` column on other multi-source tables.** Today only `prices_daily` and (post-rename) `insider_transactions` have multi-source data. Per `.claude/agents/db-architect.md` §2: *"NEVER add a redundant `source` column to a single-source table."* Don't proactively spread the pattern.
8. **Engine-keyed FK additions** (`open_orders.engine`, `risk_state.engine`, `allocations.engine`). Engine roster is a separate SoT (see `engine-roster-sot` spec). Coordinate before adding; out of this Phase 1.
9. **Behavioral / forensics integrity** (`aar_events` ticker-in-JSONB, `risk_state` payload-level shape). Forensics layer, not schema layer.

---

## 5. Schema Changes (contract only — migrations live in the plan)

The contract this spec promises the schema will hold after Phase 1 ships:

### 5.1 FK defaults (every in-scope child table)

```text
FOREIGN KEY (<fk_column>) REFERENCES platform.ticker_classifications(ticker)
    ON UPDATE CASCADE
    ON DELETE RESTRICT
```

- **`ON UPDATE CASCADE`** — ticker renames (rare; corporate ticker-change events) propagate automatically.
- **`ON DELETE RESTRICT`** — protects data; producer must handle deletion of dependents explicitly. Per `.claude/agents/db-architect.md` §4: *"Never use `ON DELETE CASCADE` for ticker FK — protect data; force the producer to handle deletion explicitly."*
- FK column is named `ticker` on every table EXCEPT `insider_sentiment` (Finnhub) where it's `symbol`. The FK references `ticker_classifications(ticker)` in both cases.

### 5.2 The `NOT VALID` + concurrent `VALIDATE` pattern for `prices_daily`

`prices_daily` is ~20.6M rows. A default `ALTER TABLE … ADD FOREIGN KEY` would take an `ACCESS EXCLUSIVE` lock and scan every row, hitting Supabase Pro's 5-minute statement timeout and locking out concurrent engine reads. The Postgres-canonical two-phase pattern (https://www.postgresql.org/docs/current/sql-altertable.html):

```sql
-- Step 1: fast, short ACCESS EXCLUSIVE; new rows protected immediately.
ALTER TABLE platform.prices_daily
    ADD CONSTRAINT fk_prices_daily_ticker
    FOREIGN KEY (ticker) REFERENCES platform.ticker_classifications(ticker)
    ON UPDATE CASCADE ON DELETE RESTRICT
    NOT VALID;

-- Step 2: SHARE UPDATE EXCLUSIVE; concurrent reads OK. May take 5–30 min.
ALTER TABLE platform.prices_daily
    VALIDATE CONSTRAINT fk_prices_daily_ticker;
```

The constraint contract:
- After Step 1: every **new** INSERT/UPDATE on `prices_daily` is FK-checked. Existing rows un-validated.
- After Step 2: every existing row has been verified; constraint is fully active. `\d+ platform.prices_daily` shows the constraint without the `NOT VALID` flag.
- If Step 2 fails (any orphan exists), the constraint remains as `NOT VALID`. Re-run Phase 5 cleanup, then re-run Step 2. The constraint is **not** dropped on validation failure — only new rows continue to be enforced. This is forward-only and safe.

### 5.3 CHECK constraints (Phase 1 prep work)

```sql
-- insider_transactions (after rename from sec_insider_transactions)
ALTER TABLE platform.insider_transactions
    ADD CONSTRAINT ck_insider_transactions_source
    CHECK (source IN ('sec', 'fmp'));

-- ticker_classifications country column
ALTER TABLE platform.ticker_classifications
    ADD CONSTRAINT ck_ticker_classifications_country_iso
    CHECK (country IS NULL OR country ~ '^[A-Z]{2}$');
```

### 5.4 Index discipline

Per `.claude/agents/db-architect.md` §6: *"Every FK column needs an index (Postgres doesn't auto-index FKs)."* Verification step before merging any FK migration:

- For every in-scope table, run `\d+ platform.<T>` and confirm a B-tree index on the FK column (typically already exists as the first column of the table's PK).
- Where missing, the migration adds `CREATE INDEX idx_<table>_ticker ON platform.<T> (ticker)` in the same migration as the FK.
- Partial index on `ticker_classifications.country` `WHERE country IS NOT NULL` (sparse column initially).

---

## 6. Audit-before-alter pattern

Per `.claude/agents/db-architect.md` §1, every FK addition is preceded by:

### 6.1 Orphan-count audit query template

```sql
SELECT
    '<T>' AS child_table,
    COUNT(*) AS orphan_count,
    COUNT(DISTINCT c.<fk_column>) AS distinct_orphan_tickers
FROM platform.<T> c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.<fk_column>
);
```

Run per table from §3.1 scope-in list. Persist results to `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md` (Phase 0 of plan).

### 6.2 Cleanup-or-backfill decision (per-table)

For each table with `orphan_count > 0`, pick one of three remediation paths:

| Path | When | Mechanism |
|---|---|---|
| **A. BACKFILL** | Orphan tickers are real-but-missing-from-classifications (foreign ADRs, recently-listed, pre-Alpaca-asset-list-refresh) | INSERT missing rows into `ticker_classifications` from `prices_daily` distinct-ticker set + Alpaca re-pull |
| **B. DELETE** | Orphan tickers are stale/wrong (bad-source rows, delisted, typos) | `DELETE FROM platform.<T> WHERE ticker IN (...)` + audit row to `application_log` |
| **C. ARCHIVE-then-DELETE** | Orphan tickers have non-trivial historical data that may be needed later | `INSERT INTO platform.<T>_archive SELECT * …` then DELETE |

Per-table decision matrix lives in the plan's Phase 5 (§7 of the plan doc). Operator sign-off required for any table where the decision is non-obvious (e.g. `prices_daily`, `insider_transactions`, `fundamentals_quarterly`).

### 6.3 Post-cleanup re-audit gate

Each cleanup migration's PR body must include the post-cleanup orphan-count verification (target: 0). FK addition only proceeds after the gate is green.

---

## 7. Dependency graph (in-flight tasks)

| Task # | Title | Relationship |
|---|---|---|
| **#8** | Auto-rebuild-from-archive for all feeds | Orthogonal. If FK rollback is needed mid-Phase 7, archive rebuild lets producers restore quickly. Doesn't block this spec. |
| **#10** | Audit-heal data-lake story (PR #167, the prior 2026-05-17 spec) | **Complementary.** `tpcore/auditheal` detects + remediates orphans after the fact via `data_quality_log`. After this spec ships, the `cross_table_audit.<T>.orphan_no_prices` checks become **assertion-style sentinels** (should always read 0; non-zero indicates a producer bug or constraint loophole) rather than active remediation feeds. |
| **#12** | Missing `fmp_daily_bars` CSV archive | **Soft-blocks Phase 3.** If FMP `daily_bars` producer has hidden coverage gaps, `classify_tickers`'s `A ∩ P` set will be wrong. Mitigation: run Phase 3 dry-run BEFORE the Phase 4 verification gate. |
| **#14** | ProviderProfile + download constraints | Orthogonal; parallel work. |
| **#15** | Per-country insider adapters → `insider_transactions` (FMP non-US fallback) | **Builds ON this spec.** Task #15 needs (a) the Phase 1 rename `sec_insider_transactions → insider_transactions` with `source IN ('sec','fmp')`, and (b) the Phase 2 `country` column on `ticker_classifications` for partitioned coverage metrics. Can land only AFTER Phase 2 of this spec's plan ships. |
| **#17** | Phase 2 denormalization | Deferred non-goal (§4.2). Lands after this spec. |
| **#186** | Deterministic-agents epic — auditheal sub-project (the parent of the 2026-05-17 prior spec) | Audit-heal is rung-1 of the Escalation & Hardening Ladder; this spec hardens the schema below rung-1 so audit-heal converges on green. |

**Cross-task ordering summary:**

```text
Phase 0 (audit)
    └─→ Phase 1 (rename) ──────────────────→ Task #15 unblocks
    └─→ Phase 2 (country column) ──────────→ Task #15 unblocks
        └─→ Phase 3 (classify_tickers producer)
            └─→ Phase 4 (verify gate)
                └─→ Phase 5 (cleanup)
                    └─→ Phase 6 (FK: light tables)
                        └─→ Phase 7 (FK: medium tables)
                            └─→ Phase 8 (FK: prices_daily HEAVY)
                                └─→ Phase 9 (FK: Tier 2 derived)
                                    └─→ Phase 10 (post-FK verification)
                                        └─→ memory item #3 marked DONE
```

---

## 8. Test contracts (load-bearing — pin invariants)

Tests live in `tpcore/tests/` (per the operating-contract heavy-lane gate `pytest -p no:xdist`). A new FK violation MUST fail loud. Required test classes:

### 8.1 Constraint-presence tests (one per in-scope table)

```python
# tpcore/tests/test_referential_integrity.py (NEW — owned by the plan)
async def test_fk_present_<table>(pool: asyncpg.Pool) -> None:
    """Every in-scope child table has the universe FK to ticker_classifications."""
    row = await pool.fetchrow("""
        SELECT conname FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid = 'platform.<T>'::regclass
          AND confrelid = 'platform.ticker_classifications'::regclass
    """)
    assert row is not None, "FK to ticker_classifications missing on <T>"
```

### 8.2 DELETE-RESTRICT smoke test

```python
async def test_delete_restrict_blocks_referenced_ticker(pool) -> None:
    """Cannot DELETE a ticker_classifications row while child rows reference it."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "DELETE FROM platform.ticker_classifications WHERE ticker = $1",
                    'AAPL',  # heavy, used by prices_daily + dozens of derived tables
                )
            # transaction rollback happens automatically on raise; no cleanup needed
```

### 8.3 INSERT-violation smoke test

```python
async def test_insert_unknown_ticker_rejected(pool) -> None:
    """Cannot INSERT a row referring to a ticker absent from classifications."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO platform.prices_daily (ticker, date, ...) "
                    "VALUES ($1, $2, ...)",
                    'NEVER_EXISTED_XYZ', date.today(),
                )
```

### 8.4 Sentinel-style invariant test (replaces `tpcore/auditheal` reds)

After Phase 1 ships, the `tpcore/auditheal` cross-table-audit `*.orphan_no_prices` checks should always read 0. Add a sentinel test that fails if any of those checks turns red:

```python
async def test_cross_table_orphans_stay_at_zero(pool) -> None:
    """Post-Phase-1: orphan checks are assertions, not active remediations."""
    from tpcore.audit.cross_table import CROSS_TABLE_CHECKS
    for check in CROSS_TABLE_CHECKS:
        count = await pool.fetchval(check.sql)
        assert count == 0, (
            f"Orphan check {check.key} returned {count}; FK should have prevented this. "
            f"Either a producer bypassed the FK (raw SQL) or a constraint was dropped."
        )
```

### 8.5 Source-tag CHECK test (`insider_transactions`)

```python
async def test_insider_transactions_source_check(pool) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO platform.insider_transactions (ticker, filing_date, source, ...) "
                    "VALUES ('AAPL', $1, 'nasdaq', ...)",
                    date.today(),
                )
```

### 8.6 Country format CHECK test (`ticker_classifications`)

```python
async def test_ticker_classifications_country_iso(pool) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE platform.ticker_classifications SET country = 'usa' WHERE ticker = 'AAPL'"
                )
            # 'US' (uppercase ISO2) must succeed:
            await conn.execute(
                "UPDATE platform.ticker_classifications SET country = 'US' WHERE ticker = 'AAPL'"
            )
```

**Test execution gate:** these tests run under the heavy-lane suite (`.venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q`). They require a live `DATABASE_URL`; if absent, they skip with an explicit `pytest.skip("DATABASE_URL not set; FK-integrity tests require live Postgres")`. Mock-pool variants are not appropriate — these tests exist to validate the actual schema.

---

## 9. Migration safety

### 9.1 Alembic round-trip requirement

Per `.claude/agents/db-architect.md` §3, every migration in this rollout (each phase of the plan) must round-trip cleanly:

```bash
DB_URL="${DATABASE_URL/postgresql/postgresql+asyncpg}" \
    .venv/bin/alembic -c platform/migrations/alembic.ini upgrade head
DB_URL=... .venv/bin/alembic -c platform/migrations/alembic.ini downgrade -1
DB_URL=... .venv/bin/alembic -c platform/migrations/alembic.ini upgrade head
```

Each `op.create_*` has a paired `op.drop_*` in `downgrade()`. The `NOT VALID` + `VALIDATE CONSTRAINT` migration in Phase 8 (`prices_daily`) has a simple `op.drop_constraint(...)` in `downgrade()` — fast (ms-scale) regardless of validation state.

### 9.2 Locking budget

| Operation | Lock | Duration estimate | Concurrent reads? |
|---|---|---|---|
| `ALTER TABLE … ADD FOREIGN KEY` (default, light tables ≤1M rows) | ACCESS EXCLUSIVE | seconds | Blocked briefly |
| `ALTER TABLE … ADD FOREIGN KEY … NOT VALID` (`prices_daily`) | ACCESS EXCLUSIVE | <1s | Blocked briefly |
| `ALTER TABLE … VALIDATE CONSTRAINT` (`prices_daily`) | SHARE UPDATE EXCLUSIVE | 5–30 min | **YES — concurrent reads + writes OK** |
| `ALTER TABLE … RENAME TO …` (Phase 1, `insider_transactions`) | ACCESS EXCLUSIVE | <1s | Blocked briefly |
| `ALTER TABLE … ADD COLUMN … NULL` (Phase 2, `country`) | ACCESS EXCLUSIVE | <1s | Blocked briefly |
| `ALTER TABLE … ADD CHECK CONSTRAINT … NOT VALID` (if needed for backfill cases) | ACCESS EXCLUSIVE | <1s | Blocked briefly |
| `CREATE INDEX CONCURRENTLY` (any missing FK-column indexes) | SHARE UPDATE EXCLUSIVE | minutes | **YES** |

### 9.3 Statement-timeout considerations

Supabase Pro tier default `statement_timeout` is 5 minutes. The `VALIDATE CONSTRAINT` on `prices_daily` (20.6M rows) may exceed this. The plan's Phase 8 sets `SET LOCAL statement_timeout = '30min'` inside the migration transaction; operator may need to raise the timeout via the Supabase dashboard before running. Run during the off-window (local laptop UTC 04:00–08:00) when no `daily_bars` producer is mid-flight.

### 9.4 Forward-only by design

Per `.claude/agents/db-architect.md` §2: *"Migrations are forward-only. Don't delete historical migration files."* Each cleanup migration in Phase 5 includes a documentation-only `downgrade()` (an SQL comment explaining the path is not auto-restorable):

```python
def downgrade() -> None:
    op.execute("-- forward-only; restore from prices_daily archive if needed")
```

The schema migrations (rename, add column, add FK, add CHECK) all have functional downgrades.

### 9.5 Heavy-lane gates

Per `.claude/agents/db-architect.md` §7, every PR in the rollout runs all four gates green before push:

```bash
.venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q
.venv/bin/python -m pytest -p no:randomly -p no:xdist -p no:cacheprovider -q
ruff check . --statistics
.venv/bin/python -m tpcore.scripts.check_imports tpcore ops reversion vector momentum sentinel canary catalyst carver
```

Plus the Alembic round-trip from §9.1. The PR body documents the orphan-count audit (Phase 0) and the post-cleanup re-audit numbers per §6.

---

## 10. Risk register

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| **R1** | `classify_tickers` producer change (Phase 3) inadvertently DELETEs valid rows when Alpaca's `/v2/assets` has a transient outage / empty response | 3 | Dry-run gate (`--param dry_run=true`); persist delete-set to `application_log.data` JSON before DELETE executes; operator review if `|D|` > 1% of universe; abort condition built into the producer |
| **R2** | Phase 5 cleanup deletes rows the operator wanted to keep (path B vs A misjudgment) | 5 | Per-table operator sign-off for non-obvious tables; path C (archive-then-delete) as default for any uncertain table; cleanup migrations are forward-only — recovery is from archive only |
| **R3** | Phase 8 `VALIDATE CONSTRAINT` hits statement timeout on `prices_daily` | 8 | Pre-set `SET LOCAL statement_timeout = '30min'` inside transaction; schedule in off-window; orphan_count = 0 pre-check; if VALIDATE fails mid-way, constraint stays `NOT VALID` (new rows still enforced) — re-run after cleanup, no downgrade needed |
| **R4** | Locks blocking engine queries during Phase 8 VALIDATE | 8 | VALIDATE CONSTRAINT uses SHARE UPDATE EXCLUSIVE (concurrent reads + writes OK by design); the ACCESS EXCLUSIVE phase is only the initial `ADD … NOT VALID` step (<1s) |
| **R5** | Orphan reintroduction race — a Tier 2 derived compute runs mid-`classify_tickers`-DELETE and produces a stale row that's then orphaned by the cascade | 3, 9 | Producer ordering: `classify_tickers` DELETE before Tier 2 refresh (already the case per memory dependency tree). If race-induced orphan slips through, the FK rejects the insert (loud fail). |
| **R6** | Derived-table refresh breakage post-FK — e.g. `liquidity_tiers` populator tries to insert a tier row for a ticker that was just dropped from `ticker_classifications` | 6+ | Producer-side ordering: `classify_tickers` always runs FIRST in the daily cycle (memory `feedback_data_update_first`); derived populators run after. Tests in §8 catch any producer ordering regression. |
| **R7** | Phase 1 rename (`sec_insider_transactions` → `insider_transactions`) breaks an external dashboard query, dbt script, or downstream consumer | 1 | Pre-push grep: `grep -r 'sec_insider_transactions' --include='*.py' --include='*.sh' --include='*.sql' --include='*.md'`. Today: 68 hits across `tpcore/` + `scripts/`. Verify `dashboard.py` is touched (post-rename column references). Forward-only — no SQL view aliasing the old name. |
| **R8** | A row INSERTed via raw SQL (bypassing the producer) violates the new FK | post-6+ | **Acceptable — this is the whole point.** Constraint violation is the desired signal. The §8.2 / §8.3 smoke tests are deliberately written to confirm this fail-loud behavior. |
| **R9** | Alembic head divergence — another `db-architect` PR lands during this rollout, conflicting migration head | all | Re-base each phase against fresh `main`; verify `alembic heads` before each migration push. Per agent contract §3: *"Set `down_revision` to the prior head (run `alembic heads` against the live DB to find it)"* |
| **R10** | Phase 8 cleanup-precondition fragile — even one orphan from a producer race condition between Phase 5 and Phase 8 will fail `VALIDATE` | 8 | Day-of pre-check: re-run the orphan-count query immediately before the migration; if non-zero, abort and re-run Phase 5 cleanup. The `NOT VALID` constraint from Step 1 prevents new orphans, so the window after Step 1 is safe. |
| **R11** | Edge-case operator decision drift — `open_orders` / `tradier_options_chains` FK opinion changes mid-rollout | 7, 9 | Operator decision recorded in plan §15 before Phase 6 begins. Default recommendations: `open_orders` IN (Phase 7); `tradier_options_chains` SKIP (frozen). Re-confirm in Phase 0 review. |
| **R12** | `aar_events` / `application_log` / `data_quality_log` / `risk_state` ALSO have ticker references — inside JSONB payloads, not columns — and operator might expect them in scope | post-10 | Documented as out-of-scope in §3.2; payload-level integrity is a forensics question, not a schema question. Operator may want a follow-up audit-style check (`tpcore/auditheal` extension) for JSONB ticker references. |
| **R13** | A new Tier 2 derived table not in §3.1 list emerges between this spec and Phase 9 | 9 | Phase 0 audit includes a `\dt platform.*` enumeration; reconcile against §3.1 list and adjust before Phase 9 starts. |

---

## 11. Relationship to the implementation plan

The implementation plan at `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md` owns:

- Per-phase Alembic migration file paths and exact SQL/op API templates
- The 11-phase ordering with wall-clock budgets (12–20 hours total)
- The per-table operator-decision matrix for Phase 5 cleanup
- The execution-time checklist (branch, round-trip, gate, PR, merge, pull, verify-gate, next-phase)
- Per-table FK migration code in Phases 6/7/8/9

This spec owns the **contract** the plan executes against:

- The invariants (§2) the schema will hold
- The scope-in / scope-out boundary (§3)
- The non-goals (§4) — what we deliberately defer
- The audit-before-alter pattern (§6) — how every FK addition is gated
- The test contracts (§8) — how a future regression fails loud
- The migration-safety budget (§9) — locking, statement_timeout, downgrade discipline
- The risk register (§10)
- The acceptance criteria (§12 below)

If a conflict surfaces between this SPEC and the PLAN, the SPEC wins (the plan is the implementation; the spec is the contract). Plan-only details (e.g. exact `op.create_foreign_key(...)` keyword arguments) don't conflict — they're below the spec's level.

---

## 12. Acceptance criteria — Phase 1 is "done" when

1. **All 15 in-scope tables (§3.1) have the universe FK.** `SELECT conname FROM pg_constraint WHERE contype='f' AND confrelid='platform.ticker_classifications'::regclass` returns ≥15 rows (one per in-scope child). Spot-check `\d+` on three sample tables (a Tier-1 raw, a Tier-2 derived, `insider_sentiment` with its `symbol` column).
2. **Pre-FK prep work complete.**
   - `sec_insider_transactions` is renamed to `insider_transactions` (the old name does not appear in `pg_tables`); `source` column exists with `CHECK (source IN ('sec','fmp'))` constraint named `ck_insider_transactions_source`.
   - `ticker_classifications.country` column exists with `CHECK (country IS NULL OR country ~ '^[A-Z]{2}$')` named `ck_ticker_classifications_country_iso`; partial index `idx_ticker_classifications_country WHERE country IS NOT NULL` exists.
   - `classify_tickers` producer applies the `A ∩ P` filter (verified by a unit test in `tpcore/tests/test_classify_tickers.py`) and DELETEs dropped tickers in the same transaction as the UPSERT.
3. **Zero orphan rows.** For every in-scope table T, `SELECT COUNT(*) FROM platform.T c WHERE NOT EXISTS (SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.<fk_col>)` returns 0.
4. **Drift invariant holds.** `SELECT COUNT(*) FROM platform.ticker_classifications tc WHERE NOT EXISTS (SELECT 1 FROM platform.prices_daily p WHERE p.ticker = tc.ticker)` returns 0. (Producer-enforced, sentinel-tested.)
5. **DELETE-RESTRICT smoke test passes** for three sample tickers (AAPL heavy / SPY derived-dependency / a newly-listed low-row ticker): each `DELETE FROM platform.ticker_classifications WHERE ticker = '<T>'` raises `ForeignKeyViolation`.
6. **Test contracts (§8) all pass** under `.venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q`.
7. **Producer regression sweep clean.** Each `--stage` runs once post-FK without an FK violation: `daily_bars`, `corporate_actions`, `fundamentals_refresh`, `compute_fundamental_ratios`, `earnings_events`, `sec_filings`, `finra_short_interest`, `iborrowdesk_borrow_rates`, `apewisdom_social_sentiment`, `finnhub_insider_sentiment`, `greeks_max_pain`, `macro_indicators`, `classify_tickers`, `assign_liquidity_tiers`, `universe_candidates_refresh`, `compute_insider_mspr_daily`.
8. **`tpcore/auditheal` cross-table-audit reads all-green.** All `cross_table_audit.<T>.orphan_no_prices` rows in `platform.data_quality_log` have `stale=false`, `confidence=1.0`. Sentinel test (§8.4) added.
9. **Alembic round-trip verified** for every migration shipped in Phases 1–9.
10. **Heavy-lane gates green** on every PR in Phases 1–10 (per `.claude/agents/db-architect.md` §7).
11. **Documentation reconciled.** `docs/DATABASE_AND_DATAFLOW.md` §2.1 ERD has FK arrows from every in-scope ticker-bearing entity to `TICKER_CLASSIFICATIONS`. §2.2 has a top-level note: *"Every ticker column FK → `platform.ticker_classifications(ticker)` with ON UPDATE CASCADE ON DELETE RESTRICT."* Memory `project_database_architecture_state_2026_05_23.md` outstanding-debt section item (3) marked DONE.

---

## 13. Open questions (resolve in Phase 0 before any migration is written)

Per the operating contract — *resolve by READING code, not guessing*. The plan's Phase 0 (read-only audit) is where these get answered:

1. **`insider_mspr_daily` column shape.** §3.1 lists it as Tier 2 derived, FK on `ticker`. Verify the column name and PK shape in Phase 0 (`\d+ platform.insider_mspr_daily`). Adjust §3.1 row if the FK column is named differently.
2. **`open_orders` FK — include or exclude?** Default recommendation (§3.2): INCLUDE in Phase 7 medium-table batch. Operator may override based on observed order-fill race incidence. Decision recorded in plan §15 before Phase 6 begins.
3. **`tradier_options_chains` FK — include or exclude?** Default recommendation (§3.2): SKIP (frozen table; `cross_ref_cleanup` already handles the cleanup; FK adds lock contention without correctness gain). Operator may override.
4. **`source` column policy on other multi-source tables.** Today only `prices_daily` and (post-rename) `insider_transactions` are multi-source. Phase 0 audit enumerates all tables and confirms no other table needs a multi-source CHECK constraint added in this rollout.
5. **Index audit per FK column.** Every FK column needs an index per agent contract §6. Phase 0 confirms via `\d+` that every in-scope table's FK column is already indexed (typically as the first column of its PK). Migrations add `CREATE INDEX CONCURRENTLY` where missing.
6. **Statement-timeout dashboard adjustment.** Confirm whether Supabase Pro tier dashboard already allows operator to raise `statement_timeout` to 30 min for the Phase 8 migration window, or whether a temporary role-level override is needed.

---

## 14. References

- **Operating contract:** `.claude/agents/db-architect.md`
- **Prior referential work (detection layer):** `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md` + PR #167 (`tpcore/auditheal`, `tpcore/audit/cross_table.py`)
- **Implementation plan (this spec's executor):** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md`
- **Schema state memory:** `project_database_architecture_state_2026_05_23.md`
- **Schema reference:** `docs/DATABASE_AND_DATAFLOW.md` §2
- **Postgres canonical `NOT VALID` + `VALIDATE CONSTRAINT` pattern:** <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>
- **Supabase Pro tier statement_timeout:** memory `project_supabase_pro_tier.md`
- **Sibling spec structure absorbed from:** `docs/superpowers/specs/2026-05-17-da1-engine-supervisor-design.md`, `docs/superpowers/specs/2026-05-17-data-supervisor-design.md`

---

**END OF SPEC.**
