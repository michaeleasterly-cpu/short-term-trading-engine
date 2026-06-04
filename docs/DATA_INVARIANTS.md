---
title: Data-Layer Invariants & Lessons-Learned Registry
version: 1.0.0
last_updated: 2026-06-03
status: canonical — the data-layer rebuild MUST preserve every invariant here
---

# Data-Layer Invariants & Lessons-Learned Registry

> **Purpose.** Every documented data-layer invariant, failure-prevention rule, and hard-learned constraint — compiled (2026-06-03) from local memory, `docs/DATABASE_AND_DATAFLOW.md`, `docs/audits/2026-06-03-identity-substrate-data-flow.md`, `.claude/rules/**`, validation-check docstrings, migrations, and the referential-integrity specs. Each entry carries a source (file:line / doc§ / memory entry) and the failure it prevents. **The data-layer rebuild MUST preserve all of these; the clean re-ingest must not re-commit any documented mistake.** Read this with `DATABASE_AND_DATAFLOW.md` §0 (the data-layer index) on any data work.

## A. Identity & attribution
- **A1 — Prove `ticker + date → classification_id → CIK` on every write** to a ticker-bearing table (15 SCD-2 BEFORE INSERT triggers enforce it). `.claude/rules/identity-path.md` + identity audit §2.8. *Prevents `classification_id IS NULL` contamination.*
- **A2 — Engine readers MUST pass `as_of`** to the dispatcher/repo; never call `ticker_to_classification_id()` without it. identity-path §2 + audit §2.9 (20/20 readers in violation). *Prevents cross-entity contamination from delisted-then-reused tickers.*
- **A3 — `ticker_classifications` PK is the immutable TKR-14 smart-key, NOT ticker, NOT CIK.** v2.2 spec §1.1–1.2. *Ticker/CIK are mutable; Codd forbids mutable PKs.*
- **A4 — `ticker_history` is load-bearing; child rows resolve to the classification valid at the ROW date** (`valid_from <= d < COALESCE(valid_to,∞)`). v2.2 §1.6 + audit §2.4 (92,318 mis-windowed bars). *Triggers are INSERT-only → historical rows need idempotent re-attribution after predecessor backfill.*
- **A5 — FPFD populates SEC-first, full-history pagination** (not FMP, not the recent ~1000-filing shard). audit §2.3 + DATABASE_AND_DATAFLOW §0. *Recent-shard-only produced decade-shifted FPFD (LMT 2016, PEP 2015); 6% of bars are pre-FPFD.*
- **A6 — `lifetime_start` backfilled from FPFD (or earliest `ticker_history.valid_from`); the `1900-01-01` sentinel is forbidden.** audit §2.3 (100% sentinel today). *Sentinel masks real inception; engines get wrong universes.*
- **A7 — SEC authoritative for US CIK-backed identity/insider/filings; FMP fallback non-US only.** `feedback_sec_authoritative_fmp_fallback_non_us` + identity-path §1.3. *Per-lane authority; mis-applying SEC-first to the wrong lane is a defect.*
- **A8 — FMP never silently overrides SEC identity; divergence routes through IdentityDispatcher.** identity-path §1.3. *Undocumented FMP-precedence is a critical defect.*

## B. Source authority & roster
- **B1 — Daily prices: FMP primary; Alpaca IEX/SIP fallback; NEVER Alpaca for backfill.** `feedback_no_alpaca_for_daily_prices_backfill` + `project_fmp_primary_daily_bars`. *Alpaca close-date/session semantics differ → per-row date inconsistency.*
- **B2 — Identity/fundamentals/filings: SEC EDGAR primary (bulk `submissions.zip` first, `full_history=True`); FMP non-US fallback.** `feedback_bulk_before_api_crawl_REINFORCED` + §0. *Bulk = 2.5min vs ~4h crawl (operator killed a crawl PR).*
- **B3 — Provider roster changes via DFCR only; never hand-edit `tpcore/providers.py`.** `.claude/rules/data-feed-roster.md`. *ProviderBinding is the ACTIVE/FALLBACK/RETIRED SoT + parity + HealSpec sync.*
- **B4 — CUTOVER automated + parity-gated; ONBOARD/RETIRE operator-approved.** data-feed-roster rule + provider-lifecycle spec. *Parity gate catches silent per-ticker divergence.*
- **B5 — Macro: FRED primary; `hy_spread` is SACRED (see K1).**
- **B6 — Finnhub free-tier insider only (premium endpoints 403).** DATABASE_AND_DATAFLOW §2 insider_sentiment.
- **B7 — Short interest: FINRA; `release_date` (= settlement + ~9 sessions) is the PIT boundary, NOT `settlement_date`.** §2 short_interest. *Filtering on settlement_date = silent look-ahead.*
- **B8 — Borrow (IBorrowDesk) fragile: skip after 3 consecutive failures, CRITICAL log, continue.** §2 borrow_rates. *Anti-bot 403/429/444 under load.*
- **B9 — AAII: browser-shaped request (UA+Accept+Referer); 403 permanent, 429/5xx retry.** §2 aaii_sentiment.
- **B10 — Tradier CLOSED (2026-06-03): no adapter, no options, FMP/Alpaca prices only.** `project_tradier_closed_no_options`.

## C. Ingest mechanics (bulk-first, CSV-first, ordering)
- **C1 — BULK file BEFORE per-row API crawl.** `feedback_bulk_before_api_crawl_REINFORCED`. *Look for a vendor bulk dump first; if a loop's ETA >1h, STOP and check conventions.*
- **C2 — CSV-first: download → validate-at-CSV → load → compress; no pure DB-side INSERT loops.** `.claude/rules/data-adapter.md`. *Validate before the DB sees it.*
- **C3 — Daily update runs FIRST; non-daily stages after daily completes.** `feedback_data_update_first`. *Engines depend on fresh prices.*
- **C4 — Idempotent stages (`ON CONFLICT …`); re-run = same end-state, no dupes.** every table spec.
- **C5 — HTTP retries via `tpcore.outage.with_retry`; never local loops / `asyncio.sleep`.** data-adapter rule.
- **C6 — Contract-population sentinel (`adapter_contract.assert_contract_populated`): producer hard-stop if a required field is empty across a non-empty pull.** data-adapter rule.
- **C7 — Backfills route through `ops.py --stage … --param`; NEVER a one-off `scripts/foo.py`.** data-adapter rule.

## D. Point-in-time correctness
- **D1 — `fundamentals_quarterly` filters `WHERE filing_date <= as_of`; natural key `(ticker, period_end_date)` (surrogate PK let 267 dup quarters accumulate — migrate to natural PK + dedup).** §2.2 + audit §2.7.
- **D2 — Canonical SCD-2 predicate: `valid_from <= d < COALESCE(valid_to,∞)`.** v2.2 §1.6.
- **D3 — `delisted`/`delisting_date` are PIT-safe for filters, not backfill logic.** §2.1.
- **D4 — Apply corporate_actions to prices_daily before backtests read; adjustment is idempotent.** §2.3 + §3.2.
- **D5 — `insider_transactions.filing_date` is the PIT boundary; CHECK `shares>0, price>=0, value>=0`.** §2.8.
- **D6 — `fundamentals_quarterly_completeness` routes by `sec_document_type_primary` + per-form MAX_GAP (10-Q 100d / annuals 450d), not a fixed 90d.** check docstring + audit §2.7.

## E. Provenance honesty / no fabrication
- **E1 — Never fabricate a missing row; honest gap = NULL/absence/escalate-only.** `feedback_no_lazy_vendor_blame`.
- **E2 — CSV-write layer applies the same CHECK predicates as the schema (reject bad rows at load).** §2.8.
- **E3 — `source` column tags the originating feed on every ingestible row.** §2.8.
- **E4 — `short_interest_pct` derived from PIT `shares_outstanding`; NULL when absent (never fabricated).** §2.15.
- **E5 — `fundamentals_period_source_evidence` was POLLUTED (FMP date-key bug); reset done 2026-06-03; rebuild via `data_quality_log`, not a sidecar.** audit §2.12.

## F. Validation & the 100%-green gate
- **F1 — `DATA_OPERATIONS_COMPLETE` only if ALL 32 checks are 100% green — no tolerance knob, no operator-task step; deterministic cascade is the only recovery.** `.claude/rules/selfheal-auditheal.md` + CLAUDE.md.
- **F2 — `prices_daily_completeness` is zero-tolerance**: every tier≤2 liquid trading common stock has a bar for every NYSE session in the recent window within its active range; ANY miss = FAIL. check docstring.
- **F3 — `prices_daily_freshness` dual gate: CRITICAL_TICKERS ≤5 sessions stale + universe staleness ≤2% at 14d.** check docstring (SPY drift broke Sentinel 2026-05-15).
- **F4 — `fundamentals_quarterly_completeness` metadata-coverage sentinel: >25% metadata-required ⇒ synthetic FAIL until `backfill_sec_metadata` extends coverage.** check docstring.
- **F5 — `row_integrity`: close>0, high>=low, no future dates; ANY violation = FAIL (cap log at 50 rows).** check docstring.
- **F6 — Validators must be IDENTITY-AWARE in the rebuild: clamp completeness to issuer FPFD / security `lifetime_start` so legitimately-young issuers don't FAIL.** audit §2.9 (32 checks currently identity-blind).

## G. Survivorship-freeness
- **G1 — `prices_daily` is survivorship-free; default feed IEX→SIP (2026-05-13) because IEX silently missed off-IEX tickers; FMP full CTA tape now primary.** §2.1.
- **G2 — `delist_stale` promotes stale-but-unflagged tickers (in-flight delistings).** §2.1.
- **G3 — `ticker_history` must cover delisted-then-reused pairs; rebuild-from-source universe (SEC + FMP delisting history) preserves survivorship-freeness.** audit §2.4 + §5 step 5.

## H. Scrape-fragility / outage handling
- **H1 — IBorrowDesk circuit-breaker: 3-consecutive-fail skip + CRITICAL + continue; `max_tickers` caps the run.** §2.14.
- **H2 — AAII browser-shaped request; 403 permanent.** §2.17.
- **H3 — FMP per-endpoint limits; reject degenerate rows at CSV validation; adapter-contract sentinel catches drift.** adapter docstrings.
- **H4 — Anthropic 529 transient: long-backoff retry in background only; foreground data ops never depend on an LLM (LLM triage removed 2026-05-22).** `feedback_anthropic_529_self_heal`.
- **H5 — SEC EDGAR 10 req/s per-IP; bulk `submissions.zip` sidesteps it; `with_retry` backoff on per-filing crawls.** edgar_adapter.

## I. Schema / PK discipline
- **I1 — No new platform table without operator-approved rationale (readers/writers/why-not-existing) in the migration docstring + PR.** `.claude/rules/migrations.md` (controls-audit §13 #11).
- **I2 — Every platform table has a PK; composite PKs OK; fundamentals_quarterly must move to natural PK (267 dups).** audit §2.1/§2.7.
- **I3 — FKs use NOT VALID → VALIDATE; child writes auto-assign `classification_id` via BEFORE INSERT.** audit §2.8 + §5 step 7.
- **I4 — Idempotent migrations (`IF NOT EXISTS`); replay-from-zero is deterministic.** migrations rule.
- **I5 — Surrogate PKs only for append-only event/audit tables; identity/substrate use immutable natural/smart keys.** v2.2 §1.1.
- **I6 — Issuer layer is design-LOCKED: `issuers` `issuer_id` surrogate PK (CIK/LEI nullable-unique), M:N `issuer_securities` SCD-2 (merger-transfer + share-class fan-out), `issuer_history` issuer SCD-2. "Settled by v2.2 precedent — operator does not need to re-decide" (corp-history spec §3). Do NOT propose CIK-as-PK or dropping issuer_securities.**

## J. Supabase / DB-ops
- **J1 — Pro tier: ~18 GB disk (auto-resized from 8 GB after the 2026-05-23 97% incident — check the UI, assumed caps are stale); ~60-connection IPv4 pooler; 4h resize cooldown; auto read-only at ~95%.** `project_supabase_constraints_2026_05_23`.
- **J2 — `DATABASE_URL_IPV4` = transaction pooler (`statement_cache_size=0`); session/IPv6 mode for DDL + bulk COPY only.** migrations rule.
- **J3 — Chunked DML for >100K-row writes (100K/chunk, commit each, ~0.5s sleep for WAL checkpoint).** supabase-constraints §1. *A single-txn 21M-row UPDATE blew 1.95 GB WAL → 97% disk → read-only (2026-05-23).*
- **J4 — Streaming commits (flush every 100–500 rows), not buffer-then-flush-at-end.** supabase-constraints §2 (a 13K-call buffer-at-end backfill was killed at 3600s → 0 rows committed).
- **J5 — Read-only-event recovery: session READ WRITE override + VACUUM + wait for WAL checkpoint (~5–15 min).** supabase-constraints §3.
- **J6 — No superuser CHECKPOINT; auto-checkpoint every 5 min / 1 GB; keep per-txn WAL small via chunking.** supabase-constraints §2.
- **J7 — TRUNCATE (not DELETE) for the wipe: immediate disk reclaim, no dead-tuple bloat, no VACUUM FULL.** Supabase best-practices.

## K. SACRED series / never re-derive
- **K1 — `hy_spread` (FRED `BAMLH0A0HYM2`, ~7,674 rows 1996→present) is SACRED: never re-fetch/re-derive/force_refresh/overwrite history (operator-stitched multi-source archeology; FRED only serves a recent window). The clean re-ingest MUST copy hy_spread verbatim, not re-pull it.** `project_hy_spread_sacred`.
- **K2 — Sentinel byte-identical sentiment-baseline tests are load-bearing; never exempt without an expert verdict.** CLAUDE.md.

## L. ISO standards (operator standing rule)
- **L1 — ISO/industry standards before custom: ISO 3166-1 α-2 (country), ISO 4217 (currency), ISO 8601 (dates), ISO 7064 Mod-97-10 (check digit), Crockford base32 (alphanumeric).** `feedback_always_use_iso_standards`.
- **L2 — Country = ISO 3166-1 α-2 ('US' not 'USA').** v2.2 §1.2.
- **L3 — Check digit = ISO 7064 Mod-97-10 (LEI/IBAN precedent), not Luhn.** v2.2 §1.4.
- **L4 — Alphanumeric encoding = Crockford base32 (no I/L/O/U).** v2.2 §1.5.
- **L5 — Dates/times = ISO 8601 (`date` / `timestamptz`).** all date columns.
