# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

## Engine structural redesign (post-2026-05-15 sweep)

The 2026-05-15 parameter sweeps validated the targeted fixes (Sigma SPY-
regime filter, Reversion Z-relaxation + T3 expansion) at the metric level
but DSR/credibility gates remain structurally blocked.

- **Sigma structural redesign.** 2026-05-15 sweep with regime filter
  applied: 80% of walk-forward Sharpe rows are negative (-3.265 to
  +1.454, median -0.666). The regime filter eliminated the −0.84
  parameter-stability swing — that win is real — but the underlying
  range-scalping signal is fragile across most market windows. Held-back
  +0.839 Sharpe / 86 trades / credibility 50 / DSR 0.0000. The next
  experiment is NOT more parameter sweeps. Candidate redesigns: (a) shift
  from band-touch entries to band-mean-reversion confirmations (require
  close back inside band before entry); (b) require explicit volatility-
  contraction prerequisite (BB-width percentile rank < N before entry);
  (c) abandon range-scalping for trend-pullback if the market structure
  is fundamentally different from the 2018-2023 calibration window.
  Decision deferred until operator picks a redesign path.
  **OU mean-reversion gate spike — rejected 2026-05-15.** Tested as one
  candidate redesign path; 50-trial walk-forward sweep showed the gate
  cut more trades in stable windows than fragile ones, regressing held-
  back Sharpe +0.839 → +0.366. Code archived in
  `tpcore/backtest/spread_estimator_archive.py`.

- **Reversion — reclassified as satellite 2026-05-15 (closed).** The
  signal-class-redesign decision was resolved by reclassifying Reversion
  as a satellite engine alongside S2: permanent 5–10% capital cap,
  per-trade graduation criteria, DSR gate retired. The combined filter
  (Z ≥ 3.0 + HIGH earnings quality) produces 19 trades / Sharpe +0.312
  / PF 1.755 / max DD −11.5% on 2018-2025 — strong per-trade metrics at
  a structurally bounded firing rate. See `docs/MASTER_PLAN.md` §4.2 and
  `backtests/reversion_satellite_backtest.json`.

## Data archival — CSV-first retrofit (DONE 2026-05-15)

**Closed.** The 2026-05-15 BAMLH0A0HYM2 incident exposed that the
CSV-first sub-protocol was implemented for only one handler. Rather
than patch FRED alone, all five ingest handlers were retrofitted to a
shared archive layer.

**Shipped:**
- `tpcore/ingestion/csv_archive.py` — shared write + gzip + shrinkage
  detection. 8 unit tests including the BAMLH0A0HYM2 truncation
  scenario (7,500 → 785 rows → `over_threshold=True`).
- All 5 handlers write a gzipped CSV archive before/after the DB
  upsert: `handle_macro_indicators`, `handle_daily_bars`,
  `handle_corporate_actions`, `handle_fundamentals_refresh`,
  `_stage_catalyst_refresh`.
- **Shrinkage detection** (the vendor-truncation alarm) is wired into
  the two *full-snapshot* sources only — `fred_macro` and
  `alpaca_corporate_actions` — which re-pull their entire history every
  run, so a row-count drop unambiguously means truncation. The three
  *incremental* sources (`alpaca_daily_bars`, `fmp_fundamentals`,
  `fmp_catalyst_events`) pull a variable window each run, so row-count
  shrinkage there is noise — they get the audit-trail archive but no
  alarm (a full-table baseline would false-flag their next incremental
  run; this was caught and corrected during the build).
- `scripts/dump_baseline_archives.py` — seeds baseline snapshots for
  the two full-snapshot sources so shrinkage detection has a real
  predecessor from run 1. Run once 2026-05-15.

**Remaining (deferred, low severity):** re-grade the FRED row of the
compliance matrix in `docs/superpowers/pipelines/data_adapter_pipeline.md`
to reflect the now-implemented CSV-first path.

---

### Original incident write-up (kept for context)

The 2026-05-15 BAMLH0A0HYM2 incident (FRED retroactively truncated the
HY OAS series to a rolling 3-year window, erasing ~1996–2023 history)
exposed the one place the documented CSV-first sub-protocol
(`docs/superpowers/pipelines/data_adapter_pipeline.md`) isn't actually
implemented end-to-end.

**What's already in place:** Heavy one-shot backfills for Alpaca bars,
FMP fundamentals, FMP corporate actions, and SEC EDGAR all write
gzipped CSVs to `data/<source>_backfill/` before loading. Those
archives are auditable and replayable — that's how the 2026-05-13
Supabase Pro-tier cutover survived.

**The gap:** `tpcore/ingestion/handlers.py:handle_macro_indicators`
goes API → memory → `INSERT` with no CSV snapshot, and there is no
`scripts/backfill_fred_csv.py` analogue to `backfill_alpaca_csv.py`.
Every FRED row in `platform.macro_indicators` is therefore single-
sourced from Postgres. If FRED truncates another series (Sahm rule,
INDPRO, IC4WSA, T10Y2Y, BAA10Y are all still exposed) we have no local
fallback — same failure mode that just hit BAMLH0A0HYM2.

**How the existing 17,500 rows got there:** Commit `9233bb0` (2026-05-15
BAA10Y swap) re-ran the daily handler with `start_date=1996-01-01,
skip_guard_days=0`. That's a 17,500-row historical pull on the same
code path used for the weekly ~30-row delta — well over the 500-row
CSV-first threshold the pipeline doc itself sets (line 34). The
shortcut was: flag-bomb the daily handler instead of writing a proper
backfill script. Same pattern as the other heavy sources (Alpaca, FMP,
corp_actions, SEC) — FRED is the one source where that step was
skipped and never corrected.

**Compliance-matrix lie to fix in the same sprint:** Line 92 of
`docs/superpowers/pipelines/data_adapter_pipeline.md` rates `fred`
ingest as ✅ 5/5 LIVE. The ✅ was awarded based on the 2026-05-14
first-ingest run (3,509 rows in 11.4s) — small enough to plausibly
sit under the trivial-pull carve-out. But yesterday's 17,500-row
1996-backfill on the same non-CSV path made that rating false in
retrospect. The matrix needs to be re-graded as ⚠ (or ❌) on the
ingest column until the backfill script lands, *or* the protocol
needs an explicit "FRED-shaped exception" carve-out (and I don't
think it should — that's how this whole gap opened).

To close it:

1. Add `scripts/backfill_fred_csv.py` matching the SEC EDGAR shape:
   one CSV per indicator under `data/fred_backfill/fred_<series>_
   <run_stamp>.csv`, validated at CSV-write, gzipped on successful
   load.
2. One-shot run it now to capture the existing 1996-onward rows out
   of Postgres into baseline `data/fred_backfill/` snapshots before
   FRED revokes anything else.
3. Either fold the CSV-write into `handle_macro_indicators` itself or
   leave the daily delta API→INSERT path alone and rely on weekly
   `backfill_fred_csv.py` runs to refresh the snapshot. (The weekly-
   refresh path is the lighter-touch option; the delta handler's
   ~30 rows/week is well under the 500-row CSV-first threshold the
   pipeline doc carves out.)
4. Re-grade the FRED row of the compliance matrix in
   `docs/superpowers/pipelines/data_adapter_pipeline.md` once the
   above lands. Update the self-verification report at line 96+ with
   real CSV-artifact paths.

**Secondary (lower-severity) gap to defer:** the four daily-delta
handlers (`handle_daily_bars`, `handle_corporate_actions`,
`handle_fundamentals_refresh`, `_stage_catalyst_refresh`) don't snapshot
their incremental rows either. The risk is much smaller because the
historical base is already archived via the one-shot backfill scripts —
worst case we'd lose 1 day of deltas, not 30 years of history. Worth
revisiting only after FRED is fixed.

## Performance — daily_bars multi-symbol fetch

**Switch the `daily_bars` active path to `fetch_daily_bars_multi`.**

`tpcore/ingestion/handlers.py::_handle_daily_bars_explicit` fetches one
ticker per HTTP call with a hard `_RATE_LIMIT_SLEEP_SEC = 0.35` sleep
after each. With the universe at ~7,669 active tickers that's a
~45-minute *pure rate-limit floor* (7,669 × 0.35s), and ~60–75 min
wall once HTTP + upsert are added — making `daily_bars` ~60% of the
full ~1.5–2 hr daily `ops.py --update`.

`tpcore/data/ingest_alpaca_bars.py::fetch_daily_bars_multi` already
exists and pulls bars for *many symbols in one request* (Alpaca's
`/v2/stocks/bars?symbols=A,B,C…` multi endpoint, same one
`handle_corporate_actions` uses in 20-symbol chunks). Batching the
active path at, say, 100–200 symbols/request collapses ~7,669 calls →
~40–80 calls — cutting the rate-limit floor from ~45 min to well
under 5 min.

Scope:
- Rewire `_handle_daily_bars_explicit` to chunk `symbols` and call
  `fetch_daily_bars_multi` instead of the per-symbol `fetch_daily_bars`
  loop. Keep the existing `end_offset_days`, `_upsert_bars`, and
  per-chunk failure handling.
- Preserve the CSV-first archive write (added 2026-05-15) — it already
  collects `archive_rows` across the loop; just move the collection
  into the chunked path.
- Verify multi-endpoint pagination + the SIP `end=today` 403 behaviour
  still hold per chunk; keep `@with_retry` semantics.
- Re-run a full `daily_bars` and confirm coverage parity (all ~7,460
  daily tickers) at the new speed.

Priority: medium. Not urgent while daily ops run overnight off the
trade-submit window, but the single biggest latency lever on the
pipeline and a prerequisite if daily-update ever needs to run closer
to market open.

## Publishing

- **Publish a GitHub gist of the entire project.** Scope: everything —
  architecture (`docs/MASTER_PLAN.md`), database + dataflow
  (`docs/DATABASE_AND_DATAFLOW.md`), operations (`docs/OPERATIONS.md`),
  style guide, engine specs (Sigma, Reversion, Vector, Momentum) with
  credibility scorecards, parameter-search methodology + walk-forward +
  held-back DSR, 5-plug architecture, FilterDiagnostics + baseline-
  equivalence framework, dashboard, the Railway/Supabase ops story.
  Public-facing — review for any embedded keys, paths, or PII before
  publishing.
- **Publish to PyPI.** Open scope — decide what gets packaged. Most likely
  candidate: `tpcore/` as a standalone library (RiskGovernor, AAR,
  parity, backtest harness, filter diagnostics, baseline-equivalence) —
  the parts that are genuinely reusable outside this repo. Engines
  (`sigma/`, `reversion/`, `vector/`, `momentum/`) and `platform/`
  schema stay private. Prereqs: pick a name (likely not `tpcore` —
  reserved/generic), pin a license, add `pyproject.toml` package
  metadata, set up `python -m build` + `twine upload`, decide on
  versioning scheme. Same key/PII review as the gist.
