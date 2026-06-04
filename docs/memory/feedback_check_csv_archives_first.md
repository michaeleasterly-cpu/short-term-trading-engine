---
name: check-csv-archives-first
description: "Operator 2026-05-23: before writing any data-pull script, ALWAYS check the CSV archives in data/<source>_*_archive/ first. The archives ARE the rebuild substrate per the R3 substrate design — they're the first-stop check for 'do we have this already'."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule (operator 2026-05-23):** *"i see now... just checking... always look at the csv repo first"*. Before designing a new vendor-pull stage/script for ANY data, inspect the `data/<source>_*_archive/` directories to see if the data is already on disk in a prior download.

**Why:** the CSV archives ARE the rebuild substrate (per R3 substrate design + `docs/runbooks/options-data-turn-on.md`). They mirror every successful vendor ingest. If the data is in there, you DON'T need to re-pull from the vendor — you can reload from CSV, which is faster, idempotent, and doesn't burn API budget.

**Pattern when working on a data task:**

1. **First**: `ls -d data/<source>_*_archive/` — what archives exist?
2. **Inspect headers**: `gunzip -c data/<source>_<feed>_archive/latest.csv.gz | head -1 | tr ',' '\n'` — does the column shape have what you need?
3. **If YES**: design a load-from-CSV path. No vendor API calls.
4. **If NO**: only THEN design a vendor pull.

**Today's example (2026-05-23):** I wrote `scripts/backfill_country_from_fmp.py` to populate the new `country` column via per-ticker FMP `/stable/profile` (12+ minutes wall-time, ~13,775 API calls). Operator asked: "why are you doing a backfill when the data was downloaded?". I'd skipped step 1. On inspection:
- `fmp_fundamentals_archive` cols: `ticker,filing_date,period_end_date,period_label,net_income,fcf,...,recorded_at` — **no country**
- `fmp_earnings_events_archive` cols: `ticker,event_date,event_type,magnitude_pct,source,recorded_at` — **no country**
- `fmp_catalyst_events_archive` cols: same shape — **no country**
- No `fmp_profile_archive/` exists — profile data was never archived

In this case the per-ticker pull was unavoidable (FMP `/stable/profile-bulk` is Premium-only on $49/mo+). But the proper sequence would have been: check first → discover no profile archive → then write the pull script. Skipping step 1 looked like wasted-vendor-call risk to the operator.

**Anti-pattern to avoid:** designing a vendor pull because "I don't remember if we have this" — always CHECK before designing.

## Related

- [[adapter-readiness]] skill — 6-stage data-adapter contract; archive-first pattern is baked into stage 1
- [[fmp_primary_daily_bars_2026_05_22]] — FMP Starter tier constraints (no bulk profile)
- [[database_architecture_state_2026_05_23]] — Tier 1 raw / Tier 2 derived; archives are the rebuild substrate
