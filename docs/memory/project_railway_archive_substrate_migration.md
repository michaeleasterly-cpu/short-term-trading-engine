---
name: railway-archive-substrate-migration
description: "Pre-Railway-migration blocker + LOCKED design: vendor-truncation DETECTION must move to durable Postgres (D2); the CSV-first archive RECOVERY artifact must move to an attached object-storage bucket (R3). Decided 2026-05-18; built at migration, not now."
metadata: 
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

**Blocker (do not let a Railway cutover silently ship the broken
substrate).** The vendor-truncation `shrinkage_detector` and the whole
operator-canonical CSV-first archive are **hardwired to a persistent
local filesystem** (`tpcore/ingestion/csv_archive.py:repo_data_dir()`
= `Path(__file__).parents[2]/"data"`, no env/volume override;
`railway.json` has no volume). On Railway's **ephemeral container FS**:
detection silently always-passes (empty `data/` → `latest_archive`
None → emits OK = "I checked nothing" — worst class for live money),
`csv_archive_presence` flaps, the recovery substrate evaporates.

**Expert verdict (2026-05-18, skeptical staff-architect pass):** the
current snapshot-vs-single-prior-CSV is the **wrong substrate even on
the Mac** — single-prior poisoned-baseline, gradual <20%/snapshot
erosion invisible, only 5 full-snapshot sources. Detection and
recovery are **separable concerns with different durability needs**.

**LOCKED design (operator-approved 2026-05-18; built AT migration,
not now — Railway is paused, re-enable deferred until an engine proves
edge per [[project_railway_hobby_tier]]):**
- **Detection → D2:** each ingest persists per-source row-count /
  min-max-date / coverage to **Postgres** (Supabase, host-agnostic);
  shrinkage = deviation vs **rolling-median of durable history** (not
  single-prior). Reuses the platform's own `prices_daily_completeness`
  / freshness pattern; fixes the local design flaws too. (D3 = fold
  full-snapshot sources into a completeness-style physical invariant
  — stronger, larger; D2 is the chosen primary.)
- **Recovery → R3:** the CSV-first archive moves to an **S3-compatible
  object-storage bucket attached to the service** (operator's bucket
  idea — Railway-attached / Supabase Storage / R2 / S3), via S3 API +
  env-injected endpoint/creds. Keeps the CSV-first canonical workflow;
  host-agnostic; not volume-size-capped. (R2 Railway Volume = weaker
  fallback; R4 Postgres-BYTEA rejected — would consume the 8GB
  [[project_supabase_pro_tier]] budget; archive is 1.4GB+ growing.)
- A bucket alone is **necessary for recovery but NOT sufficient** —
  it does not make single-prior detection less fragile; detection
  MUST become DB-derived regardless.
- Exact Railway bucket wiring (native object store vs external S3 /
  Supabase Storage, env var names, IAM) is an infra detail to
  **verify against current Railway docs at migration time**
  ([[feedback_use_official_docs]]) — not asserted now.

**Zero-risk host-agnostic preps — DONE 2026-05-18 (PR #76 merged, no
Railway infra):** (1) `csv_archive.repo_data_dir()` now honors a
`TP_DATA_DIR` env override (unset/empty = byte-identical to the prior
`Path(__file__).parents[2]/"data"`) — the R2/R3 seam; (2) the
uncheckable/empty-archive shrinkage path now emits **WARN (non-green:
persists `stale=True`), never silent OK** (FAIL>WARN>OK precedence,
FAIL path byte-unchanged) — the "no fake-green" latent-bug fix. So
the migration cutover is "set env + mount/bucket", not a code change
under pressure.

**R3 — DEPLOYED 2026-05-21 (PR feat/csv-archive-pluggable-backend-r3,
operator autonomous-lane directive "implement directly, ship"):**
`tpcore/ingestion/csv_archive.py` now routes write/read through
`tpcore.ingestion.csv_archive_backends.select_backend()`. Two
backends: `LocalFSBackend` (the default — byte-identical to the
prior local-only behaviour, every existing test stays green) and
`S3Backend` (env-driven via `CSV_ARCHIVE_BACKEND=s3` +
`CSV_ARCHIVE_S3_{ENDPOINT,BUCKET,KEY_ID,SECRET,REGION,SECURE}`). S3
client: `minio>=7.2,<8` (chosen over boto3 for transitive-dep weight
— single package, no awscli tree, natively works against ANY
S3-compatible). Operator-on-demand `_stage_rebuild_from_archive`
shipped (`scripts/ops.py`) replays the latest `<source>_archive` into
`platform.prices_daily` via the canonical idempotent upsert; works
identically against both backends. `railway.json` documents the env
vars in `_csv_archive_env_vars`; `docs/OPERATIONS.md` has the
rebuild runbook. **Railway cutover is now config (set env vars +
provision bucket), not a code change.** Actual data-migration to the
bucket is still a one-shot operator action at Railway re-enable time
— NOT done in this PR (memory rule, don't build Railway infra until
re-enable).

**D2 — STILL PENDING (NOT done in PR feat/csv-archive-pluggable-
backend-r3, separate follow-up).** Detection-rebase onto Postgres
rolling-median remains the pre-Railway blocker. The current
single-prior-CSV detector still works on local disk (where the
archive is durable) and works correctly when reading from S3 (the
backend's `list_archives` + `read_latest` are content-equivalent to
the local glob) — but the original "single-prior is the wrong
substrate even on the Mac" verdict stands. D2 is the next PR in this
epic.

**Migration trigger/sequencing:** re-base detection onto Postgres
(D2) BEFORE Railway re-enable; bucket provisioning + env-var set is
now a Railway-config task (R3 code is in). Tracked in `TODO.md` as a
pre-Railway blocker. Don't build Railway infra until the operator
re-enables Railway (memory rule, [[project_railway_hobby_tier]]).
