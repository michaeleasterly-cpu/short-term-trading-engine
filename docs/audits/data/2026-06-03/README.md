# 2026-06-03 identity substrate audit — empirical receipts

Raw query outputs and reproduction scripts behind the audit doc at
`docs/audits/2026-06-03-identity-substrate-data-flow.md`.

## File map

| File | What it contains |
|---|---|
| `step1_schema_inventory.json` | Full platform-schema snapshot — 49 tables, 22 FKs, 72 CHECK constraints, 154 indexes, 17 triggers, all columns by table |
| `step2_identity_master.json` | `ticker_classifications` + `ticker_history` + `issuer_securities` + `issuers` coverage and multi-row counts |
| `step3_ticker_trace.json` | Per-ticker deep trace for the 10 worst-offender cohort (SBET, GLXY, COLAU, SUNC, ARDT, LZ, TRAW, FA, VIVK, SUNE) and 5 multi-classification examples (EAGL, HCACU, HCAC, ABP, ACT) |
| `step4_prices_daily.json` | `prices_daily` attribution integrity — pre-FPFD counts, ticker_history window mismatches, top-50 polluted tickers |
| `step5_fundamentals.json` | `fundamentals_quarterly` defect counts — pre-FPFD, orphans, duplicates, per-filer-form breakdown |
| `step6_lifecycle.json` | `ticker_lifecycle_events`, `corporate_events`, `corporate_actions`, `issuer_history` counts and breakdowns |
| `step_ext.json` | Audit-extension batch — universe construction, identifier conflicts, foreign-issuer + asset_class breakdown |
| `stepN_*.py` | The Python query scripts that produced each `stepN_*.json`. Re-runnable against the live DB |

## Reproducibility

Each `step*.py` is a self-contained asyncpg script. Run with:

```
DATABASE_URL="$DATABASE_URL_IPV4" \
  .venv/bin/python docs/audits/data/2026-06-03/stepN_*.py
```

Re-running against the live DB will produce a fresh JSON. Differences
from the captured 2026-06-03 baselines indicate state drift.

## Status of the captured baseline

The numbers in these JSONs are the **2026-06-03 frozen audit baseline**.
A 34-case sentinel test at
`tests/test_identity_substrate_audit_documented.py` pins the
load-bearing facts so the audit doc cannot silently drift.
