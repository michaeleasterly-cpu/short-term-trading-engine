# Glossary

plug: One of five standardized modules inside each engine.
sigma: Range scalping engine (daily Bollinger Bands, ADX, stochastic).
reversion: Statistical mean reversion engine (Z-score, RSI extremes).
vector: Momentum swing engine (multi-day trend, catalyst overlay).
s2: Short squeeze engine (satellite, rare setups).
catalyst: Event-driven engine (post-earnings drift only).
sentinel: Macro inverse engine (reformed basket: SH, PSQ, TLT, GLD, SQQQ).
tpcore: Trading Platform Core — shared library for all engines.
allocator: Capital allocation service (equal-risk-weighted).
forensics: Trade analysis service (formerly Coroner).
settlement: Annual distribution + tax reporting service (formerly Harvester).
tax-loss harvester: An automated tax-optimisation feature inside `tpcore.tax` (`TaxLossHarvester`), distinct from the deprecated Harvester (now Settlement) service.
pit: Point-in-time — data as it was known on a specific historical date, not retroactively adjusted.
survivorship bias: The error introduced when backtests exclude delisted stocks.
parity harness: System that compares paper fills to live fills.
bracket order: A parent order with linked take-profit and stop-loss legs (Alpaca order_class=bracket).
@with_retry: Universal HTTP retry decorator in `tpcore.outage`. Exponential backoff with optional jitter, honors `Retry-After`, retries 429/5xx and transient network/timeout errors only — 4xx-not-429 is permanent and re-raised immediately. Every external-API adapter on the platform uses this; ad-hoc `tenacity` and `asyncio.sleep` loops are forbidden.
adapter template: Copy-paste scaffold at `tpcore/templates/adapter_template.py` — the starting point for any new external-API adapter. Demonstrates the canonical shape (env-var config, fail-fast construction, `@with_retry` on the HTTP layer, `DataProviderOutage` mapping at the boundary).
adapter readiness checklist: Pre-merge gate at `docs/superpowers/checklists/adapter_readiness.md` — every new adapter (or substantial change to an existing one) must pass all sections (error handling, logging, configuration, interface compliance, tests, rate limiting, documentation) before merging.
ticker classifications: `platform.ticker_classifications` table — asset-class taxonomy (`stock` / `etf` / `spac` / `fund`) plus ETF leverage/inverse/category flags. Backfilled 2026-05-14 from Alpaca `/v2/assets` + name-pattern classifier; used by `catalyst_freshness` validation and the sentinel engine.
catalyst_freshness: 7th validation check (added 2026-05-14). Asserts `catalyst_events.max(event_date)` ≥ today − 7 days for the active T1+T2 stock universe (ETFs/funds/SPACs filtered via `ticker_classifications`). Drives the `catalyst_refresh` ops.py stage's skip-guard.
catalyst refresh: 6th `ops.py --update` stage (added 2026-05-14). FMP earnings-history → `platform.catalyst_events` for T1+T2 stocks. Short-circuits in ~10ms when the table was refreshed within 6 days.
centralized error handling: The platform's approach to external-API failure: classify outage via `tpcore.outage.classify_outage` and act on the returned `OutageTier`; retry transient failures via `@with_retry`; map persistent failures to `DataProviderOutage` at the public-method boundary. Engine code never sees raw `httpx.HTTPError`.
