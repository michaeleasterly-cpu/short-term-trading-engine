# Phase 0 Audit — Referential Integrity Orphan Baseline

**Date:** 2026-05-23
**Plan:** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan-v2.md` Phase 0
**Method:** MVCC-safe `WHERE NOT EXISTS (SELECT 1 FROM ticker_classifications p WHERE p.ticker = c.<fk>)` per v2 spec §11.

## Per-table orphan counts

| Table | FK column | Total rows | Distinct tickers | Orphan rows | Orphan tickers |
|---|---|---:|---:|---:|---:|
| `prices_daily` | `ticker` | 21,331,836 | 7,895 | 335,159 | 166 |
| `sec_insider_transactions` | `ticker` | 647,163 | 1,306 | 0 | 0 |
| `sec_material_events` | `ticker` | 237,767 | 1,319 | 0 | 0 |
| `corporate_actions` | `ticker` | 111,726 | 3,993 | 1,506 | 69 |
| `earnings_events` | `ticker` | 35,074 | 1,449 | 12 | 1 |
| `fundamentals_quarterly` | `ticker` | 178,902 | 5,988 | 135 | 8 |
| `short_interest` | `ticker` | 4,553 | 1,498 | 3 | 1 |
| `borrow_rates` | `ticker` | 33 | 13 | 0 | 0 |
| `social_sentiment` | `ticker` | 1,355 | 533 | 0 | 0 |
| `options_max_pain` | `symbol` | 1 | 1 | 0 | 0 |
| `insider_sentiment` | `symbol` | 520 | 57 | 0 | 0 |
| `liquidity_tiers` | `ticker` | 7,692 | 7,692 | 8 | 8 |
| `spread_observations` | `ticker` | 31,900 | 7,677 | 33 | 8 |
| `universe_candidates` | `ticker` | 4,592 | 2,738 | 1 | 1 |
| `insider_mspr_daily` | `ticker` | 130,043 | 1,306 | 0 | 0 |
| **TOTAL** | | | | **336,857** | |

## Cleanup ordering for Phase 4 (smallest orphan count first per v2 plan)

**Zero-orphan tables (7):** can VALIDATE immediately after Phase 2 NOT VALID lands:
- `sec_insider_transactions`
- `sec_material_events`
- `borrow_rates`
- `social_sentiment`
- `options_max_pain`
- `insider_sentiment`
- `insider_mspr_daily`

**Tables needing cleanup-then-VALIDATE (8), in ascending orphan count:**
- `universe_candidates` — 1 orphan rows (1 distinct tickers)
- `short_interest` — 3 orphan rows (1 distinct tickers)
- `liquidity_tiers` — 8 orphan rows (8 distinct tickers)
- `earnings_events` — 12 orphan rows (1 distinct tickers)
- `spread_observations` — 33 orphan rows (8 distinct tickers)
- `fundamentals_quarterly` — 135 orphan rows (8 distinct tickers)
- `corporate_actions` — 1,506 orphan rows (69 distinct tickers)
- `prices_daily` — 335,159 orphan rows (166 distinct tickers)

## Key risk callout

**`prices_daily` carries 335,159 orphans = 99.5% of all orphans across the schema.** Its cleanup-then-VALIDATE will be the heaviest single PR in Phase 4. The orphan tickers (166 distinct) are likely delisted-and-then-removed-from-classifier (the same defect the classify_tickers DELETE-source-tracking gap is creating).

## Cross-check vs auditheal

TODO Phase 0.1: compare these counts against any rows in `platform.data_quality_log` under `cross_table_audit.<table>.orphan_no_prices` (PR #167 / `tpcore/auditheal`). Discrepancies indicate stale audit rows or different predicate — investigate before Phase 4 ordering.