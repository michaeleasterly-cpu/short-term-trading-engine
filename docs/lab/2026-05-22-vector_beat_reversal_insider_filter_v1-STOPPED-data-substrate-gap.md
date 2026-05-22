# Lab Dossier — vector_beat_reversal_insider_filter_v1 → vector [STOPPED — data-substrate gap]

**Intent:** promote_new  **Recommended exit:** none — candidate NOT probed
**Generated:** 2026-05-22 (Manila session)  **Seed:** n/a (no probe ran)  **Trials:** 0
**Source emission:** `platform.application_log` event_type=`LAB_FINDER_EMISSION`, run_id=`640e7c03-59d9-426f-98f7-bf9665582ae8`, recorded_at=`2026-05-22 03:31:16Z`
**Self-rated cost_net_sharpe at emission:** +41.03 (LLM's own simulation)

## 1. Verdict

**STOPPED at v2.1 testability pre-check.** No engine modification was written; no Lab probe was run; **no `lab_trial_ledger.vector` row was added** (cumulative still 280, all from `vector_composite`).

This is the same shape of disposition as the prior reversion finding (`docs/lab/2026-05-22-reversion_earnings_season_5d_range_normal-FAILED-seed20260522.md`), but the gap is reached earlier in the pipeline: not "the strategy produces zero trades inside the engine" — instead, "the data substrate cannot produce enough candidate entries for the strategy as specified, before any engine code runs."

## 2. The candidate (verbatim from emission)

> SP500 T1 stocks that (a) reported earnings BEAT in prior 1-5 sessions, (b) have MSPR insider sentiment score > 0 (net insider buying in trailing 30d), and (c) rank in bottom tercile of 5-day cross-sectional return — buy them.

Regime context (descriptive, not the filter): `range × normal × expansion × neutral × earnings_season` (regime_tuple_id `968624efa259`, the same regime that broke reversion's PCA-residual probe earlier today).

Param ranges proposed: `mspr_floor ∈ [0.0, 0.10]`, `hold_sessions ∈ [3, 7]`, `min_dollar_volume_m ∈ [50, 200]`, `beat_lookback_sessions ∈ [1, 5]`, `return_rank_tercile_threshold ∈ [0.20, 0.40]`.

Falsification criterion (per emission): `cost_net_sharpe < 0 OR DSR < 0.95 OR PBO > 0.20` in the final holdout, OR no significant improvement over a BEAT-only baseline.

## 3. Testability pre-check — what was measured

### `platform.earnings_events`

- Schema: `(ticker, event_date, event_type, magnitude_pct, source, recorded_at)`. **All rows use `event_type='EARNINGS_BEAT'`** — not `'BEAT'` as the candidate's hypothesis text suggested. (Cosmetic; the join works once you use the right literal.)
- Coverage: dense from 2018-01-10 through 2026-05-15 (~1.2K-1.9K events/yr). Train window (2018-2023) and final-holdout window (2024-2025) both fully covered.

### `platform.insider_sentiment` — the binding constraint

- Schema: `(symbol, year, month, mspr, net_change, recorded_at)`. **Monthly granularity, not daily.** The candidate's "MSPR > 0 in trailing 30d" can only be approximated as "MSPR > 0 in same calendar month as the earnings event."
- Coverage: **520 rows total**, spanning **2025-01 through 2026-05 only**. Zero rows in train window (2018-2023). Zero rows for 2024.
  - 2025: 373 rows, 49 distinct symbols, of which 146 rows / 45 symbols have `mspr > 0`.
  - 2026 (YTD): 147 rows, 46 symbols.

### Joinable filter set (the candidate's three-factor AND)

Within the final-holdout window 2024-01-01 → 2025-12-31:

| Filter step                                                              | Surviving rows |
| ------------------------------------------------------------------------ | -------------- |
| `EARNINGS_BEAT` in 2024-2025                                             | 3,753          |
| ∩ same-month `mspr > 0`                                                  | **13**         |
| ∩ ~bottom-tercile 5-day return (cuts ~3×)                                | **~4**         |
| ∩ regime `968624efa259` (range × normal × earnings_season, further cuts) | **<4**         |

Within the train window 2018-2023:

| Filter step                  | Surviving rows |
| ---------------------------- | -------------- |
| `EARNINGS_BEAT` in 2018-2023 | 9,224          |
| ∩ `mspr > 0` (any month)     | **0** — no insider rows exist before 2025 |

## 4. Why this is a STOP, not a "run a smaller probe"

- **Train window has zero insider data.** The walk-forward backtest cannot train on 2018-2020 → score on 2021, 2021 → 2022, 2022 → 2023, etc. — the `composite_mode='beat_reversal_insider'` branch would return an empty entry set for *every* training fold. The Lab's DSR / credibility math then sees `n_trades=0` and the verdict mechanically collapses to the same shape as the prior reversion FAIL (DSR=0, cred=45) — but with the additional defect that the substrate gap is the real cause, not the strategy.
- **Final-holdout substrate is ≤4 candidate entries.** Even if we constrained the entire run to 2025-only (giving up the walk-forward pre-evaluation), the candidate filter as written produces single-digit entries across a full year — orders of magnitude under the 30-trade minimum the task itself flagged.
- **Burning ~20 trials × ~3 windows = ~60 trials of `lab_trial_ledger.vector` spend** to discover this empirically would be lazy. The substrate makes the answer knowable in seconds via a SQL join. v2.1 persona stop rule §2.1 (testability pre-check) catches exactly this.

## 5. What this DOESN'T claim

- It does **not** claim the underlying market hypothesis is wrong. Post-earnings-beat oversold reversion conditional on insider buying is a coherent McLean-Pontiff-novel composite — the literature supports each leg in isolation. The hypothesis is unprobed because the data isn't there yet.
- It does **not** claim the finder is broken. The finder identified a hypothesis that requires substrate the platform doesn't yet have at the temporal resolution / coverage the spec demands. That's a finder-output × adapter-coverage mismatch, not a finder defect.
- It does **not** claim the gate-pilot's +41.03 self-rated cost_net_sharpe is wrong. The LLM's `cost_net_simulation` ran on n_trades=2 (the emission itself flagged this as "degenerate CI" in the MARGINAL CANDOR section), so that number was already self-marked as non-load-bearing.

## 6. What WOULD make this candidate testable

Two independent prerequisites — both adapter-readiness work, not engine work:

1. **Backfill `insider_sentiment` to 2018-01-01.** OpenInsider / Form-4 data is publicly retrievable for the full window. The adapter that wrote the 520 existing rows can be re-run with a wider date range. Cost: an adapter-readiness §6 (backfill stage) execution.
2. **Reduce `insider_sentiment` granularity from monthly to daily / 30d-rolling.** The candidate's hypothesis specifies "trailing 30d," not "same calendar month." Form-4 filings are date-stamped; aggregating to month destroys the information the hypothesis needs. The current monthly aggregate is a vendor-shaped artifact, not a forcing constraint.

Either alone is insufficient. Both together, plus the existing dense `earnings_events` coverage, would give the finder a substrate where this composite is testable.

## 7. Trial ledger state

- Pre-task `lab_trial_ledger.vector` cumulative: **280** (7 prior rows, all `vector_composite` on 2026-05-20).
- Post-task `lab_trial_ledger.vector` cumulative: **280** (no new row written — no probe ran).
- DSR `n_trials` penalty against vector therefore unchanged.

## 8. Honest signal

Per the task's "Interpretation" rubric, this run does not slot cleanly into any of the three rubric outcomes (DSR≥0.95∧cred≥60 → loop validated / DSR<0.95∧n_trades≥30 → real test that fails / n_trades<30 → defensive-engine gap), because no probe ran at all. The closest match is a **fourth outcome the rubric didn't enumerate**: *the candidate is structurally untestable on the platform's current data substrate*. That is itself a meaningful finder-loop signal:

- The finder is producing hypotheses that exercise the **edges** of the data substrate. That's healthy — a finder that only proposed already-covered combinations would be a useless restatement of the existing engines.
- But the loop's downstream gate must be aware of the substrate dependency. The current emission contract (`primary_hypothesis` free-text + `param_ranges` dict) lets the finder reference data the platform doesn't have, with no automatic detection upstream of the human Lab-probe step. A `required_tables` / `required_coverage` field on the emission, machine-checked against `platform.adapter_health` / row-count probes before the candidate is queued for SP-A, would have caught this in < 1 second and saved a subagent dispatch.

That feature is out of scope for this session, but is the natural follow-up — file as an SDLC-Lab improvement, not as a candidate to re-emit.
