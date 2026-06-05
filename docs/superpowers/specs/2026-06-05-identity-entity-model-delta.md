# Identity entity-model delta — v1.5 (supersedes the single-`ticker_classifications` framing in 2026-06-04-data-layer-rebuild-design.md)

> **Status:** design-of-record. Revises the identity sections (§3, §8.2 phasing) of the operator-approved rebuild spec v1.4. Authored from the `db-architect` expert verdict (2026-06-05) + operator directives ("use the csv files", "stocks in a different table than ETFs and all this other shit", "the main ticker JSON file is wrong — update it", "ask an expert how to do that").

## Why this delta exists

Two stacked defects the v1.4 single-table model did not address:

1. **The schema conflates entity types.** `ticker_classifications` mixes operating SEC-filing common equity (stock/reit) with ETFs, funds, SPACs, ADRs, and dot-suffix instrument variants (`.U` units, `.WS`/`.W` warrants, `.PR*` preferreds, `.A`/`.B` classes). The SEC issuer machinery (CIK → issuer, FPFD, `lifetime_start = FPFD`, SCD-2) is meaningful ONLY for operating issuers. Forcing 13,840 non-issuer rows through it produced a **73% sentinel `lifetime_start = '1900-01-01'` rot** (the A6 violation the rebuild exists to kill).

2. **The ticker→CIK map is survivorship-biased.** Identity used SEC `company_tickers.json` (`tpcore/sec/ticker_cik_map.py:39`), which lists **current filers only** (~10,365). Every delisted company drops off it, so **5,158 of 5,207 no-CIK "stocks" are delisted** — their CIK was never joined. The CIKs still exist in the full `submissions.zip` (971,748 CIK folders incl. delisted); `company_tickers.json` is the "wrong file" and must be replaced as the identity authority.

## Hard constraint (unchanged, dispositive)

`classification_id` (the TKR-14 14-char smart-key) is the FK from `prices_daily` (~21M bars) AND `fundamentals_quarterly`. It is **preserved verbatim for every snapshot row — never re-minted.** This rules out per-type physical tables (a polymorphic FK to "one of N tables" is a Postgres anti-pattern; nullable-multi-FK + CHECK quadruples the trigger/index surface on 21M rows). See "Rejected" below.

## Decision 1 — universal `securities` spine + issuer satellite conditional on instrument type

- **`ticker_classifications` is THE universal securities spine** (keep the physical name — renaming to `securities` churns 14 triggers, the gate SQL, and ~30 consumers for no behavior gain; defer the rename to a future arc). Every instrument of every type has exactly one row; `prices_daily`/`fundamentals_quarterly` FK to it unchanged. No bar is ever orphaned.
- **The SEC issuer machinery is a satellite** (`issuers` ← `issuer_securities` M:N SCD-2 ← spine; `issuer_history` SCD-2) that exists **only for stock/reit rows**. Its absence for non-stocks is legitimate, not sentineled.
- **Future per-type satellites** (e.g. `etf_attributes(classification_id PK FK, …)`) are keyed by `classification_id` and bolt onto the spine. This is "one entity = one table" done at the **satellite/attribute grain** (where types genuinely differ) while the **price-join surface stays unified** (the one thing all instruments share is having a price series).
- **No new tables.** Everything lands in existing tables; non-stock instruments stay in the spine, `asset_class`-flagged (`20260530_0100` discriminator), `issuer_securities`-unlinked. Deferred non-stock work (ETFs etc.) does NOT block this rebuild.

## Decision 2 — split key is `asset_class` (instrument type); CIK is a nullable attribute, never an axis

- "Has a CIK" is a **recovery state**, not an identity. A delisted no-CIK stock is the same entity type as AAPL. Splitting on CIK would scatter identical entities across buckets.
- Awkward cells resolved: **CIK-backed ETFs (2,632)** keep `cik` as a provenance attribute but stay OUT of the stock entity (so they don't get an FPFD-anchored lifetime). **Delisted no-CIK stocks (5,207)** ARE stocks — recover their CIK (Decision 3), residual stays `cik=NULL`.
- Stock-entity queries filter `asset_class IN ('stock','reit')`; stock-only joins go through `issuer_securities` (only stock/reit rows populate it).

## Decision 3 — survivorship-free ticker→CIK reverse index (corrects the "wrong" company_tickers.json)

- **New `scripts/ops.py --stage build_cik_reverse_index`** (heavy-lane; adjacent to the identity build): stream `submissions.zip` (971K folders) → emit a CSV of `(ticker_upper, cik, valid_from, valid_to, source)` from each folder's `tickers[]` (current/last-known) + `formerNames[]`/historical ticker assignments (windowed). CSV-first (invariant C1/C2). This is the survivorship-free replacement for `company_tickers.json` as the CIK-recovery authority.
- **Match discipline (identity-path rule):** match on `(ticker, date-window)` per the half-open SCD-2 predicate — NOT bare ticker. A delisted-then-reused ticker must map to the CIK that held it during that security's `ticker_history` window, else cross-entity contamination (the same failure the `as_of` reader fix cures).

## Decision 4 — CIK recovery phase (new §8.2 phase 4.5)

Runs **between universe ingest and `issuers_build`**: backfill `ticker_classifications.cik` (`cik_source='sec_submissions_reverse'`) for no-CIK stock/reit rows using the reverse index. `issuers_build` (which walks `cik NOT NULL` rows — `tpcore/identity/issuers_build.py:16-21`) then sees the ~5K recovered CIKs and mints their issuer rows + real FPFDs. Chunked UPDATE (~5K rows, single chunk, no WAL risk). Reversible/re-runnable.

## Decision 5 — `lifetime_start` per instrument type (kills the sentinel at root)

| spine row | `cik` | `first_public_filing_date` | `lifetime_start` (NOT NULL) |
|---|---|---|---|
| stock/reit, CIK recovered | CIK | SEC FPFD (earliest `filingDate`) | = FPFD |
| stock/reit, CIK unrecoverable | NULL | **NULL** | `min(ticker_history.valid_from)` → else `min(prices_daily.date)` |
| etf/fund/spac/adr/derivative | CIK-or-NULL (attr) | NULL | earliest `ticker_history.valid_from` / first price bar date |

**Never `1900-01-01`.** Gate probe `sentinel_lifetime_start` stays 0 (now genuinely). Probe `lifetime_start_before_fpfd` auto-passes for NULL-FPFD rows.

## Decision 6 — CSV-first seed from the pre-wipe snapshot

The snapshot CSVs at `data/rebuild_2026-06-04/ticker_graph_snapshot/` are the bulk seed (per invariant C1 + the spec's "un-re-pullable series copied verbatim" carve-out): `ticker_classifications.csv` (19,004) → spine; `ticker_history.csv` (19,013); `issuers.csv` (3,601); `issuer_securities.csv` (89); `issuer_history.csv` (8,794). `classification_id`/`id` loaded verbatim. Recovery + `lifetime_start` resolution only ADD attributes to existing rows; they never change `id`. This preserves the 21M `prices_daily` FKs through the cutover. (The FMP active leg is redundant — snapshot is current; the FMP delisted leg is unusable — Starter tier caps `/stable/delisted-companies` at page 0 / 100 rows, page 1+ = HTTP 402.)

## Decision 7 — gate change (the one validator edit this requires)

`tpcore/identity/identity_gate.py` probes `cik_classifications_without_issuer` + `cik_classifications_without_issuer_securities` get an **`asset_class IN ('stock','reit')` guard** in their WHERE clause — else CIK-backed ETFs (2,632) report as false orphans and block the gate. (`validator_or_gate_change` per CIC; grounded in the identity chain: only operating issuers require an `issuers`/`issuer_securities` link.)

## Unaffected

- The 14 BEFORE-INSERT `classification_id`-assignment triggers (operate on the spine, instrument-type-agnostic; half-open fix `20260604_0100` already shipped).
- `IdentityDispatcher` / `corp_history` readers (the `as_of` half-open fix is independent).
- `issuers_build.py` (unchanged — just sees more CIK rows after recovery).

## Rejected — per-type physical tables each owning `classification_id`

Breaks the 21M-bar FK (polymorphic FK unsupported without losing RI; multi-nullable-FK + CHECK quadruples the trigger/index surface and forces rewriting all 14 triggers; no-FK defeats the rebuild). Re-orphans non-stock bars the moment non-stock tables are deferred. The shared-spine + conditional-satellite design delivers the operator's intent (stocks cleanly separable + their own issuer machinery; non-stocks cleanly out) without the polymorphic-FK tax.

## Execution order

1. `build_cik_reverse_index` stage (the corrected ticker→CIK map) — **build first** (highest leverage; operator-requested).
2. CSV-first snapshot seed (spine + issuer graph, classification_id verbatim).
3. CIK recovery backfill (phase 4.5).
4. `issuers_build` (now sees recovered CIKs) + FPFD computation.
5. `lifetime_start` per-type resolution.
6. Gate probe guard (Decision 7) + run the 10-probe identity gate (BLOCKING, Phase-1.4).
7. Child loads (prices/fundamentals — Phase 2+).
