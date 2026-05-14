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
SEC EDGAR: U.S. Securities and Exchange Commission's public filing system. Form 4 (insider transactions) + 8-K (material events) ingested via `tpcore.sec.SECEdgarAdapter` → `platform.sec_insider_transactions` + `platform.sec_material_events`. No API key; `SEC_EDGAR_USER_AGENT` env var required per SEC fair-access policy.
Form 4: SEC filing recording insider buy/sell transactions. Parsed from XML; canonical transaction codes mapped to BUY (A=Acquisition, P=Purchase) / SELL (D=Disposition, S=Sale); exotic codes (M=Exempt, G=Gift) skipped at parse time.
8-K: SEC filing reporting a material event (acquisitions, results of operations, officer changes). The adapter parses item codes (e.g., 2.02, 9.01) from the SEC submissions index; one row per item code.
sec_filings stage: 7th stage in `ops.py --update`. Weekly SEC EDGAR ingest of Form 4 + 8-K for the T1+T2 stock universe. Reference implementation of the standard 5-stage data-adapter pipeline; CSV-first; idempotent with 6-day skip guard.
data adapter pipeline: The 5-stage contract (ingest → test → validate → dashboard → schedule) every adapter on the platform must satisfy. Canonical reference: `docs/superpowers/pipelines/data_adapter_pipeline.md`. The "ingest" stage uses the CSV-first sub-protocol (download → validate-at-CSV → load → compress) for any non-trivial pull.
CSV-first sub-protocol: The operator's canonical pattern for adapter ingest: write to a timestamped CSV under `data/<provider>_backfill/`, apply the physical-truth predicate at the CSV-write boundary, load with `INSERT ... ON CONFLICT DO NOTHING`, gzip the source on success. The CSV is the permanent audit record of what the provider returned.
