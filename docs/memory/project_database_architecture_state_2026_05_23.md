---
name: database-architecture-state-2026-05-23
description: "Honest state of the platform.* schema — no foreign keys anywhere, source-named tables convention, derived tables stand apart, table NAME is the source (not a redundant column), and the outstanding work I keep forgetting"
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Standing read for EVERY future session on platform schema work.**

## Conventions (CORRECTED 2026-05-23 — I had it backwards earlier in the session)

- **ONE table per data TYPE, with a `source` column.** Operator 2026-05-23: *"part of the issue now is that the daily prices are in two tables... that is what i meant when i said to add a source column so we know where the data for that day came from... but you put it in different tables instead of one"* + *"could be three... tradier data is somewhere for daily prices"*.

  Canonical example — `platform.prices_daily` already proves the pattern at 3 sources:

  | source | rows (2020-2025) |
  |---|---|
  | tradier | 5.8M |
  | alpaca | 2.3M |
  | fmp | 217K |

  Multiple providers coexist in ONE table. Each row tagged with its `source` provider.

- **Multi-provider domains should follow that pattern.** Wrong today:
  - `sec_insider_transactions` (SEC-only) + accidentally-dropped `insider_filings` (was FMP) → should have been ONE `insider_transactions` table with `source IN ('sec', 'fmp')`
  - Any future cross-source data — DON'T split into per-source tables; ONE table with source column

- **The `<source>_<feed>` name pattern was MY misread earlier in the session.** What the operator actually said: "table name has the source in it" referred to existing legacy names like `sec_insider_transactions` — NOT that we should create more such tables. The canonical pattern is `prices_daily`-style (one table, source column).

- **For FK references:** one canonical parent table per data type makes referential integrity straightforward. Splitting per-source breaks the FK story.

- **Derived/computed tables stand apart.** `ticker_classifications`, `liquidity_tiers`, `insider_mspr_daily`, `fear_greed`, `spread_observations` — OUTPUTS not INPUTS. Their "source" is the computation.

## The schema design hole — NO referential integrity

Operator 2026-05-22: *"how the fuck do you design a database with no referential integrity"*. The platform has NO foreign keys. Every `ticker`-bearing table is implicit-join only. Real consequences:

- `ticker_classifications` drift accumulates (was +41, then +46 today) — producer never DELETEs rows the upstream Alpaca asset list dropped
- Orphan rows persist across tables — a ticker can be in `prices_daily` but absent from `ticker_classifications`, etc.
- Cross-source mixing isn't caught at schema layer

**The right fix** (outstanding, not shipped):
- `ticker_classifications.ticker` becomes canonical PK (already is)
- Every `ticker`-bearing table gets `FOREIGN KEY (ticker) REFERENCES platform.ticker_classifications(ticker) ON UPDATE CASCADE ON DELETE RESTRICT`
- Producer for `ticker_classifications` extended with DELETE-source-tracking: rows NOT in current Alpaca source-set are DELETED in same txn as UPSERT + source_count INSERT
- New `country` column on `ticker_classifications` (TEXT, ISO country code) populated from Alpaca `/v2/assets`

**Critical for coverage metrics:** SEC `sec_insider_transactions` is structurally capped at ~85% T1+T2 STOCK coverage because the ~231 missing tickers are foreign-issuer ADRs exempt from Section 16 per 17 CFR §240.3a12-3(b). With `country` column, partition metric:
- US-subset coverage measured against SEC (target 100%)
- non-US-subset coverage measured against FMP fallback (separate metric)
- DO NOT combine — table name = source, country = subset

## prices_daily corpus — multi-source by era (Tradier-era ≈ Alpaca-era; Alpaca post-2020 drifts from FMP)

- Tradier export ends 2019-12-31, 220k rows, source='tradier'
- Alpaca: 2020-2026 era ~2.3M rows in 2020-2025 + 411K in 2026-01..05-21
- FMP: today 2026-05-21+ default, retroactive backfills in flight

Empirically verified 2026-05-22:
- Tradier vs FMP: 5/5 agree to 0.001% (same algorithm)
- Tradier vs Alpaca pre-2020: 998/1000 agree (median 0.006%, max 2.3%) — same algorithm
- **Alpaca vs FMP 2020+: 3/5 disagree by 1-5%** — Alpaca's adjustment algorithm changed post-2020

The fix: replace Alpaca-post-2020 rows with FMP for the FMP-coverable window. Pre-2020 Tradier-sourced rows stay (they agree with FMP).

**FMP coverage at operator's $200/yr Starter tier: ~16 years for major tickers** (probed: AAPL/JPM/WMT/XOM/IBM all return 2010-06-15 ✓). NOT the 5 years operator initially said — that's a different tier.

## Existing functionality / parameters — read before re-inventing

Standing rule: USE EXISTING STAGES WITH ARGUMENTS. Don't dispatch subagents to build new one-off stages. Common params I keep forgetting:

- `daily_bars` stage: `--param force_refresh=true --param feed=fmp --param universe=active --param lookback_days=N --param end_offset_days=1`. With `--force` to bypass market-open guard.
- `sec_filings` stage bulk mode: `--param _sec_backfill=true` (NOT `--param backfill=true`; that's a kwarg routed differently)
- `classify_tickers` stage override: `--param skip_guard_days=0` (NOT `--param force=true` — that param doesn't exist)
- `macro_indicators` stage override: `--param skip_guard_days=0`

## Standing rules I keep tripping on

- Operator's standing rule "use existing system with arguments" (memory `feedback_ask_expert_then_execute` + recurring 2026-05-22 corrections)
- Operator's standing rule "no one-off stages built by subagents — extend existing infra"
- Operator's standing rule "complete in-progress before pivoting to new instruction" (2026-05-22)
- Operator's standing rule "tables stand alone — table name is source, no unified views with source columns" (2026-05-22)
- Operator's standing rule "country-partition coverage metrics; don't measure US+foreign against same denominator" (2026-05-22)

## Dependency tree — derived tables silently inherit upstream staleness

Operator 2026-05-23: *"so fear and greed is dependent upon the prices daily being updated"* + *"if prices daily fails then fear and greed calculation can fail"*.

**There is NO enforced freshness constraint between source and derived tables.** A stale upstream silently produces a stale downstream. The `recorded_at` reflects when the compute ran, NOT how fresh the inputs were.

The dependency map I keep needing to remember:

- `fear_greed` (DERIVED) depends on:
  - `prices_daily WHERE ticker='SPY'` → `momentum_component`
  - `macro_indicators WHERE indicator='vix'` → `volatility_component`
  - `macro_indicators WHERE indicator='hy_spread'` → `credit_component`
  - `macro_indicators WHERE indicator='yield_curve'` → `safe_haven_component`
  - Compute returns 0 (silent skip) if any of vix/hy/spy/t10 series is empty
- `liquidity_tiers` (DERIVED) depends on `prices_daily` + `spread_observations`
- `insider_mspr_daily` (DERIVED) depends on `sec_insider_transactions`
- `spread_observations` (DERIVED) depends on `prices_daily` (Corwin-Schultz)
- `universe_candidates` (DERIVED) depends on `prices_daily`
- `ticker_classifications` (DERIVED-LITE) should be a SUBSET of `prices_daily.ticker` per operator 2026-05-23 *"if the ticker isn't in daily bars then the ticker doesn't need to be in the ticker classification"*

The producer-level rule: **`ticker_classifications.ticker ⊆ prices_daily.ticker`** — classify_tickers should filter Alpaca's asset list to those-in-prices_daily before upserting.

The freshness-failure pattern: a fear_greed.recorded_at='2026-05-22' row could be computed from VIX as of 2026-05-19. Nothing in the schema catches that.

## Two-tier architecture + refresh priority (operator 2026-05-23)

**Tier 1 — RAW (vendor-fed; refresh PRIORITY 1 daily):**
- prices_daily (FMP primary, Alpaca/Tradier historical, Tradier-export pre-2020)
- corporate_actions (Alpaca)
- sec_insider_transactions (SEC EDGAR Form 4)
- sec_material_events (SEC EDGAR 8-K)
- earnings_events (FMP)
- fundamentals_quarterly (FMP)
- macro_indicators (FRED)
- aaii_sentiment, short_interest (FINRA), social_sentiment (ApeWisdom), borrow_rates (iborrowdesk), options_max_pain (Greeks Pro), insider_sentiment (Finnhub monthly), tradier_options_chains (one-shot historical)

The "current state" IS the raw baseline. New data gets ingested on top; existing rows stay until corporate action or vendor correction.

**Tier 2 — DERIVED (computed from Tier 1; refresh PRIORITY 2 after raw):**
- ticker_classifications (Alpaca asset list filtered to in-prices_daily subset)
- liquidity_tiers (from prices_daily + spread_observations)
- spread_observations (Corwin-Schultz from prices_daily OHLC)
- insider_mspr_daily (from sec_insider_transactions)
- fear_greed (from VIX + hy_spread + yield_curve + SPY)
- universe_candidates (from prices_daily)

**Order of operations every day:**
1. Refresh ALL Tier 1 raw tables (data_operations cron at 21:30 UTC)
2. Refresh Tier 2 derived tables (auto-run after their Tier 1 inputs)
3. Validation suite checks both tiers
4. Engine dispatch reads from raw + derived

**Referential integrity goal** (operator 2026-05-23: *"we need referential integrity on these tables"*):
- ticker_classifications.ticker ⊆ prices_daily.ticker (operator rule — derived from-bars-only)
- All ticker-bearing tables FK → ticker_classifications.ticker (`ON UPDATE CASCADE ON DELETE RESTRICT`)
- Derived tables FK to their raw inputs (enforced parent-child)
- No more silent drift; constraint violation surfaces immediately

## Outstanding architectural debt (deferred, not shipped)

1. **classify_tickers DELETE-source-tracking** — drift will keep growing each Alpaca refresh that drops a ticker
2. **`country` column on `ticker_classifications`** — needed to partition coverage metrics
3. **Foreign-key relationships** across all `ticker`-bearing tables
4. **`fmp_insider_filings`** (proper-named replacement for the accidentally-dropped `insider_filings`) restricted to non-US issuers (SEC is canonical for US)
5. **`.claude/agents/db-architect.md`** drafted but NOT committed — dispatch schema work to this named agent, not generic-purpose
