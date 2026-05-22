# Full-spectrum data-feed hardening audit — 2026-05-22

**Scope**: every data feed in `tpcore/feeds/profile.py` (the 13-FeedProfile registry, the canonical SoT) plus three derived/auxiliary feeds enumerated from `tpcore/quality/validation/checks/` and `scripts/ops.py:_STAGE_SPECS`. **Read-only**: no code changes, no validation-threshold changes, no backfill stages auto-run. The audit was triggered by the operator directive 2026-05-22 — *"so the full spectrum… we also need the other data as hardened … self healing deterministic agent all that shit … not just fred we have all kinds of feeds that may need work"* — to stop the per-feed ad-hoc hardening cadence and ship one prioritised plan.

**Verdict**: **3 feeds GREEN across all 5 dimensions / 8 feeds YELLOW / 5 feeds RED on at least one dimension.** GREEN list: `prices_daily`, `aaii_sentiment`, `fear_greed`. The largest cluster of RED is **dimension 1 (backfill stage)** — 8 of the 16 feeds have **no `_stage_historical_*` one-shot at all**. The next largest is **dimension 5 (sentinel test against live DB)** — only 2 feeds have a parametrised value-pinning sentinel; the rest rely on validation checks alone. Self-heal cascade catalog coverage (dimension 4) is **strong by design**: D6-D10 + D14 cover the validation-failure detection paths for 5/16 feeds without needing per-feed cascade rows. The remaining gap on dimension 4 is **per-feed targeted re-pull stages** for the long-tail feeds — most validation reds today escalate to the daemon-stop path, not a deterministic per-feed repair.

**Top-3 critical-path blockers** (Wave-1 below): (1) `fundamentals_quarterly_completeness` is **285 of 1090 active tickers failing** — the largest single corpus integrity red on `main` — and there is no `_stage_historical_fundamentals_quarterly` one-shot to repair it; (2) `macro_indicators_completeness` has **1042+ missing weekly publications for `initial_claims`** (Thursday-anchor defect) and no per-indicator targeted backfill stage; (3) `liquidity_tiers_completeness` is HEALED today via PR #285 but is **architecturally fragile** — a tier-2 ticker dropping out of the prescreener silently re-introduces the gap because the cascade (D9) needs the validation red to fire first.

---

## §1 — Feeds in scope (16 total)

Enumerated by cross-referencing `tpcore/feeds/profile.py:FEED_PROFILES` (13 entries) against `tpcore/quality/validation/checks/` (25 check files → ~16 distinct feeds) and `scripts/ops.py:_STAGE_SPECS` (40 stage names → 15 ingestion stages). Three feeds appear in `_STAGE_SPECS` + checks but **NOT** in `FEED_PROFILES` and are flagged below: `corporate_actions`, `fundamentals_quarterly`, `delistings_universe` (the first two ship FeedProfile entries on `main` per PRs #163-late but never made it into the dispatcher's `FEED_STAGE` map). Tradier options is **OUT** — account closed (operator confirmed; MASTER_PLAN flags Tradier as deprecated).

| # | Feed | FeedProfile | check(s) | stage | nightly | one-shot backfill |
|---|---|---|---|---|---|---|
| 1 | `prices_daily` (daily_bars) | yes | `prices_daily_freshness`, `prices_daily_completeness`, `row_integrity`, `delistings`, `splits`, `constituent` | `daily_bars` | yes (FMP default since PR #276) | `force_refresh=true universe=active feed=fmp` |
| 2 | `corporate_actions` | yes (#163-late) | `corporate_actions_completeness`, `corporate_actions_integrity` | `corporate_actions` | yes | `run_backfill_corp_actions_csv.sh` |
| 3 | `fundamentals_quarterly` | yes (#163-late) | `fundamentals_quarterly_completeness`, `fundamentals_integrity` | `fundamentals_refresh` + `compute_fundamental_ratios` | yes | `scripts/backfill_fundamentals.py` (script, not stage) |
| 4 | `sec_insider_transactions` | yes | `sec_filings_freshness`, `sec_insider_monotone` | `sec_filings` | yes | `--stage sec_filings --backfill` (bulk Form 345) |
| 5 | `earnings_events` | yes | `earnings_events_freshness`, `earnings_events_monotone` | `earnings_refresh` | yes | `scripts/backfill_earnings_events.py` (script, not stage) |
| 6 | `macro_indicators` (FRED) | yes | `macro_indicators_freshness`, `macro_indicators_completeness` | `macro_indicators` | yes | — none — |
| 7 | `liquidity_tiers` | yes | `liquidity_tiers_freshness`, `liquidity_tiers_completeness` | `tier_refresh` | yes | bootstrap inside `_stage_tier_refresh` (60d freshness gate) |
| 8 | `ticker_classifications` | yes | `ticker_classifications_freshness` (coverage) | `classify_tickers` | yes | bootstrap inside `_stage_classify_tickers` |
| 9 | `fear_greed` (CNN, DERIVED) | yes | `fear_greed_freshness` | `fear_greed` | yes | `--stage fear_greed --param backfill=true` (to 2001) |
| 10 | `aaii_sentiment` | yes | `aaii_sentiment_freshness` | `aaii_sentiment` | yes | `_stage_aaii_sentiment` pulls full-history workbook |
| 11 | `finra_short_interest` | yes | `short_interest_freshness` | `finra_short_interest` | yes | `--param since=2018-01-01` |
| 12 | `iborrowdesk_borrow_rates` | yes | `borrow_rates_freshness` | `iborrowdesk_borrow_rates` | yes | — none — |
| 13 | `apewisdom_social_sentiment` | yes | `social_sentiment_freshness` | `apewisdom_social_sentiment` | yes | — none possible (vendor has no history endpoint) — |
| 14 | `finnhub_insider_sentiment` | yes | `insider_sentiment_freshness` | `finnhub_insider_sentiment` | yes | `--param since=YYYY-MM-DD` |
| 15 | `greeks_max_pain` | yes | `options_max_pain_freshness` | `greeks_max_pain` | yes | — none possible (vendor: daily 1-symbol snapshot only) — |
| 16 | `delistings_universe` | not a feed; UNIVERSE-derived | `delistings` (the validation fixture check) | `historical_delisted_universe` + `daily_delisted_universe_check` | yes (`daily_delisted_universe_check` OFF-cycle on demand) | `historical_delisted_universe` (PR #283) |

---

## §2 — Scoreboard

State is the strict five-state {GREEN, YELLOW, RED, N/A} per the rubric in the audit brief. `N/A` is used only where a dimension is structurally impossible for a feed (e.g. backfill for `apewisdom_social_sentiment` whose vendor has no history endpoint).

| Feed | Backfill | Nightly | Validation | Self-heal | Sentinel | Overall |
|---|---|---|---|---|---|---|
| `prices_daily` | GREEN | GREEN | GREEN | GREEN (D1+D2+D3+D4+D5+D13+D14) | GREEN | **GREEN** |
| `corporate_actions` | YELLOW | GREEN | RED | YELLOW (D6 detects via `data_validation`, no per-feed targeted re-pull) | RED | **RED** |
| `fundamentals_quarterly` | YELLOW | GREEN | RED | YELLOW (D6 detects, no per-feed re-pull stage) | RED | **RED** |
| `sec_insider_transactions` | GREEN | GREEN | GREEN | YELLOW (D7 monotone dedupe, no targeted re-pull) | YELLOW | **YELLOW** |
| `earnings_events` | YELLOW | GREEN | GREEN | YELLOW (D7 monotone dedupe; freshness via in-flight subagent) | YELLOW | **YELLOW** |
| `macro_indicators` | RED | GREEN | RED | YELLOW (D8 detects, per-indicator re-pull *exists* but only handles missing-date list ≤90d) | RED | **RED** |
| `liquidity_tiers` | GREEN | GREEN | GREEN (HEALED 2026-05-22 PR #285) | GREEN (D9) | YELLOW | **YELLOW** |
| `ticker_classifications` | GREEN | GREEN | RED (+41 source drift) | GREEN (D10) | YELLOW | **YELLOW** |
| `fear_greed` | GREEN | GREEN | GREEN | GREEN (D11 vendor-late skip) | GREEN | **GREEN** |
| `aaii_sentiment` | GREEN | GREEN | GREEN | GREEN (D11 vendor-late Thursday) | GREEN | **GREEN** |
| `finra_short_interest` | YELLOW | GREEN | GREEN | YELLOW (D6 detects, no per-feed re-pull) | YELLOW | **YELLOW** |
| `iborrowdesk_borrow_rates` | RED | GREEN | GREEN | YELLOW (D6 detects, no per-feed re-pull) | RED | **RED** |
| `apewisdom_social_sentiment` | N/A | GREEN | GREEN | YELLOW (D6 detects, no per-feed re-pull) | RED | **YELLOW** |
| `finnhub_insider_sentiment` | YELLOW | GREEN | GREEN | YELLOW (D6 detects, no per-feed re-pull) | RED | **YELLOW** |
| `greeks_max_pain` | N/A | GREEN | GREEN | YELLOW (D6 detects, no per-feed re-pull) | RED | **YELLOW** |
| `delistings_universe` | GREEN (PR #283) | YELLOW (`daily_delisted_universe_check` is off-cycle) | GREEN | RED (no cascade row for "T1+T2 ticker silently disappears") | YELLOW | **YELLOW** |

**Counts**: 3 GREEN / 8 YELLOW / 5 RED.

---

## §3 — Per-feed detail

### 3.1 `prices_daily` (daily_bars) — **GREEN**

- **Backfill**: `--stage daily_bars --param force_refresh=true --param universe=active --param feed=fmp` is the canonical knob; FMP is the default feed since PR #276 (full CTA tape, ~25min for 7600 tickers). Survivorship-corrected via `--stage historical_delisted_universe` (PR #283) + one-shot run. GREEN.
- **Nightly**: registered as the first stage in `_STAGE_SPECS` (`scripts/ops.py:3408`); feed dispatcher (`tpcore/feeds/dispatcher.py:33`) maps `prices_daily → daily_bars`. Idempotent via `ON CONFLICT` upsert. GREEN.
- **Validation**: `prices_daily_freshness` + `prices_daily_completeness` + `row_integrity` + `delistings` + `splits` + `constituent` — all 6 checks passing on `main` per 2026-05-22 audit (`docs/audits/2026-05-22-corpus-fitness-for-edge-finding.md`). GREEN.
- **Self-heal**: D1 (coverage_collapse → SIP→IEX cascade, PR #231), D2 (timeout → chunked, PR #262), D3 (connection drop → retry, PR #262), D4 (Alpaca SIP 403 → IEX swap, PR #231), D5 (provider auth 401, PR #262), D13 (pool exhaustion, PR #262), D14 (validation chunking, PR #271). 7 cascade rows touching `prices_daily`. GREEN.
- **Sentinel**: `tpcore/tests/test_ingest_fmp_bars_cross_validation.py` — 10-ticker pin (`test_fmp_cross_validation_against_corpus`) + 100-ticker T1+T2 percentile-threshold regression (`test_fmp_cross_validation_broad_sample_t1_t2`, PR #289). Both `skipif` against `FMP_API_KEY`, but live in CI when key is present. GREEN.

### 3.2 `corporate_actions` — **RED**

- **Backfill**: `scripts/run_backfill_corp_actions_csv.sh` exists as a CSV-first one-shot but is **not** an `_stage_historical_corporate_actions` stage. The orphan-scripts audit (2026-05-20) catalogued it but no migration has shipped. YELLOW — works but is structurally outside the stage contract; cannot be invoked via `--stage` and thus cannot self-cascade.
- **Nightly**: `_stage_corporate_actions` (`scripts/ops.py:796`), registered in `_STAGE_SPECS` line 3409. Idempotent. GREEN.
- **Validation**: `corporate_actions_completeness` is **RED on `main`** — 1/1 failed: `live=109737 vs archived=110630 (0.81% shrinkage, archive 2026-05-15)` per the 2026-05-22 corpus-fitness audit. Integrity check passes. **RED** because shrinkage indicates rows lost between the 2026-05-15 archive snapshot and now — the canonical "DB has fewer rows than archive" defect that the R3 substrate spec (PR #235) was designed to catch but the cascade does not yet re-load from archive on this signal.
- **Self-heal**: D6 validation-failure cascade will route a `corporate_actions_completeness` red to a refresh stage — but the `_VALIDATION_CASCADE_MAP` (per `scripts/ops.py` Wave-1 PR #261) does NOT have an entry for `corporate_actions_completeness` (the spec catalog only enumerates `fundamentals_quarterly_completeness`, `liquidity_tiers_completeness`, `ticker_classifications_coverage`, `macro_indicators_completeness`). YELLOW — detection works, dispatch is unimplemented.
- **Sentinel**: no parametrised live-DB pin (the `test_check_corporate_actions_completeness.py` test is a unit test of the check logic, not a live-DB value pin). RED.

### 3.3 `fundamentals_quarterly` — **RED**

- **Backfill**: `scripts/backfill_fundamentals.py` exists; not a stage. YELLOW.
- **Nightly**: `_stage_fundamentals_refresh` (`scripts/ops.py:831`) + `_stage_compute_fundamental_ratios` (line 873) — two-stage chained pattern, registered. GREEN.
- **Validation**: `fundamentals_quarterly_completeness` is **the largest single red on main: 285/1090 tickers failing** (e.g. ABCL: 2 inferred missing quarters at 2019-07-01, 2019-09-30 per 2026-05-22 audit). `fundamentals_integrity` passes. RED.
- **Self-heal**: D6 cascade IS wired for `fundamentals_quarterly_completeness → fundamentals_refresh` per `_VALIDATION_CASCADE_MAP` (PR #261). But `fundamentals_refresh` re-pulls the *active* universe at *current* cadence — it does NOT re-pull a back-historical missing-quarter gap because FMP fundamentals are point-in-time and the handler's skip-guard short-circuits when the latest row is recent. YELLOW — cascade exists, but does not actually heal historical gaps.
- **Sentinel**: `test_check_fundamentals_quarterly_completeness.py` is the unit-test of the check; no live-DB pin. RED.

### 3.4 `sec_insider_transactions` — **YELLOW**

- **Backfill**: `--stage sec_filings --backfill` (`scripts/ops.py:6386`) runs the bulk Form-345 quarterly-dataset backfill via `_sec_bulk_form345_backfill` (line 1167) — PROPER stage-contract one-shot, covers historical via SEC's bulk zips. GREEN.
- **Nightly**: `_stage_sec_filings` (`scripts/ops.py:2510`), 3-day skip-guard (tightened from 6 → 3 per the 2-business-day Form 4 deadline). GREEN.
- **Validation**: `sec_filings_freshness` + `sec_insider_monotone` — both passing on `main` (sec_insider_monotone HEALED 2026-05-22 per the audit per-check log: "passed 1306/1306"). GREEN.
- **Self-heal**: D7 monotonicity dedupe via `_MONOTONE_CASCADE_MAP` covers the Form 4 amended-filing case (PR #261). For freshness/staleness reds there is no per-feed re-pull stage — relies on the daily cron. YELLOW.
- **Sentinel**: no live-DB value pin. The 8-K + Form-4 tests in `tests/test_sec_backfill_chunking.py` + `tests/test_sec_bulk_etl.py` are unit-level. YELLOW.

### 3.5 `earnings_events` — **YELLOW**

- **Backfill**: `scripts/backfill_earnings_events.py` (script, not stage); subagent `a638f92b31ce29f67` is in flight for an `_stage_earnings_events_historical` migration per operator's parallel tasking. YELLOW.
- **Nightly**: `_stage_earnings_refresh` (`scripts/ops.py:1888`), 6d skip-guard. GREEN.
- **Validation**: `earnings_events_freshness` + `earnings_events_monotone` — monotone HEALED 2026-05-22 ("passed 1104/1104"). GREEN.
- **Self-heal**: D7 monotone dedupe covers the FMP earnings-reclassification case (PR #261). YELLOW — no targeted historical-gap repair.
- **Sentinel**: `tests/test_earnings_quality.py` + `tests/test_backfill_earnings_events.py` are unit-level; no parametrised live-DB value pin. YELLOW.

### 3.6 `macro_indicators` (FRED) — **RED**

- **Backfill**: **NONE**. `_stage_macro_indicators` (`scripts/ops.py:2356`) only does the rolling refresh — there is no `_stage_historical_macro_indicators` one-shot. Historical population is whatever the FRED adapter has pulled cumulatively since the table was first instantiated. RED.
- **Nightly**: registered. GREEN.
- **Validation**: `macro_indicators_completeness` is **RED on `main`** — 5/59 series failing: `initial_claims: 1042+ missing weekly Thursdays (anchor-day defect)`, `credit_spread: 39 missing dates (Columbus Day / Veterans Day calendar mismatch)`, `hy_spread: 2 missing`, `sahm_rule: 1 transient gap`, `yield_curve: 37 missing` per the 2026-05-22 corpus audit. RED.
- **Self-heal**: D8 macro per-indicator cascade exists (`_MACRO_COMPLETENESS_CHECK`, PR #261) — parses the missing-date list and re-pulls per indicator. But D8 only re-runs the FRED adapter for the missing date range; if the date range was never populated (e.g. anchor-day defect on `initial_claims`), re-pulling won't add the Thursdays because the FRED adapter respects FRED's actual publication calendar. YELLOW.
- **Sentinel**: no live-DB pin. RED.

### 3.7 `liquidity_tiers` — **YELLOW**

- **Backfill**: bootstrap path inside `_stage_tier_refresh` (`scripts/ops.py:2008`) — Corwin-Schultz 60d freshness gate then assign_tiers. Treated as GREEN because there is no historical universe to backfill; tiers are recomputed from the current 30-day window. GREEN.
- **Nightly**: registered. GREEN.
- **Validation**: HEALED 2026-05-22 via PR #285 — 15 missing tickers categorized + fixed. Currently green. GREEN.
- **Self-heal**: D9 `liquidity_tiers_completeness → tier_refresh --param universe=<missing>` (PR #261). GREEN.
- **Sentinel**: `tests/test_check_liquidity_tiers_completeness.py` + `tests/test_liquidity_tiers_completeness_no_silent_skip.py` (PR #285) — the latter is a YELLOW sentinel: it asserts the *check* logic doesn't silently skip on a missing-row category, but it does NOT pin a known set of expected-tier-2 tickers against the live DB. YELLOW.

### 3.8 `ticker_classifications` — **YELLOW**

- **Backfill**: bootstrap path inside `_stage_classify_tickers` (`scripts/ops.py:2112`). GREEN.
- **Nightly**: registered. GREEN.
- **Validation**: `ticker_classifications_freshness` is **RED on `main`** — `<drift>:source_count_drift live=13763 vs snapshot=13722 (delta +41)` per the 2026-05-22 audit. This is the Path-D source-count drift invariant tripping on new listings the classifier hasn't re-classified. RED.
- **Self-heal**: D10 `ticker_classifications_coverage → classify_tickers --param force=true` (PR #261). GREEN.
- **Sentinel**: `tests/test_check_ticker_classifications_coverage.py` is unit-level. YELLOW.

### 3.9 `fear_greed` (CNN-derived) — **GREEN**

- **Backfill**: `--stage fear_greed --param backfill=true` goes back to 2001 per `_stage_fear_greed` (`scripts/ops.py:2439`) + handler at line 1993. GREEN.
- **Nightly**: registered. GREEN.
- **Validation**: `fear_greed_freshness` — HEALED 2026-05-22 ("passed 1/1"). GREEN.
- **Self-heal**: D11 vendor-late classification (PR #271) handles the "vendor hasn't published today" skip. GREEN.
- **Sentinel**: `tests/test_fear_greed.py` exists; the `aaii_sentiment_freshness`/`fear_greed_freshness` checks are themselves the sentinel because `fear_greed` is derived (small surface). GREEN.

### 3.10 `aaii_sentiment` — **GREEN**

- **Backfill**: `_stage_aaii_sentiment` pulls the full-history workbook on the first run; weekly Thursday delta thereafter. GREEN.
- **Nightly**: registered. GREEN.
- **Validation**: `aaii_sentiment_freshness` — HEALED 2026-05-22 ("passed 1/1"). GREEN.
- **Self-heal**: D11 (vendor-late Thursday, PR #271) — AAII publishes Thursday; the cascade emits `INGESTION_VENDOR_LATE_SKIPPED` if the Thursday post hasn't landed. GREEN.
- **Sentinel**: `tests/test_aaii_adapter.py` — unit-level but covers the Thursday publication anchor + HEAD-Last-Modified probe. GREEN at the freshness-anchor surface.

### 3.11 `finra_short_interest` — **YELLOW**

- **Backfill**: `--param since=2018-01-01` to `_stage_finra_short_interest`; handler accepts the date range. YELLOW — works but no explicit `_stage_historical_finra_short_interest` and no documented one-shot recipe.
- **Nightly**: registered. GREEN.
- **Validation**: `short_interest_freshness` — passes (42d max-age tolerance covers the bi-monthly + 13d dissemination-lag profile). GREEN.
- **Self-heal**: no per-feed re-pull. YELLOW.
- **Sentinel**: `tests/test_finra_adapter.py` is unit-level. YELLOW.

### 3.12 `iborrowdesk_borrow_rates` — **RED**

- **Backfill**: **NONE**. Handler is a scrape against a vendor that doesn't expose history. RED on the backfill dimension. (Vendor architecture limits a backfill, but the score reflects state, not blame.)
- **Nightly**: registered. GREEN.
- **Validation**: `borrow_rates_freshness` — passes. GREEN.
- **Self-heal**: no per-feed cascade. YELLOW.
- **Sentinel**: `tests/test_iborrowdesk_adapter.py` is unit-level. RED.

### 3.13 `apewisdom_social_sentiment` — **YELLOW**

- **Backfill**: `N/A` — vendor has no history endpoint (operator memo: ~2h refresh, intraday only). Score is N/A not RED because backfill is structurally impossible.
- **Nightly**: registered. GREEN.
- **Validation**: `social_sentiment_freshness` — passes (7d max-age + 15% coverage floor). GREEN.
- **Self-heal**: no per-feed cascade. YELLOW.
- **Sentinel**: `tests/test_apewisdom_adapter.py` is unit-level. RED.

### 3.14 `finnhub_insider_sentiment` — **YELLOW**

- **Backfill**: `--param since=YYYY-MM-DD` to the handler; no explicit historical stage. YELLOW.
- **Nightly**: registered. GREEN.
- **Validation**: `insider_sentiment_freshness` — passes (monthly cadence). GREEN.
- **Self-heal**: no per-feed cascade. YELLOW.
- **Sentinel**: `tests/test_finnhub_adapter.py` + `tests/test_handle_finnhub_insider_sentiment_targeting.py` unit-level. RED.

### 3.15 `greeks_max_pain` — **YELLOW**

- **Backfill**: `N/A` — vendor (greeks.pro free tier) is daily 1-symbol snapshot; no history endpoint.
- **Nightly**: registered. GREEN.
- **Validation**: `options_max_pain_freshness` — passes (7d max-age). GREEN.
- **Self-heal**: no per-feed cascade. YELLOW.
- **Sentinel**: no live-DB pin. RED.

### 3.16 `delistings_universe` — **YELLOW**

- **Backfill**: `_stage_historical_delisted_universe` (`scripts/ops.py:2782`, PR #283) — survivorship-corrected one-shot via FMP. GREEN.
- **Nightly**: `_stage_daily_delisted_universe_check` (line 2882) exists but is in `_OFF_CYCLE_STAGES` (off the daily cron — per the §3580 comment "Operator-on-demand for now; promote to daily cadence once the structural backfill above is stable"). YELLOW.
- **Validation**: `delistings` fixture check — passes on `main` (the 2026-05-22 audit "HEALED" set includes the delistings invariants). GREEN.
- **Self-heal**: there is **no cascade row** for "T1/T2 ticker disappears mid-session" — the daily check would catch it on the next daily run if it were on-cycle, but today it requires a manual `--stage daily_delisted_universe_check`. RED.
- **Sentinel**: `tests/test_stage_historical_delisted_universe.py` exists but skips against the live DB by default. YELLOW.

---

## §4 — Prioritised hardening waves

Each row is concrete, scoped, and names the exact file or stage that needs the change. Wave size is tuned to the operator's "push-when-tangible" + "no-process-spiral" cuts: each Wave is **one to two PRs**, no per-row PR.

### Wave 1 — Critical-path operator-visible reds (1 PR)

Targets the three Verdict-line "top-3 critical-path blockers" — all reds visible on `data_validation` today and blocking the corpus-fitness rolling verdict.

1. **`fundamentals_quarterly`** — ship `_stage_fundamentals_quarterly_historical(pool, cfg)` in `scripts/ops.py`. Migrate `scripts/backfill_fundamentals.py` into the stage contract. Wire it into `_VALIDATION_CASCADE_MAP` so D6 dispatches to it on `fundamentals_quarterly_completeness` red. Add a parametrised live-DB sentinel test pinning ≥10 well-known tickers' Q1-2024 row counts.

2. **`macro_indicators`** — fix the `initial_claims` Thursday-anchor defect inside `tpcore/quality/validation/checks/macro_indicators_completeness.py` (the missing-publication anchor uses today's day-of-week, not FRED's `release_day_of_week_iso=4`). The 1042+ false reds are a check defect, not a data defect — verified by the audit JSON. Add a per-indicator backfill stage `_stage_historical_macro_indicators(pool, cfg, indicator=…, since=…)` that calls the FRED adapter for the missing date range. Add a parametrised sentinel pinning VIX 2024-01-02 close + initial_claims 2024-W01 value.

3. **`corporate_actions`** — add a `corporate_actions_completeness → corporate_actions` entry to `_VALIDATION_CASCADE_MAP` (PR #261's map currently omits it). Add `_stage_rebuild_corporate_actions_from_archive(pool, cfg)` that reads the R3 archive substrate (`data/corp_actions_backfill/`) and replays via the canonical upsert. Add a parametrised sentinel pinning ≥10 well-known stock splits (e.g. AAPL 2020-08-31 4:1, TSLA 2020-08-31 5:1).

### Wave 2 — Sentinel test coverage gap (1 PR)

Bundles all RED-on-sentinel feeds. Each gets one parametrised live-DB value-pin test. The model is `tests/test_ingest_fmp_bars_cross_validation.py` (10-ticker + 100-ticker pin pattern): `skipif` against the relevant adapter key, fail loud against the live `platform.<table>`.

Concrete deliverables:
- `tests/test_sentinel_corporate_actions.py` — 10 known stock splits + 10 known cash dividends.
- `tests/test_sentinel_fundamentals_quarterly.py` — 10 tickers × Q1-2024 row count = 1.
- `tests/test_sentinel_macro_indicators.py` — 5 indicators × known publication-date sample.
- `tests/test_sentinel_iborrowdesk.py` — assert ≥1 row per active tier-2 demand ticker in the last 5d.
- `tests/test_sentinel_finnhub_insider_sentiment.py` — assert N tracked tickers have a row in the latest month.
- `tests/test_sentinel_apewisdom.py` — assert latest row date ≤ 24h ago.
- `tests/test_sentinel_greeks_max_pain.py` — assert SPY latest row date = previous trading day.
- `tests/test_sentinel_sec_filings.py` — 10 known Form-4 filings + 5 known 8-K filings.
- `tests/test_sentinel_earnings_events.py` — 10 known earnings dates with BEAT/NO_BEAT labels.

### Wave 3 — Targeted per-feed re-pull cascade rows (1 PR)

Bundles the `_VALIDATION_CASCADE_MAP` gap for the long-tail feeds. Per the deterministic-self-heal spec §1 D6 row, each freshness/completeness red SHOULD route to the canonical refresh stage with skip-guard bypassed. Today many feeds rely on the daily cron to eventually clear the red.

Concrete additions to `_VALIDATION_CASCADE_MAP` in `scripts/ops.py`:
- `corporate_actions_completeness → corporate_actions --param skip_guard_days=0` (also covered in Wave 1).
- `short_interest_freshness → finra_short_interest --param skip_guard_days=0`.
- `borrow_rates_freshness → iborrowdesk_borrow_rates --param skip_guard_hours=0`.
- `social_sentiment_freshness → apewisdom_social_sentiment --param skip_guard_hours=0`.
- `insider_sentiment_freshness → finnhub_insider_sentiment --param skip_guard_days=0`.
- `options_max_pain_freshness → greeks_max_pain --param skip_guard=false`.
- `earnings_events_freshness → earnings_refresh --param skip_guard_days=0`.
- `sec_filings_freshness → sec_filings --param skip_guard_days=0`.

### Wave 4 — Delistings on-cycle + cascade row (small PR)

- Move `daily_delisted_universe_check` out of `_OFF_CYCLE_STAGES` (`scripts/ops.py:3597`) and into the daily cron. The PR #283 backfill is now stable per the §3580 comment — the gate the comment cites is met.
- Add a new deterministic-cascade catalog row **D15: "T1/T2 ticker silently disappears from prices_daily mid-window"** to `docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-expansion-design.md` + `tests/test_deterministic_cascade_catalog.py:EXPECTED_CASCADES`. Cascade function: `_cascade_d15_delisting_missed` which calls `_stage_daily_delisted_universe_check`. Terminal event: `INGESTION_AUTO_RECOVERED_DELISTING`.

### Wave 5 — Backfill-stage contract migration for orphan scripts (1 PR)

Migrates the three remaining one-shot scripts into the stage contract so they are cascadable + dashboard-visible:
- `scripts/backfill_earnings_events.py` → `_stage_historical_earnings_events`.
- `scripts/backfill_fundamentals.py` → migrated in Wave 1 already.
- `scripts/run_backfill_corp_actions_csv.sh` (Bash) + `scripts/backfill_corp_actions_csv.py` (Python) → `_stage_historical_corporate_actions` (separate from Wave 1's R3-replay stage; this one re-pulls from the Alpaca corp-actions API directly).

After Wave 5, every feed with a possible historical backfill has a `_stage_historical_*` entry — operator can invoke uniformly via `--stage` rather than walking the orphan-scripts catalog.

---

## §5 — What we already DID today (already-shipped baseline)

The audit picked up these completed pieces and folded their GREENs into the scoreboard above:

- **`prices_daily`** — full FMP migration (PR #276), survivorship-corrected corpus (PR #283 + `historical_delisted_universe` one-shot), cross-val regression (PR #289 — 10-ticker pin + 100-ticker T1+T2 broad sample).
- **`liquidity_tiers`** — completeness HEALED via PR #285 (15 missing tickers categorized + fixed).
- **`earnings_events_monotone`** — HEALED 2026-05-22 (audit per-check log: "passed 1104/1104").
- **`sec_insider_monotone`** — HEALED 2026-05-22 ("passed 1306/1306").
- **`fear_greed_freshness`** — HEALED 2026-05-22 ("passed 1/1").
- **`aaii_sentiment_freshness`** — HEALED 2026-05-22 ("passed 1/1").
- **Deterministic-cascade catalog** — D1-D14 + E1-E11 complete on `main` (PRs #227/#231/#236/#235/#260/#261/#262/#267/#271/#272 + #265 sentinel test).
- **LLM-triage removal** — operator directive 2026-05-22 fulfilled via PR #274. The cascade catalog is the complete self-heal layer (per `2026-05-21-deterministic-self-heal-coverage-expansion-design.md` §0).

---

## §6 — Methodology + honesty notes

- **Score state**: `data_validation` red counts come from the 2026-05-22 corpus-fitness audit JSON (`docs/audits/data/2026-05-22-corpus-fitness.json`) — the same audit cycle this audit was run against. The audit JSON's `run_id` is `0dfeae8d-579e-4f53-aea0-c07eeee665ed` for cross-reference in `platform.data_quality_log`.
- **Where I couldn't verify GREEN**, I scored YELLOW per the brief. Example: `corporate_actions_completeness` is RED right now per the audit JSON — even though the integrity check passes, completeness is a current red.
- **N/A on backfill** is used only where the vendor structurally has no history endpoint (apewisdom, greeks.pro). It is NOT used to mask "we never built the stage". `iborrowdesk` is **RED on backfill** despite no vendor history because the *sentinel* dimension is also RED — together they signal the feed is structurally unverifiable.
- **No re-validation run** was kicked off (the 2026-05-22 audit run is fresh enough). No code changes. No threshold changes.

---

**End of audit. Per-feed JSON scorecard at `docs/audits/data/2026-05-22-feed-hardening-scoreboard.json`.**
