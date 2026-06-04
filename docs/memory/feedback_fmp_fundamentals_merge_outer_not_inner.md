---
name: fmp-fundamentals-merge-outer-not-inner
description: "FMP fundamentals adapter `_merge` is OUTER-join (UNION) on period_end_date as of PR #381 (2026-05-26), not the prior INNER-join. Partial-period rows with NULL balance-sheet fields are intentional — common for pre-IPO disclosed quarters where FMP has income+cash but not balance. All financial columns on platform.fundamentals_quarterly are nullable; downstream consumers (pb/de ratio computation, forensics) already NULL-guard."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

## What changed

`tpcore/fmp/fundamentals_adapter.py::_merge` (PR #381, 2026-05-26):

  - **Before**: strict INNER JOIN of income / cash-flow / balance-sheet on
    period `date`. Periods missing in ANY endpoint were dropped.
  - **After**: OUTER JOIN (UNION) — any period present in ANY endpoint
    produces a row, with NULL on the missing-endpoint fields.

## Why

The INNER join silently dropped legitimate periods. Detected via
`fundamentals_quarterly_completeness` check 2026-05-26: ABCL had
income + cash for Q2/Q3 2019 but no balance-sheet → 2 historical
"gaps" the check flagged. FMP's balance-sheet endpoint is genuinely
sparser for recent IPOs and pre-IPO disclosed quarters.

The platform.fundamentals_quarterly schema has ALL financial columns
nullable. Downstream consumers (`tpcore/fundamentals/earnings_quality.py`,
`tpcore/fundamentals/cache.py::compute_ratios`) already NULL-guard on
total_assets / total_liabilities / shares_outstanding when computing
P/B + D/E ratios. The OUTER-join is safer for completeness without
new downstream risk.

## How to apply

  - **Treat NULL balance-sheet fields as a feature, not a bug.** They
    mean "FMP didn't have this for this period; income/cash data
    present is still useful for trend math."
  - **Backfills that need PRE-OUTER-merge tickers** (rows ingested
    before PR #381 are INNER-merged and missing the partials): re-run
    the canonical fundamentals refresh stage; new rows will use OUTER.
  - **If FMP balance is missing AND we need full balance**: SEC EDGAR
    is the authoritative source (10-Q filings). Out of scope for the
    fundamentals_quarterly_completeness check; engineering follow-up.

## Related

- [[project_r2_archive_substrate_2026_05_26]] — manifest_lifecycle
  has a separate LocalFS-only bug that prevents canonical Phase 2
  upserts on R2; workaround pattern documented there.
- [[feedback_no_lazy_vendor_blame]] — investigate vendor data
  asymmetry (income vs cash vs balance coverage) before generalizing.
