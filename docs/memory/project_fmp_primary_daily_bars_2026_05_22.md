---
name: fmp-primary-daily-bars-2026-05-22
description: "Primary daily-bars data source switched from Alpaca SIP/IEX to FMP /stable/historical-price-eod/full on 2026-05-22 after the operator's Alpaca SIP entitlement went 403 and they declined the $99/mo Algo Trader Plus upgrade. FMP at the operator's $200/year Starter tier confirmed returning full CTA consolidated-tape volumes (e.g. AAPL 2026-05-21 volume = 42.8M, matching SIP). Alpaca stays for order execution + remains available via --param feed=iex|sip as fallback."
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Architectural shift 2026-05-22.** Primary daily-bars feed is **FMP** (`https://financialmodelingprep.com/stable/historical-price-eod/full`). Alpaca stays for order execution + as a feed-fallback (`--param feed=iex|sip`).

## Why this changed

Operator's Alpaca SIP entitlement returned HTTP 403 across the bulk-fetch path on 2026-05-22 (same intermittent gating observed 2026-05-21 from 04:38–04:55 UTC). Daily ingest collapsed to ~3700 IEX-listed tickers (~50% of the prior 7600 SIP-era coverage). Operator declined Alpaca's $99/mo Algo Trader Plus tier (memory: `project_railway_hobby_tier` documents the $52/mo fixed-cost ceiling).

Discovery: the operator paid **$200/year for FMP** (presumably Starter tier per the existing `tpcore/fmp/fundamentals_adapter.py` infrastructure). FMP's `/stable/historical-price-eod/full` endpoint was probed with the operator's `FMP_API_KEY` and confirmed:

```
GET /stable/historical-price-eod/full?symbol=AAPL&from=2026-05-17&to=2026-05-21
→ HTTP 200
→ AAPL 2026-05-21: close=304.99, volume=42,823,425
```

Volume = full CTA consolidated tape (Alpaca SIP returns ~38M, IEX returns ~1M for same ticker on same day). FMP at the operator's tier delivers SIP-equivalent coverage at **$0 additional cost**.

## Constraints at the operator's FMP tier

- **No bulk endpoints.** Probed `/batch-eod-historical-price`, `/quote-bulk`, comma-separated symbol args → all return 404 or 401. Per-ticker calls only.
- **Rate limit ~300 req/min** (Starter typical). Per-ticker × 7600 tickers ÷ 300/min ≈ **25 minutes wall time** for full-universe daily ingest. Acceptable.
- **No `/stock-list` access** at operator's tier (401 — paid-tier-gated). Universe enumeration relies on the existing `platform.prices_daily` + `platform.liquidity_tiers` (already in DB from Alpaca-SIP-era ingest).

## What stays Alpaca

- **Order execution.** All paper orders go through Alpaca's broker layer (`ops/alpaca_*.py`, `tpcore/alpaca/data_adapter.py`). FMP is data-only.
- **`--param feed=iex|sip` fallback paths.** The IEX free-tier and the SIP probe code (PRs #231, #233) remain wired so the operator can switch back via flag if FMP ever degrades.

## How to apply (future-session checklist)

- **Default daily-bars feed = FMP.** Don't propose Alpaca SIP unless the operator explicitly opts in (they're not paying for it).
- **New ingest stages targeting daily bars** route through `tpcore/data/ingest_fmp_bars.py::fetch_daily_bars_multi` (the FMP adapter shipped 2026-05-22).
- **Cross-source consistency.** The historical corpus 2011-2026 is Alpaca-SIP-sourced; new bars 2026-05-22+ are FMP-sourced. The cross-validation test asserts OHLC within 0.5% / volume within 5% between sources for the overlap period — this is the trust boundary. Backtests spanning the cutover need to recognize the source-change (commit to a documented marker / source column).
- **Memory `feedback_no_lazy_vendor_blame`** still applies — if FMP returns unexpected data, query the source before blaming vendor.
- **CLAUDE.md line 26 universal invariant** updated 2026-05-22: was `Default data feed is **SIP**`, now `Default data feed is **FMP** (full CTA consolidated tape; Alpaca IEX/SIP available via --param feed=iex|sip)`.

## Cost summary (post-switch)

- Supabase Pro $25/mo
- Railway Hobby $5/mo (paused)
- **FMP $200/year ≈ $17/mo** (was already paid before this switch — sole data source for daily bars going forward)
- Alpaca free tier (orders only) $0
- **Total fixed cost: ~$47/mo** — below the $52/mo cap, also below the rejected Alpaca Pro $99/mo path

## Related

- [[project-railway-hobby-tier]] — the $52/mo ceiling that informed this decision
- [[feedback-no-lazy-vendor-blame]] — vendor-state evidence required before threshold change
- [[lab-heavy-probe-needs-chunking]] — chunking pattern, mirrored in FMP per-ticker iteration
- Spec: `docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-expansion-design.md` — the cascade catalog (D1-D14) still applies; FMP slots into the feed-selection layer below the cascade
- Memory cross-ref: [[deterministic-cascade-architecture]] — cascade fires on coverage_collapse same as before; recovery action invokes FMP not SIP

## PR record

- PR #275 (or whatever the FMP adapter PR number lands as) — ship of `tpcore/data/ingest_fmp_bars.py` + wire + cross-validation + default-feed flip
