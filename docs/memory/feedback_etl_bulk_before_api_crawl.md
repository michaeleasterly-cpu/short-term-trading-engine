---
name: etl-bulk-before-api-crawl
description: "For any historical/backfill load, find the provider's BULK FILE download first (extract-then-load ETL); never default to a per-entity API crawl"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

For any historical / bulk / backfill ingest: **check whether the
provider publishes a bulk file (full dataset download) BEFORE building
or running a per-entity API crawl.** Real ETL = Extract (download raw
to durable disk) → Transform/validate → Load. Stream-fetch-and-load
with a cosmetic post-hoc CSV is a shortcut, not ETL.

**Why:** 2026-05-16 SEC backfill. I built/ran a per-ticker EDGAR
submissions+Form-4-XML crawl: ~30h, never finished, 50/1501 tickers,
and I reported it "healthy/progressing." The operator had to say it
repeatedly ("ETL — download then ingest", "you fucking idiot", "did
you download the data first?"). SEC publishes the entire market's
insider history as ~33 quarterly Form-345 zips (~336 MB) → bulk ETL
finished in **2.5 min**, 84.1% coverage. A 30h non-starter vs 2.5 min,
purely from choosing the wrong mechanism.

**How to apply:**
- Before any backfill: spend a few minutes verifying if a bulk
  dataset/file dump exists (provider "data sets" / "bulk data" /
  "full index" pages). Only crawl per-entity if there is genuinely no
  bulk file (then verify that claim with evidence — not vendor-blame).
- Bulk-file ETL and per-entity API ingest are *different pipelines*.
  Don't jam a backfill through the incremental adapter.
- Extract must be durable + resumable: download raw to disk; a valid
  artifact already on disk is NOT re-downloaded; transform/load reads
  from disk so a re-run replays offline and is idempotent.
- The cheap per-entity API path stays the daily/weekly incremental;
  the bulk path is the historical bootstrap. Keep them separate.
- See [[feedback_no_shortcuts_100_pct]] and
  [[feedback_operating_identity_for_this_system]] (don't be a Connor:
  don't rubber-stamp your own approach as "good to go" — verify with
  numbers, own the bad approach fast).
