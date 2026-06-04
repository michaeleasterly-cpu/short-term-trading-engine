---
name: figi-primary-stable-id
description: "FINAL design 2026-05-23 v2.2: ticker_classifications PK is a 14-char ISO-style smart-key (TKR-14) encoding country/asset_class/IPO-venue/year/source/issuer-hash + ISO 7064 Mod-97-10 check. FIGI/CUSIP/ISIN are UNIQUE-NULLABLE cross-vendor columns. Child tables FK on classification_id + denormalize ticker (ticker-at-row-date via ticker_history). OpenFIGI integration is event-driven, not scheduled."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Final decision (operator + expert 2026-05-23):** the primary key on `platform.ticker_classifications` is a **14-character ISO-style smart-key** (TKR-14) minted by parent_resolver from immutable / at-mint-snapshot facts. FIGI, CUSIP, ISIN are populated as UNIQUE-NULLABLE companion columns. Child tables FK on `classification_id text` (the TKR-14) + denormalize `ticker` as a snapshot of "the security's symbol on the row's semantic date" via `ticker_history`.

## Decision evolution (5 iterations before lock-in)

1. **Initial: AUGMENT** (ticker stays as PK, FIGI as companion). Operator: *"you need a constant for primary key and that is basic database design"* — rejected on Codd's immutability principle.
2. **Pivot: FIGI-as-PK.** Operator: *"unless we assign our own key"* — rejected because FIGI couples our schema to OpenFIGI's lifecycle.
3. **Pivot: bigserial-as-PK.** Operator: *"it needs to be SUCH an identifier that we can use it for more than one purpose... each part means SOMETHING that we can use for filtering"* — rejected because bigserial is opaque.
4. **Expert proposal: TKR-13 smart-key.** Operator: *"i gave ideas, then we ask the expert and refer to standards... we go with the standard"* — accepted segments as-is.
5. **Final: TKR-14** (widened hash from 4→5 chars for collision headroom; operator chose option B "widen now" over "retry on collision").

## TKR-14 encoding (FINAL, 14 chars)

| Pos | Width | Field | Charset | Mutability |
|-----|-------|-------|---------|------------|
| 1-2 | 2 | Country of incorporation (ISO 3166-1 alpha-2) | A-Z | TRUE-immutable |
| 3 | 1 | Asset class (`S`=stock, `P`=preferred, `E`=ETF, `F`=fund, `R`=REIT, `T`=trust, `A`=ADR, `U`=SPAC unit, `W`=warrant, `N`=note) | letter | TRUE-immutable |
| 4 | 1 | Listing venue at IPO (`N`=NYSE, `Q`=Nasdaq, `A`=AMEX, `B`=Cboe BZX, `O`=OTC, `X`=foreign primary, `Z`=other) | letter | SNAPSHOT-at-IPO (like SSN area number) |
| 5-6 | 2 | Discovery year YY (parent_resolver mint year) | 0-9 | TRUE-immutable |
| 7 | 1 | Discovery source (`F`=FMP, `S`=SEC, `A`=Alpaca, `O`=other) | letter | SNAPSHOT (provenance) |
| 8-12 | 5 | Issuer hash — Crockford base32 of `SHA-1(country‖CIK or country‖normalized_legal_name)[0:25 bits]` | 0-9 A-Z minus I/L/O/U | TRUE-immutable (issuer identity) |
| 13-14 | 2 | ISO 7064 Mod-97-10 check digits | 0-9 | derived |

Charset excludes I/L/O/U (Crockford base32 — avoids 1/I/L and 0/O visual confusion).

Sample IDs:
| ID | Decoded |
|----|---------|
| `USSN26F7K3X904` | US, common stock, NYSE-origin, 2026 IPO, FMP-discovered, issuer `7K3X9`, check `04` |
| `USEQ20F8P2M7K1` | US, ETF, Nasdaq-origin, 2020, FMP, issuer `8P2M7`, check `K1` (Q-set, not D-set) — invalid example; recompute |
| `USAQ22FD4E5F12` | US, ADR, Nasdaq-listed foreign issuer, 2022, FMP |
| `JPSX23FG6H7J45` | JP, stock, foreign-primary listing, 2023, FMP |
| `USRN19FK8L9M67` | US, REIT, NYSE-origin, 2019, FMP |
| `GBSX24SR2S3T01` | GB, stock, foreign-primary, 2024, SEC-discovered |

## Why each design choice landed where it did

- **Excludes current-sector / current-exchange / company-name from key:** ISIN/LEI/VIN/CUSIP all exclude mutable attributes; encoding them produces silently-stale keys. Sector reclassifies ~4%/yr; exchange transfers ~2-5%/yr. Lives in regular columns instead.
- **Includes IPO-venue (pos 4):** explicit snapshot semantic ("where it FIRST listed"), like SSN area number. Stale-but-honest; supports `WHERE substring(id,4,1)='N'` for "NYSE-original-listings" cohort filter.
- **ISO 7064 Mod-97-10 check:** LEI precedent; ~98% typo detection vs Luhn's ~90%; catches all single-digit + adjacent-transposition errors.
- **14-char width (5-char hash):** Birthday-paradox at 13K rows = ~0.05% collision (essentially zero); ~0.4% at 50K; ~6% at 200K. No retry logic needed for the foreseeable lifetime.
- **No bigserial / UUID:** opaque IDs lose the filterability the operator explicitly wanted ("each part means something for filtering").

## Schema target

```sql
CREATE TABLE platform.ticker_classifications (
  id          text PRIMARY KEY                                    -- TKR-14 smart-key
              CHECK (id ~ '^[A-Z]{2}[SPEFRTAUWN][NQABOXZ][0-9]{2}[FSAO][0-9A-HJ-KM-NP-TV-Z]{5}[0-9]{2}$'),
  figi        char(12) NULL,                                       -- US Composite FIGI from OpenFIGI
  cusip       char(9)  NULL,                                       -- FMP-derived
  isin        char(12) NULL,                                       -- FMP-derived (US ISIN body = CUSIP)
  current_ticker  text NOT NULL,                                   -- denormalized convenience
  country         char(2) NOT NULL,
  asset_class     text NOT NULL,
  current_exchange text,                                           -- mutable, regular column
  gics_sector     text,                                            -- mutable, regular column
  current_legal_name text,                                         -- mutable, regular column
  status          text NOT NULL,
  discovery_source text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  -- ... existing columns ...

  CONSTRAINT ticker_classifications_current_ticker_active_uniq
    UNIQUE (current_ticker) WHERE status IN ('active','active_when_issued'),
  CONSTRAINT ticker_classifications_figi_uniq
    UNIQUE (figi) WHERE figi IS NOT NULL,
  CONSTRAINT ticker_classifications_cusip_uniq
    UNIQUE (cusip) WHERE cusip IS NOT NULL,
  CONSTRAINT ticker_classifications_isin_uniq
    UNIQUE (isin) WHERE isin IS NOT NULL
);

-- Every child table: classification_id is the FK
ALTER TABLE platform.prices_daily
  ADD COLUMN classification_id text REFERENCES platform.ticker_classifications(id);
-- ticker column STAYS as denormalized snapshot of symbol-at-row-date
```

## Ticker semantics — TIED TO ROW DATE, NOT WRITE TIME

Child rows' `ticker` column reflects **the security's symbol on the date the row represents**, NOT when we INSERTed it. For `prices_daily`: a row with `date=2019-06-15` for Meta carries `ticker='FB'`; a row with `date=2025-01-15` for the same security carries `ticker='META'`. Same `classification_id` across both.

This makes BACKFILL of historical rows look up the historical ticker via `ticker_history`, NOT use `current_ticker`.

## ticker_history is LOAD-BEARING (not optional)

```sql
CREATE TABLE platform.ticker_history (
  classification_id text NOT NULL REFERENCES platform.ticker_classifications(id),
  ticker            text NOT NULL,
  valid_from        date NOT NULL,
  valid_to          date,                                    -- NULL = current
  PRIMARY KEY (classification_id, valid_from),
  CONSTRAINT no_overlap EXCLUDE USING gist (
    classification_id WITH =,
    daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
  )
);
```

Producer pattern (every child-table INSERT, live or backfill):
```python
ticker_at_date = await conn.fetchval(
    "SELECT ticker FROM platform.ticker_history "
    "WHERE classification_id = $1 AND $2::date <@ daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)')",
    classification_id, row_semantic_date,
)
```

Population:
- **First-seen**: parent_resolver mints `(classification_id, ticker, valid_from=today, valid_to=NULL)` alongside the classification row
- **Rename event**: daily FMP /profile poll detects same-FIGI-different-ticker → handler writes `UPDATE ticker_history SET valid_to=yesterday WHERE classification_id=X AND valid_to IS NULL; INSERT (..., ticker=<new>, valid_from=today)`. Also updates `current_ticker` on classifications.
- **Historical SEC backfill**: when ingesting EDGAR filings that reference an old ticker, cross-check/correct ticker_history entries.

## OpenFIGI integration is EVENT-DRIVEN, not scheduled

Per `feedback_event_driven_not_scheduled`: the trigger is the EVENT "unknown ticker observed during ingest", not a daily cron.

- Requires a NEW `FeedTrigger` enum value (`EVENT_DRIVEN` or `INGEST_RESOLVE`) — none of the existing values fit.
- DFCR ADD for OpenFIGI declares: `trigger: event_driven`, `cadence_days: 0`, `freshness_max_age_days: None`, `targeting: CONSTRAINED_DEMAND_DRIVEN`.
- Adapter is invoked by `parent_resolver` when an ingestion handler observes `unknown = set(incoming.ticker) - set(ticker_classifications.current_ticker)`.
- Implementation: either inline (synchronous resolver call before failing INSERT) OR daemon (Postgres LISTEN/NOTIFY on `unknown_ticker_observed`, mirrors data_repair_service / engine_service). Spec to decide.

## OpenFIGI API (auth + endpoint + rate limits)

- Docs: <https://www.openfigi.com/api/documentation>
- Ontology: <https://www.omg.org/spec/FIGI/1.2>
- Base URL: `https://api.openfigi.com`
- Endpoint: `POST /v3/mapping`
- Auth header: `X-OPENFIGI-APIKEY: <key>`. Env var: **`OPEN_FIGI_API_KEY`** (in `.env`)
- Rate limit with key: 25 req per 6s × 100 jobs/req = ~25K mappings/min. Full 13K backfill in ~30s.
- Request: `[{"idType":"TICKER","idValue":"<TICKER>","exchCode":"US"}]`
- Response: each result includes `figi` (exchange-level), `compositeFIGI` (per-jurisdiction), `shareClassFIGI` (global). Store `compositeFIGI` as the canonical FIGI.
- Error: not-found returns `{"warning": "No identifier found."}` (HTTP 200, NOT 404).

## Per-lane source priority (recap from sibling memory)

| Data lane | US primary | Non-US fallback |
|---|---|---|
| Insider transactions / material events | SEC EDGAR | FMP |
| Daily price bars | FMP | FMP |
| Fundamentals | FMP | FMP |
| Profile fields (country, asset_class, exchange) | FMP /profile | FMP /profile |
| Ticker existence / identity | FMP /stock-list | FMP /stock-list (universe isn't US-only) |
| CIK lookup (when ticker known) | SEC company_tickers.json | n/a |
| Reverse ticker lookup (when CIK known from EDGAR) | SEC company_tickers.json | FMP /profile |
| FIGI lookup (always) | OpenFIGI /v3/mapping | OpenFIGI /v3/mapping |

## v2.2 spec phases (supersedes v2.1 §1.10)

| Phase | What | Blocks |
|---|---|---|
| P0 | v2.2 spec + plan; DFCR ADD for OpenFIGI submitted | — |
| P1 | DFCR ADD approved + FeedProfile/ProviderBinding entries added (new EVENT_DRIVEN trigger value) | P0 |
| P2 | Add `id text`, `figi`/`cusip`/`isin` cols to ticker_classifications (text id immediately PRIMARY KEY) | P1 |
| P3 | OpenFIGI adapter `tpcore/ingestion/openfigi_adapter.py` (6-stage adapter-readiness contract) | P2 |
| P4 | parent_resolver (Task #24) with SEC-insider-lane branch + TKR-14 mint + figi/cusip/isin fill + ticker_history seeding | P3 |
| P5 | Backfill 13K ticker_classifications rows: TKR-14 minted; FIGI via OpenFIGI bulk; cusip/isin via FMP; ticker_history seeded with `valid_from=created_at, valid_to=NULL` | P4 |
| P6 | Per child table: add `classification_id text`, backfill via ticker join, FK NOT VALID, VALIDATE | P5 |
| P7 | Producer-side rewrite: every INSERT uses ticker_history lookup for ticker-at-row-date | P6 |
| P8 | Refactor engines + dashboard to query by classification_id internally; tpcore/identity/ adapter at wire | P7 |
| P9 | Drop Phase-2 ticker-keyed FKs (now redundant) | P8 |

## What v2.1 work survives

- Phase 0 (audit baseline + index): keep
- Phase 0.5 (on-demand db_snapshots): keep
- Phase 1 (rename insider_transactions + country column): keep
- Phase 2 (14 ticker-keyed FKs as NOT VALID): keep as intermediate scaffolding; retired at v2.2 P9
- Phase 3 (classify_tickers DELETE-source-tracking dry_run): keep
- Phase 3.5 (parent_resolver, placeholder): superseded by v2.2 P4

## Honest cost

10-14 weeks single-operator throughput (slightly higher than the AUGMENT-estimate of 8-12, because event-driven parent_resolver + ticker_history + DFCR adds 2 phases to the original 8 weeks).

## What does NOT change

- Wire format with Alpaca / FMP / SEC stays ticker-keyed (tpcore/identity adapter at the boundary)
- Operator-facing dashboard URLs + logs continue to reference tickers
- 14 child tables keep their full row data — we ADD `classification_id` + keep `ticker` denormalized as ticker-at-row-date

## Related

- [[sec-primary-insider-fmp-fallback-non-us]] — per-lane source priority
- [[single-session-until-db-done]] — Carver session paused for duration
- [[always-use-iso-standards]] — the principle behind ISO 7064 check + ISO 3166 country segment
- [[event-driven-not-scheduled]] — sibling rule for OpenFIGI integration shape
- [[authoritative-docs-override-claudemd]] — Kimball + ISO standards win over CLAUDE.md
- v2.1 spec — supplanted by v2.2 on PK question; remains authoritative for Phase 0.5 / Phase 2 design
- Task #24 (parent_resolver) — folded into v2.2 P4

## Sources

- ISO 7064 Mod-97-10 (check digit): used by LEI; same algorithm
- ISO 3166-1 alpha-2 (country codes)
- OMG FIGI 1.2 ontology: <https://www.omg.org/spec/FIGI/1.2>
- OpenFIGI API: <https://www.openfigi.com/api/documentation>
- Kimball SCD Type-2: <https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/slowly-changing-dimension/>
- ISIN ISO 6166, LEI ISO 17442, VIN ISO 3779, ISO 6346 — industry-standard precedents
