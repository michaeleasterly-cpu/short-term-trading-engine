---
name: data-update-first
description: "Always run scripts/run_daily_update.sh (or scripts/run_data_operations.sh) BEFORE any scheduler / search. Engines score from platform.prices_daily; without the daily update they trade on stale closes."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 6626da25-0752-45ca-99c0-beeb2f8af7bb
---

**Rule:** Run the daily data update (`scripts/run_daily_update.sh` for the 7-stage ops `--update`, or the full `scripts/run_data_operations.sh` for daily-ops + validation + self-heal + emit) before any engine scheduler invocation or parameter search. The order is:

1. Data update (today's bars + corporate actions + fundamentals refresh + validation)
2. Then scheduler / search / backtest

**Why:** Every engine pulls bars from `platform.prices_daily`. The momentum scheduler ranks the universe by 12-1 momentum using the *latest available close* — if that close is yesterday or older, the rebalance is computed on stale data. The names that enter the top decile, the cost-gate evaluation, the per-position sizing all silently shift to whatever the ingestion engine last fetched.

**How to apply:**

- For a daily ops sequence, the canonical order is:
  ```
  scripts/run_daily_update.sh      # first — refresh prices_daily etc.
  python -m momentum.scheduler     # then — score against fresh data
  ```
- Never tell the operator to kick off a scheduler / search / backtest without explicitly confirming the daily update is current first. If unsure, query `SELECT MAX(date) FROM platform.prices_daily WHERE ticker='SPY'` and compare to today's NYSE session date.
- For Railway: ingestion-engine should run BEFORE the engine schedulers in the cron sequence. Currently paused, so the operator runs locally — same order.

**Originating incident (2026-05-13):** I instructed the operator to run `scripts/run_momentum_kickoff.sh` to start paper trading, then ~10 minutes later mentioned the daily update as an afterthought. The operator caught it: "the daily update should have ran before the other shit because it is going off of old data." The momentum rebalance had already submitted 54 orders against stale-by-one-day prices. With 12-1 momentum (231-day lookback), one day of staleness is small but real — the top decile membership shifts a few names. Should have updated first.
