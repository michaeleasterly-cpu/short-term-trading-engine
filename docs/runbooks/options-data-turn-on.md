# Options data turn-on procedure

**Status:** DORMANT (2026-05-23). The Tradier API token works and an existing 113K-row historical export sits in `platform.tradier_options_chains`. The refresh script `scripts/refresh_tradier_options.py` is committed but not run. Activate when an engine candidate or Lab probe needs fresh options chains.

## Decision trigger — when to actually run this

Turn options on when ANY of these are true:

1. **An engine candidate consumes options data.** Specs that mention put/call OI ratio, IV skew, max-pain, gamma exposure, or "options-flow" signals.
2. **A Lab probe needs forward-looking short positioning.** FINRA `short_interest` is biweekly and lags ~2 weeks; options-derived positioning is daily. Vector / sentinel / catalyst probes that hypothesize on short-squeeze or unwind dynamics benefit from this.
3. **A backtest needs implied volatility surface.** Volatility regime engines, vol-of-vol signals, term structure plays.
4. **You add a position-sizing rule keyed on options-implied edges** (e.g. trim when IV crush is likely; size up when IV is mispriced).

**Don't turn on:** if no consumer exists yet. The 3.5-hour daily pull burns API budget and Tradier rate limit for nothing.

## What you get from options chains (vs current data)

| Signal type | Current source | From options |
|---|---|---|
| Short interest | FINRA biweekly (2-week lag) | Put/call OI daily |
| Bearish sentiment | AAII weekly survey + ApeWisdom Reddit | IV skew (25-delta put vs call), put OI surge |
| Crash risk | Macro indicators (vix index level only) | VIX term structure + skew + tail-pricing |
| Dealer positioning | none | Gamma exposure (GEX), max-pain levels |
| Synthetic shorts | not visible in `short_interest` | Visible in options activity (long puts + short calls) |
| Forward implied move | implied from VIX | Per-ticker IV, expected-move calculation |
| Earnings IV crush | not modeled | Pre/post earnings IV vs realized vol |

## Turn-on procedure

### 1. Verify Tradier token still works
```bash
set -a && source .env && set +a
curl -s -H "Authorization: Bearer $TRADIER_PRODUCTION_TOKEN" -H "Accept: application/json" \
  "https://api.tradier.com/v1/markets/quotes?symbols=SPY" | head -c 300
```
Expected: JSON with `quotes.quote.symbol="SPY"` and current price. If token expired, contact Tradier or rotate.

### 2. Decide scope — match it to the consumer

| Scope | Wall time (3-way concurrency) | API calls | When to use |
|---|---|---|---|
| Current 50 tickers (status quo refresh) | ~5 min | ~1,500 | Smoke test the pipeline |
| T1 only stocks+ETFs (~2,113) | ~3 hours | ~65K | Most-liquid; covers 95% of engine trade decisions |
| T1+T2 stocks+ETFs (~2,085) | ~3.5 hours | ~75K | Full active universe |
| All optionable stocks (~5,000+) | ~10+ hours | ~150K+ | Research-only deep coverage |

### 3. Run the refresh
```bash
set -a && source .env && set +a
# Scope: TIER_MAX=1 for T1 only, =2 for T1+T2, etc.
# Concurrency: 3 is polite for Tradier 120/min market data ceiling.
TIER_MAX=2 CONCURRENCY=3 \
  DATABASE_URL="$DATABASE_URL_IPV4" \
  .venv/bin/python scripts/refresh_tradier_options.py
```
Streams progress per ticker, persists each ticker's UPSERT atomically. Crash mid-run leaves all already-written data intact — re-run safely.

### 4. Verify post-refresh
```bash
set -a && source .env && set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, asyncpg, os
async def m():
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL_IPV4'], min_size=1, max_size=1)
    r = await pool.fetchrow(\"SELECT COUNT(*) AS n, COUNT(DISTINCT ticker) AS tkr, MAX(retrieved_at)::text AS lt FROM platform.tradier_options_chains\")
    print(f'rows={r[\"n\"]:,} tickers={r[\"tkr\"]} latest_pull={r[\"lt\"]}')
    await pool.close()
asyncio.run(m())
"
```

### 5. Schedule daily refresh (when consumer goes live)

Add to data_operations cron OR create a new ops stage `extract_tradier_options`:
- Run after market close (21:00 UTC weekdays)
- Scope = current consumer universe (start small; expand as engines adopt)
- Idempotent UPSERT pattern preserves history; no destructive overwrites

### 6. Wire the cleanup
`scripts/ops.py::_stage_cross_ref_cleanup` already deletes expired + orphan rows from `tradier_options_chains`. Verify it runs daily or schedule it.

## What the data shape looks like

`platform.tradier_options_chains`:
- `ticker, expiration_date, strike, option_type, bid, ask, last, volume, open_interest, retrieved_at`
- PK: `(ticker, expiration_date, strike, option_type)`
- Idempotent UPSERT refreshes bid/ask/last/volume/open_interest/retrieved_at; PK never changes

## Why this is dormant infrastructure now

- 113,834 rows from a 2026-05-10 one-shot export (50 tickers, expirations through 2028-12-15)
- `platform.options_max_pain` derived table has 1 row — abandoned consumer (the `greeks_max_pain` producer feed was retired 2026-06-01)
- No engine currently reads from `tradier_options_chains`
- Tradier API works, token valid, but pulling fresh data burns budget for zero edge

The script + this doc are the on-ramp for when an engine candidate needs it.

## Related

- `scripts/refresh_tradier_options.py` — the executable refresh
- `scripts/ops.py::_stage_cross_ref_cleanup` — periodic cleanup of expired/orphan rows
- `tpcore/providers.py:191` — documents max-pain derived computation
- `tpcore/audit/cross_table.py` — orphan + null-ticker audits
