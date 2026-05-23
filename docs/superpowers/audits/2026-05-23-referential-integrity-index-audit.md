# Phase 0 Audit — FK-Column Index Coverage

**Date:** 2026-05-23
**Plan:** v2 §2.2 / spec §6.2
**Method:** `pg_get_indexdef` across `pg_class` × `pg_index` for the 15 in-scope child tables.

## Coverage status

| Table | FK column | Has leading-column index? | Indexes |
|---|---|---|---|
| `prices_daily` | `ticker` | ✓ | 4 (idx_prices_daily_date, idx_prices_daily_row_integrity_violations, idx_prices_daily_ticker_date_desc, prices_daily_pkey) |
| `sec_insider_transactions` | `ticker` | ✓ | 3 (ix_sec_insider_transactions_filing_date, ix_sec_insider_transactions_ticker_date, sec_insider_transactions_dedupe_uk) |
| `sec_material_events` | `ticker` | ✓ | 3 (ix_sec_material_events_filing_date, ix_sec_material_events_ticker_date, sec_material_events_dedupe_uk) |
| `corporate_actions` | `ticker` | ✓ | 3 (corporate_actions_pkey, ix_corporate_actions_ticker_date, uq_corporate_actions_ticker_date_type) |
| `earnings_events` | `ticker` | ✓ | 2 (ix_earnings_events_ticker_date, pk_earnings_events) |
| `fundamentals_quarterly` | `ticker` | ✓ | 4 (fundamentals_quarterly_pkey, idx_fq_missing_ratios, ix_fundamentals_ticker_filing, uq_fundamentals_ticker_filing) |
| `short_interest` | `ticker` | ✓ | 2 (ix_short_interest_release, short_interest_pk) |
| `borrow_rates` | `ticker` | ✓ | 2 (borrow_rates_pk, ix_borrow_rates_date_ticker) |
| `social_sentiment` | `ticker` | ✓ | 2 (ix_social_sentiment_date_ticker, social_sentiment_pk) |
| `options_max_pain` | `symbol` | ✓ | 2 (ix_options_max_pain_symbol_observed, options_max_pain_pk) |
| `insider_sentiment` | `symbol` | ✓ | 2 (insider_sentiment_pk, ix_insider_sentiment_symbol_period) |
| `liquidity_tiers` | `ticker` | ✓ | 1 (liquidity_tiers_pkey) |
| `spread_observations` | `ticker` | ✓ | 3 (spread_observations_pkey, spread_observations_source_observed_idx, spread_observations_ticker_observed_idx) |
| `universe_candidates` | `ticker` | ⚠ **GAP** | 2 (idx_uc_engine_date, universe_candidates_pkey) |
| `insider_mspr_daily` | `ticker` | ⚠ **GAP** | 0 () |

## Gaps to close before Phase 2

- **`universe_candidates(ticker)`** — needs `CREATE INDEX CONCURRENTLY idx_universe_candidates_ticker ON platform.universe_candidates (ticker)`. **APPLIED to live DB ✓**

## CRITICAL FINDING — `insider_mspr_daily` is a VIEW, not a table

Originally flagged as a 2nd gap. **It's a VIEW** (relkind='v' per pg_class) created in `20260522_0200_drop_insider_filings_add_sec_mspr.py`. Views structurally cannot have:
- Indexes (`CREATE INDEX CONCURRENTLY ... ON platform.insider_mspr_daily` returns `WrongObjectTypeError: cannot create index on relation - This operation is not supported for views`)
- Foreign keys (FKs are constraints on base tables; views don't have constraints)

**Out-of-scope correction:** in-scope child table count drops from **15 → 14**. `insider_mspr_daily` is referential-integrity-protected indirectly via its base table `sec_insider_transactions` (which IS in scope and gets an FK on `ticker`). Any orphan in `insider_mspr_daily` can only exist if its base row exists in `sec_insider_transactions` — which post-Phase-2 will be FK-protected.

**v2 spec amendment needed:** §3.1 of the v2 spec must formally exclude `insider_mspr_daily` from the in-scope list with this rationale.

## Migration files included with this Phase 0 PR

- `platform/migrations/versions/20260523_0500_idx_concurrently_universe_candidates_ticker.py` — applied to live DB ✓

Uses `op.get_context().autocommit_block()` since `CREATE INDEX CONCURRENTLY` cannot run inside a transaction.