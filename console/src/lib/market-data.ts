/**
 * Self-fetching market-health data layer — runs entirely in Vercel, no DB.
 *
 * Source rule (operator, 2026-06-06): FMP live quotes for fast daily series
 * (VIX, S&P 500); FRED for slow weekly/monthly macro + historical; AAII via
 * its free .xls; CAPE/Buffett via fast-numerator (live price) ÷ slow-
 * denominator (Shiller E10 / FRED GDP); Fear & Greed computed (no scrape).
 * Every value is fresh or fast-numerator-computed, with a trend over a
 * sensible window. Daily cache (revalidate 24h; a Vercel cron forces a
 * fresh pull at 00:00 ET).
 *
 * Returns the exact shape the existing /market page renders (indicators keyed
 * by series_id + bear_score + summary + vix_series + spy_series), EXTENDED
 * with `valuation` (CAPE + Buffett) and `breadth` (participation/timing).
 */
import * as XLSX from "xlsx";

const FRED_KEY = process.env.FRED_API_KEY ?? "";
const FMP_KEY = process.env.FMP_API_KEY ?? "";
const DAY = 86400;
const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36";

// Shared retry/backoff wrapper around fetch. The 6 public regional/market pages
// prerender in parallel, which fans out concurrent FRED + FMP + multpl + AAII
// calls and can trip rate limits (HTTP 429) or transient 5xx. Retry those
// (honoring Retry-After when present, else exponential backoff + jitter) before
// the caller's graceful fallback kicks in. Success/4xx behavior unchanged.
// Concurrency limiter — caps in-flight external requests per render process so
// the parallel prerender of 6 data-heavy pages can't burst past FRED's rate
// limit (the real cause of cached "—%" nulls). Slots are held through backoff,
// so a rate-limited burst naturally pauses instead of hammering.
function makeLimiter(max: number) {
  let active = 0;
  const q: Array<() => void> = [];
  return async function run<T>(fn: () => Promise<T>): Promise<T> {
    if (active >= max) await new Promise<void>((r) => q.push(r));
    active++;
    try { return await fn(); } finally { active--; q.shift()?.(); }
  };
}
const _limit = makeLimiter(5);

async function rfetch(
  url: string,
  init?: RequestInit & { next?: { revalidate?: number } },
  attempts = 5,
): Promise<Response> {
  return _limit(async () => {
    for (let i = 0; i < attempts; i++) {
      const res = await fetch(url, init);
      if (res.status !== 429 && res.status < 500) return res;
      const ra = Number(res.headers.get("retry-after"));
      const waitMs = ra
        ? ra * 1000
        : Math.min(20000, 500 * 2 ** i) + Math.floor(Math.random() * 400);
      if (i < attempts - 1) await new Promise((r) => setTimeout(r, waitMs));
      else return res;
    }
    return fetch(url, init); // unreachable
  });
}

export type Dir = "up" | "down" | "flat";
export interface Trend { delta: number; pct: number | null; dir: Dir; window: string; }
export interface Indicator { value: number; date: string; trend?: Trend; }
export interface MarketHealth {
  ts: string;
  indicators: Record<string, Indicator>;
  vix_series: Array<{ date: string; value: number }>;
  spy_series: Array<{ date: string; close: number }>;
  bear_score: {
    score: number; raw: number; max_raw: number;
    breakdown: Record<string, number>;
  };
  summary: { vol_regime: string; macro_regime: string; headline: string };
  valuation: {
    cape: Indicator | null;
    buffett: Indicator | null;   // total market cap ÷ GDP, percent
    note: string;
  };
  breadth: {
    conc_1y: number;            // equal − cap-weight 1yr return, pp (− = narrow/cap-led)
    trend_20d: number;          // equal − cap-weight 20d return, pp (recent direction)
    state: "narrow" | "broad" | "mixed";
    note: string;
  };
  liquidity: Liquidity;
}

// ── Liquidity & positioning (Part 1: self-fetching, NO DB) ─────────────────
// Six gauges that sit between Breadth and Recession watch. Every value is
// fetched live (FRED + the quote feed) at render time — there is NO database
// read or write in this layer (Part 2, persisting to the macro table, is
// explicitly backlogged). NEVER fabricate: when a source can't be fetched, the
// gauge carries an `unavailable`/`not_wired` flag and the page renders a flag,
// not a number.
export interface NetLiquidity {
  usd_bn: number;          // (WALCL/1000) − WTREGEN − RRPONTSYD, all $bn
  change_13wk_bn: number | null;  // 13-week change in $bn (null if too little history)
  as_of: string;           // oldest of the 3 component observation dates
}
// MOVE is a DISPLAY-ONLY proxy (TLT 21-day realized vol, %). No free authoritative
// ICE MOVE source exists on this tier — see docs/references/market-data-sources-
// alternates.md §1. This is a realized-vol proxy in PERCENT, NOT the implied-vol
// MOVE index in basis points — it is CLEARLY labeled and is NOT fed into the
// composite (a realized-vol proxy is not the implied-vol stress signal).
export interface MoveIndex {
  proxy_value: number | null;  // TLT 21d annualized realized vol, % (null ⇒ unavailable)
  as_of: string | null;        // latest TLT EOD date the proxy was computed from
  available: boolean;
}
export interface Vvix {
  value: number | null;    // VVIX level from CBOE CDN (null ⇒ unavailable)
  as_of: string | null;    // the CSV's latest date (MM/DD/YYYY → ISO)
  available: boolean;
}
export interface IndexConcentration {
  pct: number;             // top-10 holdings % of S&P 500 (SSGA Weight column sum)
  as_of: string;           // SSGA file's holdings date
  is_proxy: boolean;       // false ⇒ real SSGA holdings; true ⇒ Mag-7 quote-feed fallback
  source: string;          // human label of where the number came from
}
export interface PassiveFlows {
  wired: false;            // Part 1 has no clean machine-readable source + no DB
  note: string;
}
export interface PrivateCreditNote {
  proxy_pct: number | null;   // optional public BDC discount-to-NAV, "rough proxy"
  proxy_label: string | null;
  note: string;
}
export interface Liquidity {
  net_liquidity: NetLiquidity | null;
  move: MoveIndex;
  vvix: Vvix;
  concentration: IndexConcentration | null;
  passive_flows: PassiveFlows;
  private_credit: PrivateCreditNote;
}

// ── low-level fetchers ────────────────────────────────────────────────────
type Obs = [string, number];

async function fredSeries(seriesId: string, days = 480): Promise<Obs[]> {
  const start = new Date(Date.now() - days * DAY * 1000).toISOString().slice(0, 10);
  const url =
    `https://api.stlouisfed.org/fred/series/observations?series_id=${seriesId}` +
    `&api_key=${FRED_KEY}&file_type=json&observation_start=${start}&sort_order=asc`;
  const res = await rfetch(url, { next: { revalidate: DAY } });
  if (!res.ok) return [];
  const j = (await res.json()) as { observations?: Array<{ date: string; value: string }> };
  return (j.observations ?? [])
    .filter((o) => o.value !== ".")
    .map((o) => [o.date, Number(o.value)] as Obs);
}

interface FmpQuote { price: number; previousClose?: number; changePercentage?: number; }
async function fmpQuote(symbol: string): Promise<FmpQuote | null> {
  const url = `https://financialmodelingprep.com/stable/quote?symbol=${encodeURIComponent(symbol)}&apikey=${FMP_KEY}`;
  const res = await rfetch(url, { next: { revalidate: DAY } });
  if (!res.ok) return null;
  const j = (await res.json()) as FmpQuote[];
  return Array.isArray(j) && j.length ? j[0] : null;
}

async function fmpHistory(symbol: string, keep = 220): Promise<number[]> {
  const url = `https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=${encodeURIComponent(symbol)}&apikey=${FMP_KEY}`;
  const res = await rfetch(url, { next: { revalidate: DAY } });
  if (!res.ok) return [];
  const j = await res.json();
  const rows: Array<{ date: string; close: number }> = Array.isArray(j) ? j : (j.historical ?? []);
  return rows
    .filter((r) => r.close != null)
    .map((r) => [r.date, Number(r.close)] as Obs)
    .sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .slice(-keep)
    .map((o) => o[1]);
}

async function fmpHistoryDated(symbol: string, keep = 200): Promise<Obs[]> {
  const url = `https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=${encodeURIComponent(symbol)}&apikey=${FMP_KEY}`;
  const res = await rfetch(url, { next: { revalidate: DAY } });
  if (!res.ok) return [];
  const j = await res.json();
  const rows: Array<{ date: string; close: number }> = Array.isArray(j) ? j : (j.historical ?? []);
  return rows
    .filter((r) => r.close != null)
    .map((r) => [r.date, Number(r.close)] as Obs)
    .sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .slice(-keep);
}

async function fetchXls(url: string): Promise<XLSX.WorkBook | null> {
  const res = await rfetch(url, {
    headers: { "User-Agent": UA, Referer: new URL(url).origin },
    next: { revalidate: DAY },
  });
  if (!res.ok) return null;
  const buf = Buffer.from(await res.arrayBuffer());
  return XLSX.read(buf, { type: "buffer" });
}

async function fetchText(url: string): Promise<string> {
  const res = await rfetch(url, { headers: { "User-Agent": UA }, next: { revalidate: DAY } });
  return res.ok ? res.text() : "";
}

// FMP quote incl. market cap — for the Mag-7 concentration PROXY only. Same feed
// as the VIX/S&P quotes; reuses rfetch. Returns null on any failure (no fabricate).
interface FmpQuoteCap { price: number; marketCap?: number; }
async function fmpMarketCap(symbol: string): Promise<number | null> {
  const url = `https://financialmodelingprep.com/stable/quote?symbol=${encodeURIComponent(symbol)}&apikey=${FMP_KEY}`;
  const res = await rfetch(url, { next: { revalidate: DAY } });
  if (!res.ok) return null;
  const j = (await res.json()) as FmpQuoteCap[];
  const cap = Array.isArray(j) && j.length ? j[0]?.marketCap : null;
  return typeof cap === "number" && cap > 0 ? cap : null;
}

// VVIX — CBOE's authoritative daily-EOD CSV (free, no key). Two columns:
// DATE (MM/DD/YYYY), VVIX (close). Full history back to 2006; we only need the
// last row. Validated 2026-06-08 (VVIX = 92.40). Returns the latest [iso, value]
// or null on any fetch/parse failure (never fabricate). Reuses rfetch.
//   Source: docs/references/market-data-sources-alternates.md §2.
async function fetchCboeVvix(): Promise<Obs | null> {
  const csv = await fetchText(
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
  );
  if (!csv) return null;
  const lines = csv.trim().split(/\r?\n/);
  // Walk from the bottom for the last parseable "MM/DD/YYYY,<number>" row.
  for (let i = lines.length - 1; i >= 1; i--) {
    const parts = lines[i].split(",");
    if (parts.length < 2) continue;
    const m = parts[0].trim().match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    const val = Number(parts[1]);
    if (m && Number.isFinite(val) && val > 0) {
      const iso = `${m[3]}-${m[1]}-${m[2]}`; // MM/DD/YYYY → YYYY-MM-DD
      return [iso, Math.round(val * 100) / 100];
    }
  }
  return null;
}

// Index concentration — top-10 holdings % of S&P 500. Primary source: the SSGA
// SPY holdings xlsx, which carries a pre-computed `Weight` column (% units) — one
// HTTP call, no key, browser UA + redirect-follow (the published URL 301s to a
// no-locale path; native fetch follows redirects). Validated 2026-06-08:
// top-10 = 37.96%, holdings "As of 05-Jun-2026". Returns { pct, as_of } or null
// on any parse failure → orchestrator falls back to the labeled Mag-7 proxy.
//   Source: docs/references/market-data-sources-alternates.md §3.
// NB: find the header row where col 0 == "Name" rather than hard-coding an
// offset (SSGA occasionally shifts the metadata preamble).
async function fetchSsgaTop10(): Promise<{ pct: number; as_of: string } | null> {
  const wb = await fetchXls(
    "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx",
  );
  if (!wb) return null;
  try {
    const sh = wb.Sheets[wb.SheetNames[0]];
    const rows = XLSX.utils.sheet_to_json<(string | number)[]>(sh, { header: 1 });
    // Holdings date lives in a metadata preamble row like ["Holdings:", "As of 05-Jun-2026"].
    let as_of = "";
    let headerIdx = -1;
    for (let i = 0; i < Math.min(rows.length, 20); i++) {
      const r = rows[i];
      if (!r) continue;
      if (headerIdx === -1 && String(r[0]).trim() === "Name") { headerIdx = i; break; }
      const joined = r.map((c) => String(c)).join(" ");
      const dm = joined.match(/As of\s+(\d{2})-([A-Za-z]{3})-(\d{4})/);
      if (dm) {
        const months: Record<string, string> = {
          Jan: "01", Feb: "02", Mar: "03", Apr: "04", May: "05", Jun: "06",
          Jul: "07", Aug: "08", Sep: "09", Oct: "10", Nov: "11", Dec: "12",
        };
        const mo = months[dm[2]];
        if (mo) as_of = `${dm[3]}-${mo}-${dm[1]}`; // DD-Mon-YYYY → YYYY-MM-DD
      }
    }
    if (headerIdx === -1) return null;
    const hdr = (rows[headerIdx] as (string | number)[]).map((c) => String(c).trim());
    const wi = hdr.indexOf("Weight");
    const ni = hdr.indexOf("Name");
    if (wi === -1 || ni === -1) return null;
    const weights: number[] = [];
    for (let i = headerIdx + 1; i < rows.length; i++) {
      const r = rows[i];
      const name = r?.[ni];
      if (name == null || String(name).trim() === "") break; // data ends at first blank Name
      const w = Number(r[wi]);
      if (Number.isFinite(w) && w > 0) weights.push(w);
    }
    if (weights.length < 10) return null;
    // Already weight-sorted desc in the SSGA file; sort defensively then take top-10.
    weights.sort((a, b) => b - a);
    const top10 = weights.slice(0, 10).reduce((s, w) => s + w, 0);
    // Sanity: a plausible top-10 concentration is ~25-45%. Reject implausible sums.
    if (!(top10 > 15 && top10 < 60)) return null;
    return { pct: Math.round(top10 * 100) / 100, as_of: as_of || new Date().toISOString().slice(0, 10) };
  } catch {
    return null;
  }
}

// MOVE proxy — TLT 21-day annualized realized volatility (%), computed from FMP
// daily EOD closes (the same path the engines use). DISPLAY-ONLY and clearly
// labeled — this is a realized-vol proxy in percent, NOT the implied-vol ICE MOVE
// index in basis points. It is NOT fed into the composite. Returns { value, as_of }
// or null. Source: docs/references/market-data-sources-alternates.md §1.
//   ann_vol = stdev(log daily returns over last 21 closes) × √252 × 100
async function fetchTltMoveProxy(): Promise<{ value: number; as_of: string } | null> {
  const obs = await fmpHistoryDated("TLT", 30);
  if (obs.length < 22) return null;
  const closes = obs.map((o) => o[1]).slice(-22); // 22 closes → 21 returns
  const rets: number[] = [];
  for (let i = 1; i < closes.length; i++) {
    if (closes[i - 1] > 0 && closes[i] > 0) rets.push(Math.log(closes[i] / closes[i - 1]));
  }
  if (rets.length < 2) return null;
  const m = mean(rets);
  const variance = rets.reduce((s, x) => s + (x - m) ** 2, 0) / (rets.length - 1); // sample stdev
  const annVol = Math.sqrt(variance) * Math.sqrt(252) * 100;
  if (!Number.isFinite(annVol) || annVol <= 0) return null;
  return { value: Math.round(annVol * 10) / 10, as_of: obs[obs.length - 1][0] };
}

// Excel serial date → ISO (Excel epoch base 1899-12-30 to absorb the 1900 leap bug).
function xlDate(serial: number): string {
  return new Date(Date.UTC(1899, 11, 30) + serial * 86400000).toISOString().slice(0, 10);
}

// ── helpers ───────────────────────────────────────────────────────────────
// Piecewise-linear interpolation across (input → risk) anchor points, clamped
// 0-100. Mirror of the page-side `interp` (kept local so the composite-input
// risk maps below are exported + unit-testable without importing the page).
export function interp(x: number, anchors: Array<[number, number]>): number {
  const asc = anchors[0][0] < anchors[anchors.length - 1][0];
  const pts = asc ? anchors : [...anchors].reverse();
  if (x <= pts[0][0]) return Math.max(0, Math.min(100, pts[0][1]));
  if (x >= pts[pts.length - 1][0]) return Math.max(0, Math.min(100, pts[pts.length - 1][1]));
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[i + 1];
    if (x >= x0 && x <= x1) {
      const f = x1 === x0 ? 0 : (x - x0) / (x1 - x0);
      return Math.max(0, Math.min(100, y0 + f * (y1 - y0)));
    }
  }
  return 50;
}
const mean = (a: number[]) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0);
const ma = (a: number[], n: number) => mean(a.slice(-Math.min(n, a.length)));
const clamp = (x: number) => Math.max(0, Math.min(100, x));

function trend(obs: Obs[], n: number, window: string): Trend | undefined {
  if (obs.length < 2) return undefined;
  const k = Math.min(n, obs.length - 1);
  const cur = obs[obs.length - 1][1];
  const prev = obs[obs.length - 1 - k][1];
  const delta = cur - prev;
  const pct = prev ? (delta / Math.abs(prev)) * 100 : null;
  const dir: Dir = delta > 1e-9 ? "up" : delta < -1e-9 ? "down" : "flat";
  return { delta, pct, dir, window };
}

function ind(obs: Obs[], n: number, window: string): Indicator | null {
  if (!obs.length) return null;
  const [date, value] = obs[obs.length - 1];
  return { value, date, trend: trend(obs, n, window) };
}

// ── Net liquidity (Fed balance sheet − TGA − RRP), unit-asserted ───────────
// CRITICAL: we ASSERT the FRED-reported units rather than assume them. Verified
// against FRED series metadata (2026-06-08):
//   WALCL     — "Millions of U.S. Dollars"  → ÷1000 to billions
//   WTREGEN   — "Millions of U.S. Dollars"  → ÷1000 to billions  (NOT billions!)
//   RRPONTSYD — "Billions of US Dollars"    → already billions
// (An earlier spec note had WTREGEN in billions; FRED reports it in MILLIONS,
//  which is exactly why "assert, don't assume" — getting this wrong yields a
//  nonsensical −$869,000bn instead of ~+$5,800bn.)
// Exported pure so the unit-normalization test pins the millions→billions step.
//   net_liquidity_usd_bn = (WALCL/1000) − (WTREGEN/1000) − RRPONTSYD
export function netLiquidityUsdBn(
  walclMillions: number,
  wtregenMillions: number,
  rrpBillions: number,
): number {
  return walclMillions / 1000 - wtregenMillions / 1000 - rrpBillions;
}

// MOVE band thresholds (bond-market implied vol): <80 calm / 80-120 watch / >120
// stressed. Retained for reference/back-compat (the real ICE MOVE is unavailable
// on this tier; what we display is a TLT realized-vol PROXY that is NOT banded as
// MOVE and NOT fed into the composite — see fetchTltMoveProxy).
export function moveBand(value: number): "calm" | "watch" | "stressed" {
  return value < 80 ? "calm" : value <= 120 ? "watch" : "stressed";
}

// ── Composite Timing-block risk maps (JUDGMENT-CALIBRATED HEURISTICS) ─────────
// These interpolation bands are author-chosen, calibrated against observed
// historical ranges — they are NOT literature-derived. See
// docs/references/market-composite-references.md §3.3 (VVIX) which validates the
// 80/100/120/150 ladder as "sensible-but-judgment-call, NOT literature-backed".
// Centralized + exported so the timing-block test references the same anchors.
//
// VVIX → risk: long-run avg ≈ 85-90; <80 complacent, sustained >120 elevated
// fear/dislocation, ~150 near observed extremes.
export function vvixRiskFromLevel(vvix: number): number {
  return interp(vvix, [[80, 0], [100, 40], [120, 70], [150, 100]]);
}
// top-10 concentration % → breadth risk: 20% ≈ historically normal, 30% ≈
// elevated, 40% ≈ extreme top-heaviness (record ~37-40% in 2024-25).
export function concRiskFromTop10(top10Pct: number): number {
  return interp(top10Pct, [[20, 0], [30, 50], [40, 100]]);
}

// ── orchestrator ────────────────────────────────────────────────────────
export async function getMarketHealth(): Promise<MarketHealth> {
  // FRED slow series — (key, FRED id, trend-window-obs, window-label)
  // Trend windows per the markets-expert verdict (2026-06-06): SHOW-set series
  // get a window where rate-of-change is signal; LEVEL-ONLY series carry a
  // nominal window the page never renders. Daily series count in business days.
  const FRED: Array<[string, string, number, string]> = [
    ["yield_curve", "T10Y2Y", 65, "3 months"],         // SHOW (bps)
    ["t10y3m", "T10Y3M", 65, "3 months"],               // level-only (composite input; stronger recession predictor than 10Y-2Y)
    ["sahm_rule", "SAHMREALTIME", 3, "3mo"],            // level-only (already a trend)
    ["cfnai_ma3", "CFNAIMA3", 3, "3mo"],                // level-only (already smoothed)
    ["hy_spread", "BAMLH0A0HYM2", 20, "1 month"],       // SHOW (bps)
    ["credit_spread", "BAA10Y", 65, "3 months"],        // SHOW (bps)
    ["nfci", "NFCI", 4, "4wk"],                          // level-only (index around 0)
    ["epu_index", "USEPUINDXD", 20, "20d"],             // level-only (too noisy)
    ["initial_claims", "IC4WSA", 13, "13 weeks"],       // SHOW (count)
    ["michigan_sentiment", "UMCSENT", 12, "12 months"], // SHOW (points)
    ["unemployment_rate", "UNRATE", 12, "12 months"],   // SHOW (pp)
    ["fed_funds_rate", "DFF", 20, "20d"],               // level-only (policy step fn)
    ["industrial_production", "INDPRO", 3, "3mo"],       // level-only (bear-score input)
  ];

  // Mag-7 — the Magnificent Seven; concentration PROXY constituents (labeled).
  const MAG7 = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"];

  const [
    fredResults, vixQ, vixHistD, spxHistD, gspcH, rspH, aaiiWb, capeText, gdpObs, equitiesObs,
    // ── liquidity & positioning (Part 1, self-fetching, NO DB) ──
    walclObs, wtregenObs, rrpObs, moveProxy, vvixObs, ssgaTop10, mag7Caps, spxTotalCap,
  ] = await Promise.all([
    Promise.all(FRED.map(([, id]) => fredSeries(id))),
    fmpQuote("^VIX"),
    fmpHistoryDated("^VIX"),
    fmpHistoryDated("^GSPC"),
    fmpHistory("^GSPC", 300),  // ~1yr — breadth concentration needs the long window
    fmpHistory("RSP", 300),
    fetchXls("https://www.aaii.com/files/surveys/sentiment.xls"),
    fetchText("https://www.multpl.com/shiller-pe"),  // CAPE — robust daily current value
    fredSeries("GDP", 1500),
    fredSeries("NCBEILQ027S", 1500),  // Fed Z.1 corporate-equities market value (Buffett numerator)
    // Net liquidity components — assert units at fetch (see netLiquidityUsdBn):
    fredSeries("WALCL", 200),       // Fed total assets — $MILLIONS (weekly)
    fredSeries("WTREGEN", 200),     // Treasury General Account — $MILLIONS (daily) [FRED-verified, NOT billions]
    fredSeries("RRPONTSYD", 200),   // Overnight reverse repo — $BILLIONS (daily)
    fetchTltMoveProxy(),            // MOVE display-only PROXY: TLT 21d realized vol via FMP EOD
    fetchCboeVvix(),                // VVIX: CBOE authoritative daily-EOD CSV (free, no key)
    fetchSsgaTop10(),               // index concentration: SSGA SPY holdings xlsx (pre-computed Weight col)
    Promise.all(MAG7.map((s) => fmpMarketCap(s))),  // proxy numerator (Mag-7 caps) — concentration FALLBACK only
    fmpMarketCap("^GSPC"),          // proxy denominator: S&P 500 total mkt cap (0 on this feed → proxy unavailable, NOT fabricated)
  ]);

  const indicators: Record<string, Indicator> = {};
  FRED.forEach(([key, , n, w], i) => {
    const got = ind(fredResults[i], n, w);
    if (got) indicators[key] = got;
  });

  // VIX — FMP live quote (fresh, no FRED lag). Append the live quote to the EOD
  // history so the chart's right edge is the CURRENT VIX (not yesterday's close)
  // — the chart updates itself on every refresh. Trend off the same series.
  const vixVals = vixHistD.map((o) => o[1]);
  const vixChartObs: Obs[] = [...vixHistD];
  if (vixQ) {
    const today = new Date().toISOString().slice(0, 10);
    const last = vixChartObs[vixChartObs.length - 1];
    if (last && last[0] === today) vixChartObs[vixChartObs.length - 1] = [today, vixQ.price];
    else vixChartObs.push([today, vixQ.price]);
    indicators["vix"] = { value: vixQ.price, date: today, trend: trend(vixChartObs, 5, "5d") };
  } else if (vixVals.length) {
    indicators["vix"] = ind(vixHistD, 5, "5d")!;
  }

  // S&P 500 series (for chart + momentum) — FMP.
  const spxVals = spxHistD.map((o) => o[1]);

  // AAII — direct .xls (sheet SENTIMENT; bull col 1, bear col 3, from row 7).
  if (aaiiWb) {
    try {
      const sh = aaiiWb.Sheets["SENTIMENT"];
      const rows = XLSX.utils.sheet_to_json<(string | number)[]>(sh, { header: 1 });
      const bullSeries: Obs[] = [];
      const bearSeries: Obs[] = [];
      for (let i = 7; i < rows.length; i++) {
        const r = rows[i];
        if (typeof r?.[0] !== "number") break;
        const d = xlDate(r[0] as number);
        if (typeof r[1] === "number") bullSeries.push([d, (r[1] as number) * 100]);
        if (typeof r[3] === "number") bearSeries.push([d, (r[3] as number) * 100]);
      }
      if (bullSeries.length) indicators["bullish_pct"] = ind(bullSeries, 4, "4wk")!;
      if (bearSeries.length) indicators["bearish_pct"] = ind(bearSeries, 4, "4wk")!;
    } catch { /* AAII parse failure → omit, page degrades gracefully */ }
  }

  // ── Fear & Greed (computed, 0=fear..100=greed) ──────────────────────────
  const momentum = spxVals.length ? clamp(50 + (spxVals[spxVals.length - 1] / ma(spxVals, 125) - 1) * 500) : 50;
  const volatility = vixVals.length ? clamp(50 - (vixVals[vixVals.length - 1] / ma(vixVals, 50) - 1) * 200) : 50;
  const hy = indicators["hy_spread"]?.value ?? 3.5;
  const junk = clamp(100 - ((hy - 2.0) / (6.0 - 2.0)) * 100);
  const sh20 = spxVals.length > 21 ? (spxVals[spxVals.length - 1] / spxVals[spxVals.length - 22] - 1) * 100 : 0;
  const safeHaven = clamp(50 + sh20 * 5);
  const fg = Math.round((momentum + volatility + junk + safeHaven) / 4);
  indicators["score"] = { value: fg, date: new Date().toISOString().slice(0, 10) };

  // ── Valuation: CAPE (multpl.com current) + Buffett (Fed Z.1 equities ÷ GDP) ──
  // CAPE: multpl re-prices Shiller's P/E10 ~daily off the live S&P (the Shiller
  // Excel itself is monthly). Robust regex over the "Current" figure.
  let cape: Indicator | null = null;
  const capeMatch = capeText.match(/Current[^0-9]{0,40}?([0-9]{1,3}\.[0-9]+)/);
  if (capeMatch) {
    cape = { value: Number(capeMatch[1]), date: new Date().toISOString().slice(0, 10) };
  }

  // Buffett: total US corporate-equities market value ÷ GDP. FRED NCBEILQ027S
  // (Fed Z.1, $millions, quarterly) ÷ GDP ($billions, quarterly) × 100.
  let buffett: Indicator | null = null;
  const gdp = gdpObs.length ? gdpObs[gdpObs.length - 1][1] : null;
  const equities = equitiesObs.length ? equitiesObs[equitiesObs.length - 1] : null;
  if (gdp && equities) {
    const ratio = ((equities[1] / 1000) / gdp) * 100; // $M→$B ÷ $B
    buffett = { value: Math.round(ratio), date: equities[0] };
  }

  // ── Breadth: equal-weight (RSP) vs cap-weight (^GSPC). CONCENTRATION is a
  // multi-month story (the AI/mega-cap effect) — lead with the 1-year gap; the
  // 20-day gap is only the recent direction. A 20-day-only read mislabels a
  // structurally narrow market as "even".
  const gapAt = (n: number): number => {
    if (gspcH.length <= n || rspH.length <= n) return 0;
    const cw = (gspcH[gspcH.length - 1] / gspcH[gspcH.length - 1 - n] - 1) * 100;
    const ew = (rspH[rspH.length - 1] / rspH[rspH.length - 1 - n] - 1) * 100;
    return ew - cw; // + = equal-weight leading (broad); − = cap-weight/mega-caps leading (narrow)
  };
  const conc1y = Math.round(gapAt(252) * 10) / 10;
  const trend20 = Math.round(gapAt(20) * 10) / 10;
  const recentDir = trend20 > 0.5 ? "broadening" : trend20 < -0.5 ? "narrowing further" : "roughly flat";
  const breadth: MarketHealth["breadth"] = {
    conc_1y: conc1y,
    trend_20d: trend20,
    state: conc1y < -2 ? "narrow" : conc1y > 2 ? "broad" : "mixed",
    note:
      conc1y < -2
        ? `Over the past year a small group of giant (cap-weighted) stocks — the mega-cap/AI names — led the average stock by ~${Math.abs(Math.round(conc1y))}pp. Participation is narrow: a handful of names is carrying the index. Last few weeks: breadth ${recentDir}.`
        : conc1y > 2
        ? `Over the past year the average stock kept pace with or beat the mega-caps (~${Math.round(conc1y)}pp) — participation is broad. Last few weeks: ${recentDir}.`
        : `Over the past year large and average stocks are roughly even. Last few weeks: ${recentDir}.`,
  };

  // ── Bear score (mirror of console-api scorer; VIX now fresh) ────────────
  const g = (k: string) => indicators[k]?.value;
  const bsSahm = (g("sahm_rule") ?? 0) >= 0.5 ? 25 : 0;
  const ip = g("industrial_production");
  const bsIp = ip == null ? 0 : ip < 90 ? 15 : ip < 95 ? 10 : 0;
  const bsClaims = (g("initial_claims") ?? 0) >= 260000 ? 10 : 0;
  const yc = g("yield_curve");
  const bsYield = yc != null && yc < 0 ? 15 : 0;
  const cs = g("credit_spread");
  const bsCredit = cs == null ? 0 : cs >= 5 ? 5 : cs >= 4 ? 3 : cs >= 3 ? 2 : 0;
  const bsVix = (g("vix") ?? 0) >= 25 ? 15 : 0;
  const raw = bsSahm + bsIp + bsClaims + bsYield + bsCredit + bsVix;
  const bear_score = {
    score: Math.round((raw / 85) * 100), raw, max_raw: 85,
    breakdown: { sahm_rule: bsSahm, industrial_production: bsIp, initial_claims: bsClaims, yield_curve: bsYield, credit_spread: bsCredit, vix: bsVix },
  };

  // ── Liquidity & positioning (Part 1: self-fetching, NO DB) ──────────────
  // 1. net_liquidity = (WALCL/1000) − (WTREGEN/1000) − RRPONTSYD, units asserted.
  //    FRED-verified units (NOT assumed): WALCL $MILLIONS, WTREGEN $MILLIONS,
  //    RRPONTSYD $BILLIONS. netLiquidityUsdBn normalizes WALCL + WTREGEN
  //    millions→billions FIRST. as_of = oldest of the 3 dates so the card never
  //    overstates freshness (FRED series publish on different lags).
  let net_liquidity: NetLiquidity | null = null;
  if (walclObs.length && wtregenObs.length && rrpObs.length) {
    const w = walclObs[walclObs.length - 1];       // [date, $millions]
    const tg = wtregenObs[wtregenObs.length - 1];  // [date, $millions]
    const rr = rrpObs[rrpObs.length - 1];          // [date, $billions]
    const usd_bn = netLiquidityUsdBn(w[1], tg[1], rr[1]);
    // 13-week change: build a weekly net-liquidity series by aligning each
    // WALCL weekly obs to the nearest-prior TGA + RRP obs, then diff vs ~13wk ago.
    const nlSeries: Obs[] = walclObs.map((wo) => {
      const prior = (arr: Obs[]): number | null => {
        let v: number | null = null;
        for (const o of arr) { if (o[0] <= wo[0]) v = o[1]; else break; }
        return v;
      };
      const tgv = prior(wtregenObs);
      const rrv = prior(rrpObs);
      return tgv != null && rrv != null
        ? [wo[0], netLiquidityUsdBn(wo[1], tgv, rrv)] as Obs
        : [wo[0], NaN] as Obs;
    }).filter((o) => !Number.isNaN(o[1]));
    let change_13wk_bn: number | null = null;
    if (nlSeries.length > 13) {
      const k = Math.min(13, nlSeries.length - 1);
      change_13wk_bn = Math.round((nlSeries[nlSeries.length - 1][1] - nlSeries[nlSeries.length - 1 - k][1]) * 10) / 10;
    }
    const as_of = [w[0], tg[0], rr[0]].sort()[0]; // oldest (lexicographic ISO = chronological)
    net_liquidity = { usd_bn: Math.round(usd_bn * 10) / 10, change_13wk_bn, as_of };
  }

  // 2. move_index — DISPLAY-ONLY PROXY: TLT 21-day annualized realized vol (%),
  //    from FMP EOD closes. No free authoritative ICE MOVE source exists on this
  //    tier (docs/references/market-data-sources-alternates.md §1). This is a
  //    realized-vol proxy in PERCENT, NOT the implied-vol MOVE index in basis
  //    points — clearly labeled, and NOT fed into the composite. Null on any
  //    fetch/parse failure → card flags unavailable, never fabricated.
  const move: MoveIndex = moveProxy
    ? { proxy_value: moveProxy.value, as_of: moveProxy.as_of, available: true }
    : { proxy_value: null, as_of: null, available: false };

  // 3. vvix — CBOE authoritative daily-EOD CSV (free, no key). Companion to VIX.
  //    Latest [iso, close] or null → flag unavailable rather than fabricate.
  const vvix: Vvix = vvixObs
    ? { value: vvixObs[1], as_of: vvixObs[0], available: true }
    : { value: null, as_of: null, available: false };

  // 4. index_concentration — top-10 % of S&P 500. Primary: SSGA SPY holdings xlsx
  //    (pre-computed Weight column, one call, no key). FALLBACK (only if SSGA is
  //    unreachable): a clearly-labeled Mag-7 quote-feed proxy (Σ Mag-7 caps ÷
  //    S&P 500 total cap). The quote feed reports marketCap=0 for ^GSPC, so when
  //    no real total cap is available the proxy is UNAVAILABLE rather than
  //    fabricated — the card then flags "unavailable", never a fake number.
  let concentration: IndexConcentration | null = null;
  if (ssgaTop10 != null) {
    concentration = { pct: ssgaTop10.pct, as_of: ssgaTop10.as_of, is_proxy: false, source: "SSGA SPY holdings (top-10 Weight column)" };
  } else {
    const caps = (mag7Caps as Array<number | null>).filter((c): c is number => c != null && c > 0);
    const mag7Sum = caps.reduce((s, c) => s + c, 0);
    if (caps.length === MAG7.length && spxTotalCap != null && spxTotalCap > mag7Sum) {
      const pct = (mag7Sum / spxTotalCap) * 100;
      concentration = { pct: Math.round(pct * 10) / 10, as_of: new Date().toISOString().slice(0, 10), is_proxy: true, source: "Mag-7 ÷ S&P 500 total cap (quote-feed proxy)" };
    }
    // else: no reliable source → concentration stays null → card flags unavailable.
  }

  // 5. passive_flows_4wk — ICI weekly equity fund+ETF flows. No clean machine-
  //    readable source in Part 1 and NO DB → scaffold the card with an honest
  //    "not yet wired" flag. NEVER fabricate a number.
  const passive_flows: PassiveFlows = {
    wired: false,
    note: "ICI weekly fund-flow data has no clean machine-readable feed; persisting it is Part 2 (backlogged). Not yet wired.",
  };

  // 6. private_credit_note — informational CAVEAT only (a structural blind spot:
  //    risk has migrated into private credit that doesn't mark-to-market daily).
  //    Not a live gauge. No live proxy fetched in Part 1 → proxy stays null.
  const private_credit: PrivateCreditNote = {
    proxy_pct: null,
    proxy_label: null,
    note: "Caveat, not a gauge: a growing share of corporate-credit risk now sits in private credit and direct-lending funds that don't mark-to-market daily, so it doesn't show up in the public spread series above. Treat the credit gauges as a partial view.",
  };

  const liquidity: Liquidity = {
    net_liquidity, move, vvix, concentration, passive_flows, private_credit,
  };

  // ── summary regime ──────────────────────────────────────────────────────
  const vix = g("vix");
  const vol_regime = vix == null ? "unknown" : vix < 15 ? "calm" : vix < 20 ? "normal" : vix < 30 ? "stress" : "crisis";
  const macro_regime = yc == null ? "unknown" : yc < 0 ? "inverted" : "normal";

  return {
    ts: new Date().toISOString(),
    indicators,
    vix_series: vixHistD.slice(-126).map(([date, value]) => ({ date, value })), // ~6 months
    spy_series: spxHistD.map(([date, close]) => ({ date, close })),
    bear_score,
    summary: {
      vol_regime, macro_regime,
      headline:
        vol_regime === "crisis" ? "Crisis vol regime"
        : vol_regime === "stress" ? "Stressed vol regime"
        : vol_regime === "calm" ? "Calm vol regime" : "Normal vol regime",
    },
    valuation: {
      cape, buffett,
      note: "CAPE + Buffett both gauge how expensive stocks are (not when anything happens). Both stretched is a stronger signal than either alone.",
    },
    breadth,
    liquidity,
  };
}
