---
name: r2-archive-substrate-2026-05-26
description: "CSV archive substrate now lives in Cloudflare R2 (S3-compatible). CSV_ARCHIVE_BACKEND=s3 + 4 env vars. R2 endpoint MUST be host:port (no `https://` scheme prefix) — Minio rejects URL-form. Buckets: ste-archives. manifest_lifecycle has a known LocalFSBackend-only bug — archive Phase 1 writes to R2 but Phase 2 (read + upsert) raises NotImplementedError; backfills must drive Phase 2 manually."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

## Active state (as of 2026-05-26)

**Railway + Supabase + R2 + Vercel = full cloud stack.** Operator
moved everything off the local Mac on 2026-05-26 (CSV archive went to
S3 → R2, daemons shut down on Mac, Railway is the runtime).

## Env-var contract for R2 backend

Per `tpcore/ingestion/csv_archive_backends.py`:

  - `CSV_ARCHIVE_BACKEND=s3`           — selects S3Backend (default = local)
  - `CSV_ARCHIVE_S3_ENDPOINT=<host>`   — **HOST:PORT only, NO scheme**
                                         (the docstring says "no scheme;
                                         minio adds it via secure= flag
                                         below"). Putting `https://` in
                                         the value makes Minio raise
                                         `ValueError: path in endpoint
                                         is not allowed`.
  - `CSV_ARCHIVE_S3_BUCKET`            — bucket (operator's: `ste-archives`)
  - `CSV_ARCHIVE_S3_KEY_ID` + `CSV_ARCHIVE_S3_SECRET` — credentials

**Failure mode if endpoint has `https://`:** the BACKEND HAS `s3` set
but every write/read falls through to `LocalFSBackend` because
`select_backend()` raises and the caller defaults. The archive looks
healthy locally but never lands in R2. Detected 2026-05-26 — I fixed
my local `.env` by stripping the prefix; operator should verify the
same on Railway's env vars (railway env list / dashboard).

## Known bug: manifest_lifecycle is LocalFSBackend-only

`tpcore/ingestion/archive_etl.py:manifest_lifecycle` raises
`NotImplementedError: manifest_lifecycle requires LocalFSBackend; got
non-Path archive_path='s3://...'` after writing the archive to R2 in
Phase 1. Phase 2 (read CSV from disk + per-symbol upsert into DB)
fails because it expects a local Path, not an s3:// string.

**Impact**: canonical archive-first ingest handlers
(`handle_fundamentals_refresh`, etc.) succeed at writing the archive
to R2 but fail to upsert the rows into the database. The DB and the
archive get out of sync.

**Workaround**: read the just-written archive via
`backend.read_latest(source)` (backend-aware) → decompress →
per-symbol upsert via `cache.upsert_payload`. Pattern at
`/tmp/finish_fundamentals_upsert.py` (one-off script that recovered
~13,500 fundamentals rows on 2026-05-26).

**Permanent fix needed**: make `manifest_lifecycle` backend-agnostic
— read the archive via `backend.read_latest()` rather than expecting
a local file path. Heavy-lane PR. Tracked as follow-up.

## How to use

For one-off backfills that must follow the canonical R2 path:

  1. Build `archive_rows` per the relevant handler's
     `_payload_to_archive_rows`.
  2. Enter `manifest_lifecycle` — Phase 0 fetches, Phase 1 writes
     archive to R2 (via S3Backend); CATCH the
     `NotImplementedError` from `manifest_lifecycle.__aexit__`.
  3. Read the archive back via
     `tpcore.ingestion.csv_archive_backends.select_backend().read_latest('<source>')`,
     gzip-decompress, csv-parse.
  4. Group by ticker, per-symbol upsert via `cache.upsert_payload`.

## Related

- `[[project_railway_archive_substrate_migration]]` — predates this;
  the design substrate (R3) is what landed in R2.
- `[[project_railway_hobby_tier]]` — predates the 2026-05-26
  cutover; Railway is now active, not paused.
