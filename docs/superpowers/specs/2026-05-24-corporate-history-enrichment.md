# Corporate-history enrichment epic — design spec v0.2 (expert-reviewed)

**Status:** v0.2 — initial scope sketch authored 2026-05-24 in operator
session 87291947, post-v2.2-refint and post-Path-A-backfill. v0.2
incorporates expert review (schema corrected from 2 tables to 3 per the
GOOG/GOOGL share-class argument; taxonomy widened; bitemporal applied
to corporate_events; first-deliverable scope tightened). Awaiting
operator alignment on the 5 open decisions in §9 before P1 build.

**Provenance:** Promoted from the deferred "Corporate-history enrichment
epic" follow-on (TODO.md L902, logged 2026-05-24 09:30 PT) by operator
directive 2026-05-24 ("ALSO start the new full corporate-history
enrichment, the right scope is much bigger than per-ticker CIK lookup —
you'd want acquirer relationships, post-IPO renames, corporate-actions
graph").

## 1. Problem

The platform stores a flat security identity in `ticker_classifications`
(one row per security) + a flat-over-time `ticker_history` (SCD-2 ticker
renames per security). Both treat each security as **isolated** from its
corporate history. Real corporate identity is a **graph** over time:

- **Mergers**: Splunk (CIK 1353283) absorbed into Cisco (CIK 858877) on
  2024-03-18. After merger, SPLK ticker dies; Cisco's CSCO ticker
  continues. Historical SPLK bars should still resolve to "Splunk Inc"
  as the issuer-at-bar-date, but a query "show me everything Cisco has
  ever owned" should walk the merger graph and surface SPLK.
- **Spinoffs**: Warner Bros Discovery (WBD) created 2022-04-08 from
  AT&T's WarnerMedia spinoff merged with Discovery (DISCA → WBD CIK
  preserved). DISCA bars 2010-2022 should resolve to "Discovery
  Communications"; post-2022 bars to "Warner Bros Discovery".
- **Take-privates**: Twitter (TWTR, CIK 1418091) acquired by Musk
  2022-10-27 → no longer public. CIK retired. The "current parent" is
  Musk's X Corp (private; no SEC CIK).
- **Bankruptcies + post-Chapter-11 shells**: Bed Bath & Beyond (BBBY
  CIK 886158) filed Ch 11 2023-04, post-BK shell traded as BBBYQ until
  liquidation. Shares went to zero; IP sold to Overstock (which renamed
  itself "Beyond Inc"). The acquirer relationship "BBBY → Beyond Inc"
  is on the IP-asset graph, not the equity-share graph.
- **Ticker rotation / re-IPOs**: same legal entity can recycle a ticker
  symbol (e.g., a SPAC merges + the resulting company picks a new ticker
  while the SPAC's old ticker gets reused months later by another
  company). `ticker_history` handles this at the per-security level; the
  GRAPH level is separate.

**What this epic adds:** a corporate-events graph that records the
relationships above + makes them queryable, so analysts (and future
engines) can answer:
- "Walk the merger graph forward from CIK X to find every successor"
- "Walk it backward from ticker T at date D to find every predecessor"
- "Show every corporate action affecting issuer X between dates A and B"
- "Resolve a historical ticker to the issuer-at-date even when the
  issuer no longer exists"

## 2. Use cases (who needs this)

This is NOT engine-critical work — engines filter on `delisted=false AND
asset_class='stock' AND tier <= 2` and rarely care about acquirer
relationships. The primary consumers are:

1. **Backtest / research queries** that span M&A events. "Compare TWTR
   pre-Musk vs X post-Musk" needs a successor link to resolve correctly.
2. **AAR / forensics**: "an engine held SPLK; the trade closed on
   2024-03-18 (Cisco-merger effective date) at $157 — what happened?"
   The corporate-events graph attributes the close to the merger, not
   to a normal liquidation.
3. **Risk attribution**: an engine's portfolio of "Cisco-related" names
   could include historical SPLK exposure if a merger walk is wired in.
4. **Operator dashboard**: "show all corporate events affecting positions
   I held in the last 90 days" — currently impossible without manual
   reconciliation.
5. **Cross-ticker reconciliation for engine training**: when training on
   historical data that spans rename / merger events, the engine sees
   the same issuer under two ticker symbols if the graph isn't wired.
6. **Path-A FK closure for the 79 unresolvable tickers** (see
   `TODO.md` "Corporate-history enrichment epic" L902): these are
   exactly the issuers in terminal corporate states that a corp-events
   graph would describe.

## 3. Data model (3 tables, expert-reviewed v0.2)

The substrate is **three new tables** layered on top of the existing
`ticker_classifications` + `ticker_history` substrate (which stays).
The v0.1 sketch had 2 tables; expert correction added `issuer_securities`
as the issuer↔security M:N mapping because **a single issuer can have
multiple ticker_classifications rows for different share classes**
(GOOG vs GOOGL = same issuer Alphabet, two Class A/C securities;
BRK.A vs BRK.B same — Berkshire). A 1:1 `ticker_classifications.issuer_id`
FK would break Alphabet / Berkshire / News Corp from day one.

### 3.1 `platform.issuers` (point-in-time legal entity)

```sql
CREATE TABLE platform.issuers (
    issuer_id           text NOT NULL,         -- operator-minted PK (stable across CIK changes + take-privates)
    cik                 text,                  -- SEC CIK if applicable; NULLABLE UNIQUE per v2.2 pattern
    lei                 char(20),              -- GLEIF LEI if applicable; NULLABLE UNIQUE
    legal_name          text NOT NULL,         -- current legal name (renames tracked in issuer_history)
    country_of_incorp   char(2),               -- ISO 3166-1 alpha-2
    status              text NOT NULL DEFAULT 'active',  -- 'active' | 'dissolved' | 'merged' | 'private'
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT issuers_pk PRIMARY KEY (issuer_id),
    CONSTRAINT issuers_cik_uniq UNIQUE (cik),
    CONSTRAINT issuers_lei_uniq UNIQUE (lei),
    CONSTRAINT issuers_status_chk CHECK (status IN ('active','dissolved','merged','private'))
);
```

Cross-vendor identifier pattern mirrors v2.2 `ticker_classifications`:
`issuer_id` is the operator-minted stable PK; `cik` and `lei` are
nullable cross-vendor UNIQUE columns. **This decision is settled by
v2.2 precedent — operator does not need to re-decide.**

### 3.2 `platform.issuer_securities` (issuer ↔ security M:N mapping)

```sql
CREATE TABLE platform.issuer_securities (
    issuer_id           text NOT NULL,         -- FK -> issuers.issuer_id
    classification_id   text NOT NULL,         -- FK -> ticker_classifications.id (TKR-14)
    share_class         text,                  -- 'A' | 'B' | 'C' | NULL (single-class)
    valid_from          date NOT NULL,
    valid_to            date,                  -- NULL = current relationship still in force
    notes               text,
    CONSTRAINT issuer_securities_pk PRIMARY KEY (issuer_id, classification_id, valid_from),
    CONSTRAINT issuer_securities_issuer_fk    FOREIGN KEY (issuer_id) REFERENCES platform.issuers(issuer_id),
    CONSTRAINT issuer_securities_security_fk  FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id)
);

CREATE INDEX ix_issuer_securities_security ON platform.issuer_securities (classification_id, valid_from);
```

Handles Alphabet (one issuer → GOOG class C + GOOGL class A), Berkshire
(BRK.A + BRK.B), and the SCD-2 case where a security gets transferred
between issuers via merger.

### 3.3 `platform.issuer_history` (SCD-2 legal-name / CIK changes)

```sql
CREATE TABLE platform.issuer_history (
    issuer_id           text NOT NULL,         -- FK -> issuers.issuer_id
    cik                 text,                  -- CIK at that point in time (can change in re-domiciliation)
    legal_name          text NOT NULL,         -- name at that point in time
    valid_from          date NOT NULL,
    valid_to            date,                  -- NULL = current
    source              text NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT issuer_history_pk PRIMARY KEY (issuer_id, valid_from),
    CONSTRAINT issuer_history_issuer_fk FOREIGN KEY (issuer_id) REFERENCES platform.issuers(issuer_id)
);
```

Single-timeline SCD-2 (like `ticker_history`), NOT bitemporal — name
changes don't get retroactively revised.

### 3.4 `platform.corporate_events` (BITEMPORAL — M&A graph)

```sql
CREATE TABLE platform.corporate_events (
    event_id            text NOT NULL,         -- minted: SHA-256(predecessor||successor||event_date||event_kind)
    event_kind          text NOT NULL,
    event_date          date NOT NULL,         -- effective date
    announced_date      date,                  -- announcement date (typically earlier)
    predecessor_cls_id  text,                  -- FK -> ticker_classifications.id (the security that dies/transforms)
    successor_cls_id    text,                  -- FK -> ticker_classifications.id (the security that emerges; NULL for take-privates / liquidations)
    predecessor_issuer_id text,                -- denormalized for fast graph walks
    successor_issuer_id   text,                -- denormalized; NULL for go-private / liquidation
    successor_external  text,                  -- free-text when successor isn't a tracked security
    ratio_num           numeric,
    ratio_den           numeric,
    cash_per_share      numeric,
    extra_terms         jsonb,                 -- {"contingent_value_rights": [...], "cash_or_stock_election": {...}}
                                               -- complex M&A terms (CVRs, election windows, etc.)
    source              text NOT NULL,         -- 'sec_8k' | 'operator_manual' | 'fmp_corp_actions' | 'polygon_io' | ...
    source_filing_url   text,
    notes               text,
    -- Bitemporal: M&A announcements DO get amended (deal sweeteners, revised
    -- ratios, terminations + re-announcements). Single-version would lose
    -- the audit trail of how a deal evolved.
    realtime_start      timestamptz NOT NULL DEFAULT now(),
    realtime_end        timestamptz NOT NULL DEFAULT 'infinity',
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT corporate_events_pk PRIMARY KEY (event_id, realtime_start)
);

-- Hot read paths:
CREATE INDEX ix_corp_events_predecessor ON platform.corporate_events (predecessor_cls_id, event_date)
    WHERE realtime_end = 'infinity';
CREATE INDEX ix_corp_events_successor   ON platform.corporate_events (successor_cls_id, event_date)
    WHERE realtime_end = 'infinity';
CREATE INDEX ix_corp_events_pred_issuer ON platform.corporate_events (predecessor_issuer_id, event_date)
    WHERE realtime_end = 'infinity';
CREATE INDEX ix_corp_events_date        ON platform.corporate_events (event_date)
    WHERE realtime_end = 'infinity';
```

### 3.5 Event-kind taxonomy (expert-widened from v0.1)

```
'merger'                  — predecessor absorbed into successor (Splunk → Cisco)
'acquisition'             — predecessor acquired but kept as subsidiary
'spinoff'                 — successor created from predecessor (PayPal from eBay)
'reverse_merger'          — SPAC mechanic; private co becomes public via SPAC shell
'rename'                  — same CIK, ticker change (FB → META)
'name_only_change'        — cosmetic legal-name change, no CIK/CUSIP/ticker change
'cik_change'              — rare; re-domiciliation (Bermuda → US, etc.)
'ticker_swap'             — same security, ticker rotated to a different symbol
'take_private'            — public → private acquisition (TWTR → X Corp)
'going_private_transaction' — public → private buyback (no external acquirer)
'bankruptcy_reorg'        — Chapter 11 emergence, same entity continues
'bankruptcy_liquidation'  — Chapter 7 dissolution, equity to zero (BBBY)
'delisting'               — equity stays alive but moves off-exchange (often to OTC)
'asset_sale'              — entity sold subsidiary / division (no equity event)
'asset_sale_partial'      — entity sold part of operations (no equity event)
'recapitalization'        — capital structure change (no acquirer)
'share_class_collapse'    — dual-class collapse (BRK A/B unification — if ever)
'going_concern_warning'   — auditor flag; not an event per se but engine-relevant
```

Dropped from v0.1: `reverse_split` (already in `platform.corporate_actions`;
not a structural event).

### 3.6 Open design questions for the expert

1. **Graph walk: SQL recursive CTEs vs graph DB layer?** Expert verdict:
   stay in Postgres recursive CTEs — corp graphs are <10 hops in 99.9%
   of cases. Apache AGE only becomes worth it at >100K events with
   multi-hop joins inside hot loops, far beyond this epic's scope.
   Pre-materialize a `successor_walk_cache` view if any hot query
   exceeds 50ms.
2. ~~**Issuer-id minting**~~ — settled by v2.2 precedent (operator-minted
   PK; CIK + LEI as nullable cross-vendor UNIQUEs). Removed from open
   list.
3. ~~**Event-kind taxonomy**~~ — widened per §3.5 above per expert.
4. **Effective-date semantics**: `event_date` = effective date. The
   predecessor security's "death" is at `event_date`; pre-effective bars
   stay attributed to the predecessor.
5. **Complex M&A terms**: drafted `extra_terms jsonb` for CVRs /
   cash-or-stock elections / multi-tranche deals. Operator-extensible.

## 4. Data sources (where the events come from)

| Source | Coverage | Cost | Freshness | Notes |
|---|---|---|---|---|
| **SEC EDGAR 8-K filings** | every US public company M&A / spinoff | free | T+1 | 8-K Item 1.01 (Material Agreement) + Item 2.01 (Completion of Acquisition) are the canonical M&A items. **Expert correction**: the EDGAR filing index exposes item numbers as structured fields, but acquirer/target/effective-date/exchange-ratio live in free-text Ex-2.1 narrative — requires NLP parsing. Expect ~70% extraction completeness from SEC alone; the remaining ~30% needs operator-manual review or vendor backstop. |
| **GLEIF LEI** | every US issuer that uses derivatives | free, bulk + API | daily | Good for issuer-identity anchor. **Expert correction**: Level 2 "Who Owns Whom" covers CURRENT ultimate/direct parent only — does NOT carry predecessor→successor M&A chains. Useful for `issuers.lei` cross-vendor column; useless for the merger graph itself. |
| **OpenCorporates** | legal-entity registration events (incorporation, dissolution) by jurisdiction | free tier rate-limited | manual / on-demand | Useful for take-private deadends (TWTR → X Corp Delaware filing); does NOT carry SEC-filing M&A metadata. |
| **FMP corporate-actions API** | splits, dividends; M&A coverage limited | shared FMP Starter budget | T+1 | Already used for `corporate_actions` table; doesn't carry merger metadata |
| **Alpaca corporate-actions** | splits, dividends only | free | T+1 | Same limitations as FMP |
| **Polygon.io ticker-events** | ticker_change events; partial M&A | $79/mo Developer | T+1 | Covers ticker_change only per current docs; full M&A history requires higher tier; not a v0.2 dependency |
| **PermID (LSEG)** | entity graph with some M&A linkage | free for non-commercial after LSEG registration | rate-limited ~5 req/sec | Decent coverage but not authoritative; v0.2 doesn't depend on it |
| **Wikipedia "List of mergers and acquisitions"** | high-profile events | free | manual | Useful sanity-check; not API-friendly |
| **Operator manual** | high-priority known events | free | operator-paced | The TEST ORACLE for the SEC EDGAR extractor (per expert) — without 15-30 known-good rows, the EDGAR parser has nothing to validate against. |
| **CRSP** | gold-standard academic database | very expensive | T+1 | Out of scope cost-wise |

**Probable rollout** (expert-recommended):
1. Start with **operator-manual entries** for the 15-25 highest-priority
   Path-A unresolvable tickers (TWTR, SPLK, DISCA, MGI, FTCH, BBBY, ATVI,
   FRC, SBNY, SIVB, VMW, etc.) — these are the TRUTH SET.
2. Then build the SEC EDGAR 8-K extractor + validate against the truth set.
3. FMP / Polygon / PermID are downstream upgrades if coverage gaps surface
   after EDGAR ships.

## 5. Phased rollout (v0.2 — tightened first-deliverable per expert)

The expert's recommended first-deliverable ("Path-A FK closure v1") is a
single-week slice that proves the substrate before any EDGAR-parsing
investment. The phases below sequence around that.

| Phase | What | Wall-clock |
|---|---|---|
| P0 | This spec + expert review + operator alignment on §9 open decisions | ½ session (this session) |
| **P1 + thinned P2 (first deliverable, ≤1 week)** | (a) migration: `issuers` + `issuer_securities` + `issuer_history` + `corporate_events` per 3-table schema (§3.1-§3.4). (b) one-shot `ops.py corporate_events_seed` stage loading a hand-curated CSV of ~15-25 high-value Path-A tickers (TWTR→X, SPLK→CSCO, DISCA→WBD, ATVI→MSFT, VMW→AVGO, FRC→JPM, SBNY/SIVB→FDIC, MGI→take-private, FTCH→Coupang, BBBY→liquidated). (c) one helper: `tpcore.corp_history.resolve_issuer_at_date(ticker, date) → issuer_id` (recursive CTE, ~30 LOC). | ~3 hr operator (CSV curation) + ~4 hr build |
| P3 | SEC EDGAR 8-K event extractor: poll EDGAR for new 8-K filings + parse item 1.01/2.01 + Ex-2.1 narrative (NLP) for M&A events. Validate output against the P1+P2 truth-set fixture; gate ship at ≥70% recall on the fixture. | 2-3 days build |
| P4 | Helper expansion: `walk_successors(issuer_id)`, `walk_predecessors(issuer_id)`, `events_affecting(ticker, start, end)` — recursive-CTE-backed. | 1 day |
| P5 | Dashboard view (operator scope): corporate-events tab on the operator console | operator-scheduled |
| P6 | Engine integration (operator scope): AAR / forensics / attribution consumers of the graph | operator-scheduled |

## 6. Operator-visible changes

- **None during P0-P2.** Spec + manual backfill only.
- **P3+:** `corporate_events` accumulates new SEC-sourced rows daily.
- **P4 helpers** make the data queryable from dashboard / engines.

## 7. Out of scope

- **Bond / debt M&A events** — equity-only initially.
- **Crypto / non-US asset corporate actions** — US public-equity scope.
- **Real-time event detection** — T+1 cadence via SEC EDGAR daily pull
  is acceptable; sub-day detection is a separate epic.
- **Dividend / split events** — `platform.corporate_actions` already
  handles these; this epic is for M&A / structural events specifically.
- **Direct integration with `parent_resolver`** — that resolver handles
  per-ticker identity; the corp-events graph layers above it.

## 8. References

- [[corporate-history-enrichment]] follow-on in TODO.md (the deferred-then-promoted parent)
- v2.2 spec: docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.2.md
- Task #18 spec: docs/superpowers/specs/2026-05-23-task-18-macro-data-consolidation.md
- Path-A backfill 79-unresolvable analysis: in-conversation 2026-05-24
- TKR-14 spec: docs/STYLE_GUIDE.md (project identity)
- SEC EDGAR data: <https://www.sec.gov/os/accessing-edgar-data>

## 9. Decisions still needed from operator (5 open after v0.2 expert review)

1. **Confirm the 3-table schema** (`issuers` + `issuer_securities` +
   `issuer_history` + `corporate_events`). v0.1's 2-table sketch was
   wrong — see §3 GOOG/GOOGL argument.
2. **Confirm first-deliverable scope** is "Path-A FK closure v1"
   (P1+thinned-P2: migration + ~25-row CSV + one resolve helper).
3. **Approve / extend the event_kind taxonomy** in §3.5 (16 kinds;
   widened from v0.1's 9).
4. **Authorize operator hours** to curate the 15-25-row truth-set CSV
   (~3 hr operator time). The CSV is the P3 EDGAR-parser's validation
   oracle — can't ship P3 without it.
5. **Authorize the bitemporal shape on `corporate_events`** (M&A
   announcements get amended; bitemporal preserves audit trail vs
   single-version overwriting). `issuer_history` stays single-timeline.

**Settled by v0.2 (no operator decision needed):**
- ~~Graph walk approach~~ — Postgres recursive CTE per expert
- ~~Issuer-id minting / external identifier choice~~ — v2.2 precedent:
  operator-minted PK + cross-vendor UNIQUE columns for CIK / LEI
- ~~Phased order P2 vs P3~~ — P2 must precede P3 (P2 is the test oracle)
