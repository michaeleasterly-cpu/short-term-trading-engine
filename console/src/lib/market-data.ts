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
async function rfetch(
  url: string,
  init?: RequestInit & { next?: { revalidate?: number } },
  attempts = 4,
): Promise<Response> {
  for (let i = 0; i < attempts; i++) {
    const res = await fetch(url, init);
    if (res.status !== 429 && res.status < 500) return res;
    const ra = Number(res.headers.get("retry-after"));
    const waitMs = ra
      ? ra * 1000
      : Math.min(8000, 400 * 2 ** i) + Math.floor(Math.random() * 250);
    if (i < attempts - 1) await new Promise((r) => setTimeout(r, waitMs));
    else return res;
  }
  return fetch(url, init); // unreachable
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

// Excel serial date → ISO (Excel epoch base 1899-12-30 to absorb the 1900 leap bug).
function xlDate(serial: number): string {
  return new Date(Date.UTC(1899, 11, 30) + serial * 86400000).toISOString().slice(0, 10);
}

// ── helpers ───────────────────────────────────────────────────────────────
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

  const [
    fredResults, vixQ, vixHistD, spxHistD, gspcH, rspH, aaiiWb, capeText, gdpObs, equitiesObs,
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
  };
}
