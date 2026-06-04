---
name: feedback-bulk-before-api-crawl-reinforced
description: "SECOND time I shipped a per-row API crawl when a bulk file existed (2026-05-24 EDGAR formerNames). Operator caught it after 4-hour ETA. The check \"is there a bulk dump?\" must run BEFORE writing ANY per-entity loop, not after the slow loop fails."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

I have written `feedback_etl_bulk_before_api_crawl` (SEC 30h crawl → 2.5min
bulk ETL) and then violated it in the same session by shipping the
`corp_history_edgar_backfill` stage as a per-CIK serial walk
(6,735 × ~1.4 sec = ~4 hours). Operator killed it at 11% completion:
"learn from your mistakes... dont you have the edgar data in csv".

**Specifically for SEC EDGAR** the bulk dataset is:

  - `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
    (1.54 GB, refreshed daily; contains every CIK's submissions JSON
    file)
  - `https://www.sec.gov/files/company_tickers.json` (current
    ticker↔CIK)
  - `https://www.sec.gov/Archives/edgar/full-index/master.idx`
    (quarterly filing index)

For the bulk path the typical perf profile is:
  - First-run download: ~30s (1.5 GB on a decent connection)
  - Cached re-run: 0s
  - Parse: ~5 sec
  - Bulk INSERT via `asyncpg.executemany`: ~10 sec for ~10K rows
  - Total: ~45s first run, ~15s cached

**How to apply (BEFORE writing the loop, not after):**

  1. For ANY backfill that walks a per-entity HTTP endpoint:
     **STOP and look for a bulk dump from the same vendor**.
  2. Vendor bulk-data conventions:
     - SEC: `Archives/edgar/.../bulkdata/` and `full-index/`.
     - FMP: `/api/v3/historical-price-full/<ticker>` per ticker, but
       bulk EOD via `/api/v3/historical-price-full/...?from=&to=` or
       the `/stable/historical-price-eod/full` endpoint.
     - Alpaca: `bars` endpoint with batched symbols.
     - FRED: per-series JSON; bulk via `fred-md` quarterly CSV.
  3. If no bulk file exists, document WHY in the stage docstring
     before shipping the per-row loop.
  4. The 4-hour ETA from the per-row pattern is the cue to KILL the
     run and check for bulk, not to wait it out.

**Sibling memory:** [[feedback_etl_bulk_before_api_crawl]] (the
original lesson), [[feedback_apply_my_own_documented_constraints]]
(same-session repeat-failure rule — this entry is the second-strike
on that meta-rule too).
