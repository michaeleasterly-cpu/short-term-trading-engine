# Referential-Integrity Implementation Plan v2.2 — TKR-14 smart-key + cross-vendor identity + event-driven OpenFIGI

**Status:** PLAN v2.2 — implements the v2.2 spec. Phases 0/0.5/1/2/3 from v2.1 already merged (PRs #317/#322/#318/#319/#320); v2.2 P0–P9 builds on top. v2.1's Phase 3.5 (parent_resolver placeholder) is fully replaced by v2.2's P3 + P4.

**Supersedes:** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan-v2.1.md` on Phase-3.5-and-beyond. v2.1's Phase 0/0.5/1/2/3 details remain authoritative for the surviving merged work; v2.2 detail starts at P0 (this plan's own header) and runs through P9. Where v2.1 and v2.2 conflict on phase semantics post-3.5, **v2.2 wins**.

**Spec basis (read before executing any phase):**
1. `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.2.md` — **read first.** §1.2 TKR-14 encoding, §1.6 ticker-at-row-date semantics, §1.7 ticker_history, §1.8 event-driven OpenFIGI, §1.10 per-handler-lane dispatch, §3 phase plan, §6 filter examples, §9 concern map.
2. `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.1.md` — §1.5 (statement_timeout, NOTE: per operator 2026-05-23 use `SET LOCAL` not dashboard raise), §1.7 (classify_tickers context).
3. `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.md` — §5 NOT-VALID pattern, §6 index audit, §8 test contracts, §9 verification gates, §11 cleanup-template `ctid` fix — all inherited.
4. `docs/superpowers/checklists/data_feed_change_request.md` — DFCR template + system-routing semantics (Phase P0 dependency).
5. `docs/superpowers/checklists/adapter_readiness.md` — 6-stage contract (Phase P3 dependency).
6. Memory `project_figi_primary_stable_id.md` — canonical decision record (final, post-5-iteration lock-in).
7. Memory `feedback_always_use_iso_standards.md` — ISO 7064 / ISO 3166-1 / Crockford base32 anchor.
8. Memory `feedback_sec_authoritative_fmp_fallback_non_us.md` — per-lane source priority.
9. Memory `feedback_event_driven_not_scheduled.md` — OpenFIGI event-trigger pattern.
10. Memory `feedback_single_session_until_db_done.md` — Carver-session paused; shared main is single-tenant for v2.2 duration.

**Goal (refined from v2.1):** every `ticker`-bearing table in `platform.*` has a real FK to `platform.ticker_classifications(id)` where `id` is a 14-char TKR-14 smart-key (NOT ticker, NOT bigserial, NOT FIGI). Cross-vendor identity (FIGI/CUSIP/ISIN) is captured as UNIQUE-NULLABLE columns. Ticker stays on every child row as the symbol-at-row-date snapshot, looked up via the `ticker_history` table on every INSERT. Drift becomes a constraint violation at INSERT time; ticker-rename events are mechanical; cross-vendor reconciliation is one-JOIN.

**Non-goals (carried forward from v2.1):** composite `(classification_id, date)` FK chains beyond what FK semantics already give us, Tier 2 freshness constraints, RLS policies, macro-table consolidation (Task #18), Phase 2 denormalization (Task #17), per-country insider adapters (Task #15). NEW non-goals: `figi NOT NULL` invariant on `ticker_classifications` (kept nullable indefinitely so a new ticker can land without blocking on OpenFIGI), `prices_daily` re-key away from `(ticker, date)` PK (stays composite; `classification_id` is FK-only).

---

## 1. v2.2 phase summary + wall-clock budget

| Phase | Topic | Status | Migrations | Est. wall-clock |
|---|---|---|---|---|
| **P0** | v2.2 spec + this plan + DFCR ADD request for OpenFIGI | spec SHIPPED (PR #324); plan + DFCR pending | 0 | spec done; plan ~2 hr; DFCR ~30 min |
| **P1** | DFCR ADD approved + new `FeedTrigger.EVENT_DRIVEN` enum value + OpenFIGI `ProviderBinding` + `FeedProfile` entries land (DFCR-system-generated diff) | NEW | 0 schema; 1 code PR (enum + binding via DFCR diff) | 4–6 hr (incl. operator approval cycle) |
| **P2** | Add `id text PRIMARY KEY`, `figi char(12) NULL`, `cusip char(9) NULL`, `isin char(12) NULL`, `current_ticker text NOT NULL`, supporting columns, CHECK + UNIQUE constraints to `ticker_classifications`. Add `ticker_history` table. | NEW | 2 migrations (column adds + ticker_history create) | 3–4 hr |
| **P3** | OpenFIGI adapter at `tpcore/ingestion/openfigi_adapter.py` (6-stage adapter-readiness contract) | NEW | 0 schema; 1 module PR + tests | 6–8 hr |
| **P4** | `parent_resolver` at `tpcore/ingestion/parent_resolver.py` — per-handler-lane dispatch (§1.10 of spec), TKR-14 mint, figi/cusip/isin fill, `ticker_history` first-seen seeding | NEW | 0 schema; 1 module PR + tests + tpcore/identity/tkr14.py generator | 8–10 hr |
| **P5** | Backfill ~13K `ticker_classifications` rows: TKR-14 minted + FIGI via OpenFIGI bulk + CUSIP/ISIN via FMP + `ticker_history` seeded; flip NOT NULL/UNIQUE constraints | NEW | 1 migration (NOT NULL + UNIQUE flip) + 1 backfill script (`scripts/backfill_tkr14.py`) | 4–6 hr (incl. ~30s OpenFIGI bulk + FMP rate-limited batch) |
| **P6** | Per child table: add `classification_id text` column, backfill via ticker join, FK NOT VALID, VALIDATE under `SET LOCAL statement_timeout='30min'`. 14 tables; light → heavy ordering. | NEW | 14 × (1 migration column-add + backfill + FK NOT VALID + 1 migration VALIDATE) = 28 migrations max; operator may batch light tables together | 8–12 hr |
| **P7** | Producer-side rewrite: every child-table INSERT looks up `ticker_history` for ticker-at-row-date; `parent_resolver` invoked on `UNKNOWN_TICKER_OBSERVED` event before any failing INSERT | NEW | 0 schema; ~10 producer modules touched + tests | 6–8 hr |
| **P8** | Refactor engines + dashboard to query by `classification_id` internally; `tpcore/identity/` adapter layer for `ticker↔classification_id` translation at every wire boundary (Alpaca, FMP API, SEC, Streamlit) | NEW | 0 schema; ~30 files touched across 7 engines + dashboard | 12–16 hr |
| **P9** | Drop the 14 Phase-2 ticker-keyed NOT-VALID FKs (now redundant) | NEW | 1 migration (14 × DROP CONSTRAINT) | 1 hr |

**Total v2.2 wall-clock: 52–73 hours.** Single-operator, single-session (per `single-session-until-db-done`). At ~6 productive hours/day → 9–12 working days; with operator review cycles → 10–14 calendar weeks. Larger than v2.1's residual budget because P6 (per-table backfill+VALIDATE × 14) and P8 (engine/dashboard refactor) are bigger than the v2.1 P4 / P5 they replace.

---

## 2. Phase P0 — spec + plan + DFCR ADD request

### 2.1 Spec (DONE)

Shipped PR #324 (`docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.2.md`). All three operator decisions locked: expert's segment set (no current-sector/exchange/name in key); 14-char width + ISO 7064 Mod-97-10 check; 5-char issuer hash (~0.05% collision at 13K, ~0% at scale).

### 2.2 Plan (THIS DOCUMENT)

To merge as PR #(this PR). Then P0 work is "operator authorizes the DFCR ADD" — at which point P1 unblocks.

### 2.3 DFCR ADD request for OpenFIGI — to be filed by operator

The request block per `docs/superpowers/checklists/data_feed_change_request.md`:

```
DATA FEED CHANGE REQUEST
operation:   ADD
feed:        openfigi
kind:        external
provider:    openfigi
adapter:     tpcore.ingestion.openfigi_adapter
need:        Resolves ticker → US Composite FIGI for cross-vendor identity
             reconciliation on ticker_classifications. Required by v2.2 P4
             (parent_resolver) and P5 (one-shot 13K backfill).
cadence:     event_driven — invoked by parent_resolver on UNKNOWN_TICKER_OBSERVED
             event. NOT a scheduled feed. Requires new FeedTrigger.EVENT_DRIVEN
             enum value.
```

System routes through ONBOARD (`adapter_readiness.md` 6-stage contract). Produces a prepared diff for: new `ProviderBinding`, new `FeedProfile`, new `FeedTrigger.EVENT_DRIVEN` enum value, audit-list entry. Operator approves diff (y/n) → P1 lands the prepared diff.

### 2.4 Exit-gate for P0

- v2.2 spec merged (DONE PR #324)
- v2.2 plan merged (THIS PR)
- DFCR ADD request submitted by operator
- DFCR system produces prepared diff with all 6 adapter-readiness invariants green
- Operator approval (y) recorded

---

## 3. Phase P1 — DFCR ADD apply + new FeedTrigger enum value

### 3.1 Deliverables

- **`tpcore/feeds/profile.py`** — add `FeedTrigger.EVENT_DRIVEN = "event_driven"` enum value with docstring: `# parent_resolver-invoked on UNKNOWN_TICKER_OBSERVED; no scheduled cron.`
- **`tpcore/providers.py`** — add OpenFIGI `ProviderBinding` (via DFCR-system-generated diff, NOT hand-edit — `.claude/rules/data-feed-roster.md` + the hook block hand-edits).
- **`tpcore/feeds/profile.py` `FEED_PROFILES` dict** — add OpenFIGI `FeedProfile` entry: `feed="openfigi", trigger=FeedTrigger.EVENT_DRIVEN, cadence_days=0, dissemination_lag_days=0, freshness_max_age_days=None, skip_guard_days=None, targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN, publication_probe=False, evidence="event-driven resolver; no cadence/freshness gates because parent_resolver invokes on UNKNOWN_TICKER_OBSERVED, not on schedule."`
- **HealSpec entry** (if event-driven feeds participate in self-heal) — TBD per DFCR system output.
- **Audit-list entry** — DFCR system generates.

### 3.2 Pre-gates

- P0 exit-gate green (DFCR approved).
- Local gates green (gitleaks + ruff + `pytest -k feed_profile`).

### 3.3 Verification

```python
from tpcore.feeds.profile import FEED_PROFILES, FeedTrigger
assert "openfigi" in FEED_PROFILES
assert FEED_PROFILES["openfigi"].trigger == FeedTrigger.EVENT_DRIVEN

from tpcore.providers import _BINDINGS
assert any(b.feed == "openfigi" for b in _BINDINGS)
```

Sentinel test: `tests/test_feed_profile_event_driven.py` — `EVENT_DRIVEN` profiles must have `cadence_days=0` and `freshness_max_age_days=None`.

### 3.4 Exit-gate for P1

- DFCR diff applied; sentinel passes; `python -m tpcore.scripts.check_imports` green.

---

## 4. Phase P2 — schema additions to `ticker_classifications` + `ticker_history` table

### 4.1 Deliverables

- **Migration `20260524_0000_tkr14_columns_on_ticker_classifications.py`** — adds:
  - `id text NULL` (initially nullable; CHECK constraint immediately enforced for non-null values)
  - `figi char(12) NULL`
  - `cusip char(9) NULL`
  - `isin char(12) NULL`
  - `current_ticker text NULL` (initially nullable; will be populated from existing `ticker` column in same migration, then NOT NULL flipped)
  - `current_exchange text NULL`
  - `current_legal_name text NULL`
  - `gics_sector text NULL`
  - `ipo_venue text NULL`
  - `discovery_source text NULL`
  - `cik text NULL`
  - `updated_at timestamptz NOT NULL DEFAULT now()` (if not already present)
  - CHECK constraint: `id ~ '^[A-Z]{2}[SPEFRTAUWN][NQABOXZ][0-9]{2}[FSAO][0-9A-HJ-KM-NP-TV-Z]{5}[0-9]{2}$'` (applies WHERE id IS NOT NULL)
  - Partial UNIQUE: `current_ticker WHERE status IN ('active','active_when_issued')`
  - Partial UNIQUE: `figi WHERE figi IS NOT NULL`
  - Partial UNIQUE: `cusip WHERE cusip IS NOT NULL`
  - Partial UNIQUE: `isin WHERE isin IS NOT NULL`
  - Partial UNIQUE: `cik WHERE cik IS NOT NULL`
  - Expression indexes: `(substring(id,1,2))`, `(substring(id,3,1))`, `(substring(id,4,1))`, `(substring(id,5,2))`, `(substring(id,7,1))`
  - Same-migration data-move: `UPDATE platform.ticker_classifications SET current_ticker = ticker` (populates the new column from the existing `ticker` PK)
- **Migration `20260524_0100_create_ticker_history.py`** — creates `ticker_history` table with `EXCLUDE USING gist` no-overlap constraint per spec §1.7. Requires `pg_trgm` + `btree_gist` extensions (verify via Phase 0 audit; install if missing in a pre-migration step).

### 4.2 Pre-gates

- P1 exit-gate green.
- Snapshot `ticker_classifications` via `bash scripts/run_db_snapshots.sh ticker_classifications` (per v2.1 Phase 0.5 protocol).
- `db_snapshots` verified: row count matches live; sha256 verified.

### 4.3 Verification

```sql
-- After migration:
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema='platform' AND table_name='ticker_classifications'
  AND column_name IN ('id', 'figi', 'cusip', 'isin', 'current_ticker');
-- Expected: 5 rows; id/figi/cusip/isin nullable=YES (will flip later); current_ticker NOT NULL

SELECT count(*) FROM platform.ticker_classifications WHERE current_ticker IS NOT NULL;
-- Expected: equal to row count (data-move populated)

\d+ platform.ticker_history
-- Expected: table exists with EXCLUDE constraint
```

### 4.4 Exit-gate for P2

- Both migrations applied; `alembic current` shows latest; partial UNIQUE constraints visible via `\d+`; expression indexes visible via `\di`.
- `current_ticker` populated for 100% of existing rows (data-move worked).
- `id` column exists, nullable, no rows have non-NULL value yet (P5 fills).

---

## 5. Phase P3 — OpenFIGI adapter (6-stage adapter-readiness contract)

### 5.1 Deliverables

- **`tpcore/ingestion/openfigi_adapter.py`** — async client with:
  - `async def map_tickers(tickers: list[str], exch_code: str = "US") -> dict[str, OpenFIGIResult]` — batches up to 100 per request (the with-key rate-limit cap).
  - Rate-limit aware: respects `ratelimit-remaining` + `ratelimit-reset` headers; sleeps + retries on HTTP 429.
  - Retry-with-exponential-backoff on HTTP 500/503.
  - `OpenFIGIResult` Pydantic model with `figi`, `composite_figi`, `share_class_figi`, `name`, `ticker`, `security_type`, `market_sector` fields.
  - Regex validation of every returned FIGI against `^(((?!BS|BM|GG|GB|VG|GH|KY)[BCDFGHJKLMNPQRSTVWXZ]{2})G[BCDFGHJKLMNPQRSTVWXYZ\d]{8}\d)$` before storing.
  - `{"warning": "No identifier found."}` treated as `OpenFIGIResult(figi=None, ...)`, NOT as an exception.
- **Adapter-readiness 6-stage contract:**
  1. **Ingest** — `map_tickers` function (above).
  2. **Test** — `tests/openfigi_adapter/test_map_tickers.py` with mocked + live (gated) modes.
  3. **Validate** — validation check at `tpcore/quality/validation/checks/openfigi_health.py` — verifies last-N-days `compositeFIGI` fill rate on `ticker_classifications` is >= 99% for active tickers (excludes the recent-mint window).
  4. **Dashboard** — Streamlit panel in `dashboard_components/data_quality.py` shows: total mappings made today, success rate, error breakdown.
  5. **Schedule** — N/A for event-driven; placeholder docstring records "no schedule; invoked by parent_resolver".
  6. **Self-heal** — `tpcore/selfheal/specs/openfigi.py` HealSpec for the rare cases where a FIGI we stored gets reassigned (`IDENTITY_DIVERGENCE_INVESTIGATE` event handler).
- **`OPEN_FIGI_API_KEY`** env var (already in `.env` per operator confirmation 2026-05-23).

### 5.2 Pre-gates

- P2 exit-gate green.
- `OPEN_FIGI_API_KEY` exported via `set -a && source .env && set +a` in the test environment.

### 5.3 Verification

```python
import asyncio
from tpcore.ingestion.openfigi_adapter import map_tickers
result = asyncio.run(map_tickers(["AAPL", "BABA", "QQQ"]))
# Expected: AAPL → BBG000B9XRY4 (Apple US Composite)
# Expected: BABA → BBG006G2JVL2 (Alibaba ADR US Composite)
# Expected: QQQ  → BBG000BSWKH7 (QQQ ETF US Composite)
```

Local gates: `gitleaks`, `ruff`, `pytest -k openfigi`, `check_imports`.

### 5.4 Exit-gate for P3

- All 6 adapter-readiness stages green.
- Live smoke test against the 3 reference tickers above returns valid composite FIGIs matching the regex.
- HealSpec registered in `tpcore/selfheal/registry.py`.

---

## 6. Phase P4 — `parent_resolver` + TKR-14 generator + per-lane dispatch

### 6.1 Deliverables

- **`tpcore/identity/tkr14.py`** — pure functions:
  - `mint(country, asset_class, ipo_venue, discovery_source, cik, legal_name, now) -> str` — per spec §1.5 algorithm.
  - `validate(id: str) -> bool` — regex check + ISO 7064 Mod-97-10 verification.
  - `decode(id: str) -> TKR14Segments` — extracts each segment as a typed dict.
  - `iso_7064_mod_97_10(prefix: str) -> str` — pure check-digit calculator.
  - `crockford_base32(value: int, width: int) -> str` — pure encoder.
- **`tpcore/identity/dispatcher.py`** — `ticker_to_classification_id(ticker, as_of=None)` + `classification_id_to_ticker(id, as_of=None)`; cached in-process with TTL (60s for `ticker_to_classification_id`; longer for the inverse since IDs are immutable).
- **`tpcore/ingestion/parent_resolver.py`** — async event-driven resolver:
  - `async def resolve(unknown_ticker: str, calling_handler: HandlerKind) -> ClassificationRow` — branches per spec §1.10.
  - For SEC-source handlers: try `sec_company_tickers_lookup(cik)` first; fallback FMP `/profile`; then FMP `/profile` for enrichment; then OpenFIGI `map_tickers`.
  - For FMP-source handlers: FMP `/profile` first; then OpenFIGI `map_tickers`.
  - Mints TKR-14 via `tpcore.identity.tkr14.mint`.
  - Seeds `ticker_history` with `(classification_id, ticker, valid_from=today, valid_to=NULL)`.
  - Pin-at-first-resolve discipline: if FIGI/CUSIP/ISIN already non-null on existing row, never overwrites; logs `IDENTITY_DIVERGENCE_INVESTIGATE` event on mismatch.
- **`tpcore/ingestion/handlers.py`** — integration hook: every handler's pre-INSERT sentinel (`unknown = set(incoming.ticker) - set(active_tickers)`) calls `parent_resolver.resolve(t, HandlerKind.<X>)` for each unknown ticker before retrying the INSERT.
- **Tests:**
  - `tests/identity/test_tkr14.py` — mint produces 14-char output matching regex; ISO 7064 check verifies; round-trip mint→validate green for 1000 random inputs.
  - `tests/identity/test_tkr14_iso_7064.py` — test against published LEI test vectors (since same algorithm).
  - `tests/ingestion/test_parent_resolver_dispatch.py` — per-handler-lane dispatch returns correct call order (mocked SEC/FMP/OpenFIGI calls).
  - `tests/ingestion/test_parent_resolver_pin_first_resolve.py` — divergence on later resolve writes IDENTITY_DIVERGENCE_INVESTIGATE event, never overwrites.

### 6.2 Pre-gates

- P3 exit-gate green.
- OpenFIGI adapter live-smoke against 3 reference tickers green.

### 6.3 Verification

```python
from tpcore.identity.tkr14 import mint, validate, decode
from datetime import datetime, UTC
id = mint(country="US", asset_class="S", ipo_venue="N", discovery_source="F",
          cik="0000320193", legal_name="APPLE INC", now=datetime.now(UTC))
assert validate(id)
assert decode(id).country == "US"
assert decode(id).asset_class == "S"
```

Live integration smoke: invoke `parent_resolver.resolve("NEWLY_IPO_TICKER", HandlerKind.PRICES)` for a synthetic test ticker; verify TKR-14 minted; FIGI populated; ticker_history seeded with first-seen entry; pin-at-first-resolve event NOT triggered.

### 6.4 Exit-gate for P4

- All four test files green.
- Live smoke against 3 distinct handler kinds (PRICES, INSIDER, FUNDAMENTALS) produces the per-lane-correct dispatch order in logs.
- `IDENTITY_DIVERGENCE_INVESTIGATE` test fires when expected.

---

## 7. Phase P5 — 13K-row backfill (CSV-archive-FIRST, API-second)

### 7.1 Deliverables

**Per `feedback_etl_bulk_before_api_crawl` + operator 2026-05-23 reminder ("you have all of the csv files already in the local repo to use for backfills"): CSV archives + existing adapters are the FIRST resort for CUSIP/ISIN/identity data. External APIs (OpenFIGI, FMP `/profile`) are SECOND, only for rows the archives don't cover.**

Local archive inventory verified 2026-05-23:
- `data/fmp_fundamentals_archive/` — FMP `/profile` historical responses (contains `cusip`, `isin`, `cik` per ticker by date)
- `data/fmp_backfill/` — bulk FMP backfill staging
- `data/alpaca_backfill/` — bulk Alpaca backfill staging
- `data/alpaca_corporate_actions_archive/`, `data/alpaca_daily_bars_archive/` — operational archives
- Plus per-feed archives for fmp_catalyst_events, fmp_earnings_events, finra_short_interest, iborrowdesk_borrow_rates, apewisdom_social_sentiment, finnhub_insider_sentiment, fred_macro, aaii_sentiment, greeks_max_pain.

Existing modules to reuse:
- `tpcore/ingestion/csv_archive.py` — archive reader API
- `tpcore/ingestion/csv_archive_backends.py` — backend dispatch
- `tpcore/ingestion/adapter_contract.py` — adapter base contract
- `tpcore/ingestion/handlers.py` — per-feed handler implementations

**NEW stage in `scripts/ops.py`: `tkr14_backfill`** — NOT a one-off script. Per operator standing rule "use existing infrastructure, dont write one offs, you can pass arguments through the data feed to get what you want", this is added to the existing `OPS_UPDATE_STAGES` registry alongside `daily_bars`, `historical_*`, `rebuild_from_archive`, etc. Invocation matches the rest of the stage ecosystem:

```bash
python scripts/ops.py --stage tkr14_backfill                            # full backfill
python scripts/ops.py --stage tkr14_backfill --param ticker=AAPL        # single-row re-resolve
python scripts/ops.py --stage tkr14_backfill --param mode=cusip_isin_only   # narrow scope
python scripts/ops.py --stage tkr14_backfill --param mode=figi_only         # narrow scope
python scripts/ops.py --stage tkr14_backfill --param skip_archive=true      # bypass archive read; force API
```

The stage uses existing infrastructure:
- **`tpcore/ingestion/csv_archive.py`** — `read_archive("fmp_fundamentals", ticker=...)` to read existing CSV.gz archives for cusip/isin/cik.
- **Existing FMP adapter** in `tpcore/ingestion/handlers.py` — invoked with backfill-mode parameters for rows the archive doesn't cover.
- **`tpcore/ingestion/openfigi_adapter.py`** (from P3) — invoked for FIGI lookup via the same adapter contract.
- **`tpcore/identity/tkr14.py`** (from P4) — mint function.
- **`tpcore/ingestion/parent_resolver.py`** (from P4) — orchestrates the per-lane dispatch; the stage just iterates `ticker_classifications` rows + invokes `parent_resolver.resolve()` for each row missing `id`.

Pull-order INSIDE the stage:
1. **CSV archive scan first** — `tpcore.ingestion.csv_archive.read_archive("fmp_fundamentals", ticker=t)` for each row; extract `cusip`, `isin`, `cik` from the archive. Per `feedback_etl_bulk_before_api_crawl`. No FMP API call needed for archive-covered rows (expected ~95% coverage).
2. **FMP `/profile` API only for rows the archive doesn't cover** — invoked via the existing FMP adapter, NOT a one-off curl. Rate-limited via FMP Starter (300/min). Expected miss-rate <5% of 13K.
3. **OpenFIGI bulk for FIGI** — via `tpcore.ingestion.openfigi_adapter.map_tickers([ticker_batch])`. Batches up to 100; ~25K mappings/min with key.
4. **TKR-14 mint + ticker_history seed** — via `parent_resolver.resolve()` which already encapsulates this logic per P4.
5. **Idempotent** — `parent_resolver.resolve()` skips rows where `id IS NOT NULL` per pin-at-first-resolve discipline.
6. **Progress reporting** via the standard ops.py logging path (every 100 rows; structlog INFO; persisted to `application_log` table per existing convention).
7. **Wall-clock estimate:** archive-first means most rows skip API calls. ~10-15 min total.

- **Wrapper:** `scripts/run_ops_tkr14_backfill.sh` (mirrors existing `scripts/run_*` wrapper convention).
- **Migration `20260525_0000_tkr14_not_null_unique.py`** — runs AFTER backfill script completes:
  - `ALTER COLUMN id SET NOT NULL`
  - `DROP CONSTRAINT ticker_classifications_pkey` (old, on `ticker`)
  - `ADD CONSTRAINT ticker_classifications_pkey PRIMARY KEY (id)`
  - `current_ticker` already partial-UNIQUE active per P2; no further constraint needed.

### 7.2 Pre-gates

- P4 exit-gate green.
- Snapshot `ticker_classifications` + `ticker_history` (empty at this point) via `bash scripts/run_db_snapshots.sh ticker_classifications ticker_history`.
- OpenFIGI rate-limit budget check (no other batch jobs in flight).

### 7.3 Verification

```sql
-- After backfill script + migration:
SELECT count(*), count(id), count(figi), count(cusip), count(isin)
FROM platform.ticker_classifications;
-- Expected: count(*)=count(id) (100% id filled); count(figi)>=99% of count(*) (some misses OK);
--          count(cusip) higher than count(figi) (FMP returns reliably); count(isin) ~= count(cusip).

SELECT count(*) FROM platform.ticker_history WHERE valid_to IS NULL;
-- Expected: equal to count(*) on ticker_classifications (one current row per security).

\d platform.ticker_classifications
-- Expected: PRIMARY KEY on (id), NOT on (ticker).
```

### 7.4 Exit-gate for P5

- `id` is the PK on `ticker_classifications`; 100% of rows have valid TKR-14 (regex passes).
- `figi` fill rate ≥99% for active tickers.
- `ticker_history` has one row per security.
- No `IDENTITY_DIVERGENCE_INVESTIGATE` events from the backfill (clean first-run).

---

## 8. Phase P6 — per-child-table `classification_id` FK rollout (CSV-archive cross-check)

**For each child table, the existing per-feed CSV archive serves as a cross-check on the in-database `ticker → classification_id` join:** if the archive has rows for a ticker that the live table is missing or has under a different ticker (rename event), the archive is the historical truth. Use `tpcore.ingestion.csv_archive.read_archive(feed_name, date_range)` to verify.

### 8.1 Deliverables

For each of the 14 FK-protected tables, in light → heavy order per orphan count from v2.1 Phase 0 audit:

| Order | Table | Orphan rows |
|---|---|---|
| 1 | `universe_candidates` | 1 |
| 2 | `short_interest` | 3 |
| 3 | `liquidity_tiers` | 8 |
| 4 | `earnings_events` | 12 |
| 5 | `spread_observations` | 33 |
| 6 | `fundamentals_quarterly` | 135 |
| 7 | `corporate_actions` | 1,506 |
| 8 | `prices_daily` | 335,159 |
| 9-14 | `insider_transactions`, `sec_material_events`, `borrow_rates`, `social_sentiment`, `options_max_pain`, `insider_sentiment` | 0 each |

Per-table migration pattern:

- **Migration `20260526_NNNN_<table>_add_classification_id.py`**:
  - `ALTER TABLE platform.<table> ADD COLUMN classification_id text NULL`
  - `UPDATE platform.<table> ch SET classification_id = tc.id FROM platform.ticker_classifications tc WHERE ch.ticker = tc.current_ticker AND tc.status IN ('active','active_when_issued')` (under `SET LOCAL statement_timeout='30min'`)
  - For `prices_daily`: this UPDATE is the slow one — ~21M row rewrite. Operator may split into chunked sub-migrations if needed.
- **Migration `20260526_NNNN_<table>_classification_id_fk.py`**:
  - `ALTER TABLE platform.<table> ADD CONSTRAINT <table>_classification_id_fk FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id) ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID` (NOT VALID first per v2 §5)
  - Operator-batched VALIDATE: `ALTER TABLE platform.<table> VALIDATE CONSTRAINT <table>_classification_id_fk` (under `SET LOCAL statement_timeout='30min'`)

### 8.2 Pre-gates

- P5 exit-gate green.
- Per-table snapshot via `bash scripts/run_db_snapshots.sh <table>` before each table's migration pair.
- For `prices_daily`: also run `EXPLAIN ANALYZE` of the UPDATE on a sample (1K rows) to estimate wall-clock; chunk if estimate exceeds 30min.

### 8.3 Verification

Per table:
```sql
-- After column add + backfill:
SELECT count(*), count(classification_id) FROM platform.<table>;
-- Expected: equal (every row has a classification_id, even orphans which now have NULL → drop step needed)

SELECT count(*) FROM platform.<table> WHERE classification_id IS NULL;
-- For orphan-bearing tables, this is the orphan-cleanup decision point per spec §1.11
-- Path A (BACKFILL via parent_resolver): re-run parent_resolver for these tickers, get classification_id, UPDATE
-- Path B (DELETE): DELETE rows with NULL classification_id

-- After FK NOT VALID + VALIDATE:
SELECT convalidated FROM pg_constraint
WHERE conname = '<table>_classification_id_fk';
-- Expected: TRUE (VALIDATE completed)
```

### 8.4 Exit-gate for P6

- All 14 tables have `classification_id` populated for 100% of rows that survive cleanup.
- All 14 FK constraints `convalidated=TRUE`.
- No spurious INSERTs blocked (verify via 24-hour application_log scan post-migration).

---

## 9. Phase P7 — producer-side ticker_history lookup rewrite

### 9.1 Deliverables

For each producer module (`tpcore/ingestion/handlers.py` + the ~10 stage functions in `scripts/ops.py` + engine AAR writers):

- Replace `INSERT INTO ... (ticker, ...) VALUES ($1, ...)` patterns with:
  ```python
  ticker_at_date = await ticker_history_lookup(conn, classification_id, row_semantic_date)
  INSERT INTO ... (classification_id, ticker, ...) VALUES ($1, $2, ...)
  ```
- Pre-INSERT sentinel: if `unknown = set(incoming.ticker) - set(active_tickers)`, fire `UNKNOWN_TICKER_OBSERVED` event → parent_resolver resolves → retry INSERT.
- New helper `tpcore/identity/ticker_history.py` — `async def lookup(conn, classification_id, date) -> str` (cached per-classification_id with TTL for the active window).
- Update producer tests: `tests/ingestion/test_<handler>.py` for each touched handler.

### 9.2 Pre-gates

- P6 exit-gate green.
- `ticker_history` populated for all classification_ids (P5 seeded; subsequent rename events update).

### 9.3 Verification

Sentinel test (`tests/test_producer_ticker_lookup_consistency.py`): for every child-table INSERT path, the test injects a "rebrand event" (FB→META) at a specific date and verifies that subsequent INSERTs use the as-of-date ticker, NOT current_ticker. Pre-rebrand date INSERT → `ticker='FB'`; post-rebrand → `ticker='META'`; same classification_id throughout.

### 9.4 Exit-gate for P7

- Sentinel test green.
- 24-hour application_log scan shows zero "FK violation" or "ticker mismatch" events from producers.

---

## 10. Phase P8 — engine + dashboard refactor to `classification_id`-internal

### 10.1 Deliverables

- **`tpcore/identity/`** adapter layer (extension of P4's dispatcher.py):
  - Wire-boundary adapters: every Alpaca API call (`tpcore/order_management/alpaca_client.py`) does `classification_id → current_ticker` at order placement; every FMP/SEC ingest does `ticker → classification_id` at row arrival.
- **7 engines** — refactor:
  - `reversion/`, `vector/`, `momentum/`, `sentinel/`, `canary/`, `catalyst/`, `carver/` — every `WHERE ticker = $1` becomes `WHERE classification_id = $1`; adapter translates at the wire boundary.
- **`dashboard.py` + `dashboard_components/`** — Streamlit panels show `current_ticker` (translated at display time) but internal joins use `classification_id`.
- **AAR** — `tpcore/aar/` records reference `classification_id`; `current_ticker` stored on each AAR row as ticker-at-trade-date snapshot.

### 10.2 Pre-gates

- P7 exit-gate green.
- All producer paths verified green.

### 10.3 Verification

- Full-suite pytest (`python -m pytest -p no:xdist -q`) green.
- Order-flip test (`python -m pytest -p no:xdist -q --order-flip`) green.
- Live paper-trading dry-run for 1 trading day (XNYS session) — verify all engines emit orders + AAR rows + dashboard displays correctly.

### 10.4 Exit-gate for P8

- Full-suite green; order-flip green; live-paper-dry-run produces consistent dashboard view with no spurious errors.

---

## 11. Phase P9 — drop redundant Phase-2 ticker-keyed FKs

### 11.1 Deliverables

- **Migration `20260601_0000_drop_phase2_ticker_fks.py`** — drops the 14 NOT-VALID ticker-keyed FKs added in v2.1 Phase 2 (PR #319). These were intermediate scaffolding; the `classification_id`-keyed FKs from P6 are the real ones.

### 11.2 Pre-gates

- P8 exit-gate green.
- 7-day post-P8 quiet period (no production incidents traced to FK changes).

### 11.3 Verification

```sql
-- After drop:
SELECT conname FROM pg_constraint
WHERE conrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'platform'::regnamespace)
  AND contype = 'f' AND conname LIKE '%_ticker_fk';
-- Expected: 0 rows (all the old ticker-keyed FKs gone)

SELECT conname, convalidated FROM pg_constraint
WHERE conrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'platform'::regnamespace)
  AND contype = 'f' AND conname LIKE '%_classification_id_fk';
-- Expected: 14 rows, all convalidated=TRUE
```

### 11.4 Exit-gate for P9

- 14 old ticker-keyed FKs gone; 14 classification_id-keyed FKs remain validated; full-suite still green.

---

## 12. Standing discipline references

Every phase follows these:

- **Local gates before push** per `feedback_run_gates_locally_on_commit` (standing rule).
- **Push only after major deliverable completion** per `feedback_push_when_tangible_batch_prs`.
- **Snapshot before each cleanup migration** per v2.1 Phase 0.5 (on-demand `db_snapshots`).
- **Standards-first** per `feedback_always_use_iso_standards` (ISO 7064, ISO 3166-1, Crockford base32 already locked in).
- **NOT-VALID-first pattern** per v2 spec §5 (every FK addition lands NOT VALID; operator-batched VALIDATE).
- **`SET LOCAL statement_timeout='30min'`** for slow VALIDATE migrations per operator 2026-05-23 directive (NOT a dashboard raise).
- **Single-session-mode** per `feedback_single_session_until_db_done` (shared main is single-tenant for v2.2 duration).
- **DFCR for any feed change** per `.claude/rules/data-feed-roster.md` (P1 follows this; no hand-edits).

---

## 13. Per-phase PR mapping (suggested)

Operator may batch differently; this is the natural single-deliverable cadence:

| Phase | Suggested PR(s) | Push-after-batch granularity |
|---|---|---|
| P0 | spec PR (#324, done) + plan PR (this) + DFCR submission (operator-filed) | 2-3 PRs total |
| P1 | DFCR-system-generated diff merge | 1 PR |
| P2 | schema migrations | 1 PR (both migrations) |
| P3 | OpenFIGI adapter + 6-stage contract | 1 PR |
| P4 | parent_resolver + tkr14.py + dispatcher.py + tests | 1-2 PRs (split if too large) |
| P5 | backfill script + NOT NULL/UNIQUE migration | 1 PR (script first, then migration after script-run is verified) |
| P6 | 14 table FK rollouts | 4-6 PRs (batched: light tables together, prices_daily alone) |
| P7 | producer rewrites | 2-3 PRs (handlers / stages / engines AAR) |
| P8 | engine + dashboard refactor | 4-6 PRs (one per engine + dashboard) |
| P9 | drop old FKs | 1 PR |

Total estimated PRs: ~18-25 for the full v2.2 work. Spread over 10-14 calendar weeks.

---

## 14. Sources

- Spec: `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.2.md`
- DFCR template: `docs/superpowers/checklists/data_feed_change_request.md`
- Adapter-readiness 6-stage contract: `docs/superpowers/checklists/adapter_readiness.md`
- OpenFIGI API: <https://www.openfigi.com/api/documentation>
- OMG FIGI 1.2: <https://www.omg.org/spec/FIGI/1.2>
- ISO 7064 Mod-97-10 (used by LEI ISO 17442): check digit algorithm
- ISO 3166-1 alpha-2: country code segment
- Kimball SCD Type-2 (surrogate key + as-of-date dimension): pattern reference
