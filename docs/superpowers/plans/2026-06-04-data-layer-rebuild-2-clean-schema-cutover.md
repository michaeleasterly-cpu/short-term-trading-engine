# Data-Layer Rebuild — Plan 2: Clean-Schema Cutover (the wipe)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> ⚠️ **DESTRUCTIVE / IRREVERSIBLE.** This plan TRUNCATEs the 21.39M-row `prices_daily` and the entire ticker identity+substrate+signal graph, and DROPs dead tables. The only rollback is the Task-1 snapshot + Supabase PITR (7-day). **Execution of the wipe (Task 7) requires a SEPARATE explicit operator go** beyond authoring this plan. Tasks 1–6 (snapshot, pause, schema migrations) and Tasks 8–9 are prerequisites/cleanup; Task 7 is the point of no return.

**Goal:** Cut the live DB over to the clean target schema — drop the dead/folded tables, redesign `data_quality_log`, wipe the ticker data graph (preserving macro + the PRESERVE-class ops tables), and tighten the identity/fundamentals schema on the now-empty tables — leaving a correct, empty, identity-first schema ready for Plan 3's re-ingest.

**Architecture:** Phase 1 snapshots the non-re-derivable data (PRESERVE-class ops tables to CSV + a Supabase on-demand snapshot; SACRED `hy_spread` is already backed up off-DB and lives in `macro_data`, which is never touched). Phase 2 pauses every writer. Phase 3 runs reversible-where-possible Alembic migrations (DROP set, count-snapshot→view, `data_quality_log` redesign) BEFORE the wipe, then the single gated `TRUNCATE` (one statement so the mutual `classification_id` FKs are satisfiable), then the schema-tightening migration on the empty tables (`lifetime_start` no-default, `fundamentals_quarterly` 3-part PK).

**Tech Stack:** Python 3.11, Alembic (`platform/migrations/`), asyncpg, Supabase Postgres (session-mode `:5432` for DDL/TRUNCATE; `DATABASE_URL_IPV4`), Railway GraphQL (writer pause), pytest (`-p no:xdist` authoritative).

**Source spec:** `docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md` v1.4 §2.2/§2.3/§2.4, §3.1/§3.2/§3.3, §7, §8.1/§8.2/§8.3 (approved 2026-06-04). **Predecessor:** Plan 1 (identity-predicate + aar_events FK) SHIPPED — DB at alembic head `20260604_0200`.

**Heavy-lane:** `platform/migrations/**` + `tpcore/quality/validation/**` (data_quality_log readers) → full §1 pipeline; whole-suite + order-flip authoritative.

---

## Table disposition (live counts as of 2026-06-04)

| Action | Tables |
|---|---|
| **TRUNCATE** (one statement, RESTART IDENTITY — the ticker graph) | `prices_daily` (21.39M), `prices_daily_staging`, `fundamentals_quarterly` (183.8K), `ticker_classifications`, `ticker_history`, `issuers`, `issuer_securities`, `issuer_history`, `corporate_events`, `corporate_actions`, `earnings_events`, `short_interest`, `borrow_rates`, `insider_transactions`, `insider_sentiment`, `social_sentiment`, `sec_material_events`, `spread_observations`, `liquidity_tiers`, `universe_candidates`, `aar_events` (must be in-statement — it FKs `ticker_classifications`) |
| **DROP** (dead / Tradier / re-derivable-folded) | `tradier_options_chains` (113.8K, Tradier closed), `options_max_pain` (1) + its trigger fn, `ticker_lifecycle_events` (1129 → re-derived into `corporate_events`), `fundamentals_period_source_evidence` (0), `parity_drift_log` (0), `forensics_triggers` (0), `ingestion_metrics` (6 → routes to `ingest_manifest`) |
| **DROP table, CREATE VIEW** | `earnings_events_count_snapshot` (§2.4) |
| **REDESIGN** (drop+recreate; rows reproduced) | `data_quality_log` (6567 → uuid id + `kind` + nullable typed + jsonb `notes` + partial indexes) |
| **PRESERVE — never touched** | `macro_data` (out of scope; holds SACRED `hy_spread`), `ingest_manifest` (116), `allocations` (11), `risk_close_ledger` (0) |
| **KEEP standalone — NOT folded, NOT truncated by the ticker wipe** | `failed_alpha_ledger` (5), `ingest_quarantine` (4) — RESET only if the operator wants; default LEAVE (re-populated by ops) |
| **NOT dropped (deferred)** | `split_pre_image_log` (0) — retained until the §5.6 cumulative-factor `adjusted_close` model lands in Plan 3 (spec §2.3) |
| **Ops — left alone** | `risk_state`, `open_orders`, `aar_deferred`, `daemon_heartbeats`, `application_log` (operational; re-derived/ephemeral; not ticker data) |

---

## Phase 0 (ADDED 2026-06-04 — discovered scope) — `data_quality_log` consolidation rewiring

The db-architect authoring trace found the consolidation layer's blast radius is wider than Tasks 3–5 assumed. Operator chose to **rewire everything before the wipe**. This phase completes + is green BEFORE Tasks 3/5 apply; a `code-quality-reviewer` pass on the full diff precedes the wipe. Producer inventory + dispositions:

**`data_quality_log` writers** (must emit the new shape — `kind` + jsonb `notes`, NO `ON CONFLICT (source,timestamp)`):
- `tpcore/quality/data_quality.py` — canonical `DataQualityWriter.write` — done (shim). Add a `kind` param so non-validation callers route correctly.
- `tpcore/audit/cross_table.py:145` — route through the canonical writer, `kind='validation'`.
- `scripts/audit_data_pipeline.py:1407` — `kind='validation'`.
- `scripts/ops.py:5001,5038,5101,6292` — `kind='validation'` (ingestion-metric rows → `ingest_manifest`).

**Credibility kind-split (reader-coupled):** `write_credibility_score` flows through `DataQualityWriter` and sets `confidence` (a typed col); the `dql_typed_cols_validation_only` CHECK forbids typed cols on non-`validation` kinds. A true `kind='backtest_credibility'` row would have to carry `confidence` in `notes`, forcing the reader `tpcore/backtest/credibility.py:255` (`graduation_ready`) to read `notes->>'confidence'`. **Decision: keep credibility as `kind='validation'` (the CHECK-valid shim; reader unchanged) for this cutover; defer the clean split.**

**Dropped-sidecar producers** (0300 drops the tables → rewire or they break at runtime):
- `forensics_triggers` — `tpcore/forensics/{service,__init__,__main__}.py`, `ops/engine_ladder.py`, `ops/aar_autotune.py` → dql `kind='forensics_trigger'` (fingerprint/dossier in `notes`).
- `parity_drift_log` — `tpcore/parity/harness.py` → dql `kind='parity_drift'`.
- `ingestion_metrics` — `tpcore/ingestion/{handlers,d2_metrics}.py` → `ingest_manifest` (reconciliation counts, spec v1.3), NOT dql.

**count_snapshot correction (SUPERSEDES Task 4):** `earnings_events_count_snapshot` is a STATEFUL monotone baseline (`earnings_events_monotone.py` does `SELECT … FOR UPDATE` + upsert to detect count *shrinkage*), NOT a cache. A VIEW always equals the live count → defeats the monotone invariant. **Migration `0400` is DROPPED entirely; the table STAYS a mutable baseline table; re-chain `0500.down_revision → 0300`.** Spec §2.4 updated to exempt stateful-baseline snapshots from view-demotion.

Each rewired producer keeps its mock/unit tests green; the live-coupled whole-suite goes green only AFTER the Task-7 migration apply.

---

### Task 1: Phase-1 snapshot — PRESERVE-class + full DB snapshot

**Files:**
- Create: `scripts/rebuild_snapshot_preserve_tables.sh`
- Artifacts: `data/rebuild_2026-06-04/preserve/{ingest_manifest,allocations,risk_close_ledger}.csv`

- [ ] **Step 1: Write the snapshot script (COPY each PRESERVE-class table to CSV)**

```bash
#!/usr/bin/env bash
# Phase-1 snapshot of the PRESERVE-class ops tables before the Plan 2 cutover.
# These are EXCLUDED from the TRUNCATE, but a verbatim off-DB copy is the
# belt-and-suspenders rollback (the SACRED-carve-out analog for ops state).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
OUT="data/rebuild_2026-06-04/preserve"
mkdir -p "$OUT"
# Session-mode psql via the IPv4 URL; \copy runs client-side (no server FS needed).
PSQL_URL="${DATABASE_URL_IPV4%%\?*}"   # strip any ?params for psql
for t in ingest_manifest allocations risk_close_ledger; do
  psql "$PSQL_URL" -c "\copy (SELECT * FROM platform.$t) TO '$OUT/$t.csv' WITH CSV HEADER"
  echo "snapshot: $t -> $OUT/$t.csv ($(wc -l < "$OUT/$t.csv") lines incl header)"
done
echo "Phase-1 PRESERVE snapshot complete: $OUT"
```

- [ ] **Step 2: Run it + verify row counts match live**

Run: `bash scripts/rebuild_snapshot_preserve_tables.sh`
Expected: `ingest_manifest` 116+1, `allocations` 11+1, `risk_close_ledger` 0+1 (header) lines. If any count is 0-unexpected, STOP.

- [ ] **Step 3: Take a Supabase on-demand snapshot + record the PITR window**

Run (operator action, or via the Supabase dashboard/API): create an on-demand backup of the project, and record the current timestamp as the PITR restore anchor in `data/rebuild_2026-06-04/PITR_ANCHOR.txt`. The SACRED `hy_spread` is already at `data/macro_hy_spread_sacred_archive/` + S3 (and lives in untouched `macro_data`) — note that in the anchor file too.
Expected: a confirmed snapshot id + a recorded UTC anchor timestamp. **This is the rollback for the irreversible Task 7.**

- [ ] **Step 4: Commit the snapshot script + artifacts manifest**

```bash
git add scripts/rebuild_snapshot_preserve_tables.sh data/rebuild_2026-06-04/PITR_ANCHOR.txt
git commit -m "chore(rebuild): Plan 2 phase-1 snapshot script + PITR anchor"
```
(Do NOT commit the CSVs if they contain anything sensitive — `ingest_manifest`/`allocations`/`risk_close_ledger` are operational metadata, no secrets; gitignore them under `data/rebuild_2026-06-04/preserve/` and keep only the anchor + script tracked.)

---

### Task 2: Phase-2 — pause every writer

**Files:**
- Reference: `ops/apply_railway_service_config.py` (the GraphQL `serviceInstanceUpdate` pattern), `reference_railway_access` memory.

- [ ] **Step 1: Confirm the data-operations cron is still cleared**

Query Railway (GraphQL, `Authorization: Bearer $RAILWAY_API_TOKEN`) for the `data-operations` service (`d39b7e55-…`) `cronSchedule`. Expected: `null` (cleared 2026-06-04). If a schedule reappeared, clear it.

- [ ] **Step 2: Pause engine-service + lane-service + trade-monitor**

Via `serviceInstanceUpdate` (env production `58653d3b-…`), scale each of `engine-service`, `lane-service`, `trade-monitor` to zero / disable. engine-service is the substrate reader holding live momentum state; lane-service + trade-monitor are reactive substrate writers (spec §8.1).
Expected: all three confirmed paused. Record the prior config so Plan 4 can restore (data-operations cron → `30 21 * * MON-FRI`).

- [ ] **Step 3: Verify no live writer is touching the DB**

Run a read-only check: no `application_log` rows written in the last 5 minutes from a non-rebuild `run_id`, and `pg_stat_activity` shows no active write query against `platform.*` from the daemons.
```bash
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv()
from tpcore.db import build_asyncpg_pool
async def m():
    p=await build_asyncpg_pool(os.environ['DATABASE_URL_IPV4'], read_only=True, max_size=2)
    async with p.acquire() as c:
        n=await c.fetchval(\"SELECT count(*) FROM platform.application_log WHERE recorded_at > now()-interval '5 min'\")
        print('recent application_log writes (last 5min):', n)
    await p.close()
asyncio.run(m())"
```
Expected: 0 (or only this session's). If writers are active, STOP — the pause didn't take.

---

### Task 3: DROP-set migration (dead / Tradier / re-derivable-folded tables)

**Files:**
- Create: `platform/migrations/versions/20260604_0300_drop_dead_and_folded_tables.py`
- Test: `tests/test_plan2_cutover_migrations.py`

- [ ] **Step 1: Write the static sentinel (failing) for the DROP set**

```python
"""Static sentinels for the Plan 2 cutover migrations (no live DB)."""
from __future__ import annotations
from pathlib import Path

DROP_MIG = Path("platform/migrations/versions/20260604_0300_drop_dead_and_folded_tables.py")
DROPPED = ["tradier_options_chains", "options_max_pain", "ticker_lifecycle_events",
           "fundamentals_period_source_evidence", "parity_drift_log", "forensics_triggers",
           "ingestion_metrics"]
KEPT = ["split_pre_image_log", "ingest_quarantine", "failed_alpha_ledger", "ingest_manifest"]


def test_drop_migration_pins_and_drops_only_the_dead_set() -> None:
    assert DROP_MIG.exists(), f"missing {DROP_MIG}"
    src = DROP_MIG.read_text()
    assert "20260604_0200" in src  # down_revision = Plan 1 head
    for t in DROPPED:
        assert f"DROP TABLE IF EXISTS platform.{t}" in src, f"{t} not dropped"
    for t in KEPT:
        assert f"DROP TABLE IF EXISTS platform.{t}" not in src, f"{t} must NOT be dropped (kept/deferred)"
    # options_max_pain's trigger function is dropped with it
    assert "tg_set_classification_id_options_max_pain" in src
```

Run: `.venv/bin/python -m pytest tests/test_plan2_cutover_migrations.py -v` → FAIL (missing migration).

- [ ] **Step 2: Verify nothing live still references the DROP-set tables**

Run a read-only check: no FK from a KEEP table to any DROP-set table; grep the codebase for live readers/writers of each dropped table.
```bash
grep -rn -E "tradier_options_chains|options_max_pain|ticker_lifecycle_events|fundamentals_period_source_evidence|parity_drift_log|forensics_triggers|ingestion_metrics" \
  tpcore/ ops/ scripts/ --include=*.py | grep -viE "test_|migration|#|archive/" | head -30
```
Expected: only references that are themselves being retired (or none). If a live engine/validator reads a dropped table, STOP — that consumer must be migrated first (escalate; this is a CIC `validator_or_gate_change` boundary). `ticker_lifecycle_events` readers must move to `corporate_events` (Task 8 ensures the `event_kind` covers delisting).

- [ ] **Step 3: Write the DROP migration**

```python
"""Plan 2 cutover — drop the dead / Tradier / re-derivable-folded tables.

Spec §2.2/§2.3. tradier_options_chains (Tradier closed), options_max_pain (no
producer) + its trigger fn, ticker_lifecycle_events (re-derived into
corporate_events in Plan 3), the empty evidence/parity/forensics sidecars
(folded into data_quality_log via `kind`), and ingestion_metrics (routes to
ingest_manifest). split_pre_image_log, ingest_quarantine, failed_alpha_ledger
are KEPT. macro_data + the PRESERVE-class ops tables are untouched.
"""
from __future__ import annotations
from alembic import op

revision = "20260604_0300"
down_revision = "20260604_0200"
branch_labels = None
depends_on = None

_DROP = [
    "tradier_options_chains", "options_max_pain", "ticker_lifecycle_events",
    "fundamentals_period_source_evidence", "parity_drift_log", "forensics_triggers",
    "ingestion_metrics",
]


def upgrade() -> None:
    # options_max_pain carries a classification_id trigger fn — drop the trigger+fn first.
    op.execute("DROP TRIGGER IF EXISTS tg_set_classification_id_options_max_pain ON platform.options_max_pain")
    op.execute("DROP FUNCTION IF EXISTS platform.tg_set_classification_id_options_max_pain()")
    for t in _DROP:
        op.execute(f"DROP TABLE IF EXISTS platform.{t} CASCADE")


def downgrade() -> None:
    # Irreversible for data; the tables are recreated only by replaying the
    # ORIGINAL migrations that created them (not re-implemented here). The Plan 2
    # rollback path is the Task-1 snapshot + Supabase PITR, not this downgrade.
    raise NotImplementedError(
        "Plan 2 DROP migration is forward-only; roll back via the phase-1 snapshot + Supabase PITR."
    )
```

Run the sentinel: `.venv/bin/python -m pytest tests/test_plan2_cutover_migrations.py::test_drop_migration_pins_and_drops_only_the_dead_set -v` → PASS.

- [ ] **Step 4: Commit (do NOT apply yet — applies in Task 7's gated sequence)**

```bash
git add platform/migrations/versions/20260604_0300_drop_dead_and_folded_tables.py tests/test_plan2_cutover_migrations.py
git commit -m "feat(rebuild): Plan 2 DROP-set migration (dead/Tradier/folded tables)"
```

---

### Task 4: `earnings_events_count_snapshot` → VIEW migration

**Files:**
- Create: `platform/migrations/versions/20260604_0400_count_snapshot_to_view.py`
- Test: extend `tests/test_plan2_cutover_migrations.py`

- [ ] **Step 1: Confirm OQ-4 — no writer depends on the count snapshot being materialized**

Run: `grep -rn "earnings_events_count_snapshot" tpcore/ ops/ scripts/ --include=*.py | grep -viE "test_|migration"`
Expected: no daemon that writes a point-in-time snapshot row. If one exists, it folds into `data_quality_log` (`kind='count_snapshot'`) instead (spec OQ-4) — STOP and escalate. If only readers, the VIEW satisfies them.

- [ ] **Step 2: Write the migration (drop table, create equivalent view)**

```python
"""Plan 2 — demote earnings_events_count_snapshot to a VIEW (spec §2.4, OQ-4)."""
from __future__ import annotations
from alembic import op

revision = "20260604_0400"
down_revision = "20260604_0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.earnings_events_count_snapshot CASCADE")
    # Reproduce the snapshot's columns as a live VIEW over earnings_events.
    # VERIFY the original table's columns first (Step 1) and match them here.
    op.execute("""
        CREATE OR REPLACE VIEW platform.earnings_events_count_snapshot AS
        SELECT ticker, count(*) AS n_events, max(event_date) AS last_event_date
        FROM platform.earnings_events
        GROUP BY ticker
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.earnings_events_count_snapshot")
```

> Before writing the `SELECT`, read the dropped table's real columns (`\d platform.earnings_events_count_snapshot`) and reproduce them so existing readers don't break. If the columns can't be reproduced from `earnings_events` alone, that's a sign the snapshot was load-bearing → fold to `data_quality_log` instead (OQ-4).

- [ ] **Step 3: Add the sentinel + commit**

Append to `tests/test_plan2_cutover_migrations.py`:
```python
def test_count_snapshot_becomes_view() -> None:
    src = Path("platform/migrations/versions/20260604_0400_count_snapshot_to_view.py").read_text()
    assert "20260604_0300" in src
    assert "DROP TABLE IF EXISTS platform.earnings_events_count_snapshot" in src
    assert "CREATE OR REPLACE VIEW platform.earnings_events_count_snapshot" in src
```
Run the sentinels (PASS), then commit.

---

### Task 5: `data_quality_log` redesign migration

**Files:**
- Create: `platform/migrations/versions/20260604_0500_data_quality_log_redesign.py`
- Test: extend `tests/test_plan2_cutover_migrations.py`

- [ ] **Step 1: Write the redesign migration (drop + recreate; rows are reproduced)**

```python
"""Plan 2 — redesign data_quality_log into the consolidation substrate (spec §3.3).

LIVE (20260509_0000) is a single-purpose freshness-metric log: id bigint,
source, timestamp, latency_ms/missing_bars/stale/confidence (all NOT NULL),
notes text, UNIQUE(source,timestamp), ~6.5K rows. Those rows are REPRODUCED on
the next validation pass, so this drops + recreates rather than migrating data.

Target: uuid id, `kind` discriminator, the typed metric columns become
VALIDATION-ONLY + NULLABLE (CHECK ties them to kind='validation'), notes->jsonb,
per-kind partial indexes. Fold sources: fundamentals_period_source_evidence +
parity_drift_log + forensics_triggers (all dropped empty in 0300) become `kind`
values here. failed_alpha_ledger + ingest_quarantine stay STANDALONE (v1.4).
"""
from __future__ import annotations
from alembic import op

revision = "20260604_0500"
down_revision = "20260604_0400"
branch_labels = None
depends_on = None

KINDS = ("validation", "confirmed_data_gap_evidence", "parity_drift",
         "forensics_trigger", "backtest_credibility")


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.data_quality_log CASCADE")
    kinds_sql = ", ".join(f"'{k}'" for k in KINDS)
    op.execute(f"""
        CREATE TABLE platform.data_quality_log (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind         text NOT NULL CHECK (kind IN ({kinds_sql})),
            source       text NOT NULL,
            "timestamp"  timestamptz NOT NULL,
            latency_ms   integer,
            missing_bars integer,
            stale        boolean,
            confidence   numeric,
            notes        jsonb,
            recorded_at  timestamptz NOT NULL DEFAULT now(),
            -- typed metric columns are VALIDATION-ONLY: populated iff kind='validation'
            CONSTRAINT dql_typed_cols_validation_only CHECK (
                kind = 'validation'
                OR (latency_ms IS NULL AND missing_bars IS NULL AND stale IS NULL AND confidence IS NULL)
            )
        )
    """)
    # Partial indexes per hot kind (the live hot path is overwhelmingly validation).
    op.execute("CREATE INDEX ix_dql_validation ON platform.data_quality_log (\"timestamp\", source) WHERE kind='validation'")
    op.execute("CREATE INDEX ix_dql_parity_drift ON platform.data_quality_log (\"timestamp\") WHERE kind='parity_drift'")
    op.execute("CREATE INDEX ix_dql_forensics ON platform.data_quality_log (\"timestamp\") WHERE kind='forensics_trigger'")
    op.execute("CREATE INDEX ix_dql_notes_gin ON platform.data_quality_log USING gin (notes)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.data_quality_log CASCADE")
    op.execute("""
        CREATE TABLE platform.data_quality_log (
            id           bigserial PRIMARY KEY,
            source       text NOT NULL,
            "timestamp"  timestamptz NOT NULL,
            latency_ms   integer NOT NULL,
            missing_bars integer NOT NULL DEFAULT 0,
            stale        boolean NOT NULL DEFAULT false,
            confidence   numeric NOT NULL,
            notes        text,
            UNIQUE (source, "timestamp")
        )
    """)
```

- [ ] **Step 2: Confirm the validation-suite writer matches the new shape**

The `tpcore/quality/validation/` suite writes `data_quality_log`. Read its writer (the `CheckResult` persister) and confirm it can write the new shape (`kind='validation'` + the typed cols + jsonb `notes`). If the writer inserts the OLD column set unconditionally, it must be updated in the SAME migration PR (heavy-lane `tpcore/quality/validation/**`). Name the writer file:line in the PR. This is the one place Plan 2 touches code beyond migrations.

> If updating the writer is non-trivial, split it: land the table redesign + a thin writer shim (write `kind='validation'` + the 4 typed cols, `notes=jsonb_build_object(...)`) so the suite stays green. The full per-`kind` writer paths land as their producers are wired in Plan 3/4.

- [ ] **Step 3: Sentinel + commit**

Append the sentinel (assert `kind` CHECK, the `dql_typed_cols_validation_only` CHECK, uuid PK, the 4 partial indexes), run (PASS), commit.

---

### Task 6: Pre-wipe FK-coverage map (the TRUNCATE-safety gate)

**Files:** none (verification only — produces the exact TRUNCATE statement)

- [ ] **Step 1: Enumerate every table with an FK into the wipe set**

A single `TRUNCATE` fails if any table with an FK to a truncated table is NOT also in the statement (Postgres rule). Produce the authoritative list:
```bash
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv()
from tpcore.db import build_asyncpg_pool
async def m():
    p=await build_asyncpg_pool(os.environ['DATABASE_URL_IPV4'], read_only=True, max_size=2)
    async with p.acquire() as c:
        rows=await c.fetch('''SELECT DISTINCT c.conrelid::regclass::text AS child, c.confrelid::regclass::text AS parent
          FROM pg_constraint c WHERE c.contype='f' AND c.connamespace='platform'::regnamespace
          ORDER BY 2,1''')
        for r in rows: print(r['child'],'->',r['parent'])
    await p.close()
asyncio.run(m())"
```
Expected: every `child` that references a table in the TRUNCATE set must itself be in the set. Cross-check against the disposition table's TRUNCATE list. **If a child is missing (e.g. an ops table FKs `ticker_classifications` and isn't listed), add it to the TRUNCATE statement OR the statement will error.** `aar_events` is the known one (its Plan 1 FK → `ticker_classifications`) — already in the list. Record the FINAL, FK-complete TRUNCATE list for Task 7.

---

### Task 7: ⚠️ THE WIPE — gated TRUNCATE (IRREVERSIBLE; separate operator go)

**Files:**
- Create: `scripts/rebuild_truncate_ticker_graph.sh`

- [ ] **Step 1: PRECONDITION GATE — all must be true (abort if any is false)**
  - Task 1 PRESERVE snapshot taken + Supabase on-demand snapshot confirmed + PITR anchor recorded.
  - Task 2 writers paused + verified (0 recent writes).
  - Tasks 3–5 migrations applied (`alembic current` == `20260604_0500`).
  - Task 6 FK-coverage map confirms the TRUNCATE list is FK-complete.
  - **Operator has given the explicit go for the wipe** (separate from authoring).

- [ ] **Step 2: Apply migrations 0300→0500 to the live DB (the reversible-where-possible part)**

```bash
scripts/run_alembic_upgrade.sh 20260604_0500
```
Verify `alembic current` == `20260604_0500`; the DROP set is gone; `data_quality_log` has the new shape; the whole suite is still green (`-p no:xdist`).

- [ ] **Step 3: Write + run the TRUNCATE script (session-mode, single statement)**

```bash
#!/usr/bin/env bash
# Plan 2 Task 7 — the irreversible ticker-graph wipe. One TRUNCATE statement so
# the mutual classification_id FKs are satisfiable. EXCLUDES macro_data + the
# PRESERVE-class ops tables. Session-mode :5432 (DDL/TRUNCATE; not the pooler).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
[[ "${REBUILD_WIPE_CONFIRM:-}" == "I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO" ]] || {
  echo "Refusing: set REBUILD_WIPE_CONFIRM=I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO" >&2; exit 1; }
PSQL_URL="${DATABASE_URL_IPV4%%\?*}"
psql "$PSQL_URL" -v ON_ERROR_STOP=1 -c "
TRUNCATE TABLE
  platform.prices_daily, platform.prices_daily_staging, platform.fundamentals_quarterly,
  platform.ticker_classifications, platform.ticker_history, platform.issuers,
  platform.issuer_securities, platform.issuer_history, platform.corporate_events,
  platform.corporate_actions, platform.earnings_events, platform.short_interest,
  platform.borrow_rates, platform.insider_transactions, platform.insider_sentiment,
  platform.social_sentiment, platform.sec_material_events, platform.spread_observations,
  platform.liquidity_tiers, platform.universe_candidates, platform.aar_events
  RESTART IDENTITY;"
echo "WIPE complete."
```
Run: `REBUILD_WIPE_CONFIRM=I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO bash scripts/rebuild_truncate_ticker_graph.sh`
Expected: `WIPE complete.` (Replace the table list with Task 6's FK-complete list if it differs.)

- [ ] **Step 4: Verify the wipe + that PRESERVE/macro survived**

```bash
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv()
from tpcore.db import build_asyncpg_pool
async def m():
    p=await build_asyncpg_pool(os.environ['DATABASE_URL_IPV4'], read_only=True, max_size=2)
    async with p.acquire() as c:
        for t in ['prices_daily','fundamentals_quarterly','ticker_classifications','ticker_history']:
            print(t,'=',await c.fetchval(f'SELECT count(*) FROM platform.{t}'))
        for t in ['macro_data','ingest_manifest','allocations']:
            print('PRESERVED',t,'=',await c.fetchval(f'SELECT count(*) FROM platform.{t}'))
    await p.close()
asyncio.run(m())"
```
Expected: wiped tables = 0; `macro_data` unchanged (~186,937), `ingest_manifest`=116, `allocations`=11. **If macro or PRESERVE counts dropped, STOP and restore from PITR.**

- [ ] **Step 5: Commit the wipe script**

```bash
git add scripts/rebuild_truncate_ticker_graph.sh
git commit -m "feat(rebuild): Plan 2 Task 7 gated ticker-graph TRUNCATE script"
```

---

### Task 8: Schema-tightening migration (on the now-empty tables)

**Files:**
- Create: `platform/migrations/versions/20260604_0600_tighten_identity_fundamentals_schema.py`
- Test: extend `tests/test_plan2_cutover_migrations.py`

- [ ] **Step 1: Confirm the exact constraint names + that `fundamentals_quarterly` is empty**

```bash
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv()
from tpcore.db import build_asyncpg_pool
async def m():
    p=await build_asyncpg_pool(os.environ['DATABASE_URL_IPV4'], read_only=True, max_size=2)
    async with p.acquire() as c:
        print('fq rows:', await c.fetchval('SELECT count(*) FROM platform.fundamentals_quarterly'))
        for r in await c.fetch('''SELECT conname, pg_get_constraintdef(oid) d FROM pg_constraint
          WHERE conrelid='platform.fundamentals_quarterly'::regclass AND contype IN ('p','u')'''):
            print(r['conname'], r['d'])
        print('corp_events event_kind CHECK:', await c.fetchval('''SELECT pg_get_constraintdef(oid) FROM pg_constraint
          WHERE conrelid='platform.corporate_events'::regclass AND contype='c' AND conname ILIKE '%event_kind%' LIMIT 1'''))
    await p.close()
asyncio.run(m())"
```
Expected: `fq rows: 0` (Task 7 done). Record the surrogate PK name + the `(ticker, filing_date)` UNIQUE name for the migration.

- [ ] **Step 2: Write the tightening migration**

```python
"""Plan 2 — tighten identity + fundamentals schema on the empty tables (spec §3.1/§3.2).

Runs AFTER the Task-7 wipe (the tables are empty, so these are clean/fast):
  - ticker_classifications.lifetime_start: DROP the '1900-01-01' DEFAULT (stays
    NOT NULL, no default) so a load that fails to populate FPFD errors instead of
    silently sentineling (spec §3.1 / invariant A6).
  - fundamentals_quarterly: replace the surrogate PK + UNIQUE(ticker,filing_date)
    with the 3-part natural PK (ticker, period_end_date, filing_date) — restatement-
    preserving (spec §1.2 decision 8 / §3.2).
  - corporate_events.event_kind: ensure it admits delisting/bankruptcy so the
    re-ingest can absorb the dropped ticker_lifecycle_events (spec §2.2).
"""
from __future__ import annotations
from alembic import op

revision = "20260604_0600"
down_revision = "20260604_0500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform.ticker_classifications ALTER COLUMN lifetime_start DROP DEFAULT")

    # fundamentals_quarterly -> 3-part natural PK. Use the REAL constraint names
    # confirmed in Step 1 (placeholders below — replace before running).
    op.execute("ALTER TABLE platform.fundamentals_quarterly ALTER COLUMN period_end_date SET NOT NULL")
    op.execute("ALTER TABLE platform.fundamentals_quarterly ALTER COLUMN filing_date SET NOT NULL")
    op.execute("ALTER TABLE platform.fundamentals_quarterly DROP CONSTRAINT IF EXISTS fundamentals_quarterly_pkey")
    op.execute("ALTER TABLE platform.fundamentals_quarterly DROP CONSTRAINT IF EXISTS fundamentals_quarterly_ticker_filing_date_key")
    op.execute("ALTER TABLE platform.fundamentals_quarterly ADD PRIMARY KEY (ticker, period_end_date, filing_date)")
    # The surrogate `id` column: keep as a plain column unless Step 1 proved no FK
    # references it. (Do NOT drop blindly.)

    # corporate_events.event_kind — extend the CHECK to admit delisting/bankruptcy
    # IF the live CHECK (Step 1) does not already include them. Otherwise no-op.


def downgrade() -> None:
    op.execute("ALTER TABLE platform.ticker_classifications ALTER COLUMN lifetime_start SET DEFAULT '1900-01-01'")
    op.execute("ALTER TABLE platform.fundamentals_quarterly DROP CONSTRAINT IF EXISTS fundamentals_quarterly_pkey")
    op.execute("ALTER TABLE platform.fundamentals_quarterly ADD PRIMARY KEY (id)")
    op.execute("ALTER TABLE platform.fundamentals_quarterly ADD CONSTRAINT fundamentals_quarterly_ticker_filing_date_key UNIQUE (ticker, filing_date)")
```

- [ ] **Step 3: Apply + verify live, add sentinel, commit**

```bash
scripts/run_alembic_upgrade.sh 20260604_0600
```
Verify: `lifetime_start` has no default; `fundamentals_quarterly` PK == `(ticker, period_end_date, filing_date)`. Add the sentinel (assert the DDL strings), run, commit.

---

### Task 9: Post-cutover verification + whole-suite + push

**Files:** none (verification + gate)

- [ ] **Step 1: Full post-cutover live assertion**

Confirm in one read-only pass: `alembic current` == `20260604_0600`; DROP-set tables absent; `data_quality_log` new shape; ticker graph empty; `macro_data`/PRESERVE-class intact; `lifetime_start` no-default; `fundamentals_quarterly` 3-part PK; `split_pre_image_log` still present (deferred); `failed_alpha_ledger`/`ingest_quarantine` still present.

- [ ] **Step 2: Whole-suite, single process (authoritative)**

Run: `.venv/bin/python -m pytest -p no:xdist -q`
Expected: green. Note: some validation/identity tests may now find empty tables — that is EXPECTED post-wipe and PRE-re-ingest; any test that asserts non-empty ticker data must be marked to run only after Plan 3 (flag these, do not delete). The `DATA_OPERATIONS_COMPLETE` gate will be RED until Plan 3+4 — that is correct ("don't trade on empty data").

- [ ] **Step 3: Manifest sentinels + push**

`.venv/bin/python scripts/check_manifests.py` → OK; then `git push origin main`; confirm CI green within 60s.

---

## Self-Review

**Spec coverage (Plan 2 scope = §2.2/§2.3/§2.4 + §3.1/§3.2/§3.3 + §7 + §8.1/§8.2 phases 1–3):**
- Phase-1 snapshot (PRESERVE-class + Supabase + SACRED-already-off-DB) → Task 1. ✓
- Phase-2 writer pause (engine/lane/trade-monitor + cron) → Task 2. ✓
- DROP set (§2.3) incl. options_max_pain trigger fn; `split_pre_image_log` correctly KEPT (deferred) → Task 3. ✓
- count_snapshot → VIEW (§2.4, OQ-4) → Task 4. ✓
- `data_quality_log` redesign (§3.3) + the writer-shim caveat → Task 5. ✓
- TRUNCATE single-statement w/ FK-coverage gate + PRESERVE/macro exclusion (§7/§8.2) → Tasks 6–7. ✓
- `lifetime_start` no-default + FQ 3-part PK + corporate_events delisting kind (§3.1/§3.2/§2.2) → Task 8. ✓
- Rollback = snapshot + PITR (§8.3); DROP/redesign migrations are forward-only by design → stated in Task 3 downgrade. ✓
- **Deferred to Plan 3:** the `spread_observations_retention_trg` disable-during-load wraps the spread *backfill* (a re-ingest step), not the empty-schema cutover — noted, not in Plan 2.

**Placeholder scan:** the "replace the real constraint name before running" notes (Task 8) + "match the dropped table's columns" (Task 4) + "verify the validation writer matches the new shape" (Task 5) are explicit verify-live-first steps with the exact query to run — the destructive-plan analog of Plan 1's pattern, not hand-waves. The DDL is concrete and grounded in the 2026-06-04 live introspection.

**Type/name consistency:** revision chain `20260604_0200 → 0300 → 0400 → 0500 → 0600` (each migration pins its `down_revision`; the sentinels assert it). The TRUNCATE list in Task 7 == the disposition-table TRUNCATE row == Task 6's FK-complete output (Task 6 is the reconciliation gate). `data_quality_log` `KINDS` (Task 5) == the §3.3 enum == the spec revision-history (no `failed_alpha`/`ingest_quarantine`/`execution_quality`).

---

## Roadmap — Plans 3 & 4 (authored after Plan 2 lands)

- **Plan 3 — Identity-first re-ingest.** With the empty clean schema: run `scripts/ops.py` stages in identity-first order (universe → issuers → identity → prices → fundamentals → signals); the `adjusted_close` cumulative-factor model (§5.6, lets `split_pre_image_log` finally drop); disable `spread_observations_retention_trg` around the spread backfill; the re-attribution verify (0 NULL classification_id, 0 orphans, 0 pre-FPFD, 0 out-of-window); FK VALIDATE pass.
- **Plan 4 — Validation green-gate + acceptance.** Identity-aware validation; the 32-check 100%-green gate; first `DATA_OPERATIONS_COMPLETE`; restore the data-operations cron `30 21 * * MON-FRI` + un-pause engine/lane/trade-monitor; bring `docs/DATABASE_AND_DATAFLOW.md` §2/§3 current (§9).
