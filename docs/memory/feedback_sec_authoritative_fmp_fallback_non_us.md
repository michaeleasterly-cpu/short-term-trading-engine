---
name: sec-primary-insider-fmp-fallback-non-us
description: "Operator 2026-05-23 (paraphrased professionally): SEC is PRIMARY for INSIDER data (Form 4 / 13D / 13G / 8-K material-event signatures) for US tickers; FMP is the backup for non-US insider data. NOT a universal SEC-first rule — applies specifically to the insider/regulatory lane, not to prices/fundamentals/profile-fields."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Rule (operator 2026-05-23, paraphrased professionally, after two corrections):** the SEC-primary / FMP-fallback ordering applies SPECIFICALLY to the **insider data lane** — Form 4 (insider transactions), Form 3/5, Schedule 13D/G, material-event signatures from 8-K. Not to prices, not to fundamentals, not to country/asset_class profile resolution.

## Per-lane priority (the actual map)

| Data lane | US primary | Non-US fallback | Why |
|---|---|---|---|
| **Insider transactions** (Form 4 / 5 / 13D / 13G) | **SEC EDGAR** | FMP | SEC is the regulatory source-of-truth; every US insider trade is legally required to be filed; CIK-keyed. FMP carries insider data for foreign issuers SEC doesn't cover. |
| **Material events** (8-K equivalents) | **SEC EDGAR** | FMP | Same regulatory-truth principle. |
| **Daily price bars** | **FMP** (`/stable/historical-price-eod/full`) | FMP (foreign exchanges) | Per `project_fmp_primary_daily_bars_2026_05_22`. CTA consolidated tape via FMP; Alpaca IEX/SIP only for fallback/diagnostics. SEC doesn't carry prices. |
| **Fundamentals** (10-K/10-Q parsed values) | FMP (parsed numbers) | FMP | FMP parses SEC filings into normalized fields. SEC has the raw XBRL but FMP's parsing is the operational source. |
| **Profile fields** (country, asset_class, exchange, sector) | FMP `/stable/profile` | FMP | SEC's `company_tickers.json` only carries ticker+CIK+name. FMP `/profile` is the source for country/asset_class/exchange. |
| **Ticker existence / CIK lookup** | SEC `company_tickers.json` | FMP `/profile` | SEC is the ticker↔CIK canonical map for US. FMP is the only path for non-US-listed identity. |

## What §1.10 of v2.1 spec actually means in light of this

The `parent_resolver` design in §1.10 lists `FMP primary → Alpaca fallback → SEC last`. That ordering is RIGHT *for the parent_resolver's use case* (populating `ticker_classifications` rows with country/asset_class/exchange — fields only FMP carries). It's NOT a "FMP is primary for everything" claim.

What §1.10 is missing: the source-lane distinction. When parent_resolver is invoked FROM an insider-data handler (EDGAR source), the CIK is already in hand — the right resolution path is SEC `company_tickers.json` (CIK→ticker reverse-lookup) THEN FMP `/profile` for the country/asset_class enrichment. §1.10 doesn't capture this branch and would benefit from a per-handler-lane dispatch when Task #24 gets designed in detail.

## Why (failure-derived 2026-05-23)

I over-generalized the operator's first correction ("sec is not a fallback it is the authoritative source for us") into a universal "SEC first for all ticker identity" rule. Operator narrowed it (paraphrased professionally): "SEC is primary for insider data for US; FMP is backup for non-US." The narrowing matters because parent_resolver and ticker_classifications care about country/asset_class fields that ONLY FMP carries — applying a blanket SEC-first rule there would be wrong.

## How to apply

When designing/reviewing any data-ingest path:

1. Identify which **lane** the data belongs to (insider / material-event / price / fundamentals / profile / identity).
2. Use the per-lane table above to pick the primary source.
3. **Don't generalize.** "SEC is authoritative" is true for insider/material-event/identity lanes. "FMP is primary" is true for price/fundamentals/profile lanes. Mixing them produces wrong designs.

## Related

- [[no-lazy-vendor-blame]] — SEC/EDGAR authoritative for regulatory/insider data (the original rule this entry refines)
- [[fmp-primary-daily-bars-2026-05-22]] — FMP-primary for prices (the lane that's NOT SEC-first)
- [[etl-bulk-before-api-crawl]] — SEC bulk file beats any per-ticker crawl
- `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.1.md` §1.10 — the design that needs the per-lane dispatch when Task #24 gets implemented
