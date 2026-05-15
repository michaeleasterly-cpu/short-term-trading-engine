# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

## Engine structural redesign (post-2026-05-15 sweep)

The 2026-05-15 parameter sweeps validated the targeted fixes (Sigma SPY-
regime filter, Reversion Z-relaxation + T3 expansion) at the metric level
but DSR/credibility gates remain structurally blocked. Both engines need
strategy-level redesign, not parameter tuning:

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

- **Reversion signal-class redesign.** 2026-05-15 sweep with Z=2.5 +
  T3+fundamentals: held-back Sharpe +0.732 (was +0.43), trades 8 (was
  2), credibility 45-50/100 (ceiling unchanged), DSR 0.0000. The metric
  improvements are real and the relaxed config is retained, but no
  config in the search space clears DSR ≥ 0.95 / credibility ≥ 60.
  Z-score + earnings-quality is too narrow a signal class for the
  multiple-testing correction. Candidate redesigns: (a) add a
  complementary momentum-divergence signal (RSI divergence on the
  oversold extreme); (b) require a volatility-collapse confirmation
  (ATR drops > X% in the days before the reversal entry); (c)
  pair-trade variant (Z-score relative to sector peer, not just self).
  Decision deferred until operator picks a redesign path.

## Liquidity-tier ranking is inverted for individual stocks (P1 correctness)

`platform.liquidity_tiers` is misranking high-volatility mega-caps as
illiquid and illiquid microcaps as ultra-liquid. The classifier
(`classify_tickers`) is correct — the bug is in the **Corwin-Schultz
spread estimator** that feeds `assign_tiers`.

**Evidence (2026-05-15 forensic query):**

| Ticker | Real spread | C-S median | Tier | |
|---|---|---|---|---|
| SPY  | ~0.5 bp | 7.5 bp  | T2 | broadly OK |
| AAPL | ~1-2 bp | 25 bp   | T3 | 15× overstated |
| MSFT | ~1-2 bp | 25 bp   | T3 | 15× overstated |
| NVDA | ~1-3 bp | 63 bp   | T4 | 30× overstated |
| TSLA | ~2-5 bp | 81 bp   | T4 | 30× overstated |
| QQQ  | ~0.5 bp | 19 bp   | T3 | 30× overstated |
| BEBE | illiquid microcap | 0.1 bp  | T1 | wildly understated |
| FONR | illiquid microcap | 3.5 bp  | T1 | understated |
| APXT | illiquid microcap | 4.1 bp  | T1 | understated |

Also: every ticker has only 2-3 observations vs the
`MIN_OBSERVATIONS_FOR_STABLE = 5` threshold; tiers are getting
assigned with `provisional=True`.

**Why C-S inverts the ranking.** Corwin-Schultz infers spread from
daily HIGH/LOW range. High-volatility, ultra-liquid stocks (NVDA,
TSLA, AAPL) have wide daily ranges driven by *price discovery*, not
wide quotes — C-S incorrectly inflates spread. Illiquid microcaps
(BEBE, FONR, APXT) have narrow daily ranges because nobody trades
enough to move the price — C-S incorrectly tightens spread. The
estimator works decently on broad-market ETFs (SPY ≈ correct) but
fails catastrophically on individual stocks. This is a known failure
mode in the academic literature.

**Platform-wide impact.** Every engine query against
`liquidity_tiers <= 2` is running on the wrong universe:

  - T1+T2 currently has 66 'stocks' — almost all small/micro-cap
    (APXT, BANFP, BEBE, BPOPM, FONR, GIX, SBXD, TPH, WLYB, …)
  - AAPL, MSFT, AMZN, GOOGL, META, NVDA, TSLA, V, WMT, JPM —
    all classified `stock` but **none** are in T1+T2
  - Sigma's universe scan is therefore *excluding the exact stocks
    it should be including*. Explains why its candidate yield is
    so low even on otherwise-good market days.
  - Reversion's 2026-05-15 expansion to T3+stock+fundamentals
    accidentally pulls the megacaps back in — verified live this
    evening (AVPT, TXRH candidates), but that's a side-effect of
    going to T3, not a deliberate fix.

**Remediation paths (operator decision):**

1. **Dollar-volume floor override in `assign_tiers`** (smallest
   surface): require `avg_daily_dollar_volume >= $50M` (or similar)
   to qualify for T1 regardless of C-S estimate; require `>= $10M`
   for T2. Overrides the C-S inversion at the assignment step
   without replacing the estimator. Data already in
   `prices_daily.volume × close`. Easiest fix.
2. **Replace Corwin-Schultz with Abdi-Ranaldo or Roll's estimator.**
   Both handle high-volatility-low-spread stocks better than C-S.
   Larger code change in `tpcore/backtest/spread_estimator.py`.
3. **Use Alpaca SIP quote data directly.** Real bid/ask, eliminates
   the estimator. Requires Algo Trader Plus subscription (different
   dimension than the SIP bars we already pay for).

Recommend #1 as the immediate fix; #2 or #3 as longer-term
robustness improvements. Until this lands, treat `liquidity_tiers`
as an unreliable filter for individual stocks — engines that need a
real mega-cap universe should hardcode it or use a different source.

## Platform integrity

- **Make pipeline-test non-destructive.** `ops/platform_pipeline.py --force`
  (and any other "test the full pipeline now" path) currently writes real
  partial data when run mid-session: `ops.py --update --force` invokes the
  `daily_bars` stage which pulls Alpaca's intraday bars for the more-liquid
  tickers and INSERTs them into `platform.prices_daily` for today's date.
  Verified destructive 2026-05-14: a single `--force` run during market
  hours wrote 474 corrupted today-rows that had to be `DELETE`'d manually
  before the scheduled 21:30 UTC daemon fire could run cleanly. The engine
  sweep half of the pipeline can also fire real paper-trading orders.
  Fix sketch: `platform_pipeline.py` should accept `--dry-run` and forward
  it to `ops.py --update --dry-run` (the stage runner already returns
  `DRY_RUN` status without invoking the handler — non-destructive by
  construction at the stage level); when `--dry-run` is set, also skip
  the engine sweep entirely (or extend each engine scheduler to honor
  `--dry-run` if not already). Goal: `platform_pipeline.py --dry-run
  --force` becomes the canonical "verify the wire path works during
  market hours" command with zero side effects on `prices_daily`,
  `open_orders`, `aar_events`, or Alpaca.

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
