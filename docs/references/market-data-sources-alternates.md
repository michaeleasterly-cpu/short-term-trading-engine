# Market-Data Source Alternates — Validated 2026-06-08

Three market gauges are unavailable on the operator's **FMP Starter** tier and need
alternate, authoritative, server-accessible feeds: **MOVE index**, **VVIX**, and
**index_concentration** (top-10 holdings % of S&P 500).

Every source below was **fetched live on 2026-06-08** with the operator's real FMP /
FRED keys (`.env`). A source only appears here if it returned **real, parseable data**
from a plain server fetch. `yfinance` / Yahoo scraping is BANNED (CLAUDE.md universal
invariant) and is not proposed anywhere in this doc.

Vendor-profile notes tie to **Task #8** (vendor-profile: rate-limit / pagination /
publish-lag / base-URL belong in the vendor profile, not re-derived per adapter).

---

## TL;DR — ranked recommendation per gauge

| Gauge | Verdict | Wire this |
|---|---|---|
| **VVIX** | **WORKS** | CBOE CDN CSV `VVIX_History.csv` (authoritative publisher, free, no key). VVIX = **92.40** as of 2026-06-08. |
| **index_concentration** | **WORKS** | **SSGA SPY holdings xlsx** — single HTTP call, has a pre-computed `Weight` column. Top-10 = **37.96%** as of 2026-06-05. No market-cap calls needed. |
| **MOVE index** | **PROXY-ONLY** | No free authoritative source exists (ICE-proprietary; not on FRED). Honest proxy: **TLT 21-day annualized realized vol** via FMP EOD (already in the operator's tier). Clearly labeled — NOT the MOVE index, different units. |

Key disqualifications proven below: **iShares IVV** CSV is bot-walled (returns HTML, not CSV, even with a browser UA). **FMP S&P 500 constituents** endpoint is `HTTP 402` (Starter-restricted). **FMP `^MOVE`/`^VVIX` quotes** are premium-only (`^VIX` is free). **FRED** carries `VIXCLS` but **neither MOVE nor VVIX**.

---

## 1. MOVE index — PROXY-ONLY (no free authoritative source)

### What was tested

| Candidate | Result |
|---|---|
| **FRED** series search (`MOVE`, `bond volatility`, `ICE BofA option volatility`, `Treasury implied volatility`) | **No series.** Zero matches. |
| **FRED ICE BofA release** (`release_id=209`, 192 series) | Carries only credit OAS / yield indices (e.g. `BAMLH0A0HYM2`). **No MOVE / no option-vol series.** |
| **FMP** `stable/quote?symbol=^MOVE` | `HTTP 402` Premium — *"not available under your current subscription"*. |
| **ICE Developer Portal** (`developer.ice.com/.../ice-data-indices-move-index`) | Licensed/commercial feed. Not free, not key-accessible. |

**Conclusion:** The ICE BofA MOVE index is genuinely **not available** from any free,
authoritative, machine-readable source. It is ICE-proprietary; FRED licenses ICE's
credit/yield indices but **not** the MOVE volatility index. This is an honest NO-SOURCE
for the real index.

### Proposed PROXY (clearly labeled — this is NOT the MOVE index)

**TLT 21-day annualized realized volatility**, computed from FMP daily EOD closes
(already on the operator's Starter tier — same `historical-price-eod/full` path the
engines use for daily bars).

- **Endpoint:** `GET https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=TLT&apikey=$FMP_API_KEY`
- **Validated 2026-06-08:** returned **1255 rows**, latest close `TLT 84.62 (2026-06-08)`.
- **Computed proxy value:** 21-day annualized realized vol = **9.2%** (log-return stdev × √252).

```python
# proxy computation (validated)
rets = [log(c[i]/c[i-1]) for i in window]      # last 21 daily closes
ann_vol = stdev(rets) * sqrt(252) * 100        # = 9.2% on 2026-06-08
```

**What the proxy CAN represent:** direction and regime of long-duration Treasury
turbulence — when bond stress rises, TLT realized vol rises, and the two are
directionally correlated with MOVE.

**What it CANNOT represent:** the MOVE index is *forward-looking implied* yield
volatility in **basis points** (typically ~60–140), built from OTC options on
2/5/10/30Y Treasuries. The TLT proxy is *backward-looking realized price* vol in
**percent**. **Different methodology, different units — never display it labeled "MOVE".**
Label it e.g. `"TLT 21d realized vol (MOVE proxy)"`.

> Alternative proxy if an *implied* measure is later wanted: TLT option-implied vol.
> Not available now — the project's only options feed (Tradier) is CLOSED
> (`project_tradier_closed_no_options`), and FMP options are premium. Realized-vol is the
> only honest proxy on the current tier.

**Verdict: PROXY-ONLY.** Wire `TLT 21d realized vol` via the existing FMP EOD path, labeled as a proxy.

---

## 2. VVIX — WORKS (CBOE authoritative CDN CSV)

### Validated source

- **URL:** `https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv`
- **Method:** plain `GET`, **no API key**, no auth, no special UA.
- **Fetched 2026-06-08:** `HTTP 200`, `Content-Type: text/csv`.

**Sample (real data retrieved):**

```
DATE,VVIX
03/06/2006,71.730000      <- full history back to 2006
...
06/03/2026,89.800000
06/04/2026,85.750000
06/05/2026,102.040000
06/08/2026,92.400000      <- VVIX = 92.40 as of 2026-06-08
```

- **Format:** 2-column CSV, `DATE,VVIX`, date as `MM/DD/YYYY`, full history from 2006-03-06.
- **Cadence / freshness:** daily, end-of-day. The 2026-06-08 row was present on fetch
  (same-day EOD availability).
- **Auth / rate-limit:** none required; public CDN. Be polite (cache once/day — it is an
  EOD series). **Vendor-profile (Task #8):** base-URL `cdn.cboe.com/api/global/us_indices/daily_prices/`,
  no key, EOD publish, full-history file (re-download is the whole series — cache locally,
  diff for new rows).
- **Cross-check sibling:** `.../VIX_History.csv` validated too (`HTTP 200`,
  OHLC 4-col, VIX close `21.51` on 2026-06-05) — useful to back the existing VIX card
  directly from the publisher instead of FMP if desired.

**Verdict: WORKS — wire it.** Authoritative publisher (CBOE), free, machine-readable, current.

> FRED does **not** carry VVIX (searched — no series). FRED *does* carry VIX as
> `VIXCLS` (validated: `2026-06-05 = 21.51`), confirming the existing VIX card's
> cross-check source. FMP `^VVIX` quote is premium (`HTTP 402`); `^VIX` is free
> (validated `^VIX = 18.92`).

---

## 3. index_concentration — WORKS (SSGA SPY holdings xlsx)

Goal: top-10 holdings as a % of the S&P 500.

### What was tested

| Candidate | Result |
|---|---|
| **(a) iShares IVV holdings CSV** (`ishares.com/.../IVV_holdings...ajax?fileType=csv`) | **Bot-walled.** `HTTP 200` but body is an HTML consent/bot page (1564 lines of HTML), not CSV — even with a browser `User-Agent` + `Referer`. **Not server-accessible.** DISQUALIFIED. |
| **(b) SSGA SPY holdings xlsx** | **WORKS** (see below). |
| **(c) FMP-computable path** (constituents + market caps) | **BLOCKED at constituents step:** `stable/sp500-constituent` → `HTTP 402` Restricted (Starter). The v3 legacy path → `HTTP 403`. Market-cap **batch** endpoint *does* work, but with no constituent list on this tier the pure-FMP path can't enumerate the 500. DISQUALIFIED on Starter. |

### Validated source (b)

- **URL:** `https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx`
- **Method:** `GET` with a **browser `User-Agent`** (required — default curl UA is fine on
  status but a normal browser UA returns the real file reliably). No API key.
- **Fetched 2026-06-08:** `HTTP 200`,
  `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  54,437 bytes, confirmed `Microsoft OOXML` (real xlsx, not an HTML wall).

**Sample (real data retrieved + parsed with `openpyxl`):** 608 rows; metadata header
says `Holdings: As of 05-Jun-2026`; data header row is
`Name, Ticker, Identifier, SEDOL, Weight, Sector`. **505 holdings parsed**, total weight
99.92%.

```
index_concentration (top-10 % of S&P 500) = 37.96%   (as of 2026-06-05)

  NVDA    7.857%   NVIDIA CORP
  AAPL    7.114%   APPLE INC
  MSFT    4.878%   MICROSOFT CORP
  AMZN    3.789%   AMAZON.COM INC
  GOOGL   3.405%   ALPHABET INC CL A
  AVGO    2.883%   BROADCOM INC
  GOOG    2.724%   ALPHABET INC CL C
  META    2.045%   META PLATFORMS INC CLASS A
  TSLA    1.734%   TESLA INC
  MU      1.533%   MICRON TECHNOLOGY INC
```

- **Key advantage:** the file already has a **`Weight` (%) column**, so concentration is a
  single sum of the top-10 weights — **no market-cap math, no per-constituent calls.**
- **Cadence / freshness:** daily holdings file; the 2026-06-08 fetch carried 05-Jun-2026
  holdings (SSGA publishes with a ~1–3 business-day lag; expected and fine for a
  slow-moving concentration gauge).
- **Auth / rate-limit:** none / no key. **Vendor-profile (Task #8):** base-URL
  `ssga.com/.../etfs/us/`, **browser UA required**, xlsx (parse with `openpyxl`), header
  block is 4 metadata rows then the column header — find the row where col 0 == `"Name"`
  rather than hard-coding an offset (SSGA occasionally shifts the preamble).

### Request budget comparison

| Path | Calls to get top-10 concentration |
|---|---|
| **SSGA SPY xlsx (recommended)** | **1 HTTP call**, no key. Weights pre-computed. |
| FMP-computable (if Starter ever exposes constituents) | 1 constituents call **+ up to 500 market-cap calls** (or ~ceil(500/N) batch calls via `market-capitalization-batch?symbols=...` — batch validated working), then sum top-10 / sum-all. Currently blocked: constituents = `HTTP 402`. |

**Verdict: WORKS — wire SSGA SPY xlsx.** One call, authoritative fund issuer (State
Street), pre-computed weights. Keep the FMP market-cap-batch path documented as a future
fallback **only if** the operator upgrades to a tier that exposes the constituents list.

---

## Appendix — validation log (all fetched 2026-06-08, operator's keys)

| Source | URL / endpoint | Result |
|---|---|---|
| FRED VIXCLS | `api.stlouisfed.org/fred/series/observations?series_id=VIXCLS` | `2026-06-05 = 21.51` ✓ |
| FRED MOVE search | `fred/series/search?search_text=MOVE...` | 0 series ✗ |
| FRED VVIX search | `fred/series/search?search_text=VVIX` | 0 series ✗ |
| FRED ICE BofA release 209 | `fred/release/series?release_id=209` | 192 series, no vol index ✗ |
| CBOE VVIX | `cdn.cboe.com/.../VVIX_History.csv` | `HTTP 200` text/csv, VVIX=92.40 (06/08/2026) ✓ |
| CBOE VIX | `cdn.cboe.com/.../VIX_History.csv` | `HTTP 200` OHLC, close=21.51 (06/05/2026) ✓ |
| FMP ^VIX | `stable/quote?symbol=^VIX` | `^VIX = 18.92` ✓ (free) |
| FMP ^MOVE / ^VVIX | `stable/quote?symbol=^MOVE` / `^VVIX` | `HTTP 402` premium ✗ |
| FMP TLT EOD | `stable/historical-price-eod/full?symbol=TLT` | 1255 rows, close 84.62 (06/08) ✓ |
| FMP sp500 constituents | `stable/sp500-constituent` | `HTTP 402` restricted ✗ |
| FMP market-cap batch | `stable/market-capitalization-batch?symbols=NVDA,MSFT,AAPL` | works ✓ (but no constituent list) |
| iShares IVV CSV | `ishares.com/.../IVV_holdings...ajax` | `HTTP 200` but HTML bot-wall, not CSV ✗ |
| SSGA SPY xlsx | `ssga.com/.../holdings-daily-us-en-spy.xlsx` | `HTTP 200` real OOXML, 505 holdings, top-10=37.96% ✓ |
