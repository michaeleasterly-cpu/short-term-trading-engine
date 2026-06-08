# Market data sources — fund flows, private credit, corporate bonds

Research + validation reference for three `/market`-page gauges that are currently
"not wired" or caveat-only. The `/market` page self-fetches at render (live fetch,
no DB), so every source below is validated by an **actual server-side fetch** with a
**real sample value** shown. No yfinance / Yahoo scraping. Accessible = free or on the
operator's existing **FMP Starter** + **FRED** keys (both present in `.env` as
`FMP_API_KEY` / `FRED_API_KEY`, 32-char each, confirmed working).

- Validated: **2026-06-08** (FMP/FRED quotes are EOD 2026-06-05; ICI week ending 2026-06-03 file).
- Vendor profiles: FMP binding lives in `tpcore/providers.py` (`feed="prices_daily"`,
  Starter tier, ~300 req/min). FRED binding: `feed="macro_indicators"`, provider `fred`.
  Any new feed onboarded from this doc goes through the **DFCR** (`/dfcr` skill) — never
  hand-edit `tpcore/providers.py` (the hook blocks it). See `.claude/rules/data-feed-roster.md`.

Verdict legend: **WIRE** = real authoritative source, fetch it directly · **PROXY** =
real but indirect, must be labeled as a proxy · **CAVEAT-ONLY** = no accessible source,
keep the honest caveat.

---

## Gauge 1 — Equity fund flows  →  **WIRE** (ICI weekly XLS)

**Question:** Is money flowing into or out of stock funds (weekly net flows into/out of
equity mutual funds + ETFs)?

### Validated source — ICI weekly combined MF + ETF flows (machine-readable .xls)

- **URL (current year, stable pattern):**
  `https://www.ici.org/system/files/2026-01/combined_flows_data_2026.xls`
  - Pattern is `https://www.ici.org/system/files/<YYYY>-01/combined_flows_data_<YYYY>.xls`.
    The directory segment is always `<YYYY>-01` (the file is created in Jan and updated in
    place all year — confirmed: the 2026 file's "Last Saved" timestamp is 2026-06-03).
    Prior years validated too: `.../2025-01/combined_flows_data_2025.xls`,
    `.../2022-01/combined_flows_data_2022.xls` (both HTTP 200).
  - **Do NOT** guess `<YYYY>-06/...` — that 404s. Only the `-01` segment is correct.
- **Fetch method:** plain HTTPS GET with a non-empty User-Agent
  (`-A "Mozilla/5.0 ..."`; default curl UA is fine, but send one to be safe). No key, no auth.
  - Returns a legacy **BIFF/OLE2 .xls** (`Content-Type: application/vnd.ms-excel`, ~44 KB).
    Parse with `pandas.read_excel(..., engine="xlrd")` — **both `pandas` (3.0.3) and
    `xlrd` (2.0.2) are already in `.venv`**, so no new dependency. (System `/usr/bin/python3`
    lacks them; use the repo venv.)
  - Single sheet: `"Weekly MF & ETF Public Report"`, shape ~(45, 18). Header rows 4–6 define
    the columns; the **weekly** block starts after a `"Estimated weekly fund flows"` marker
    row (~row 37). Column order in the data rows (skipping the NaN spacer cols):
    `Date | Total LT MF+ETF | Equity Total | Equity Domestic | Equity World | Hybrid |
    Bond Total | Bond Taxable | Bond Municipal | Commodity`. Units = **millions USD**.
    The same file also carries the monthly history block above the weekly block.

### REAL sample value retrieved (file fetched 2026-06-08)

Latest weekly row, **week ending 05/27/2026**:

| Series | Value ($M) |
|---|---|
| Total LT MF + ETF flows | **+15,641** |
| **Equity — Total** | **−2,214** |
| Equity — Domestic | −1,575 |
| Equity — World | −640 |
| Hybrid | −1,529 |
| Bond — Total | +19,471 |
| Commodity | −87 |

Prior week (05/20/2026): Equity Total **−13,268**; (05/13/2026): Equity Total **+13,274**.
So the headline gauge "money into/out of stock funds" reads **net OUT of equity funds the
last reported week (−$2.2B), and choppy** (a big inflow two weeks prior, big outflow one
week prior). This is real, fetched data — not fabricated.

### Cadence / freshness / auth

- **Cadence:** weekly, published **Wednesday** for the week ending the prior Wednesday
  (the in-file timestamp 2026-06-03 = a Wednesday). File is overwritten in place.
- **Auth / rate limit:** none. Public file. Polite weekly fetch; cache for the day.
- **Freshness check for the wiring:** read the max Date in the weekly block; if it's
  > ~10 days stale, surface "ICI flows stale as of <date>" rather than silently showing old data.

### Alternatives considered

- **FRED equity-fund-flow series:** the closest are the quarterly **Z.1 Financial Accounts**
  flows (e.g. `BOGZ1FA653064100Q` Mutual Funds; Corporate Equities; Transactions). Validated
  reachable but **quarterly + a quarter lagged** — far too slow for a "weekly flows" gauge.
  Keep FRED Z.1 only if a long-horizon context line is wanted; the weekly gauge should use ICI.
- **No yfinance / no ETF.com scraping** — ICI is the authoritative publisher and is directly fetchable.

**Verdict: WIRE.** Fetch the ICI weekly XLS, parse the last weekly `Equity — Total` row
with the repo venv's pandas+xlrd. Real, free, authoritative, no key.

---

## Gauge 2 — Private credit  →  **PROXY** (BDC discount-to-NAV via FMP) + structural caveat

**Caveat being addressed:** corporate risk has migrated to private credit / direct lending
that doesn't mark-to-market, so it's a market-stress blind spot. **Private credit is
structurally opaque (mark-to-model); anything here is a partial, clearly-labeled proxy.**
The best *accessible, daily, market-priced* read on private-credit stress is the
**public BDC (Business Development Company) discount-to-NAV** — public BDCs are the
listed wrapper around direct-lending books, and the market's discount to stated NAV is a
real-time vote on whether those marks are believed.

### Validated source — BDC price-to-book (= price/NAV) via FMP

- **Discount/premium to NAV** = `priceToBookRatioTTM − 1`. For a BDC, book value per
  share ≈ NAV per share, so **price-to-book is the discount-to-NAV** directly.
- **FMP endpoints (Starter tier, key works):**
  - Price: `https://financialmodelingprep.com/stable/quote?symbol=<T>&apikey=$FMP_API_KEY`
  - NAV/book + ratio: `https://financialmodelingprep.com/stable/ratios-ttm?symbol=<T>&apikey=$FMP_API_KEY`
    → fields `priceToBookRatioTTM`, `bookValuePerShareTTM`, `priceToFairValueTTM`.
  - (Cross-check available: `balance-sheet-statement` totalStockholdersEquity ÷
    `shares-float` outstandingShares — the `shares-float` endpoint even cites the SEC filing URL.)
- **ETF roll-up:** **BIZD** (VanEck BDC Income ETF) — one `quote` call gives a basket
  price + 52-week range, a simpler single-number gauge than the per-name basket.

### REAL sample values retrieved (FMP, EOD 2026-06-05, fetched 2026-06-08)

BDC basket, discount-to-NAV = (P/B − 1):

| Ticker | Price | P/B (=P/NAV) | NAV/sh (bvps) | Discount |
|---|---|---|---|---|
| ARCC (Ares) | 18.77 | 0.958 | 19.59 | **−4.2%** |
| BXSL (Blackstone) | 23.40 | 0.891 | 26.27 | **−10.9%** |
| ORCC (Blue Owl) | 13.48 | 0.766 | 14.34 | **−23.4%** |
| OBDC (Blue Owl) | 10.98 | 0.766 | 14.34 | **−23.4%** |
| FSK (FS KKR) | 10.64 | 0.565 | 18.83 | **−43.5%** |

- **BIZD** (ETF roll-up): price = **12.45** on 2026-06-08 quote (EOD 2026-06-05),
  52-week range **11.97 – 16.95**, 50-day avg 12.71, 200-day avg 13.89 → trading near
  the **bottom of its 52-week range and below both moving averages**.

Read: the listed direct-lending complex is trading at a **meaningful discount to stated
NAV across the board** (cap-weighted leaders single-digit, weaker names 20–44% below NAV),
and BIZD is near 52-week lows — i.e. the market is **skeptical of private-credit marks**.
That is exactly the blind-spot signal the gauge wants, delivered with daily-priced data.

### Cadence / freshness / auth

- **Price / BIZD:** daily (EOD), FMP Starter, ~300 req/min (per `tpcore/providers.py`
  FMP profile). Basket of ~5 names + BIZD = ~6 calls, trivial vs the rate limit.
- **NAV (book value):** updates only when the BDC files (quarterly 10-Q/10-K), so the
  *denominator is quarterly-stale by design*. **Label it: "discount vs last-reported NAV
  (quarterly)."** The price (numerator) is daily, which is the point — a widening discount
  intra-quarter is the early warning before NAVs are restated.
- **Auth:** FMP key (already in `.env`).

### Slower structural context (optional second line) — FRED Z.1

- **Finance companies; total financial assets** `BOGZ1FL614090005Q` — validated:
  **$2,891,672M (2025-Q4)**, up from $2,830,030M (2025-Q3). This is the nonbank-lending
  aggregate from the Fed Financial Accounts (Z.1). **Quarterly + ~1 quarter lagged**, so
  it's a slow context number, not a market signal — but it's the authoritative size-of-the-
  pool series if a "how big is nonbank credit" line is wanted alongside the BDC discount.

**Verdict: PROXY (label clearly).** Wire the **BDC discount-to-NAV** (per-name basket
average + BIZD price/52-wk position) as the private-credit gauge, explicitly labeled
"public-BDC proxy; private credit is mark-to-model and structurally opaque." Optionally
add the Z.1 finance-company asset total as a slow structural context line. Do **not**
claim it measures private-credit losses directly — it measures the *market's discount to
self-reported marks*, which is the honest, accessible best.

---

## Gauge 3 — Corporate bonds  →  **WIRE** (BAA−AAA quality spread on FRED + IG/HY bond-ETF trend on FMP)

**Operator question:** "what about corporate bonds?" The existing credit gauges already use
**HY/IG OAS spreads** (`BAMLH0A0HYM2`, etc.). The additions below are *not* duplicates of
OAS — they add a **quality-spread** dimension and a **fund-flow / price-trend** dimension.

### 3a. Moody's BAA−AAA quality spread (FRED) — WIRE

- **Series (FRED, key works):**
  - `DBAA` — Moody's Seasoned **Baa** Corporate Bond Yield (daily)
  - `DAAA` — Moody's Seasoned **Aaa** Corporate Bond Yield (daily)
  - Gauge = `DBAA − DAAA` (the classic credit-**quality** spread; distinct from OAS because
    it's a *level-yield* quality differential on long-maturity seasoned bonds, not an
    option-adjusted index spread).
- **Fetch:** `https://api.stlouisfed.org/fred/series/observations?series_id=DBAA&api_key=$FRED_API_KEY&file_type=json&sort_order=desc&limit=1` (and DAAA).
- **REAL values (2026-06-05):** **DBAA = 6.06%**, **DAAA = 5.53%** →
  **BAA−AAA quality spread = 0.53 pp**. (For reference the existing IG OAS `BAMLC0A0CM`
  = 0.74% and HY OAS `BAMLH0A0HYM2` = 2.76% on the same date — the BAA−AAA spread is a
  different, complementary view.)
- **Cadence:** daily (business days), ~1 business-day lag. Free, FRED key.

### 3b. IG / HY corporate-bond ETF price trend (FMP) — WIRE (flow/trend proxy)

The existing gauges cover *spreads*; they don't cover *what investors are doing with their
money* in corporate-bond funds. The accessible, no-yfinance way is the two benchmark
corporate-bond ETFs via FMP `quote` (price vs 50/200-day moving averages = trend; volume
= activity). True creation/redemption flow needs a premium flows feed, so use price-trend
as the labeled proxy.

- **Endpoints:** `https://financialmodelingprep.com/stable/quote?symbol=LQD&apikey=$FMP_API_KEY`
  (IG: iShares iBoxx $ Investment Grade) and `...symbol=HYG...` (HY: iShares iBoxx $ High Yield).
- **REAL values (EOD 2026-06-05, fetched 2026-06-08):**
  - **LQD** (IG): price **108.06**, 50-day avg **108.90**, 200-day avg **110.36**, vol 21.9M
    → price **below both MAs** = mild IG-bond-fund downtrend.
  - **HYG** (HY): price **79.54**, 50-day avg **79.96**, 200-day avg **80.47**, vol 25.1M
    → also **below both MAs**, but only slightly = HY roughly flat-to-soft.
- **Cadence:** daily EOD, FMP Starter (~300 req/min). 2 calls.
- **Label:** "IG/HY corporate-bond ETF price trend (proxy for fund demand); not net flows."

### Why these and not more OAS

OAS already answers "how much extra yield for credit risk." **3a** adds the *quality
gradient* (Baa vs Aaa) as a daily level spread; **3b** adds *price/demand trend* in the
two dominant corporate-bond fund vehicles. Neither is covered by the existing OAS series.

**Verdict: WIRE both.** BAA−AAA quality spread (FRED `DBAA`−`DAAA`) is the highest-value
add (authoritative, daily, free, distinct from OAS). LQD/HYG price-trend is a useful
second line, labeled as a demand/trend proxy (not true flows).

---

## Ranked recommendation — what to wire next

| Rank | Gauge | Action | Source (validated) | Verdict |
|---|---|---|---|---|
| 1 | **Equity fund flows** | Wire | ICI weekly XLS `…/2026-01/combined_flows_data_2026.xls`, parse last `Equity — Total` (venv pandas+xlrd). Sample: **−$2,214M** wk-ending 05/27/2026 | **WIRE** |
| 2 | **Corporate bonds (quality spread)** | Wire | FRED `DBAA − DAAA`. Sample: **6.06 − 5.53 = 0.53 pp** (2026-06-05) | **WIRE** |
| 3 | **Private credit** | Wire as labeled proxy | FMP BDC discount-to-NAV: ARCC −4.2% / BXSL −10.9% / ORCC −23.4% / FSK −43.5%; **BIZD = 12.45**, near 52-wk low. Label "public-BDC proxy; PC is mark-to-model" | **PROXY** |
| 4 | **Corporate bonds (demand trend)** | Wire as labeled proxy | FMP `quote` LQD **108.06** (below 50/200-day), HYG **79.54** (below 50/200-day) | **PROXY** |
| 5 | Private-credit size context | Optional slow line | FRED Z.1 `BOGZ1FL614090005Q` = **$2,891,672M** (2025-Q4), quarterly | PROXY (slow) |

**Honest caveats to keep visible on the page:**
- Private credit has **no direct mark-to-market source**; the BDC discount is the market's
  vote on self-reported marks, not realized losses. Always labeled.
- The corporate-bond **ETF trend** is a demand/price proxy, not creation/redemption flows
  (true flows need a premium feed not on the Starter tier).
- The private-credit **size** (Z.1) and the **equity-flow** monthly history are lagged; only
  the ICI *weekly* block and the FMP/FRED daily series are current.

**Nothing here is CAVEAT-ONLY** — every one of the three operator gauges has a real,
fetched, accessible source. The only honesty constraints are the *proxy labels* on private
credit and on bond-ETF trend, as noted above.

---

### Validation appendix (exact calls that returned the sample values)

```
# Gauge 1 — ICI equity flows (no key)
curl -s -A "Mozilla/5.0 (server data fetch)" \
  https://www.ici.org/system/files/2026-01/combined_flows_data_2026.xls -o ici.xls
# parse with repo venv:
.venv/bin/python -c "import pandas as pd; \
  df=pd.ExcelFile('ici.xls').parse('Weekly MF & ETF Public Report',header=None); \
  print(df.tail(8))"   # last weekly row 05/27/2026 -> Equity Total -2214

# Gauge 2 — BDC discount-to-NAV (FMP)
curl -s "https://financialmodelingprep.com/stable/ratios-ttm?symbol=ARCC&apikey=$FMP_API_KEY"
  # priceToBookRatioTTM 0.958, bookValuePerShareTTM 19.59  -> -4.2% discount
curl -s "https://financialmodelingprep.com/stable/quote?symbol=BIZD&apikey=$FMP_API_KEY"
  # price 12.45, yearLow 11.97, yearHigh 16.95

# Gauge 3a — BAA-AAA quality spread (FRED)
curl -s "https://api.stlouisfed.org/fred/series/observations?series_id=DBAA&api_key=$FRED_API_KEY&file_type=json&sort_order=desc&limit=1"  # 6.06 (2026-06-05)
curl -s "https://api.stlouisfed.org/fred/series/observations?series_id=DAAA&api_key=$FRED_API_KEY&file_type=json&sort_order=desc&limit=1"  # 5.53 (2026-06-05)

# Gauge 3b — IG/HY bond-ETF trend (FMP)
curl -s "https://financialmodelingprep.com/stable/quote?symbol=LQD&apikey=$FMP_API_KEY"  # 108.06, avg50 108.90, avg200 110.36
curl -s "https://financialmodelingprep.com/stable/quote?symbol=HYG&apikey=$FMP_API_KEY"  # 79.54, avg50 79.96, avg200 80.47

# Z.1 nonbank size context (FRED)
curl -s "https://api.stlouisfed.org/fred/series/observations?series_id=BOGZ1FL614090005Q&api_key=$FRED_API_KEY&file_type=json&sort_order=desc&limit=1"  # 2891672 (2025-Q4)
```
