# Referential integrity design — v2.2 (TKR-14 smart-key primary keys)

**Status:** v2.2. **Supersedes v2.1 (`2026-05-23-referential-integrity-design-v2.1.md`) on the primary-key / parent_resolver / cross-vendor-identity questions.** Where v2.2 and v2.1 conflict on PK type, ticker semantics, OpenFIGI integration shape, or the per-handler resolution path, **v2.2 wins**. The Phase 0 / 0.5 / 1 / 2 / 3 work merged under v2.1 stays landed; v2.2 adds on top without unwinding any merged migration. Phase 3.5 (parent_resolver placeholder in v2.1 §1.10) is fully superseded by v2.2's TKR-14 + event-driven OpenFIGI design.

**Author / role:** in-thread session 87291947 with `general-purpose` subagent expert verdicts (structured-key encoding, identifier-as-PK question). Operator-driven design iteration over five corrections. Final lock-in 2026-05-23.

**ISO standards anchor (operator standing rule):** ISO 7064 Mod-97-10 (check digit, LEI precedent), ISO 3166-1 alpha-2 (country segment), implicit Crockford base32 (issuer-hash charset). Per `feedback_always_use_iso_standards.md` — standards FIRST, custom only with documented rationale.

## 1. The design corrections v2.2 makes to v2.1

### 1.1 PK type — text smart-key, not ticker, not bigserial, not FIGI

**Operator constraint (Codd's relational model):** PK must be immutable. Ticker is mutable (FB→META rebrand 2021, ticker-reuse-after-delist), so ticker cannot be the PK. v2.1 §1.10 implied keeping ticker as the natural PK with FK propagation via `ON UPDATE CASCADE` — that mechanically works but violates the principle and destroys historical truth on rename.

**v2.2 decision:** the PK on `platform.ticker_classifications` is **`id text PRIMARY KEY`**, populated with a 14-character ISO-style smart-key (**TKR-14**) minted by `parent_resolver` from immutable / at-mint-snapshot facts.

Rejected alternatives:
- **FIGI-as-PK** — couples our schema to OpenFIGI's lifecycle decisions (OMG ontology does not axiomatize FIGI non-reuse; OpenFIGI is a free Bloomberg-funded service with no SLA guarantee).
- **bigserial / UUID-as-PK** — opaque; loses the filterability the operator explicitly wanted ("each part means something for filtering"). Repo precedent of `gen_random_uuid()` is for event-log tables (`application_log`, `open_orders`, `aar_deferred`), a different tier.

### 1.2 TKR-14 encoding (full per-position spec)

| Pos | Width | Field | Charset | Mutability |
|-----|-------|-------|---------|------------|
| 1-2 | 2 | Country of incorporation (ISO 3166-1 alpha-2) | A-Z | TRUE-immutable |
| 3 | 1 | Asset class (`S`=stock, `P`=preferred, `E`=ETF, `F`=fund, `R`=REIT, `T`=trust, `A`=ADR, `U`=SPAC unit, `W`=warrant, `N`=note) | letter | TRUE-immutable |
| 4 | 1 | Listing venue at IPO (`N`=NYSE, `Q`=Nasdaq, `A`=AMEX, `B`=Cboe BZX, `O`=OTC, `X`=foreign primary, `Z`=other) | letter | SNAPSHOT-at-IPO (like SSN area number) |
| 5-6 | 2 | Discovery year YY (parent_resolver mint year) | 0-9 | TRUE-immutable |
| 7 | 1 | Discovery source (`F`=FMP, `S`=SEC, `A`=Alpaca, `O`=other) | letter | SNAPSHOT (provenance) |
| 8-12 | 5 | Issuer hash — Crockford base32 of `SHA-1(country‖CIK or country‖normalized_legal_name)[0:25 bits]` | 0-9 A-Z minus I/L/O/U | TRUE-immutable (issuer identity) |
| 13-14 | 2 | ISO 7064 Mod-97-10 check digits | 0-9 | derived |

**Total: 14 chars fixed-width.** Charset for issuer-hash excludes I/L/O/U (Crockford base32 — avoids 1/I/L and 0/O visual confusion).

Postgres regex CHECK constraint:
```sql
id text PRIMARY KEY
   CHECK (id ~ '^[A-Z]{2}[SPEFRTAUWN][NQABOXZ][0-9]{2}[FSAO][0-9A-HJ-KM-NP-TV-Z]{5}[0-9]{2}$')
```

Sample IDs:
| ID | Decoded |
|----|---------|
| `USSN26F7K3X904` | US, common stock, NYSE-origin, 2026 IPO, FMP-discovered, issuer `7K3X9`, check `04` |
| `USAQ22FD4E5F12` | US, ADR, Nasdaq-listed foreign issuer, 2022, FMP, issuer `D4E5F`, check `12` |
| `JPSX23FG6H7J45` | JP, common stock, foreign-primary listing, 2023, FMP, issuer `G6H7J`, check `45` |
| `USRN19FK8L9M67` | US, REIT, NYSE-origin, 2019, FMP, issuer `K8L9M`, check `67` |
| `USUN21FA1B2C33` | US, SPAC unit, NYSE-origin, 2021, FMP, issuer `A1B2C`, check `33` |
| `GBSX24SR2S3T01` | GB, stock, foreign-primary, 2024, SEC-discovered, issuer `R2S3T`, check `01` |

### 1.3 Why each segment is in (and why mutable attributes are NOT)

**Included because TRUE-immutable per security:**
- Country of incorporation — re-domicile in practice creates a new security with a new CUSIP
- Asset class — a SPAC converting to common stock gets a new CUSIP; we mint a new TKR-14
- Discovery year YY — the year parent_resolver first minted the row; never changes
- Issuer hash — derived from CIK (primary) or normalized legal name (fallback); the issuer identity is the most stable attribute

**Included because SNAPSHOT-at-mint with honest semantic:**
- Listing venue at IPO (pos 4) — explicit "where it FIRST listed", analogous to SSN's area number (state-of-issuance, immutable even if you move). Supports `WHERE substring(id,4,1)='N'` for the "NYSE-original-listings" cohort filter; the staleness is honest.
- Discovery source (pos 7) — which feed first told us about this security; never changes. Provenance audit value.

**Excluded because mutable with no honest snapshot semantic:**
- **Current exchange** — NYSE↔Nasdaq transfers happen ~2-5%/year. Encoding "current exchange" produces silently-stale keys that mislead `LIKE` filters forever. Lives in `current_exchange` column (mutable, indexed).
- **Sector (GICS)** — reclassifications happen ~3-5%/year. No clean "sector at IPO" semantic worth encoding. Lives in `gics_sector` column.
- **Company name** — M&A absorptions, rebrands. Covered by issuer-hash via CIK; not encoded as a name string.
- **Current ticker** — the entire reason ticker isn't the PK. Lives in `current_ticker` column with effective-dating via `ticker_history` (§1.6).
- **Status (active / delisted / etc.)** — mutable lifecycle state.

### 1.4 ISO 7064 Mod-97-10 check digit algorithm

Same algorithm as LEI (ISO 17442). Stronger than CUSIP's Luhn variant (~98% typo detection vs ~90%; catches all single-digit + adjacent-transposition errors).

Generation:
1. Take the 12-char prefix (positions 1-12).
2. Remap A-Z → 10-35 (after I/L/O/U exclusion in issuer-hash segment, the remaining 22 letters still remap to a sequential range).
3. Append `"00"` to the prefix.
4. Convert the resulting string to a single decimal integer N.
5. `check = 98 - (N mod 97)`, zero-padded to 2 digits.
6. Validate: the full 14-char ID, interpreted as an integer mod 97, equals 1.

### 1.5 Issuer hash — 5-char Crockford base32 (25 bits)

`SHA-1(country‖CIK or country‖normalized_legal_name)[0:25 bits]` → 5 chars of Crockford base32 (0-9 A-Z minus I/L/O/U).

- **25 bits = ~33M unique values.**
- Birthday-paradox at 13K rows = ~0.05% collision probability (essentially zero).
- At 50K rows = ~0.4%.
- At 200K rows = ~6%.

No collision-retry logic required at the foreseeable lifetime of this system (universe is single-broker US-focused; growth ~5K/year). If we ever cross ~200K rows, widen issuer-hash to 6 chars (30 bits) and bump total width to 15.

Generation algorithm (Python pseudocode):
```python
def mint_tkr14(
    country: str,            # ISO 3166-1 alpha-2; "US"
    asset_class: AssetClass, # AssetClass.STOCK → "S"
    ipo_venue: Venue,        # Venue.NYSE → "N"
    discovery_source: Source,# Source.FMP → "F"
    cik: str | None,         # "0000320193" preferred
    legal_name: str,         # fallback: "APPLE INC"
    now: datetime,           # UTC
) -> str:
    yy = f"{now.year % 100:02d}"
    issuer_seed = f"{country}|{cik}" if cik else f"{country}|{normalize(legal_name)}"
    h = hashlib.sha1(issuer_seed.encode()).digest()
    issuer_int = int.from_bytes(h[:4], "big") >> (32 - 25)  # top 25 bits
    issuer_hash = crockford_base32(issuer_int, width=5)
    prefix = f"{country}{asset_class.value}{ipo_venue.value}{yy}{discovery_source.value}{issuer_hash}"
    assert len(prefix) == 12
    check = iso_7064_mod_97_10(prefix)
    return f"{prefix}{check}"
```

### 1.6 Ticker semantics — TIED TO ROW DATE, NOT WRITE TIME

**Operator constraint (clarification 2026-05-23):** *"not at the time the row was written but at the time of the row itself... so facebook would show whatever that ticker was before meta then meta pops up after the change"*.

Child rows' `ticker` column reflects **the security's symbol on the date the row represents**, NOT when we INSERTed it. For `prices_daily`:
- Row with `date=2019-06-15` for Meta: `ticker='FB'` (that was the symbol on that date)
- Row with `date=2021-10-27` (day before rebrand): `ticker='FB'`
- Row with `date=2021-10-28` (rebrand day): `ticker='META'`
- Row with `date=2025-01-15`: `ticker='META'`

All four rows have the SAME `classification_id`. The security identity is stable across the rebrand; the ticker reflects each row's date semantic.

**Implication for BACKFILL:** historical INSERTs must look up the historical ticker, NOT use `current_ticker`. The naive pattern (write `current_ticker` from `ticker_classifications`) is WRONG for backfill.

### 1.7 `ticker_history` is LOAD-BEARING (not optional)

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
    "WHERE classification_id = $1 "
    "  AND $2::date <@ daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)')",
    classification_id, row_semantic_date,
)
```

Population:
- **First-seen:** parent_resolver mints `(classification_id, ticker, valid_from=today, valid_to=NULL)` alongside the classification row.
- **Rename event:** daily FMP `/profile` poll detects same-FIGI-different-ticker → handler writes `UPDATE ticker_history SET valid_to=yesterday WHERE classification_id=X AND valid_to IS NULL; INSERT (..., ticker=<new>, valid_from=today)`. Also updates `current_ticker` on classifications.
- **Historical SEC backfill:** when ingesting EDGAR filings that reference an old ticker, cross-check/correct `ticker_history` entries.

### 1.8 OpenFIGI integration is EVENT-DRIVEN, not scheduled

Per `feedback_event_driven_not_scheduled` + operator standing rule: data feeds dispatch on EVENTS, not schedules. OpenFIGI's trigger is the EVENT "unknown ticker observed during ingest", NOT a daily cron.

- Mechanically: when an ingestion handler tries to INSERT a row for ticker T and T isn't in `ticker_classifications.current_ticker` (active rows), an `UNKNOWN_TICKER_OBSERVED` event fires. `parent_resolver` listens (Postgres LISTEN/NOTIFY or inline-sync) and does the resolution chain (§1.10).
- This requires a NEW `FeedTrigger` enum value: `EVENT_DRIVEN` (or `INGEST_RESOLVE`). None of the existing values (`MARKET_CLOSE / VENDOR_BIMONTHLY / VENDOR_WEEKLY / VENDOR_RELEASE / VENDOR_QUARTERLY / CONTINUOUS / INTRADAY / RECOMPUTE / DERIVED`) fit cleanly.

### 1.9 OpenFIGI API details (anchor for the adapter)

- Docs: <https://www.openfigi.com/api/documentation>
- OMG FIGI 1.2 ontology: <https://www.omg.org/spec/FIGI/1.2>
- Base URL: `https://api.openfigi.com`
- Endpoint: `POST /v3/mapping`
- Auth header: `X-OPENFIGI-APIKEY: <key>`. Env var: `OPEN_FIGI_API_KEY` (already in operator's `.env`).
- Rate limit with key: 25 requests per 6 seconds × 100 jobs per request = ~25K mappings/min. Full 13K backfill in ~30s.
- Request shape for US ticker → FIGI:
  ```json
  [{"idType":"TICKER","idValue":"<TICKER>","exchCode":"US"}]
  ```
- Response: each result includes `figi` (exchange-level), `compositeFIGI` (per-jurisdiction), `shareClassFIGI` (global across countries). We store **`compositeFIGI`** as the canonical FIGI (per-jurisdiction; for ADRs identifies the ADR specifically, not the foreign underlying; stable across US exchange transfers).
- Error: not-found returns `{"warning": "No identifier found."}` at HTTP 200 (NOT a 404).
- Other errors: HTTP 429 with `ratelimit-reset` header (sleep + retry); HTTP 400 invalid; HTTP 500/503 retry-with-backoff.
- FIGI structure regex (from OMG ontology, applied as adapter-side sanity check before storing):
  ```
  ^(((?!BS|BM|GG|GB|VG|GH|KY)[BCDFGHJKLMNPQRSTVWXZ]{2})G[BCDFGHJKLMNPQRSTVWXYZ\d]{8}\d)$
  ```

### 1.10 Per-handler-lane resolution dispatch

`parent_resolver.resolve(unknown_ticker, calling_handler)` branches by which handler called it:

**Insider/material-events handlers (data source = SEC EDGAR):**
1. We already have CIK from the EDGAR record. Reverse-lookup ticker via SEC `company_tickers.json` (single call, free, US-only by definition — fine because EDGAR is US-only).
2. If miss (rare — a CIK without a tradeable ticker, e.g., insider filer): FMP `/stable/profile` fallback for foreign-issuer cases.
3. Then FMP `/stable/profile` for country/asset_class/exchange enrichment (regardless of how we got the ticker).
4. Then OpenFIGI `/v3/mapping` for `compositeFIGI`.

**Prices / fundamentals / profile handlers (data source = FMP):**
1. FMP `/stable/profile/{ticker}` for ticker, country, asset_class, exchange, CUSIP, ISIN, CIK (if available).
2. OpenFIGI `/v3/mapping` for `compositeFIGI`.

**Pin-at-first-resolve discipline:** never overwrite an existing non-null FIGI/CUSIP/ISIN. Divergence on later resolve writes `event_type='IDENTITY_DIVERGENCE_INVESTIGATE'` to `application_log` — never silent update.

### 1.11 Per-lane source priority (recap of sibling `sec-primary-insider-fmp-fallback-non-us`)

| Data lane | US primary | Non-US fallback | Source |
|---|---|---|---|
| Insider transactions / material events | **SEC EDGAR** | FMP | EDGAR is regulatory truth |
| Daily price bars | **FMP** | FMP | CTA consolidated tape; SEC doesn't carry prices |
| Fundamentals | **FMP** | FMP | FMP parses SEC XBRL |
| Profile fields (country, asset_class, exchange) | **FMP** `/profile` | FMP `/profile` | SEC's company_tickers.json doesn't carry these |
| Ticker existence / general identity | **FMP** `/stock-list` | FMP `/stock-list` | Universe isn't US-only |
| CIK lookup (when ticker known) | **SEC** `company_tickers.json` | n/a | SEC is canonical for US ticker↔CIK |
| Reverse ticker lookup (when CIK known) | **SEC** `company_tickers.json` | FMP `/profile` | Already have CIK from EDGAR; SEC reverse is cheapest |
| FIGI lookup | **OpenFIGI** `/v3/mapping` | OpenFIGI `/v3/mapping` | The only source for FIGI |

## 2. Schema target

```sql
-- ticker_classifications gets TKR-14 as PK + cross-vendor companion columns
CREATE TABLE platform.ticker_classifications (
  id          text PRIMARY KEY
              CHECK (id ~ '^[A-Z]{2}[SPEFRTAUWN][NQABOXZ][0-9]{2}[FSAO][0-9A-HJ-KM-NP-TV-Z]{5}[0-9]{2}$'),

  -- cross-vendor identifiers (UNIQUE-NULLABLE; populated by parent_resolver)
  figi             char(12) NULL,
  cusip            char(9)  NULL,
  isin             char(12) NULL,

  -- operator-facing + mutable attributes (regular columns, not in key)
  current_ticker        text NOT NULL,
  current_exchange      text,
  current_legal_name    text,
  gics_sector           text,
  status                text NOT NULL,

  -- immutable / at-mint metadata (mirrors key but as columns for joinless access)
  country               char(2) NOT NULL,
  asset_class           text NOT NULL,
  ipo_venue             text,
  discovery_source      text NOT NULL,
  cik                   text,                                  -- US issuers; NULL otherwise

  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ticker_classifications_current_ticker_active_uniq
    UNIQUE (current_ticker) WHERE status IN ('active','active_when_issued'),
  CONSTRAINT ticker_classifications_figi_uniq
    UNIQUE (figi) WHERE figi IS NOT NULL,
  CONSTRAINT ticker_classifications_cusip_uniq
    UNIQUE (cusip) WHERE cusip IS NOT NULL,
  CONSTRAINT ticker_classifications_isin_uniq
    UNIQUE (isin) WHERE isin IS NOT NULL,
  CONSTRAINT ticker_classifications_cik_uniq
    UNIQUE (cik) WHERE cik IS NOT NULL
);

-- Expression indexes for the filter patterns the TKR-14 enables
CREATE INDEX ix_tc_country     ON platform.ticker_classifications ((substring(id, 1, 2)));
CREATE INDEX ix_tc_asset_class ON platform.ticker_classifications ((substring(id, 3, 1)));
CREATE INDEX ix_tc_ipo_venue   ON platform.ticker_classifications ((substring(id, 4, 1)));
CREATE INDEX ix_tc_discovery_yy ON platform.ticker_classifications ((substring(id, 5, 2)));
CREATE INDEX ix_tc_discovery_src ON platform.ticker_classifications ((substring(id, 7, 1)));

-- ticker_history: load-bearing for ticker-at-row-date lookup
CREATE TABLE platform.ticker_history (
  classification_id text NOT NULL REFERENCES platform.ticker_classifications(id),
  ticker            text NOT NULL,
  valid_from        date NOT NULL,
  valid_to          date,
  PRIMARY KEY (classification_id, valid_from),
  CONSTRAINT no_overlap EXCLUDE USING gist (
    classification_id WITH =,
    daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
  )
);

CREATE INDEX ix_th_ticker_active
  ON platform.ticker_history (ticker)
  WHERE valid_to IS NULL;

-- Every child table: classification_id is the FK; ticker stays as snapshot
ALTER TABLE platform.prices_daily
  ADD COLUMN classification_id text REFERENCES platform.ticker_classifications(id);
-- (repeat for the 13 other FK-protected tables; full list in §3.6 P6 below)
```

## 3. Phased implementation plan

| Phase | What | Blocks |
|---|---|---|
| P0 | This spec (v2.2) + plan; DFCR ADD for OpenFIGI submitted | — |
| P1 | DFCR ADD approved + FeedProfile/ProviderBinding entries added; new `FeedTrigger.EVENT_DRIVEN` enum value lands; `OPEN_FIGI_API_KEY` already in `.env` (operator-confirmed 2026-05-23) | P0 |
| P2 | Add `id text`, `figi`, `cusip`, `isin` columns to `ticker_classifications` (nullable initially); CHECK constraint on `id` regex; expression indexes for filter patterns | P1 |
| P3 | OpenFIGI adapter at `tpcore/ingestion/openfigi_adapter.py` (6-stage adapter-readiness contract: ingest / test / validate / dashboard / schedule / self-heal — but `schedule` slot is replaced by event-driven trigger) | P2 |
| P4 | `parent_resolver` at `tpcore/ingestion/parent_resolver.py` with: per-handler-lane dispatch (§1.10), TKR-14 mint via `tpcore/identity/tkr14.py` generator, figi/cusip/isin fill, `ticker_history` first-seen seeding | P3 |
| P5 | Backfill 13K `ticker_classifications` rows: TKR-14 minted for every existing row (one-shot script `scripts/backfill_tkr14.py`); FIGI via OpenFIGI bulk; cusip/isin via FMP `/profile`; `ticker_history` seeded with `(classification_id, current_ticker, valid_from=created_at, valid_to=NULL)`. NOT NULL/UNIQUE constraints flipped on after backfill verifies green | P4 |
| P6 | Per child table: add `classification_id text` column, backfill via `classification_id = (SELECT id FROM ticker_classifications WHERE current_ticker = child.ticker AND status IN ('active','active_when_issued'))`, FK NOT VALID, VALIDATE. Tables: prices_daily, insider_transactions, sec_material_events, corporate_actions, earnings_events, fundamentals_quarterly, short_interest, borrow_rates, social_sentiment, options_max_pain, insider_sentiment, liquidity_tiers, spread_observations, universe_candidates, application_log, data_quality_log | P5 |
| P7 | Producer-side rewrite: every child-table INSERT looks up `ticker_history` for ticker-at-row-date; `parent_resolver` invoked on unknown-ticker event before any failing INSERT | P6 |
| P8 | Refactor engines + dashboard to query by `classification_id` internally; `tpcore/identity/` adapter layer for `ticker_to_classification_id` + `classification_id_to_ticker` translation at every wire boundary (Alpaca, FMP API, SEC, Streamlit) | P7 |
| P9 | Drop the 14 Phase-2 ticker-keyed NOT-VALID FKs (now redundant — Phase 6's `classification_id` FKs are the real ones) | P8 |

## 4. What v2.1 work survives unchanged

- **Phase 0** (PR #317) — audit baseline + `universe_candidates(ticker)` index: keep
- **Phase 0.5** (PR #322) — on-demand `db_snapshots/` (v2.1 corrected to on-demand only): keep
- **Phase 0.6** — DROPPED, stays dropped (Supabase Pro daily backups + 7-day PITR cover this)
- **Phase 1** (PR #318) — rename `sec_insider_transactions` → `insider_transactions` + `country` column + CSV cleanup: keep
- **Phase 2** (PR #319) — 14 ticker-keyed FKs as NOT VALID + drop `sec_insider_transactions` compat view: keep as intermediate scaffolding; retired at v2.2 P9
- **Phase 3** (PR #320) — `classify_tickers` DELETE-source-tracking + `⊆ prices_daily` filter (dry_run=True default): keep; live-DELETE re-design happens after v2.2 P7 when FK targets are `classification_id`
- **Phase 3.5** (v2.1 §1.10 placeholder) — SUPERSEDED by v2.2 P4 (the full parent_resolver design with per-lane dispatch + TKR-14)

## 5. New components

- **`tpcore/identity/`** — new module
  - `tkr14.py` — TKR-14 generator (mint, validate, decode-segments)
  - `dispatcher.py` — `ticker_to_classification_id(ticker, as_of_date=None)` + `classification_id_to_ticker(id, as_of_date=None)` adapter layer at wire boundary; cached in-process with TTL; cache invalidated on parent_resolver writes
- **`tpcore/ingestion/openfigi_adapter.py`** — OpenFIGI client (6-stage adapter-readiness contract; `OPEN_FIGI_API_KEY` env)
- **`tpcore/ingestion/parent_resolver.py`** — the event-driven resolver with per-handler-lane dispatch
- **`scripts/backfill_tkr14.py`** — one-shot backfill of 13K existing classifications rows (P5)
- **`tpcore/feeds/profile.py`** — new `FeedTrigger.EVENT_DRIVEN` enum value (DFCR-gated edit per `.claude/rules/data-feed-roster.md`)

## 6. Filter examples (what the TKR-14 enables)

```sql
-- All US securities
WHERE id LIKE 'US%'

-- All stocks (common + preferred) anywhere
WHERE substring(id, 3, 1) IN ('S', 'P')

-- All ETFs
WHERE substring(id, 3, 1) = 'E'

-- All NYSE-original-listings (regardless of current venue)
WHERE substring(id, 4, 1) = 'N'

-- All US REITs
WHERE id LIKE 'USR%'

-- All securities discovered via FMP (provenance audit)
WHERE substring(id, 7, 1) = 'F'

-- All securities discovered in 2026
WHERE substring(id, 5, 2) = '26'

-- All US-listed ADRs
WHERE substring(id, 3, 1) = 'A'

-- All foreign-domiciled securities (non-US)
WHERE id NOT LIKE 'US%'
```

For "current" attributes (sector, exchange, name) — use the regular indexed columns:
```sql
WHERE gics_sector = 'Financials'
WHERE current_exchange = 'XNYS'
WHERE current_legal_name ILIKE 'apple%'
```

## 7. DFCR ADD request for OpenFIGI (Phase P0 deliverable)

To be filed per `docs/superpowers/checklists/data_feed_change_request.md`:

```
DATA FEED CHANGE REQUEST
operation:   ADD
feed:        openfigi
kind:        external
provider:    openfigi
adapter:     tpcore.ingestion.openfigi_adapter
need:        Resolves ticker → US Composite FIGI for cross-vendor identity reconciliation on ticker_classifications. Required by v2.2 P4 (parent_resolver) and P5 (one-shot 13K backfill).
cadence:     event_driven — invoked by parent_resolver when an ingestion handler observes an unknown ticker. NOT a scheduled feed. Requires new FeedTrigger.EVENT_DRIVEN enum value.
```

System routes through ONBOARD (`adapter_readiness.md` 6-stage contract). Operator approves the prepared diff (y/n). No hand-edits to `_BINDINGS` / `FEED_PROFILES`.

## 8. Cost estimate

**10-14 weeks single-operator throughput.** Higher than v2.1's remaining-work estimate because v2.2 adds:
- TKR-14 generator + validator + adapter layer (~1 week)
- `tpcore/identity/` module with ticker↔classification_id translation (~1 week)
- `ticker_history` schema + producer-side rewrite for ticker-at-row-date (~2 weeks)
- DFCR for OpenFIGI + event-driven trigger plumbing (~1 week)
- Per-child-table `classification_id` column + backfill + FK NOT VALID + VALIDATE for 14 tables (~3-4 weeks; prices_daily is the slow one)
- Engine + dashboard refactor to use `classification_id` internally (~2-3 weeks)

Per `feedback_single_session_until_db_done`: this is the sole work track until Phase 9 verification is green. No Carver-session work in parallel.

## 9. Concern map — extended from v2.1

| Concern | v2.2 coverage |
|---|---|
| 1. Schema changes | New PK type on ticker_classifications; new ticker_history table; classification_id FK on 14 child tables. Covered §2. |
| 2. Producer changes | parent_resolver event-driven dispatch + ticker_history lookup on every INSERT. Covered §1.10 + §3 P7. |
| 3. Consumer changes | tpcore/identity adapter layer at wire boundaries; engines + dashboard refactored to classification_id-internal. Covered §3 P8. |
| 4. Migration safety | All FKs NOT VALID first, VALIDATE under raised `SET LOCAL statement_timeout='30min'` (no Supabase dashboard change per operator 2026-05-23 directive); prices_daily VALIDATE is the longest-running statement at ~5-15min on 21M rows. |
| 5. Data quality | Pin-at-first-resolve discipline (§1.10) prevents silent overwrites; IDENTITY_DIVERGENCE_INVESTIGATE event for vendor anomalies. |
| 6. Rollback / snapshot | v2.1 Phase 0.5 db_snapshots covers; one-shot snapshot before each P6 child-table migration. |
| 7. Backup / DR | Unchanged — Supabase Pro daily + 7-day PITR. |
| 8. Test coverage | Per phase: ruff + targeted pytest + integration test against live OpenFIGI sandbox. parent_resolver gets dedicated test for each lane dispatch branch. |
| 9. Ongoing operations | Event-driven trigger means parent_resolver fires only on unknown-ticker; no new cron. Daily FMP `/profile` poll watches for rename events (already a scheduled job). |
| 10. Documentation | This spec; updates to `docs/DATABASE_AND_DATAFLOW.md` § ticker_classifications + ticker_history; runbook at `docs/runbooks/tkr14-mint-and-backfill.md`. |
| 11. Cross-table change ordering | P5 (ticker_classifications backfill) before P6 (any child table); P6 light tables first (universe_candidates 1 → short_interest 3 → ...) before prices_daily (335,159 orphans). |
| 12. Operator manual actions | OpenFIGI API key already in `.env` (confirmed). No Supabase dashboard changes (use `SET LOCAL statement_timeout` instead per 2026-05-23 directive). |
| 13. Event-driven discipline | OpenFIGI is event-driven, not scheduled (§1.8). Mirrors data_repair_service / engine_service daemon pattern per `feedback_event_driven_not_scheduled`. |
| 14. Cross-vendor reconciliation | figi/cusip/isin UNIQUE-NULLABLE on ticker_classifications enable join-on-FIGI for cross-vendor diffs; ticker_history enables ticker-at-row-date for historical reconciliation. |
| 15. Standards anchoring | ISO 7064 Mod-97-10 check (LEI precedent); ISO 3166-1 alpha-2 country; Crockford base32 charset. Per `feedback_always_use_iso_standards`. |
| 16. Industry-PK precedent | TKR-14 follows ISIN (country prefix) + LEI (Mod-97-10 check) + VIN (snapshot-at-issuance) + CUSIP (charset exclusions) precedents. |

## 10. References

- v2.1 spec — `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.1.md` (supplanted on §1.10; surviving phases listed in §4)
- OMG FIGI 1.2 ontology — <https://www.omg.org/spec/FIGI/1.2>
- OpenFIGI API docs — <https://www.openfigi.com/api/documentation>
- ISIN ISO 6166 — country-prefix-then-issuer-then-check precedent
- LEI ISO 17442 — source of the Mod-97-10 check-digit algorithm
- VIN ISO 3779 — source of the "snapshot-at-issuance" segment philosophy
- CUSIP — charset-exclusion guidance
- Kimball SCD Type-2 — surrogate-key pattern for slowly-changing dimensions
- DFCR checklist — `docs/superpowers/checklists/data_feed_change_request.md`
- Adapter-readiness 6-stage contract — `docs/superpowers/checklists/adapter_readiness.md`
- Memory: `project_figi_primary_stable_id` (canonical decision record), `feedback_always_use_iso_standards`, `feedback_sec_authoritative_fmp_fallback_non_us`, `feedback_event_driven_not_scheduled`, `project_single_session_until_db_done`
