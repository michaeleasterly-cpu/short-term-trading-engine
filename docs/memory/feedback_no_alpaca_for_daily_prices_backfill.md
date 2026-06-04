---
name: feedback-no-alpaca-for-daily-prices-backfill
description: "NEVER use Alpaca to fill platform.prices_daily — Alpaca's close-date semantics are skewed vs FMP/Tradier. For daily-bar backfill: FMP primary, Tradier secondary, never Alpaca."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-25 — repeat violation):** when backfilling
`platform.prices_daily`, source priority is:

  1. **FMP** (`/stable/historical-price-eod/full`) — primary.
  2. **Tradier** (`/v1/markets/history`) — secondary / acceptable for daily.
  3. **Alpaca** — **NEVER** for daily-bar backfill into `prices_daily`.

**Why:** Alpaca's daily-bar close-date semantics differ from FMP and Tradier
(session boundary / timezone aggregation differs). Inserting Alpaca daily bars
into `prices_daily` creates per-row close-date inconsistency that contaminates
backtest and engine signals. The operator's documented decision (see
[[project_fmp_primary_daily_bars_2026_05_22]]): "We decided to use FMP and
not Alpaca for the daily ingest."

**How to apply:**

  - Any one-shot backfill of `prices_daily` MUST go FMP → Tradier (if FMP
    misses) → STOP. If neither has the bar, the gap is real and unfillable;
    document, do not fabricate, do not silently fall through to Alpaca.
  - Existing 4.6M `prices_daily WHERE source='alpaca'` rows are HISTORICAL
    artifact and tolerated; do not add more.
  - The `--stage daily_bars` handler default is FMP. If a stage invocation
    appears to write Alpaca bars to `prices_daily`, that's the bug — fix the
    stage, do not backfill with Alpaca.
  - If a script tries to UPSERT Alpaca data into `prices_daily`, that's a
    rule violation. Use FMP/Tradier or accept the gap.

**Failure mode this prevents (recorded 2026-05-25):** I tried Alpaca as a
fallback when FMP came up short on 33 tickers for 2026-05-22, inserted 29
rows, and the operator caught it on the same turn. Rolled back via
`DELETE WHERE source='alpaca' AND ticker = ANY(...)`. Cost: one turn + a
DELETE.

**Related:**
- [[project_fmp_primary_daily_bars_2026_05_22]] — original FMP-primary decision
- [[feedback_apply_my_own_documented_constraints]] — meta rule that I
  should have read memory BEFORE backfilling
