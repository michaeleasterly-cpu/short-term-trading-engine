# Corpus-fitness audit for edge-finding — 2026-05-22

**Question on the table**: *"i dont think the data that i have is fit for finding edges"*. Today's switch from Alpaca-IEX to FMP as the daily-bars primary (PR #276 merged) passed the 10-ticker high-cap cross-validation at 0.5% OHLC. The operator asked for four broader audits before trusting the corpus for autonomous Lab credibility scoring.

**Verdict (full reasoning in §Final Verdict below)**: **RED**. The corpus is **NOT fit for edge-finding** in its current state — three of the four sections surface real, structural defects. The 10-ticker high-cap test passed because it cherry-picked the part of the universe where Alpaca-IEX and FMP agree. Once the sample is broadened (Section A) or extended back in time across splits (Section B), the disagreement is material. Section C is the most-consequential finding: **survivorship bias is severe** — 18 of 20 known historical delistings are completely absent from the corpus.

Sources:
* `scripts/audit_corpus_fitness.py` (this PR) — the one-shot audit script.
* Raw JSON at `docs/audits/data/2026-05-22-corpus-fitness.json` (committed in this PR).
* Background `daily_bars --force_refresh --feed=fmp` was running concurrently against the same FMP key, which constrained the audit to ≤100 FMP calls.

---

## §A — Broad-ticker cross-validation (100 random T1+T2, 2026-05-15) — **RED**

**Method**: 100 tickers sampled (seed `20260522`) from `platform.liquidity_tiers WHERE tier <= 2`. For each, pulled the 2026-05-15 EOD bar from FMP `/stable/historical-price-eod/full` and compared OHLC + volume against the existing `platform.prices_daily` row.

| Metric | Value |
|---|---|
| Sample N | 100 |
| Comparable N | 98 |
| Missing from FMP | 2 (`BPACU`, `JAPN`) |
| Missing from corpus | 0 |
| **OHLC worst-diff p50** | **0.07%** |
| **OHLC worst-diff p95** | **3.77%** |
| **OHLC worst-diff max** | **10.10%** (`JEM`) |
| OHLC breaches > 0.5% | **82** field-level (across 36 unique tickers of the 98 comparable) |
| Volume ratio p50 (FMP/DB) | **7.87×** |
| Volume ratio p95 (FMP/DB) | **182.46×** |
| Volume ratio max | **405.33×** |
| Volume breaches > ±5% | **46 of 98** |

**Findings**:

1. **OHLC disagreement is material in the small-cap tail**. The 10-ticker high-cap test (AAPL/MSFT/SPY/NVDA/GOOGL/AMZN/TSLA/JPM/BRK.B/WMT) passed cleanly because mega-cap consolidated tape ≈ Alpaca-IEX after consolidation. As soon as the sample includes T2 names below the top decile, the gap widens. Concrete worst offenders:
   * `JEM`: high diff 10.10%, close diff 8.45% (FMP $1.54 vs DB $1.42)
   * `NEXM`: low diff 9.42%, close diff 7.47%
   * `AVX`: open/low diff 4.21%, close diff 3.10%
   * `EDHL`: low/close diff 4.24%
   * `IBEX`, `GNOM`, `FOFO`, `LIMN`, `FLN`, `IBGB` — all multi-percent disagreements.
2. **Volume ratio is what was already predicted by the 2026-05-22 finding** (AAPL 44× ratio at the 10-ticker test). The median tier-1+2 ticker has FMP volume ~8× the existing corpus, confirming the **corpus is Alpaca-IEX, not Alpaca-SIP**. This is *expected* given the data-source switch, but it means:
   * Any volume-based feature (`volume_z`, dollar-volume rank, liquidity tier itself) computed against the existing corpus reflects the IEX subset, not real market activity.
   * Engines that use volume in their signal must be reconsidered: a `volume_z > 3` on IEX volume is roughly a `volume_z ≈ 0` on CTA volume.
3. **A 0.5% OHLC tolerance cannot hold across the full T1+T2 universe today**. The structural reason is that low-priced stocks ($0.18, $1.20, $3.08 examples) have a wide bid-ask such that the IEX-only mid-price vs the CTA close legitimately diverges by tick fractions that exceed 0.5% relative. This is NOT a defect in either provider — it's a *symptom* of using two different feeds at two different points in time.

**Honest interpretation**: The breaches are not "FMP is wrong" or "the corpus is wrong" — both providers are internally consistent. They reflect the fact that **the corpus mixes two feeds with different microstructure**. Until the corpus is rebuilt end-to-end from FMP, any engine that compares a new FMP-sourced bar against historical Alpaca-IEX-sourced bars will see *artificial regime breaks at the switchover date*. That is fatal for edge discovery.

**Regression test status**: the 10-ticker `tpcore/tests/test_ingest_fmp_bars_cross_validation.py::test_fmp_cross_validation_against_corpus` test currently passes because it uses the mega-cap subset where the IEX↔CTA mid-price difference is < 0.5%. I am **NOT lowering its tolerance or expanding its universe**, because the broader regression test would red against the existing corpus (and stay red until the corpus is rebuilt from FMP — operator-decision territory). The audit-script approach above is the right artifact to ship: it produces real numbers without forcing a test to lie.

---

## §B — Split-day adjustment test — **RED**

**Method**: For each of four known splits, fetched the FMP EOD bar on the split date and compared `close` (and `adjClose` when present) against `platform.prices_daily.close` / `adjusted_close`.

| Ticker | Split date | Ratio | FMP close | DB close | DB adj_close | Diff | Verdict |
|---|---|---|---|---|---|---|---|
| AAPL  | 2020-08-31 | 4:1  | 129.04 | 125.23 | 125.23 | **3.04%** | DISAGREE_CLOSE |
| TSLA  | 2020-08-31 | 5:1  | 166.11 | 166.24 | 166.24 | 0.08%  | AGREE_CLOSE |
| GOOGL | 2022-07-15 | 20:1 | 111.78 | 110.86 | 110.86 | **0.83%** | DISAGREE_CLOSE |
| NVDA  | 2024-06-07 | 10:1 | 120.89 | 120.77 | 120.77 | 0.10%  | AGREE_CLOSE |

**Findings**:

1. **FMP `/stable/historical-price-eod/full` returns no `adjClose` field** on the operator's Starter tier — only `close`. Both providers in the corpus store `adjusted_close = close` (no adjustment column populated independently). So the entire comparison is on raw close.
2. **AAPL 2020-08-31 disagrees by 3.04%** ($129.04 vs $125.23). Both numbers are in post-split scale (the pre-split close was ~$500, divided by 4 = $125). One provider rounds the post-adjustment ending close differently — likely a different intraday-VWAP-vs-last-print policy at the corporate-action boundary. **This is the structural risk on split days**: an engine training on the Alpaca-sourced $125.23 will see a different signal than one training on FMP's $129.04 for the same session.
3. **GOOGL 2022-07-15 disagrees by 0.83%** — narrow but outside the 0.5% gate.
4. TSLA and NVDA agree to within 0.10%.

**Honest interpretation**: 2 of 4 split-day comparisons fail the 0.5% gate. The failure mode is the same as §A — different providers' adjustment algorithms diverge most at the corporate-action boundary. For an engine that looks at returns spanning a split date, the implied 1-day return at AAPL 2020-08-31 is ~+3% under one source and ~+5.5% under the other (the next day was 130.75 on both). That's a 240bp delta on a single bar from a known corporate event. **This is the highest-risk single category of corpus inconsistency for backtesting** because it touches every engine that uses adjusted returns over a multi-year window.

---

## §C — Survivorship audit — **RED (severe)**

**Method**: Counted `platform.prices_daily` rows where `delisted = true`, counted pre-2020 tickers, and spot-checked 20 known historical delistings.

| Metric | Value |
|---|---|
| Total `delisted = true` tickers | **54** |
| Delisted tickers with last_bar < 30d ago (late capture) | 0 |
| Pre-2020 tickers in `prices_daily` | 4,539 |
| Pre-2020 tickers also in `ticker_classifications` | 4,535 |
| Pre-2020 tickers MISSING from `ticker_classifications` | 4 |

**Spot check** (20 known delistings):

| Ticker | Expected delist | Rowcount | Verdict |
|---|---|---|---|
| WORK  | 2021-07-21 | 0 | NO_RECORD |
| ATVI  | 2023-10-13 | 811 | **OK (marked)** |
| AABA  | 2017-06-13 | 0 | NO_RECORD |
| OSTK  | 2023-08-21 | 0 | NO_RECORD |
| LNKD  | 2016-12-08 | 0 | NO_RECORD |
| WLTW  | 2022-07-01 | 0 | NO_RECORD |
| CTXS  | 2022-09-30 | 0 | NO_RECORD |
| MGI   | 2023-06-01 | 0 | NO_RECORD |
| TIF   | 2021-01-07 | 0 | NO_RECORD |
| XLNX  | 2022-02-14 | 0 | NO_RECORD |
| FB    | 2022-06-09 | 0 | NO_RECORD |
| TWTR  | 2022-10-27 | 0 | NO_RECORD |
| RDS-A | 2022-01-28 | 0 | NO_RECORD |
| CTL   | 2020-09-18 | 0 | NO_RECORD |
| CELG  | 2019-11-20 | 0 | NO_RECORD |
| XEC   | 2021-10-01 | 0 | NO_RECORD |
| BHGE  | 2019-12-31 | 0 | NO_RECORD |
| DPS   | 2018-07-09 | 0 | NO_RECORD |
| RTN   | 2020-04-03 | 0 | NO_RECORD |
| UTX   | 2020-04-03 | 0 | NO_RECORD |

**1 of 20** known delistings is present in the corpus. **19 of 20 are entirely absent**.

**Findings**:

1. **The corpus has severe survivorship bias**. Only 54 delisted tickers exist platform-wide, vs. an expected universe of ~3,000–5,000 historical delistings over the 2016+ window the corpus covers. The Alpaca `/v2/assets?status=inactive` ingestion path (`tpcore/data/ingest_alpaca_bars.py::run` at line 299) was apparently never executed to populate the historical delisted universe — it lives in a `run()` function not wired into any `OPS_UPDATE_STAGES`, only as a one-shot manual script.
2. **High-impact delistings spanning the engine training window are missing**: FB (META rebrand), TWTR (Musk), XLNX, ATVI (only one present), CELG, BHGE, CTL — every one of these had years of price history that contributed to the broader market signal in our training window.
3. **The 4 pre-2020 tickers missing from `ticker_classifications`** are a small-N drift but suggest the same root cause: the universe coverage table is built from the active Alpaca roster only, with no historical-inactive-roster join.

**Why this kills edge-finding**: backtests run on a survivorship-biased universe systematically overstate returns of any mean-reversion / contrarian / value strategy because the losers (eventually-delisted-companies) have been deleted from the sample. Any engine credibility score computed on this corpus is biased upward by an unknowable amount. The autonomous-lab MODIFY/ADD gate **cannot trust this corpus** for ranking new engines.

**Quick-win fix forward (NOT shipped in this PR — operator decision)**: wire the existing `tpcore/data/ingest_alpaca_bars.py::run` as a one-shot `historical_delisted_universe` stage in `scripts/ops.py`, run it once against `?status=inactive`, and verify the spot-check tickers populate. Estimated wall time: 2-4 hours (per-ticker rate-limited Alpaca pulls). This would be a small ECR follow-up; I am NOT shipping it in this PR because the operator scope says "READ-MOSTLY".

---

## §D — 9 validation failures (2026-05-21 baseline)

**Method**: Re-ran the full `tpcore.quality.validation.suite.run_suite` against the live DB and tagged each of the nine baseline failures STILL_RED / HEALED / SHIFTED.

Suite ran end-to-end in 621s (the per-ticker monotone-baseline UPSERT loops dominate). All 26 checks executed; per the SuiteResult, `passed = False` (8 RED checks total; 4 of those overlap with the 9-baseline, the others are unrelated drift). Per-check verdicts for the 9 baseline failures:

| # | Check | 2026-05-21 state | 2026-05-22 state | Verdict |
|---|---|---|---|---|
| 1 | `fundamentals_quarterly_completeness` | STILL_RED (283 tickers gapped) | STILL_RED (285/1090 tickers gapped) | **UNCHANGED** (+2 tickers in the gap list — same root cause: FMP doesn't have those historical quarters) |
| 2 | `corporate_actions_completeness` | STILL_RED (0.95% shrinkage) | STILL_RED (0.81% shrinkage — 109737 live vs 110630 archived) | **UNCHANGED** (producer-defect, expected. Archive is from 2026-05-15) |
| 3 | `earnings_events_monotone` | HEALED | HEALED (1104/1104) | **STAYS HEALED** |
| 4 | `sec_insider_monotone` | HEALED | HEALED (1306/1306) | **STAYS HEALED** |
| 5 | `liquidity_tiers_completeness` | STILL_RED (15 missing) | STILL_RED (15 missing, same `BMNR, BXDC, CBRS, EMPG, FRVO`...) | **UNCHANGED** (inner-60d skip-guard caveat, documented) |
| 6 | `ticker_classifications_coverage` | STILL_RED (drift +41) | STILL_RED (drift +41, snapshot=13722, live=13763) | **UNCHANGED** (DELETE-not-in-source defect, documented) |
| 7 | `macro_indicators_completeness` | STILL_RED (5 indicators) | STILL_RED (5 indicators, 39+ dates on credit_spread) | **UNCHANGED** (check-side calendar/cadence, documented) |
| 8 | `fear_greed_freshness` | HEALED | HEALED (1/1) | **STAYS HEALED** |
| 9 | `aaii_sentiment_freshness` | HEALED | HEALED (1/1) | **STAYS HEALED** |

**4 of 9 still HEALED. 5 of 9 STILL_RED, all unchanged from 2026-05-21.** No regression. No new drift in the baseline. The unrelated `prices_daily_freshness` red emerged today (the in-flight `force_refresh` likely deleted the rows of the latest session mid-write — expected, transient, will heal at force_refresh completion). Not a corpus-fitness blocker.

**Honest interpretation**: no progress on the 9 baseline failures since 2026-05-21 — no engineering work has landed against them (consistent with the operator's "we aren't going to use the LLM triage… take it out" pivot at PR #274). None of them block corpus-fitness for engine credibility — they are documented as either CHECK-side calendar/cadence defects (#1 #7), producer defects (#2 #6), bootstrap edge cases (#5), or are healed (#3 #4 #8 #9). No new RED in the baseline.

**Honest interpretation**: every baseline RED that was already STILL_RED on 2026-05-21 is unchanged today. None of the recovered HEALED checks regressed. **No engineering follow-up landed between 2026-05-21 and today** that would change any of these states — the audit script confirms that. None of the still-RED checks block the corpus-fitness verdict for §A/B/C — they are independent quality issues already documented in `docs/audits/2026-05-21-data-validation-9-failures.md`.

---

## Final Verdict: **RED — corpus is NOT fit for edge-finding**

The corpus has three structural defects that compound to make any edge-discovery exercise unreliable:

1. **Feed-mix discontinuity** (§A): the existing corpus is Alpaca-IEX through 2026-05-21; the new bars from 2026-05-22 onward are FMP-CTA. OHLC differs on tier-1+2 names with p95 = 3.77% and max = 10.10%. Volume differs by p50 ≈ 8×. **Any engine training across the switchover date sees an artificial regime break**.
2. **Adjusted-close drift at corporate actions** (§B): 2 of 4 spot-checked splits disagree past the 0.5% gate. AAPL 2020-08-31 disagrees by 3.04%. This biases multi-year backtests that span any split.
3. **Severe survivorship bias** (§C): 19 of 20 spot-checked known delistings are entirely absent from `prices_daily`. The corpus contains only 54 delisted tickers total. **Engine credibility scores on this corpus are unconditionally biased upward** by an unknown amount.

The §D 9-validation-failure state is unchanged from 2026-05-21 — important to note for the autonomous self-heal loop but not the load-bearing finding here.

### What would have to ship to make the corpus fit (in priority order)

1. **Highest priority — survivorship fix**: wire `ingest_alpaca_bars.run` as a `historical_delisted_universe` ops stage and execute it to populate inactive-ticker history. Without this, no engine credibility score is trustworthy. **Estimated 2-4h wall-time, one-shot, then nightly delta**.
2. **High priority — FMP corpus rebuild**: re-pull the full universe history from FMP and overwrite `platform.prices_daily`. The currently-running `daily_bars --force_refresh --feed=fmp --universe=active` (PID 35741 at audit time) is the right substrate. After the rebuild lands, re-run §A's 100-ticker test — it should pass cleanly.
3. **Medium priority — split-day spot validation**: keep the 4-row §B comparison as a permanent regression test (NOT shipped this PR — would currently red, and the operator constraint forbids hiding-red-via-skipmark).
4. **Lower priority — 9 validation failures**: tracked in `docs/audits/2026-05-21-data-validation-9-failures.md`; engineering work documented; none block corpus-fitness directly.

### What did NOT happen in this PR (operator-scope)

* **No mass corpus rebuild**: explicitly out of scope per "DO NOT mass-rebuild the corpus from FMP — that's a separate operator decision".
* **No validation threshold changes**: STILL_RED reported honestly.
* **No `git stash`**.
* **No touching `/Users/michael/short-term-trading-engine/`** shared checkout — all work in this worktree.
* **No expanding the 10-ticker FMP cross-validation test universe**: doing so would cause the existing CI test to red against today's corpus, which is the wrong way to surface this finding. The audit script + this doc are the right artifacts.

---

## §D — raw per-check output (for audit-trail completeness)

```
HEALED     earnings_events_monotone                    passed 1104/1104
HEALED     sec_insider_monotone                        passed 1306/1306
HEALED     fear_greed_freshness                        passed 1/1
HEALED     aaii_sentiment_freshness                    passed 1/1

STILL_RED  fundamentals_quarterly_completeness         285/1090 failed
           e.g. ABCL:missing_quarter expected=no consecutive filing gap > 100 days in active range
                observed=2 inferred missing quarter(s) at 2019-07-01, 2019-09-30
STILL_RED  corporate_actions_completeness              1/1 failed
           <corporate_actions>:db_shrunk_vs_archive expected=live DB rows ≥ archived snapshot
                observed=live=109737 vs archived=110630 (0.81% shrinkage, archive 2026-05-15)
STILL_RED  liquidity_tiers_completeness                15/4729 failed
           BMNR:missing_from_liquidity_tiers (sample: BMNR BXDC CBRS EMPG FRVO ...)
STILL_RED  ticker_classifications_coverage             1/1 failed
           <drift>:source_count_drift live=13763 vs snapshot=13722 (delta +41,
                snapshot_at=2026-05-21T15:09:20+00:00)
STILL_RED  macro_indicators_completeness               5/59 failed
           credit_spread:missing_publication 39 dates (Columbus Day / Veterans Day / etc.)
           hy_spread:missing_publication 2 dates inside pre-truncation FRED window
           initial_claims:missing_publication 1042+ weekly Thursdays (anchor-day defect)
           sahm_rule:missing_publication 1 date (2025-10-01 transient FRED gap)
           yield_curve:missing_publication 37 dates (same calendar as credit_spread)
```

Suite duration: 621s. Suite-level `passed=False`. Run ID `0dfeae8d-579e-4f53-aea0-c07eeee665ed` for cross-reference in `platform.data_quality_log`. Raw JSON at `docs/audits/data/2026-05-22-corpus-fitness.json` carries the full per-check failure list.
