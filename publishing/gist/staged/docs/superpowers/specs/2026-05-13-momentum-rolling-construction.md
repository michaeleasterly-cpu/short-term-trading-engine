# Momentum — Rolling-Portfolio Construction (Phase 3)

- **Path**: `docs/superpowers/specs/2026-05-13-momentum-rolling-construction.md`
- **Version**: 1.0
- **Date**: 2026-05-13
- **Status**: scoped — no code yet
- **Referenced from**: `docs/MASTER_PLAN.md` (Phase 4b — Momentum), `docs/EDGE_VALIDATION_PLAN.md` (Phase 5a follow-up)

---

## What this is

Today, Momentum rebalances monthly: on the first NYSE session of each calendar month, all 54 positions are recomputed against fresh data and the full portfolio is replaced (sells then buys). This matches the academic-paper presentation of Jegadeesh-Titman 1993 ("12-1 momentum, monthly rebalance") and was the construction validated by the held-back parameter search (Sharpe +1.58, PF +2.80).

**Rolling-portfolio construction** replaces the synchronized monthly event with **per-position aging**: each position has its own holding-period clock (default 21 trading days). On any given day, the ~1/21 of positions whose clocks expire that day are rotated out and replaced with fresh top-decile names. No "rebalance day"; trading happens daily but only on the ~5% of positions that age out.

This is the same construction Jegadeesh-Titman *actually used* for their statistical analysis (overlapping sub-portfolios, staggered by 1 day each). Real production momentum funds (AQR, AlphaArchitect's MOM ETF, Two Sigma) implement this rolling variant — monthly all-at-once is a presentation convenience, not a production discipline.

## Why consider it

| Property | Monthly all-at-once (today) | Rolling overlapping |
|---|---|---|
| Mathematical expected return | identical | identical |
| Execution risk on any single day | high (54 trades concentrated) | low (~3 trades / day) |
| Time to add a newly-qualifying name | up to 31 days | next day |
| Time to drop a name that's blown out of decile | up to 31 days | when its timer expires (avg 11 days, max 21) |
| Per-position state needed | none | entry_date |
| Daily operational cost | zero on most days | ~3 orders/day |
| Tax friction | one big bunch of short-term gains monthly | smoothed across the month — same total, different cadence |
| Audit / explainability | very simple | slightly more complex |

The directional case for rolling is real and well-documented. The magnitude — does it actually improve realized Sharpe on *our* data with *our* universe — is empirical and currently unknown.

## Two variants — choose one

The proposal collapses into one of two specific designs. Mixing them is bad form.

### Variant A — Pure timer-rolling (recommended for first build)

* Each position carries `entry_date`. Default hold period: 21 trading days.
* Daily evaluation: for each held position, if `today − entry_date ≥ hold_days`, exit it. Compute today's top decile. Refill the slots vacated by aging-out positions, drawing from today's top decile names *not currently held*.
* The portfolio retains its target size (~54 names) every day.
* **Exit rule**: timer only. A position that's currently down -30% still rides out its 21 days. Same discipline as monthly: trust the cross-section, ignore intra-position drawdowns.

### Variant B — Score-decay exit

* Each position carries `entry_date` *and* `entry_score`.
* Daily evaluation: for each held position, exit if either (a) timer expired OR (b) its *current* 12-1 score has dropped out of the top decile. Refill from current top decile.
* More responsive but higher turnover. AQR-style.

**Recommendation: build Variant A first.** It's mathematically equivalent to monthly in expected return, so it's the conservative migration. Variant B layers an additional behavioural change on top — bigger jump from the validated baseline. If A validates, B is a follow-up.

## Implementation scope (Phase 3.0 — backtest only)

The first deliverable is a backtest that lets us answer "does rolling beat or match monthly on our data?" No live-trading changes yet.

**Files**:

- `momentum/backtest_rolling.py` *or* a `--rolling` flag on `momentum/backtest.py` (operator preference)
- New per-position state model: `HoldingPosition(ticker, entry_date, entry_price, score_at_entry, exit_date, exit_price, pnl_pct)` — replaces the per-rebalance trade record
- Updated `_compute_one_rebalance` → `_compute_one_day`: every trading day, age out timer-expired positions, refill from today's top decile
- Search-pipeline wiring: add `rolling` to the engine's `PARAM_RANGES` (or a sibling engine name `"momentum-rolling"` for cleaner separation)

**Validation gate (Phase 3.0 acceptance criteria)**:

The rolling backtest must produce, on the same T1+T2 universe + 2018-2023 walk-forward + 2024-2025 held-back:

1. Held-back Sharpe ≥ 1.4 (vs monthly's 1.58 — allow modest degradation since smoothed turnover costs slightly more)
2. Held-back profit factor ≥ 2.5 (vs monthly's 2.80)
3. Held-back max drawdown not materially worse than monthly's 32.4%
4. Walk-forward top-5 parameter cluster consistent with monthly's (lookback ~210, skip ~25, hold ~21-25, decile ~0.08-0.15)
5. **No worse than monthly** on any of (1)-(4) within 0.2 Sharpe units, 0.3 PF units, or 5% drawdown points

If rolling passes all five, proceed to Phase 3.1. If it fails any, stop — monthly construction stays.

## Implementation scope (Phase 3.1 — live scheduler, contingent on 3.0 validation)

**Files**:

- `momentum/scheduler_rolling.py` *or* a `--rolling` flag on `momentum/scheduler.py`
- `momentum/plugs/lifecycle_analysis.py`: replace "is today the first of the month?" with "which currently-held positions age out today?"
- `momentum/plugs/setup_detection.py`: unchanged
- `momentum/plugs/execution_risk.py`: rewrite `build_decision` for the daily-slice case — current_holdings now includes per-position entry_date metadata, exit decisions driven by timer
- New state-persistence path for per-position entry_date — Alpaca's order history already carries the bought_at timestamp; verify it's reliably accessible via `broker.get_positions()` or fall back to a `platform.momentum_holdings` table

**Operational change**: scheduler runs daily and *almost always* trades (~3 orders/day instead of zero/everything). The cancel-stale-orders pattern still applies.

## Phase 3.2 — Migration

If 3.0 and 3.1 ship, the migration is:

1. Existing monthly scheduler continues on the current paper account.
2. New rolling scheduler runs against a sibling paper account (Alpaca lets you have multiple).
3. Run both side-by-side for 60+ trading days minimum.
4. Compare realized Sharpe, drawdown, fee load, slippage. If rolling materially better, retire monthly; otherwise keep monthly.

## What NOT to do

These are explicit non-goals so a future-reader doesn't drift:

* **Don't touch the running monthly paper experiment.** It's accumulating real OOS evidence on the validated construction. Don't break it to build rolling.
* **Don't conflate rolling with score-decay exit.** They're independent changes. Validate Variant A in isolation before considering Variant B.
* **Don't add per-name stop-losses just because per-position state now exists.** Momentum's edge comes from holding through drawdowns; stops bleed alpha (this is settled in the literature). Rolling does not change that.
* **Don't ship rolling without re-running the credibility gate.** The validated artifact is monthly. Rolling needs its own held-back rubric row in `platform.data_quality_log`.
* **Don't migrate based on backtest results alone.** Phase 3.2's paper run is the discriminating evidence; backtest gates *entry* to paper, not entry to live.

## Bibliography (light)

- Jegadeesh & Titman, "Returns to buying winners and selling losers" (Journal of Finance, 1993) — original 12-1 momentum, overlapping-portfolio statistical methodology
- Asness, Moskowitz, Pedersen, "Value and momentum everywhere" (Journal of Finance, 2013) — modern momentum factor, rolling implementation
- Moskowitz & Grinblatt, "Do industries explain momentum?" (Journal of Finance, 1999) — turnover and implementation considerations

## Deliverables checklist

When Phase 3.0 is built, this is what should exist:

- [ ] `momentum/backtest_rolling.py` (or `--rolling` flag) — passes existing momentum unit tests AND new tests for per-position aging logic
- [ ] `scripts/run_momentum_rolling_search.sh` — runs the same parameter-search pipeline against the rolling backtest, produces a held-back verdict
- [ ] Sibling row in `platform.data_quality_log` with `source = "backtest_credibility.momentum-rolling"` so the tip sheet can show it alongside monthly's
- [ ] Update to this spec marking Phase 3.0 status, with the validation-gate numbers from the actual run
- [ ] Decision: proceed to Phase 3.1, or stop with monthly as the production construction

Phase 3.1 and 3.2 deliverables get scoped *only* after Phase 3.0 produces a passing verdict.
