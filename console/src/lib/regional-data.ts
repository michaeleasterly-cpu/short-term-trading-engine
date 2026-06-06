/**
 * Self-fetching REGIONAL economic-development data layer — runs entirely in
 * Vercel, no DB, no console-api. Faithful TypeScript port of the console-api
 * `public_*` regional endpoints (console-api/main.py): FRED macro series + live
 * Census ACS + BLS QCEW + USAspending + ACS labor-truth + a pure community-
 * health-score derivation.
 *
 * Footprint #1 (Charleston / Coles County) is the first consumer. The other 4
 * footprints (carbondale=Jackson, murphysboro, cefs=13-county, mantracon) reuse
 * these helpers via a `FootprintConfig` — county-fips arrays + a place-fips +
 * a fred-series map (indicatorKey → FRED series_id).
 *
 * Every fetch uses Next `fetch(url, { next: { revalidate: 86400 } })` for a
 * daily cache, matching the /market market-data.ts idiom. The page that calls
 * getRegionalData() should set `export const revalidate = 86400`.
 *
 * Source rule (ported verbatim from console-api):
 *  - FRED indicators: api.stlouisfed.org, key in process.env.FRED_API_KEY.
 *  - Census ACS (DP profile + B23025): api.census.gov, key in
 *    process.env.CENSUS_DATA_API_KEY (or CENSUS_API_KEY). REQUIRED for the
 *    demographics / labor-truth / health-score sections — without it those
 *    sections return empty and the page degrades gracefully.
 *  - BLS QCEW: data.bls.gov, KEYLESS (descriptive User-Agent only).
 *  - USAspending: api.usaspending.gov, KEYLESS (POST search endpoints).
 */

const FRED_KEY = process.env.FRED_API_KEY ?? "";
const CENSUS_KEY =
  process.env.CENSUS_DATA_API_KEY ?? process.env.CENSUS_API_KEY ?? "";
const DAY = 86400;

// BLS throttles unidentified UAs from cloud egress IPs; a descriptive UA with a
// contact endpoint gets through reliably (BLS API guidance). Ported verbatim.
const BLS_HEADERS: Record<string, string> = {
  "User-Agent":
    "ste-console/1.0 (operator-console contact: ops@packetvoidlabs.dev)",
  Accept: "text/csv,application/json,*/*",
};

// Shared retry/backoff wrapper around fetch. The 6 public regional/market pages
// prerender in parallel, which fans out concurrent FRED + Census calls and can
// trip rate limits (HTTP 429) or transient 5xx. Retry those (honoring
// Retry-After when present, else exponential backoff + jitter) before letting
// the caller's graceful-null fallback kick in. Success/4xx behavior unchanged.
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

// ── shared types (mirror the page data contract) ───────────────────────────
export interface Indicator {
  value: number;
  date: string;
}

export interface BusinessOps {
  top_awards: Array<{
    amount: number;
    recipient: string;
    agency: string;
    description: string;
    naics_code: string | null;
    naics_desc: string | null;
    start_date: string;
    end_date: string;
  }>;
  top_naics: Array<{ code: string; name: string; amount: number }>;
  totals: { awards_count: number; awards_dollars: number; lookback_months: number };
  sam_gov_search_link: string;
}

export interface IndustryRow {
  code: string;
  name: string;
  total_employment: number;
  private_employment: number;
  public_employment: number;
  avg_weekly_wage: number;
  annual_pay_equivalent: number;
}
/** Per-county QCEW snapshot (faithful port of console-api `by_county` rows). */
export interface CountyIndustryRow {
  fips: string;
  name: string;
  total_employment: number;
  top_supersectors: Array<{
    code: string;
    name: string;
    employment: number;
    avg_weekly_wage: number;
  }>;
}
export interface IndustryMix {
  as_of_quarter: string;
  top_supersectors: IndustryRow[];
  total_employment: number;
  /** Per-county breakdown — populated for multi-county footprints (CEFS/mantracon). */
  by_county?: CountyIndustryRow[];
  source: string;
}

export interface CityDemographics {
  name: string;
  place_fips: string;
  year: number;
  population: number | null;
  median_age: number | null;
  pct_hs_graduate_or_higher?: number | null;
  pct_no_hs_diploma?: number | null;
  pct_bachelors_plus: number | null;
  acs_unemployment_rate: number | null;
  median_household_income: number | null;
  poverty_rate_families: number | null;
  median_home_value: number | null;
  median_gross_rent: number | null;
  pct_owner_occupied: number | null;
  pct_renter_occupied: number | null;
  pct_housing_vacant?: number | null;
  pct_foreign_born: number | null;
  mean_commute_minutes: number | null;
  pct_white_alone: number | null;
  pct_black_alone: number | null;
  pct_hispanic_or_latino: number | null;
  source: string;
}

export interface DemoDelta {
  abs_change: number;
  pct_change: number;
  prior_value: number;
}
export interface DemographicsTrend {
  current: CityDemographics;
  comparison_years: [number, number];
  deltas: Record<string, DemoDelta>;
}

export interface HealthComponent {
  key: string;
  label: string;
  value: string;
  score: number | null;
  weight: number;
  rationale: string;
}
export interface HealthScore {
  score: number | null;
  label: string;
  components: HealthComponent[];
  methodology: string;
}

export interface LaborTruthGeo {
  name: string;
  fips: string;
  pop_16plus: number;
  in_labor_force: number;
  employed: number;
  unemployed: number;
  not_in_labor_force: number;
  lfpr: number;
  ep_ratio: number;
  not_lf_pct: number;
  ue_rate: number | null;
  gap_lfpr_vs_state: number;
  gap_ep_vs_state: number;
}
export interface LaborTruth {
  geos: LaborTruthGeo[];
  aggregate: LaborTruthGeo | null;
  benchmarks: {
    il_state_lfpr: number;
    il_state_ep: number;
    il_state_not_lf_pct: number;
    us_national_lfpr: number;
    us_national_ep: number;
  };
  year: number;
  source: string;
}

export interface RegionalData {
  ts: string;
  indicators: Record<string, Indicator>;
  unemployment_series: Array<{ date: string; value: number }>;
  business_opportunities_city?: BusinessOps;
  business_opportunities_county?: BusinessOps;
  industry_mix?: IndustryMix;
  city_demographics?: CityDemographics;
  demographics_trend?: DemographicsTrend;
  health_score?: HealthScore;
  labor_truth?: LaborTruth;
}

export interface FootprintConfig {
  /** 3-digit IL county FIPS suffixes (state 17 is prefixed where needed). */
  countyFips: string[];
  /** 5-digit place FIPS for the city ACS profile + labor truth (omit for county-only footprints). */
  placeFips?: string;
  /** recipient city to filter USAspending awards by (uppercased); omit for county-wide only. */
  recipientCity?: string;
  /** indicatorKey → FRED series_id (e.g. cle_coles_unemployment_rate → ILCOLE3URN). */
  fredSeries: Record<string, string>;
  /** Census ACS years: [latest, prior]. Defaults to [2023, 2018]. */
  acsYears?: [number, number];
  stateFips?: string;
}

// ── benchmarks (ported from console-api module constants) ───────────────────
const IL_STATE_LFPR = 65.1;
const IL_STATE_EP = 61.2;
const IL_STATE_NOTLF_PCT = 34.9;
const US_NATIONAL_LFPR = 62.6;
const US_NATIONAL_EP = 60.3;
const IL_STATE_MEDIAN_HH_INCOME_2023 = 78433;

// ── FRED ────────────────────────────────────────────────────────────────
/** Latest non-missing observation for a FRED series, or null. */
async function fredLatest(seriesId: string): Promise<Indicator | null> {
  const url =
    `https://api.stlouisfed.org/fred/series/observations?series_id=${seriesId}` +
    `&api_key=${FRED_KEY}&file_type=json&sort_order=desc&limit=20`;
  try {
    const res = await rfetch(url, { next: { revalidate: DAY } });
    if (!res.ok) return null;
    const j = (await res.json()) as {
      observations?: Array<{ date: string; value: string }>;
    };
    for (const o of j.observations ?? []) {
      if (o.value !== "." && o.value !== "") {
        return { value: Number(o.value), date: o.date };
      }
    }
    return null;
  } catch {
    return null;
  }
}

/** Fetch the configured FRED-series map → { indicatorKey: {value, date} }. */
async function fetchFredIndicators(
  fredSeries: Record<string, string>,
): Promise<Record<string, Indicator>> {
  const keys = Object.keys(fredSeries);
  const results = await Promise.all(keys.map((k) => fredLatest(fredSeries[k])));
  const out: Record<string, Indicator> = {};
  keys.forEach((k, i) => {
    const got = results[i];
    if (got) out[k] = got;
  });
  return out;
}

// ── Census ACS data-profile (DP02/DP03/DP04/DP05) — one place, one year ─────
const ACS_VARS_MAP: Record<string, string> = {
  DP05_0001E: "population",
  DP05_0018E: "median_age",
  DP02_0067PE: "pct_hs_graduate_or_higher",
  DP02_0068PE: "pct_bachelors_plus",
  DP03_0009PE: "acs_unemployment_rate",
  DP03_0062E: "median_household_income",
  DP03_0119PE: "poverty_rate_families",
  DP04_0089E: "median_home_value",
  DP04_0134E: "median_gross_rent",
  DP04_0046PE: "pct_owner_occupied",
  DP04_0003PE: "pct_housing_vacant",
  DP02_0094PE: "pct_foreign_born",
  DP03_0025E: "mean_commute_minutes",
  DP05_0037PE: "pct_white_alone",
  DP05_0038PE: "pct_black_alone",
  DP05_0071PE: "pct_hispanic_or_latino",
};

const fNum = (v: string | null | undefined): number | null => {
  if (v == null) return null;
  const f = Number(v);
  return Number.isFinite(f) && f >= 0 ? f : null;
};
const fInt = (v: string | null | undefined): number | null => {
  const f = fNum(v);
  return f == null ? null : Math.trunc(f);
};

async function censusAcsPlace(
  placeFips: string,
  stateFips: string,
  year: number,
): Promise<CityDemographics | null> {
  if (!CENSUS_KEY) return null;
  const fields = "NAME," + Object.keys(ACS_VARS_MAP).join(",");
  const url =
    `https://api.census.gov/data/${year}/acs/acs5/profile?get=${encodeURIComponent(fields)}` +
    `&for=${encodeURIComponent(`place:${placeFips}`)}&in=${encodeURIComponent(`state:${stateFips}`)}` +
    `&key=${CENSUS_KEY}`;
  try {
    const res = await rfetch(url, { next: { revalidate: DAY } });
    if (!res.ok) return null;
    const data = (await res.json()) as string[][];
    if (!data || data.length < 2) return null;
    const header = data[0];
    const row = data[1];
    const raw: Record<string, string> = {};
    header.forEach((h, i) => {
      raw[h] = row[i];
    });
    const pctOwner = fNum(raw.DP04_0046PE);
    const pctHsPlus = fNum(raw.DP02_0067PE);
    return {
      name: raw.NAME ?? "",
      place_fips: placeFips,
      year,
      population: fInt(raw.DP05_0001E),
      median_age: fNum(raw.DP05_0018E),
      pct_hs_graduate_or_higher: pctHsPlus,
      pct_no_hs_diploma: pctHsPlus != null ? 100 - pctHsPlus : null,
      pct_bachelors_plus: fNum(raw.DP02_0068PE),
      acs_unemployment_rate: fNum(raw.DP03_0009PE),
      median_household_income: fInt(raw.DP03_0062E),
      poverty_rate_families: fNum(raw.DP03_0119PE),
      median_home_value: fInt(raw.DP04_0089E),
      median_gross_rent: fInt(raw.DP04_0134E),
      pct_owner_occupied: pctOwner,
      pct_renter_occupied: pctOwner != null ? 100 - pctOwner : null,
      pct_housing_vacant: fNum(raw.DP04_0003PE),
      pct_foreign_born: fNum(raw.DP02_0094PE),
      mean_commute_minutes: fNum(raw.DP03_0025E),
      pct_white_alone: fNum(raw.DP05_0037PE),
      pct_black_alone: fNum(raw.DP05_0038PE),
      pct_hispanic_or_latino: fNum(raw.DP05_0071PE),
      source: `US Census Bureau, American Community Survey ${year} 5-year estimates, Data Profile DP02/DP03/DP04/DP05.`,
    };
  } catch {
    return null;
  }
}

const ACS_STABLE_VARS = [
  "population",
  "median_age",
  "median_household_income",
  "poverty_rate_families",
  "median_home_value",
  "median_gross_rent",
  "pct_owner_occupied",
  "acs_unemployment_rate",
  "mean_commute_minutes",
] as const;

/** Latest-year ACS snapshot + 5-year-prior deltas for stable vars. */
export async function censusAcsMultiyear(
  placeFips: string,
  { stateFips = "17", years = [2023, 2018] as [number, number] } = {},
): Promise<DemographicsTrend | null> {
  const [current, prior] = await Promise.all([
    censusAcsPlace(placeFips, stateFips, years[0]),
    censusAcsPlace(placeFips, stateFips, years[1]),
  ]);
  if (!current) return null;

  const deltas: Record<string, DemoDelta> = {};
  for (const k of ACS_STABLE_VARS) {
    const a = current[k as keyof CityDemographics] as number | null;
    const b = prior
      ? (prior[k as keyof CityDemographics] as number | null)
      : null;
    if (a != null && b != null && b !== 0) {
      const absChange = a - b;
      const pctChange = (absChange / b) * 100;
      deltas[k] = {
        abs_change: Math.round(absChange * 100) / 100,
        pct_change: Math.round(pctChange * 10) / 10,
        prior_value: b,
      };
    }
  }
  return {
    current,
    comparison_years: [years[0], years[1]],
    deltas,
  };
}

// ── Census ACS labor truth (B23025 Employment Status) ───────────────────────
export async function acsLaborTruth(
  {
    countyFips,
    placeFips,
    stateFips = "17",
    year = 2023,
  }: {
    countyFips?: string[];
    placeFips?: string;
    stateFips?: string;
    year?: number;
  },
): Promise<LaborTruth | null> {
  if (!CENSUS_KEY) return null;
  const fields =
    "NAME,B23025_001E,B23025_002E,B23025_004E,B23025_005E,B23025_007E";
  let forClause: string;
  if (countyFips && countyFips.length) {
    forClause = `for=${encodeURIComponent(`county:${countyFips.join(",")}`)}`;
  } else if (placeFips) {
    forClause = `for=${encodeURIComponent(`place:${placeFips}`)}`;
  } else {
    return null;
  }
  const url =
    `https://api.census.gov/data/${year}/acs/acs5?get=${encodeURIComponent(fields)}` +
    `&${forClause}&in=${encodeURIComponent(`state:${stateFips}`)}&key=${CENSUS_KEY}`;

  let rows: string[][];
  try {
    const res = await rfetch(url, { next: { revalidate: DAY } });
    if (!res.ok) return null;
    rows = (await res.json()) as string[][];
  } catch {
    return null;
  }
  if (!rows || rows.length < 2) return null;

  const header = rows[0];
  const geos: LaborTruthGeo[] = [];
  const agg = { pop: 0, in_lf: 0, emp: 0, unemp: 0, not_lf: 0 };
  const round1 = (x: number) => Math.round(x * 10) / 10;

  for (const row of rows.slice(1)) {
    const d: Record<string, string> = {};
    header.forEach((h, i) => {
      d[h] = row[i];
    });
    const pop = Number(d.B23025_001E || 0) || 0;
    if (pop === 0) continue;
    const inLf = Number(d.B23025_002E || 0) || 0;
    const emp = Number(d.B23025_004E || 0) || 0;
    const unemp = Number(d.B23025_005E || 0) || 0;
    const notLf = Number(d.B23025_007E || 0) || 0;
    const lfpr = (inLf / pop) * 100;
    const ep = (emp / pop) * 100;
    const notLfPct = (notLf / pop) * 100;
    const ueRate = inLf ? (unemp / inLf) * 100 : null;
    geos.push({
      name: d.NAME ?? "",
      fips: d.county ?? d.place ?? "",
      pop_16plus: pop,
      in_labor_force: inLf,
      employed: emp,
      unemployed: unemp,
      not_in_labor_force: notLf,
      lfpr: round1(lfpr),
      ep_ratio: round1(ep),
      not_lf_pct: round1(notLfPct),
      ue_rate: ueRate != null ? round1(ueRate) : null,
      gap_lfpr_vs_state: round1(lfpr - IL_STATE_LFPR),
      gap_ep_vs_state: round1(ep - IL_STATE_EP),
    });
    agg.pop += pop;
    agg.in_lf += inLf;
    agg.emp += emp;
    agg.unemp += unemp;
    agg.not_lf += notLf;
  }

  let aggregate: LaborTruthGeo | null = null;
  if (agg.pop > 0 && geos.length > 1) {
    const lfpr = (agg.in_lf / agg.pop) * 100;
    const ep = (agg.emp / agg.pop) * 100;
    aggregate = {
      name: "Aggregate",
      fips: "",
      pop_16plus: agg.pop,
      in_labor_force: agg.in_lf,
      employed: agg.emp,
      unemployed: agg.unemp,
      not_in_labor_force: agg.not_lf,
      lfpr: round1(lfpr),
      ep_ratio: round1(ep),
      not_lf_pct: round1((agg.not_lf / agg.pop) * 100),
      ue_rate: agg.in_lf ? round1((agg.unemp / agg.in_lf) * 100) : null,
      gap_lfpr_vs_state: round1(lfpr - IL_STATE_LFPR),
      gap_ep_vs_state: round1(ep - IL_STATE_EP),
    };
  }

  geos.sort((a, b) => b.pop_16plus - a.pop_16plus);
  return {
    geos,
    aggregate,
    benchmarks: {
      il_state_lfpr: IL_STATE_LFPR,
      il_state_ep: IL_STATE_EP,
      il_state_not_lf_pct: IL_STATE_NOTLF_PCT,
      us_national_lfpr: US_NATIONAL_LFPR,
      us_national_ep: US_NATIONAL_EP,
    },
    year,
    source:
      "Census ACS 5y table B23025 (Employment Status for population 16+). " +
      "These metrics go BEYOND the headline UE rate to capture discouraged workers and the " +
      "long-term not-in-labor-force population — the picture politicians rarely cite because " +
      "it's less flattering.",
  };
}

// ── Community Health Score (pure derivation from ACS) ────────────────────────
function linearScore(
  value: number | null,
  worst: number,
  best: number,
): number | null {
  if (value == null) return null;
  if (worst === best) return 50.0;
  const raw = ((value - worst) / (best - worst)) * 100;
  return Math.max(0, Math.min(100, Math.round(raw * 10) / 10));
}

export function communityHealthScore(
  acsCurrent: CityDemographics | null,
  trend: DemographicsTrend | null,
): HealthScore {
  const cur = acsCurrent;
  const components: HealthComponent[] = [];

  const noHs = cur?.pct_no_hs_diploma ?? null;
  components.push({
    key: "no_hs_diploma",
    label: "Educational attainment",
    value:
      noHs != null
        ? `${noHs.toFixed(1)}% adults 25+ without HS diploma`
        : "—",
    score: linearScore(noHs, 20.0, 0.0),
    weight: 1.0,
    rationale:
      "Census EIG DCI weights this most heavily — strongest single predictor of long-term distress.",
  });

  const pov = cur?.poverty_rate_families ?? null;
  components.push({
    key: "poverty",
    label: "Family poverty",
    value: pov != null ? `${pov.toFixed(1)}% of families in poverty` : "—",
    score: linearScore(pov, 30.0, 0.0),
    weight: 1.0,
    rationale: "Census SAIPE / ACS family poverty rate.",
  });

  const ue = cur?.acs_unemployment_rate ?? null;
  components.push({
    key: "unemployment",
    label: "Unemployment",
    value:
      ue != null ? `${ue.toFixed(1)}% ACS 5y unemployment (ages 25+)` : "—",
    score: linearScore(ue, 15.0, 0.0),
    weight: 1.0,
    rationale:
      "ACS 5y narrower than BLS LAUS but captures discouraged workers more honestly over a 5y window.",
  });

  const inc = cur?.median_household_income ?? null;
  const ratio = inc ? inc / IL_STATE_MEDIAN_HH_INCOME_2023 : null;
  components.push({
    key: "income_vs_state",
    label: "Income vs IL state median",
    value:
      inc && ratio
        ? `$${inc.toLocaleString()} vs $${IL_STATE_MEDIAN_HH_INCOME_2023.toLocaleString()} (state median) — ${(ratio * 100).toFixed(0)}% of state`
        : "—",
    score: linearScore(ratio, 0.3, 1.0),
    weight: 1.0,
    rationale:
      "How well does the city's median household income compare to the Illinois state median? Below 50% signals serious wage gap.",
  });

  const popDl = trend?.deltas?.population;
  const popPct = popDl ? popDl.pct_change : null;
  components.push({
    key: "pop_change_5y",
    label: "Population change (5y)",
    value:
      popPct != null
        ? `${popPct > 0 ? "+" : ""}${popPct.toFixed(1)}% since prior ACS5`
        : "—",
    score: linearScore(popPct, -20.0, 20.0),
    weight: 1.0,
    rationale:
      "Population growth signals economic vitality; shrinkage signals out-migration / aging.",
  });

  const incDl = trend?.deltas?.median_household_income;
  const incPct = incDl ? incDl.pct_change : null;
  components.push({
    key: "income_change_5y",
    label: "Income change (5y)",
    value:
      incPct != null
        ? `${incPct > 0 ? "+" : ""}${incPct.toFixed(1)}% median HH income vs prior ACS5`
        : "—",
    score: linearScore(incPct, -25.0, 25.0),
    weight: 1.0,
    rationale:
      "Direction of household-income travel — real-terms inflation-adjusted growth would be even more informative.",
  });

  const methodology =
    "Six equally-weighted components, each scored 0-100 by linear interpolation between worst/best thresholds, then averaged. Inspired by EIG Distressed Communities Index methodology; thresholds are transparent and tunable.";
  const weighted = components.filter((c) => c.score != null);
  if (!weighted.length) {
    return { score: null, label: "Insufficient data", components, methodology };
  }
  const totalW = weighted.reduce((s, c) => s + c.weight, 0);
  const totalS = weighted.reduce((s, c) => s + (c.score as number) * c.weight, 0);
  const score = Math.round((totalS / totalW) * 10) / 10;

  const label =
    score >= 80
      ? "Healthy"
      : score >= 60
        ? "Stable"
        : score >= 40
          ? "At-Risk"
          : score >= 20
            ? "Distressed"
            : "Crisis";
  return { score, label, components, methodology };
}

// ── BLS QCEW (supersector employment + avg weekly wage, per county) ──────────
const BLS_NAICS_SUPERSECTOR: Record<string, string> = {
  "1011": "Natural Resources and Mining",
  "1012": "Construction",
  "1013": "Manufacturing",
  "1021": "Trade, Transportation, and Utilities",
  "1022": "Information",
  "1023": "Financial Activities",
  "1024": "Professional and Business Services",
  "1025": "Education and Health Services",
  "1026": "Leisure and Hospitality",
  "1027": "Other Services",
  "1028": "Public Administration",
  "1029": "Unclassified",
};

/** Minimal CSV parser for the BLS QCEW area CSV (quoted fields, comma sep). */
function parseCsv(text: string): Array<Record<string, string>> {
  const lines = text.split(/\r?\n/).filter((l) => l.length > 0);
  if (!lines.length) return [];
  const splitLine = (line: string): string[] => {
    const out: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (inQ) {
        if (ch === '"') {
          if (line[i + 1] === '"') {
            cur += '"';
            i++;
          } else {
            inQ = false;
          }
        } else {
          cur += ch;
        }
      } else if (ch === '"') {
        inQ = true;
      } else if (ch === ",") {
        out.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    out.push(cur);
    return out;
  };
  const header = splitLine(lines[0]);
  const rows: Array<Record<string, string>> = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = splitLine(lines[i]);
    const rec: Record<string, string> = {};
    header.forEach((h, j) => {
      rec[h] = cells[j] ?? "";
    });
    rows.push(rec);
  }
  return rows;
}

/** Probe BLS for the most recent published quarter; fall back to 2025Q3. */
async function qcewLatestQuarter(): Promise<[number, number]> {
  const today = new Date();
  let y = today.getUTCFullYear();
  let q = Math.floor(today.getUTCMonth() / 3) + 1;
  const candidates: Array<[number, number]> = [];
  for (let i = 0; i < 6; i++) {
    q -= 1;
    if (q < 1) {
      q = 4;
      y -= 1;
    }
    candidates.push([y, q]);
  }
  // Probe against a stable county (Jackson, 17077) like the console-api does.
  for (const [yy, qq] of candidates) {
    try {
      const res = await rfetch(
        `https://data.bls.gov/cew/data/api/${yy}/${qq}/area/17077.csv`,
        { method: "HEAD", headers: BLS_HEADERS, next: { revalidate: DAY } },
      );
      if (res.ok) return [yy, qq];
    } catch {
      /* try next */
    }
  }
  return [2025, 3];
}

async function qcewFetchOneCounty(
  year: number,
  qtr: number,
  fips5: string,
): Promise<Array<Record<string, string>>> {
  const areaCode = "17" + fips5;
  try {
    const res = await rfetch(
      `https://data.bls.gov/cew/data/api/${year}/${qtr}/area/${areaCode}.csv`,
      { headers: BLS_HEADERS, next: { revalidate: DAY } },
    );
    if (!res.ok) return [];
    return parseCsv(await res.text());
  } catch {
    return [];
  }
}

export async function qcewSupersectorBlock(
  countyFips: string[],
  /** Optional 3-digit-FIPS → county-name map; populates `by_county[].name` with
   *  real county names (faithful port of console-api `_COUNTY_FIPS_NAME`). When
   *  omitted (CEFS), `name` falls back to the FIPS string as before. */
  countyNames?: Record<string, string>,
): Promise<IndustryMix | null> {
  const [year, qtr] = await qcewLatestQuarter();
  const agg: Record<
    string,
    {
      code: string;
      name: string;
      private_emp: number;
      public_emp: number;
      wage_sum: number;
      wage_weight: number;
    }
  > = {};

  const countyResults = await Promise.all(
    countyFips.map((f) => qcewFetchOneCounty(year, qtr, f)),
  );

  // Per-county snapshot (faithful port of console-api `by_county`) — keyed by
  // the 3-digit county FIPS so multi-county footprints (CEFS, mantracon) can
  // surface each county's own supersector mix. Single-county footprints simply
  // never read it.
  const byCounty: CountyIndustryRow[] = [];

  countyFips.forEach((fips5, ci) => {
    const rows = countyResults[ci];
    const countyAgg: Record<
      string,
      { code: string; name: string; emp: number; wage_sum: number; wage_w: number }
    > = {};
    for (const row of rows) {
      if (row.agglvl_code !== "73") continue;
      const ic = row.industry_code ?? "";
      if (!(ic in BLS_NAICS_SUPERSECTOR)) continue;
      const own = row.own_code || "0";
      const emp = parseInt(row.month3_emplvl || "0", 10) || 0;
      const wage = parseFloat(row.avg_wkly_wage || "0") || 0;
      // Cross-county aggregate
      if (!agg[ic]) {
        agg[ic] = {
          code: ic,
          name: BLS_NAICS_SUPERSECTOR[ic],
          private_emp: 0,
          public_emp: 0,
          wage_sum: 0,
          wage_weight: 0,
        };
      }
      if (own === "5") agg[ic].private_emp += emp;
      else if (own === "1" || own === "2" || own === "3")
        agg[ic].public_emp += emp;
      agg[ic].wage_sum += wage * emp;
      agg[ic].wage_weight += emp;
      // Per-county snapshot
      if (!countyAgg[ic]) {
        countyAgg[ic] = {
          code: ic,
          name: BLS_NAICS_SUPERSECTOR[ic],
          emp: 0,
          wage_sum: 0,
          wage_w: 0,
        };
      }
      countyAgg[ic].emp += emp;
      countyAgg[ic].wage_sum += wage * emp;
      countyAgg[ic].wage_w += emp;
    }
    const countyItems = Object.values(countyAgg)
      .filter((cd) => cd.emp > 0)
      .map((cd) => ({
        code: cd.code,
        name: cd.name,
        employment: cd.emp,
        avg_weekly_wage: Math.round(cd.wage_w ? cd.wage_sum / cd.wage_w : 0),
      }))
      .sort((a, b) => b.employment - a.employment);
    byCounty.push({
      fips: fips5,
      name: countyNames?.[fips5] ?? fips5,
      total_employment: countyItems.reduce((s, c) => s + c.employment, 0),
      top_supersectors: countyItems.slice(0, 6),
    });
  });

  const items: IndustryRow[] = [];
  for (const d of Object.values(agg)) {
    const totalEmp = d.private_emp + d.public_emp;
    if (totalEmp === 0) continue;
    const avgWkly = d.wage_weight ? d.wage_sum / d.wage_weight : 0;
    items.push({
      code: d.code,
      name: d.name,
      total_employment: totalEmp,
      private_employment: d.private_emp,
      public_employment: d.public_emp,
      avg_weekly_wage: Math.round(avgWkly),
      annual_pay_equivalent: Math.round(avgWkly * 52),
    });
  }
  items.sort((a, b) => b.total_employment - a.total_employment);

  if (!items.length) return null;
  const byCountySorted = byCounty
    .filter((c) => c.total_employment > 0)
    .sort((a, b) => b.total_employment - a.total_employment);
  return {
    as_of_quarter: `${year}Q${qtr}`,
    top_supersectors: items,
    total_employment: items.reduce((s, i) => s + i.total_employment, 0),
    by_county: byCountySorted,
    source:
      "BLS Quarterly Census of Employment & Wages (QCEW); NAICS supersector aggregation, all ownerships.",
  };
}

// ── USAspending (federal contract awards by place-of-performance) ───────────
async function usaspendingPost(
  path: string,
  body: unknown,
): Promise<Record<string, unknown>> {
  try {
    const res = await rfetch(`https://api.usaspending.gov${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      next: { revalidate: DAY },
    });
    if (!res.ok) return {};
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

const truncate = (s: string | null | undefined, n = 140): string => {
  if (!s) return "";
  const t = s.split(/\s+/).join(" ");
  return t.length <= n ? t : t.slice(0, n - 1) + "…";
};

export async function usaspendingBlock({
  countyFips,
  recipientCity,
  lookbackMonths = 24,
  stateFips = "17",
}: {
  countyFips: string[];
  recipientCity?: string | null;
  lookbackMonths?: number;
  stateFips?: string;
}): Promise<BusinessOps | null> {
  const end = new Date();
  const start = new Date(end.getTime() - lookbackMonths * 30 * DAY * 1000);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  // state code from FIPS (17 → IL); this lib is IL-scoped but keep the mapping explicit.
  const stateCode = stateFips === "17" ? "IL" : stateFips;
  const locations = countyFips.map((c) => ({
    country: "USA",
    state: stateCode,
    county: c,
  }));
  const baseFilters: Record<string, unknown> = {
    time_period: [{ start_date: iso(start), end_date: iso(end) }],
    place_of_performance_locations: locations,
    award_type_codes: ["A", "B", "C", "D"],
  };
  if (recipientCity) {
    baseFilters.recipient_locations = [
      { country: "USA", state: stateCode, city: recipientCity.toUpperCase() },
    ];
  }

  const awardsBody = {
    filters: baseFilters,
    fields: [
      "Award ID",
      "Recipient Name",
      "Award Amount",
      "Awarding Agency",
      "Description",
      "Period of Performance Start Date",
      "Period of Performance Current End Date",
      "NAICS",
      "Place of Performance State Code",
    ],
    page: 1,
    limit: 25,
    sort: "Award Amount",
    order: "desc",
    subawards: false,
  };
  const naicsBody = { filters: baseFilters, limit: 10 };

  const [awardsResp, naicsResp] = await Promise.all([
    usaspendingPost("/api/v2/search/spending_by_award/", awardsBody),
    usaspendingPost("/api/v2/search/spending_by_category/naics/", naicsBody),
  ]);

  const rawAwards = (awardsResp.results as Array<Record<string, unknown>>) ?? [];
  const rawNaics = (naicsResp.results as Array<Record<string, unknown>>) ?? [];

  const topAwards = rawAwards.map((a) => {
    const naics = a.NAICS as { code?: string; description?: string } | undefined;
    return {
      amount: Number(a["Award Amount"] ?? 0) || 0,
      recipient: (a["Recipient Name"] as string) ?? "",
      agency: (a["Awarding Agency"] as string) ?? "",
      description: truncate(a.Description as string | undefined),
      naics_code:
        naics && typeof naics === "object" ? (naics.code ?? null) : null,
      naics_desc:
        naics && typeof naics === "object" ? (naics.description ?? null) : null,
      start_date: (a["Period of Performance Start Date"] as string) ?? "",
      end_date:
        (a["Period of Performance Current End Date"] as string) ?? "",
    };
  });

  const topNaics = rawNaics
    .filter((n) => n.code)
    .map((n) => ({
      code: String(n.code ?? ""),
      name: (n.name as string) ?? "",
      amount: Number(n.amount ?? 0) || 0,
    }));

  const totals = {
    awards_count: rawAwards.length,
    awards_dollars: topAwards.reduce((s, a) => s + a.amount, 0),
    lookback_months: lookbackMonths,
  };

  let samLink =
    "https://sam.gov/search/?index=opp&page=1" +
    "&sort=-modifiedDate&pageSize=25&sfm[status][is_active]=true" +
    "&sfm[placeOfPerformance][country][name]=USA" +
    "&sfm[placeOfPerformance][state][code]=" +
    stateCode;
  if (topNaics.length) {
    samLink += `&sfm[naics][naics][0][code]=${topNaics[0].code}`;
  }

  return {
    top_awards: topAwards,
    top_naics: topNaics,
    totals,
    sam_gov_search_link: samLink,
  };
}

// ── orchestrator ────────────────────────────────────────────────────────
/**
 * Build the full regional payload for a footprint. Each section degrades to
 * absent/empty on failure so the page's graceful fallbacks render.
 */
export async function getRegionalData(
  cfg: FootprintConfig,
): Promise<RegionalData> {
  const stateFips = cfg.stateFips ?? "17";
  const acsYears = cfg.acsYears ?? [2023, 2018];

  const [
    indicators,
    businessCity,
    businessCounty,
    industryMix,
    acsTrend,
    laborTruth,
  ] = await Promise.all([
    fetchFredIndicators(cfg.fredSeries),
    cfg.recipientCity
      ? usaspendingBlock({
          countyFips: cfg.countyFips,
          recipientCity: cfg.recipientCity,
          stateFips,
        })
      : Promise.resolve<BusinessOps | null>(null),
    usaspendingBlock({
      countyFips: cfg.countyFips,
      recipientCity: null,
      stateFips,
    }),
    qcewSupersectorBlock(cfg.countyFips),
    cfg.placeFips
      ? censusAcsMultiyear(cfg.placeFips, { stateFips, years: acsYears })
      : Promise.resolve<DemographicsTrend | null>(null),
    cfg.placeFips
      ? acsLaborTruth({ placeFips: cfg.placeFips, stateFips, year: acsYears[0] })
      : Promise.resolve<LaborTruth | null>(null),
  ]);

  const cityDemographics = acsTrend?.current ?? undefined;
  const healthScore = acsTrend
    ? communityHealthScore(acsTrend.current, acsTrend)
    : undefined;

  return {
    ts: new Date().toISOString(),
    indicators,
    unemployment_series: [], // parity with console-api public_charleston (Coles FRED micro series TBD)
    business_opportunities_city: businessCity ?? undefined,
    business_opportunities_county: businessCounty ?? undefined,
    industry_mix: industryMix ?? undefined,
    city_demographics: cityDemographics,
    demographics_trend: acsTrend ?? undefined,
    health_score: healthScore,
    labor_truth: laborTruth ?? undefined,
  };
}

// ── Charleston (footprint #1) ───────────────────────────────────────────────
// Coles County FIPS 029 · Charleston Place FIPS 12567. FRED series per
// tpcore.fred.adapter INDICATOR_SERIES (cle_coles_* family + IL state context).
export const CHARLESTON_CONFIG: FootprintConfig = {
  countyFips: ["029"],
  placeFips: "12567",
  recipientCity: "CHARLESTON",
  fredSeries: {
    cle_coles_unemployment_rate: "ILCOLE3URN",
    cle_coles_labor_force: "ILCOLE3LFN",
    cle_coles_population: "ILCOLE3POP",
    cle_coles_personal_income: "PI17029",
    cle_coles_real_gdp: "REALGDPALL17029",
    cle_coles_median_hh_income: "MHIIL17029A052NCEN",
    cle_coles_snap_recipients: "CBR17029ILA647NCEN",
    cle_coles_poverty_universe: "PUAAIL17029A647NCEN",
    cle_coles_single_parent_pct: "S1101SPHOUSE017029",
    cle_coles_housing_median_listing: "MEDLISPRI17029",
    cle_coles_housing_new_listings: "NEWLISCOU17029",
    cle_coles_housing_new_listings_mom: "NEWLISCOUMM17029",
    il_unemployment_rate: "ILUR",
    il_nonfarm_payrolls: "ILNA",
    phci_il: "ILPHCI",
  },
};

export async function getCharlestonData(): Promise<RegionalData> {
  return getRegionalData(CHARLESTON_CONFIG);
}

// ── Carbondale (footprint #2) ───────────────────────────────────────────────
// Jackson County FIPS 077 · Carbondale city Place FIPS 11163 · Carbondale-Marion
// MSA (CBSA 16060 = Jackson + Williamson). FRED series per tpcore.fred.adapter
// INDICATOR_SERIES (crb_jackson_* + crb_msa_* families + IL state context).
//
// Faithful port of console-api `public_carbondale()`: that endpoint calls
// `_usaspending_block(county_fips=["077"], recipient_city=None)` (county-only,
// returned as the singular `business_opportunities`), `_qcew_supersector_block`,
// `_census_acs_multiyear("11163")`, `_acs_labor_truth(place_fips="11163")`, and
// `_community_health_score`. It also returns an `unemployment_series` +
// `labor_force_series` for the crb_msa_* trend charts. The page reads
// `data.business_opportunities` (singular) + `data.unemployment_series`, so the
// Carbondale payload shape diverges slightly from `RegionalData` — captured here.
export const CARBONDALE_CONFIG: FootprintConfig = {
  countyFips: ["077"],
  placeFips: "11163",
  // console-api passes recipient_city=None for Carbondale → county-only awards.
  fredSeries: {
    // Jackson County, IL
    crb_jackson_unemployment_rate: "ILJAURN",
    crb_jackson_labor_force: "ILJALFN",
    crb_jackson_personal_income: "PI17077",
    crb_jackson_real_gdp: "REALGDPALL17077",
    crb_jackson_median_hh_income: "MHIIL17077A052NCEN",
    crb_jackson_snap_recipients: "CBR17077ILA647NCEN",
    crb_jackson_poverty_universe: "PUAAIL17077A647NCEN",
    crb_jackson_single_parent_pct: "S1101SPHOUSE017077",
    // Carbondale-Marion MSA (CBSA 16060)
    crb_msa_population: "CRBPOP",
    crb_msa_unemployment_rate: "LAUMT171606000000003",
    crb_msa_labor_force: "LAUMT171606000000006",
    crb_msa_private_service_jobs: "SMU17160600800000001SA",
    crb_msa_avg_hourly_earnings: "SMU17160600500000003SA",
    crb_msa_avg_weekly_earnings: "SMU17160600500000011SA",
    crb_msa_housing_days_on_market: "MEDDAYONMAR16060",
    crb_msa_housing_new_listings_mom: "NEWLISCOUMM16060",
    crb_msa_housing_price_inc_yoy: "PRIINCCOUYY16060",
    // IL state context
    il_unemployment_rate: "ILUR",
    il_nonfarm_payrolls: "ILNA",
    phci_il: "ILPHCI",
  },
};

/** Carbondale payload — RegionalData with the singular county `business_opportunities`
 *  + a `labor_force_series`, matching console-api `public_carbondale()`. */
export interface CarbondaleData
  extends Omit<
    RegionalData,
    "business_opportunities_city" | "business_opportunities_county"
  > {
  labor_force_series: Array<{ date: string; value: number }>;
  business_opportunities?: BusinessOps;
}

/**
 * FRED daily-bars trend series (last ~60 months, ascending) for one series_id.
 * Mirrors the console-api crb_msa_* trend-chart queries against macro_data.
 */
async function fredSeriesRecent(
  seriesId: string,
  limit = 70,
): Promise<Array<{ date: string; value: number }>> {
  const url =
    `https://api.stlouisfed.org/fred/series/observations?series_id=${seriesId}` +
    `&api_key=${FRED_KEY}&file_type=json&sort_order=desc&limit=${limit}`;
  try {
    const res = await rfetch(url, { next: { revalidate: DAY } });
    if (!res.ok) return [];
    const j = (await res.json()) as {
      observations?: Array<{ date: string; value: string }>;
    };
    const out: Array<{ date: string; value: number }> = [];
    for (const o of j.observations ?? []) {
      if (o.value !== "." && o.value !== "") {
        out.push({ date: o.date, value: Number(o.value) });
      }
    }
    out.reverse(); // ascending, like the SQL `ORDER BY observed_date ASC`
    return out;
  } catch {
    return [];
  }
}

export async function getCarbondaleData(): Promise<CarbondaleData> {
  const cfg = CARBONDALE_CONFIG;
  const stateFips = cfg.stateFips ?? "17";
  const acsYears = cfg.acsYears ?? [2023, 2018];
  const placeFips = cfg.placeFips!;

  const [
    indicators,
    business,
    industryMix,
    acsTrend,
    laborTruth,
    urSeries,
    lfSeries,
  ] = await Promise.all([
    fetchFredIndicators(cfg.fredSeries),
    usaspendingBlock({
      countyFips: cfg.countyFips,
      recipientCity: null,
      stateFips,
    }),
    qcewSupersectorBlock(cfg.countyFips),
    censusAcsMultiyear(placeFips, { stateFips, years: acsYears }),
    acsLaborTruth({ placeFips, stateFips, year: acsYears[0] }),
    fredSeriesRecent(cfg.fredSeries.crb_msa_unemployment_rate),
    fredSeriesRecent(cfg.fredSeries.crb_msa_labor_force),
  ]);

  const cityDemographics = acsTrend?.current ?? undefined;
  const healthScore = acsTrend
    ? communityHealthScore(acsTrend.current, acsTrend)
    : undefined;

  return {
    ts: new Date().toISOString(),
    indicators,
    unemployment_series: urSeries,
    labor_force_series: lfSeries,
    business_opportunities: business ?? undefined,
    industry_mix: industryMix ?? undefined,
    city_demographics: cityDemographics,
    demographics_trend: acsTrend ?? undefined,
    health_score: healthScore,
    labor_truth: laborTruth ?? undefined,
  };
}

// ── Murphysboro (footprint #3) ───────────────────────────────────────────────
// Jackson County FIPS 077 (same as Carbondale; Murphysboro is the county seat,
// 8 mi W of Carbondale) · Murphysboro city Place FIPS 51453 · Carbondale-Marion
// MSA (CBSA 16060). Reuses the entire crb_jackson_* / crb_msa_* FRED family per
// tpcore.fred.adapter INDICATOR_SERIES — there are no murphysboro-specific series.
//
// Faithful port of console-api `public_murphysboro()`: that endpoint calls
// `_usaspending_block(county_fips=["077"], recipient_city="MURPHYSBORO")` (city
// awards) AND `_usaspending_block(county_fips=["077"], recipient_city=None)`
// (county awards) — returned as `business_opportunities_city` +
// `business_opportunities_county`. It also calls `_qcew_supersector_block`,
// `_census_acs_multiyear("51453")`, `_acs_labor_truth(place_fips="51453")`,
// `_community_health_score`, and returns an `unemployment_series` from the
// `crb_jackson_unemployment_rate` series (last 60 months, ascending). The
// page reads both business blocks + the Jackson UR series, so the Murphysboro
// payload shape diverges from `RegionalData` (its `unemployment_series` is
// hardcoded empty) — captured here.
export const MURPHYSBORO_CONFIG: FootprintConfig = {
  countyFips: ["077"],
  placeFips: "51453",
  recipientCity: "MURPHYSBORO",
  // console-api TARGETS for public_murphysboro: Jackson County + Carbondale-Marion
  // MSA + IL state context (il_unemployment_rate + phci_il; no il_nonfarm_payrolls).
  fredSeries: {
    // Jackson County, IL (Murphysboro is the county seat)
    crb_jackson_unemployment_rate: "ILJAURN",
    crb_jackson_labor_force: "ILJALFN",
    crb_jackson_personal_income: "PI17077",
    crb_jackson_real_gdp: "REALGDPALL17077",
    crb_jackson_median_hh_income: "MHIIL17077A052NCEN",
    crb_jackson_snap_recipients: "CBR17077ILA647NCEN",
    crb_jackson_poverty_universe: "PUAAIL17077A647NCEN",
    crb_jackson_single_parent_pct: "S1101SPHOUSE017077",
    // Carbondale-Marion MSA (CBSA 16060)
    crb_msa_population: "CRBPOP",
    crb_msa_unemployment_rate: "LAUMT171606000000003",
    crb_msa_labor_force: "LAUMT171606000000006",
    crb_msa_avg_hourly_earnings: "SMU17160600500000003SA",
    crb_msa_avg_weekly_earnings: "SMU17160600500000011SA",
    crb_msa_housing_days_on_market: "MEDDAYONMAR16060",
    crb_msa_housing_new_listings_mom: "NEWLISCOUMM16060",
    crb_msa_housing_price_inc_yoy: "PRIINCCOUYY16060",
    // IL state context
    il_unemployment_rate: "ILUR",
    phci_il: "ILPHCI",
  },
};

/** Murphysboro payload — RegionalData with both city + county `business_opportunities`
 *  blocks required (non-optional), matching console-api `public_murphysboro()`. */
export interface MurphysboroData
  extends Omit<
    RegionalData,
    "business_opportunities_city" | "business_opportunities_county"
  > {
  business_opportunities_city: BusinessOps;
  business_opportunities_county: BusinessOps;
}

/** An empty BusinessOps block — graceful fallback so the page's required city/county
 *  blocks always render (console-api never returns null there; _usaspending_block
 *  returns a populated structure even with zero awards). */
const EMPTY_BUSINESS_OPS: BusinessOps = {
  top_awards: [],
  top_naics: [],
  totals: { awards_count: 0, awards_dollars: 0, lookback_months: 24 },
  sam_gov_search_link:
    "https://sam.gov/search/?index=opp&page=1&sort=-modifiedDate&pageSize=25" +
    "&sfm[status][is_active]=true&sfm[placeOfPerformance][country][name]=USA" +
    "&sfm[placeOfPerformance][state][code]=IL",
};

export async function getMurphysboroData(): Promise<MurphysboroData> {
  const cfg = MURPHYSBORO_CONFIG;
  const stateFips = cfg.stateFips ?? "17";
  const acsYears = cfg.acsYears ?? [2023, 2018];
  const placeFips = cfg.placeFips!;

  const [
    indicators,
    businessCity,
    businessCounty,
    industryMix,
    acsTrend,
    laborTruth,
    urSeries,
  ] = await Promise.all([
    fetchFredIndicators(cfg.fredSeries),
    usaspendingBlock({
      countyFips: cfg.countyFips,
      recipientCity: cfg.recipientCity,
      stateFips,
    }),
    usaspendingBlock({
      countyFips: cfg.countyFips,
      recipientCity: null,
      stateFips,
    }),
    qcewSupersectorBlock(cfg.countyFips),
    censusAcsMultiyear(placeFips, { stateFips, years: acsYears }),
    acsLaborTruth({ placeFips, stateFips, year: acsYears[0] }),
    fredSeriesRecent(cfg.fredSeries.crb_jackson_unemployment_rate),
  ]);

  const cityDemographics = acsTrend?.current ?? undefined;
  const healthScore = acsTrend
    ? communityHealthScore(acsTrend.current, acsTrend)
    : undefined;

  return {
    ts: new Date().toISOString(),
    indicators,
    unemployment_series: urSeries,
    business_opportunities_city: businessCity ?? EMPTY_BUSINESS_OPS,
    business_opportunities_county: businessCounty ?? EMPTY_BUSINESS_OPS,
    industry_mix: industryMix ?? undefined,
    city_demographics: cityDemographics,
    demographics_trend: acsTrend ?? undefined,
    health_score: healthScore,
    labor_truth: laborTruth ?? undefined,
  };
}

// ── CEFS / LWA-23 (footprint #4) — 13-county East Central IL aggregate ───────
// Service area: Clark, Clay, Coles, Crawford, Cumberland, Edgar, Effingham,
// Fayette, Jasper, Lawrence, Marion, Moultrie, Richland.
//
// Faithful port of console-api `public_cefs()`. That endpoint does NOT read a
// pre-aggregated FRED series — it computes the 13-county aggregate IN SQL from
// the per-county FRED UR + labor-force series stored in platform.macro_data:
//   • labor_force aggregate  = SUM(county labor_force) by month, only for months
//     where all 13 counties report (HAVING COUNT(*) = 13).
//   • unemployment_rate_weighted = SUM(county_UR · county_LF) / SUM(county_LF)
//     by month, again only for fully-covered months.
// The latest fully-covered month becomes the headline lwa_aggregate. We replicate
// this in TS by fetching each county's FRED UR + LF series directly (the same
// series_ids the FRED adapter loads into macro_data) and computing the weighted
// aggregate here. console-api uses cle_coles_* for Coles + eci_<county>_* for the
// other 12; the underlying FRED series_ids are what matter (listed below).
const CEFS_COUNTY_FRED: Array<{
  fips: string;
  name: string;
  ur: string; // FRED series_id for the county unemployment rate
  lf: string; // FRED series_id for the county labor force
}> = [
  { fips: "023", name: "Clark", ur: "ILCLAR3URN", lf: "ILCLAR3LFN" },
  { fips: "025", name: "Clay", ur: "ILCYURN", lf: "ILCYLFN" },
  { fips: "029", name: "Coles", ur: "ILCOLE3URN", lf: "ILCOLE3LFN" },
  { fips: "033", name: "Crawford", ur: "ILCWURN", lf: "ILCWLFN" },
  { fips: "035", name: "Cumberland", ur: "ILCUMB5URN", lf: "ILCUMB5LFN" },
  { fips: "045", name: "Edgar", ur: "ILEDGA5URN", lf: "ILEDGA5LFN" },
  { fips: "049", name: "Effingham", ur: "ILEFURN", lf: "ILEFLFN" },
  { fips: "051", name: "Fayette", ur: "ILFAURN", lf: "ILFALFN" },
  { fips: "079", name: "Jasper", ur: "ILJSURN", lf: "ILJSLFN" },
  { fips: "101", name: "Lawrence", ur: "ILLWURN", lf: "ILLWLFN" },
  { fips: "121", name: "Marion", ur: "ILMRURN", lf: "ILMRLFN" },
  { fips: "139", name: "Moultrie", ur: "ILMOUL9URN", lf: "ILMOUL9LFN" },
  { fips: "159", name: "Richland", ur: "ILRIURN", lf: "ILRILFN" },
];

const CEFS_FIPS = CEFS_COUNTY_FRED.map((c) => c.fips);

export interface LwaAggregate {
  labor_force: number | null;
  labor_force_date: string | null;
  unemployment_rate_weighted: number | null;
  unemployment_rate_date: string | null;
  county_count: number;
}

/** CEFS (LWA-23) payload — 13-county aggregate. Faithful port of
 *  console-api `public_cefs()`. */
export interface CefsData {
  ts: string;
  indicators: Record<string, Indicator>;
  lwa_aggregate: LwaAggregate;
  lwa_labor_force_series: Array<{ date: string; value: number }>;
  lwa_unemployment_series: Array<{ date: string; value: number }>;
  business_opportunities?: BusinessOps;
  top_federal_recipients?: Array<{ name: string; amount: number; share_pct?: number }>;
  industry_mix?: IndustryMix;
  labor_truth?: LaborTruth;
}

/**
 * Compute the LWA weighted-UR + summed-LF monthly aggregates from per-county
 * FRED series. Faithful port of the two SQL CTEs in console-api `public_cefs()`:
 *   • include a month only if ALL N counties report both UR and LF that month
 *     (the SQL `HAVING COUNT(*) = N` guard);
 *   • weighted UR = round(Σ(ur·lf) / Σ(lf), 2);
 *   • summed LF = Σ(lf).
 * Returns ascending series + the latest fully-covered month as the headline.
 */
function lwaWeightedAggregate(
  perCounty: Array<{
    ur: Array<{ date: string; value: number }>;
    lf: Array<{ date: string; value: number }>;
  }>,
): {
  lf_series: Array<{ date: string; value: number }>;
  ur_series: Array<{ date: string; value: number }>;
  latest_lf: number | null;
  latest_lf_date: string | null;
  latest_ur: number | null;
  latest_ur_date: string | null;
} {
  const n = perCounty.length;
  // Map per county: date → ur, date → lf.
  const urMaps = perCounty.map((c) => {
    const m = new Map<string, number>();
    for (const o of c.ur) m.set(o.date, o.value);
    return m;
  });
  const lfMaps = perCounty.map((c) => {
    const m = new Map<string, number>();
    for (const o of c.lf) m.set(o.date, o.value);
    return m;
  });

  // Candidate months = union of all observed dates across all series.
  const dates = new Set<string>();
  for (const c of perCounty) {
    for (const o of c.ur) dates.add(o.date);
    for (const o of c.lf) dates.add(o.date);
  }
  const sortedDates = Array.from(dates).sort(); // ISO dates sort lexicographically

  const lf_series: Array<{ date: string; value: number }> = [];
  const ur_series: Array<{ date: string; value: number }> = [];
  for (const d of sortedDates) {
    let lfSum = 0;
    let urLfSum = 0;
    let covered = 0;
    for (let i = 0; i < n; i++) {
      const lf = lfMaps[i].get(d);
      const ur = urMaps[i].get(d);
      if (lf == null || ur == null) break; // require both for this county
      lfSum += lf;
      urLfSum += ur * lf;
      covered++;
    }
    if (covered !== n || lfSum <= 0) continue; // HAVING COUNT(*) = N
    lf_series.push({ date: d, value: lfSum });
    ur_series.push({ date: d, value: Math.round((urLfSum / lfSum) * 100) / 100 });
  }

  const lastLf = lf_series.length ? lf_series[lf_series.length - 1] : null;
  const lastUr = ur_series.length ? ur_series[ur_series.length - 1] : null;
  return {
    lf_series,
    ur_series,
    latest_lf: lastLf ? lastLf.value : null,
    latest_lf_date: lastLf ? lastLf.date : null,
    latest_ur: lastUr ? lastUr.value : null,
    latest_ur_date: lastUr ? lastUr.date : null,
  };
}

// FRED-series map for the latest-value indicator panel + IL state context.
// Mirrors console-api's `indicators` dict (per-county UR/LF latest + state).
export const CEFS_CONFIG: FootprintConfig = {
  countyFips: CEFS_FIPS,
  // 13-county aggregate — no single place / recipient-city filter.
  fredSeries: (() => {
    const m: Record<string, string> = {};
    for (const c of CEFS_COUNTY_FRED) {
      const prefix = c.fips === "029" ? "cle_coles" : `eci_${c.name.toLowerCase()}`;
      m[`${prefix}_unemployment_rate`] = c.ur;
      m[`${prefix}_labor_force`] = c.lf;
    }
    m.il_unemployment_rate = "ILUR";
    m.il_nonfarm_payrolls = "ILNA";
    m.phci_il = "ILPHCI";
    return m;
  })(),
  stateFips: "17",
};

export async function getCefsData(): Promise<CefsData> {
  const cfg = CEFS_CONFIG;
  const stateFips = cfg.stateFips ?? "17";

  // Fetch the per-county FRED UR + LF monthly series (for the weighted aggregate),
  // plus the latest-value indicator panel, USAspending county awards, QCEW
  // industry mix (with by_county), and the ACS labor-truth across all 13 counties.
  const [
    indicators,
    business,
    industryMix,
    laborTruth,
    perCountySeries,
  ] = await Promise.all([
    fetchFredIndicators(cfg.fredSeries),
    usaspendingBlock({ countyFips: cfg.countyFips, recipientCity: null, stateFips }),
    qcewSupersectorBlock(cfg.countyFips),
    acsLaborTruth({ countyFips: cfg.countyFips, stateFips, year: 2023 }),
    Promise.all(
      CEFS_COUNTY_FRED.map(async (c) => {
        const [ur, lf] = await Promise.all([
          fredSeriesRecent(c.ur),
          fredSeriesRecent(c.lf),
        ]);
        return { ur, lf };
      }),
    ),
  ]);

  const aggr = lwaWeightedAggregate(perCountySeries);

  // Derive top_federal_recipients from the county business block (USAspending
  // spending_by_award doesn't give a recipient-category roll-up here; we surface
  // the largest distinct recipients from the awards list as a faithful-enough
  // stand-in — the page does not render this field, so it stays advisory).
  const recipMap = new Map<string, number>();
  for (const a of business?.top_awards ?? []) {
    if (!a.recipient) continue;
    recipMap.set(a.recipient, (recipMap.get(a.recipient) ?? 0) + a.amount);
  }
  const recipTotal = Array.from(recipMap.values()).reduce((s, v) => s + v, 0);
  const top_federal_recipients = Array.from(recipMap.entries())
    .map(([name, amount]) => ({
      name,
      amount,
      share_pct: recipTotal ? Math.round((amount / recipTotal) * 1000) / 10 : 0,
    }))
    .sort((a, b) => b.amount - a.amount)
    .slice(0, 12);

  return {
    ts: new Date().toISOString(),
    indicators,
    lwa_aggregate: {
      labor_force: aggr.latest_lf,
      labor_force_date: aggr.latest_lf_date,
      unemployment_rate_weighted: aggr.latest_ur,
      unemployment_rate_date: aggr.latest_ur_date,
      county_count: CEFS_FIPS.length,
    },
    lwa_labor_force_series: aggr.lf_series,
    lwa_unemployment_series: aggr.ur_series,
    business_opportunities: business ?? undefined,
    top_federal_recipients,
    industry_mix: industryMix ?? undefined,
    labor_truth: laborTruth ?? undefined,
  };
}

// ── Man-Tra-Con / SIWIB · LWA-25 (footprint #5, FINAL) — 5-county aggregate ──
// Five-county Southern Illinois Workforce Development service area:
//   Franklin (055), Jackson (077), Jefferson (081), Perry (145), Williamson (199).
//
// Faithful port of console-api `public_mantracon()` (/api/public/mantracon).
// That endpoint computes the LWA-25 aggregate IN SQL from the per-county FRED
// UR + labor-force series in platform.macro_data (same HAVING COUNT(*) = 5 guard
// + LF-weighted UR as CEFS / LWA-23), then layers:
//   • USAspending county awards (`business_opportunities`),
//   • USAspending top-recipients with SBA classification (`top_federal_recipients`),
//   • QCEW supersector industry mix WITH by_county (real county names),
//   • ACS labor-truth across all 5 counties,
//   • a pure training-demand-alignment derivation against the QCEW mix,
//   • GD-OTS subaward lanes (heavy USAspending subaward crawls — these are
//     `| null` in the contract and the page hides the tables when null).
// We reuse `lwaWeightedAggregate` + `usaspendingBlock` + `qcewSupersectorBlock`
// + `acsLaborTruth` exactly as CEFS does; the recipients + training blocks are
// faithful TS ports of the console-api helpers below.

const MANTRACON_COUNTY_FRED: Array<{
  fips: string; // 3-digit county FIPS
  name: string; // page county key (lowercase) + display
  ur: string; // FRED series_id for county unemployment rate
  lf: string; // FRED series_id for county labor force
}> = [
  { fips: "077", name: "jackson", ur: "ILJAURN", lf: "ILJALFN" },
  { fips: "055", name: "franklin", ur: "ILFRURN", lf: "ILFRLFN" },
  { fips: "081", name: "jefferson", ur: "ILJEURN", lf: "ILJELFN" },
  { fips: "145", name: "perry", ur: "ILPRURN", lf: "ILPRLFN" },
  { fips: "199", name: "williamson", ur: "ILWMURN", lf: "ILWMLFN" },
];

const MANTRACON_FIPS = MANTRACON_COUNTY_FRED.map((c) => c.fips);

// FIPS → display county name (faithful port of console-api `_COUNTY_FIPS_NAME`).
const MANTRACON_FIPS_NAME: Record<string, string> = {
  "055": "Franklin",
  "077": "Jackson",
  "081": "Jefferson",
  "145": "Perry",
  "199": "Williamson",
};

export const MANTRACON_CONFIG: FootprintConfig = {
  countyFips: MANTRACON_FIPS,
  // 5-county aggregate — no single place / recipient-city filter.
  fredSeries: (() => {
    const m: Record<string, string> = {};
    for (const c of MANTRACON_COUNTY_FRED) {
      m[`crb_${c.name}_unemployment_rate`] = c.ur;
      m[`crb_${c.name}_labor_force`] = c.lf;
    }
    m.il_unemployment_rate = "ILUR";
    m.il_nonfarm_payrolls = "ILNA";
    m.phci_il = "ILPHCI";
    return m;
  })(),
  stateFips: "17",
};

// ── top_federal_recipients (faithful port of `_usaspending_top_recipients`) ──
export interface MantraconTopRecipient {
  name: string;
  amount: number;
  share_pct: number;
  alias_count: number;
  sba_status?: string;
  location_tag?: string;
  founder_note?: string;
  source_url?: string;
}
export interface MantraconSdvosbSummary {
  count: number;
  local_count: number;
  out_of_region_count: number;
  total_dollars: number;
  total_share_pct: number;
}
export interface MantraconTopRecipientsBlock {
  recipients: MantraconTopRecipient[];
  total_dollars: number;
  lookback_months: number;
  top1_share: number;
  top3_share: number;
  concentration_label: string;
  sdvosb_summary?: MantraconSdvosbSummary;
  source: string;
}

// Manually-maintained SBA classification lookup (faithful port of
// console-api `KNOWN_SBA_STATUS`, web-sourced 2026-05-27). Verify any specific
// classification at SAM.gov before acting on it.
const MANTRACON_KNOWN_SBA_STATUS: Record<
  string,
  { sba_status: string; location_tag: string; founder_note: string; source_url: string }
> = {
  "SMITH HAFELI": { sba_status: "SDVOSB", location_tag: "LOCAL · Marion IL", founder_note: "USAF Col. Lance Hafeli", source_url: "https://smith-hafeli.com/about-us/" },
  "SDV OFFICE": { sba_status: "SDVOSB", location_tag: "OUT-OF-REGION · Fletcher NC", founder_note: "Two USMC officers · UEI verify at SAM.gov", source_url: "https://sdvosystems.com/contracts/" },
  "JETT": { sba_status: "SDVOSB", location_tag: "OUT-OF-REGION · Paducah KY", founder_note: "Jeffrey Jett · UEI verify at SAM.gov", source_url: "https://www.veteranownedbusiness.com/business/33768/jetts-specialty-contracting" },
  "ABOVE GROUP": { sba_status: "SDVOSB", location_tag: "OUT-OF-REGION · Melbourne FL", founder_note: "Founded 2014 · UEI N5WANJDVRMG8 · CAGE 7DG75 · 40 emp · $14M rev", source_url: "https://www.abovegroupinc.com/" },
  "NAPHCARE": { sba_status: "LARGE", location_tag: "OUT-OF-REGION · Vestavia Hills AL", founder_note: "NaphCare, Inc. (legal entity is Inc., not LLC) · ~$483M rev, largest BOP healthcare TPA", source_url: "https://www.naphcare.com/about" },
  "CDM FEDERAL": { sba_status: "LARGE", location_tag: "OUT-OF-REGION · 131 offices", founder_note: "CDM Smith subsidiary, ~5,000 employees", source_url: "https://en.wikipedia.org/wiki/CDM_Smith" },
  "ILLINOIS POWER MARKETING": { sba_status: "LARGE", location_tag: "OUT-OF-REGION · utility", founder_note: "Illinois Power Marketing Company, LLC — Vistra Corp. subsidiary (post-Dynegy merger). Confirm via Vistra 10-K Exhibit 21 (Subsidiaries List).", source_url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001692819&type=10-K" },
  "AOD & RBT": { sba_status: "UNCLASSIFIED", location_tag: "OUT-OF-REGION · Texarkana TX", founder_note: "AOD & RBT JV (UEI SEGBEAE2A2F5) — joint venture; recent LWA-25-area federal work: DOJ Federal Correctional Institution / Satellite Prison Camp roof replacement (likely USP / FCI Marion). Contract set-aside per USAspending: None — competitive award, NOT an SBA mentor-protégé set-aside despite the JV naming pattern.", source_url: "https://www.usaspending.gov/recipient/?recipient_uei=SEGBEAE2A2F5" },
  "FFE - HEAPY": { sba_status: "UNCLASSIFIED", location_tag: "OUT-OF-REGION · Blue Ash OH", founder_note: "FFE - HEAPY JV-II, LLC (UEI NB1SW7MGPU76) — joint venture with HEAPY Engineering (large Ohio-based MEP/engineering firm). Recent LWA-25-area federal work: VA Design of New Energy Center Construction (likely Marion VA Medical Center). Contract set-aside per USAspending: None — competitive engineering award, not riding a small-business set-aside.", source_url: "https://www.usaspending.gov/recipient/?recipient_uei=NB1SW7MGPU76" },
  "LAKE CONTRACTING": { sba_status: "UNCLASSIFIED", location_tag: "REGIONAL · Addieville IL (Washington Co., adjacent to LWA-25)", founder_note: "Lake Contracting, Inc. — small regional construction contractor (UEI TWNXK9JMJK55, 4650 Stone Church Rd, Addieville IL 62214). Primary federal work: USACE / DoD construction including Rend Lake West Side Sewer + similar utility/civil contracts. Not a set-aside firm; just a regional small business doing federal civil-construction work.", source_url: "https://www.usaspending.gov/recipient/?recipient_uei=TWNXK9JMJK55" },
};

const MANTRACON_DISPLAY_NAME_OVERRIDES: Record<string, string> = {
  NAPHCARE: "NaphCare, Inc.",
};

async function mantraconTopRecipients(
  countyFips: string[],
  { lookbackMonths = 24, topN = 12, stateFips = "17" } = {},
): Promise<MantraconTopRecipientsBlock> {
  const end = new Date();
  const start = new Date(end.getTime() - lookbackMonths * 30 * DAY * 1000);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  const stateCode = stateFips === "17" ? "IL" : stateFips;
  const locations = countyFips.map((c) => ({ country: "USA", state: stateCode, county: c }));
  const body = {
    filters: {
      time_period: [{ start_date: iso(start), end_date: iso(end) }],
      place_of_performance_locations: locations,
      award_type_codes: ["A", "B", "C", "D"],
    },
    limit: topN,
  };
  const resp = await usaspendingPost(
    "/api/v2/search/spending_by_category/recipient/",
    body,
  );
  const raw = (resp.results as Array<Record<string, unknown>>) ?? [];

  // Normalize recipient names to collapse punctuation variants of one entity.
  const normalize = (name: string): string => {
    let n = name.toUpperCase();
    for (const ch of [",", ".", "'", "&"]) {
      n = n.split(ch).join(ch === "&" ? " AND " : " ");
    }
    for (const ch of ["INC", "LLC", "CORPORATION", "CORP"]) {
      n = n.split(ch).join("");
    }
    return n.split(/\s+/).filter(Boolean).join(" ");
  };

  const collapsed = new Map<
    string,
    { name: string; amount: number; namesSeen: Set<string> }
  >();
  for (const r of raw) {
    const name = (r.name as string) ?? "";
    const amt = Number(r.amount ?? 0) || 0;
    const key = normalize(name);
    let entry = collapsed.get(key);
    if (!entry) {
      entry = { name, amount: 0, namesSeen: new Set<string>() };
      collapsed.set(key, entry);
    }
    entry.amount += amt;
    entry.namesSeen.add(name);
  }

  const displayName = (rawName: string): string => {
    const u = rawName.toUpperCase();
    for (const [key, override] of Object.entries(MANTRACON_DISPLAY_NAME_OVERRIDES)) {
      if (u.includes(key)) return override;
    }
    return rawName;
  };
  const lookupSba = (recName: string) => {
    const n = recName.toUpperCase();
    for (const [key, val] of Object.entries(MANTRACON_KNOWN_SBA_STATUS)) {
      if (n.includes(key)) return val;
    }
    return { sba_status: "UNCLASSIFIED", location_tag: "", founder_note: "", source_url: "" };
  };

  const items: MantraconTopRecipient[] = Array.from(collapsed.values())
    .map((v) => {
      const sba = lookupSba(v.name);
      return {
        name: displayName(v.name),
        amount: v.amount,
        alias_count: v.namesSeen.size,
        share_pct: 0,
        sba_status: sba.sba_status,
        location_tag: sba.location_tag,
        founder_note: sba.founder_note,
        source_url: sba.source_url,
      };
    })
    .sort((a, b) => b.amount - a.amount);

  const total = items.reduce((s, x) => s + x.amount, 0);
  for (const x of items) {
    x.share_pct = total ? Math.round((x.amount / total) * 1000) / 10 : 0;
  }

  const sdvosbItems = items.filter((x) => x.sba_status === "SDVOSB");
  const sdvosbTotal = sdvosbItems.reduce((s, x) => s + x.amount, 0);
  const localSdvosbCount = sdvosbItems.filter((x) =>
    (x.location_tag ?? "").includes("LOCAL"),
  ).length;
  const sdvosb_summary: MantraconSdvosbSummary = {
    count: sdvosbItems.length,
    local_count: localSdvosbCount,
    out_of_region_count: sdvosbItems.length - localSdvosbCount,
    total_dollars: sdvosbTotal,
    total_share_pct: total ? Math.round((sdvosbTotal / total) * 1000) / 10 : 0,
  };

  const top1Share = items.length ? items[0].share_pct : 0;
  const top3Share =
    Math.round(items.slice(0, 3).reduce((s, x) => s + x.share_pct, 0) * 10) / 10;
  let concentrationLabel: string;
  if (top1Share >= 70) {
    concentrationLabel =
      "EXTREME — single recipient dominates the regional federal-dollar flow";
  } else if (top1Share >= 40) {
    concentrationLabel =
      "HIGH — one recipient captures most federal contract dollars";
  } else if (top3Share >= 60) {
    concentrationLabel = "MODERATE — three recipients dominate";
  } else {
    concentrationLabel =
      "DIVERSE — federal contract dollars spread across many recipients";
  }

  return {
    recipients: items,
    total_dollars: total,
    lookback_months: lookbackMonths,
    top1_share: top1Share,
    top3_share: top3Share,
    concentration_label: concentrationLabel,
    sdvosb_summary,
    source:
      "USAspending.gov spending_by_category/recipient. Recipients are deduplicated " +
      "across name variants (punctuation differences) before aggregation. SBA " +
      "set-aside classification is from a manually-maintained lookup table sourced " +
      "to each recipient's website / SAM.gov / veteranownedbusiness.com — verify any " +
      "specific classification at SAM.gov before acting on it.",
  };
}

// ── training_alignment (faithful port of `_training_demand_alignment`) ───────
const MIT_LIVING_WAGE_JACKSON_IL_1A0C_WKLY = 758.0; // single adult no kids
const MIT_LIVING_WAGE_JACKSON_IL_1A2C_WKLY = 1870.0; // single adult + 2 kids
const MIT_LIVING_WAGE_YEAR = 2026;

interface TrainingLadderSeed {
  id: string;
  name: string;
  supersector_code: string;
  ladder: string;
  typical_journey_wage_wkly: number;
  training_duration: string;
  notes: string;
  local_employer_override?: number;
  total_package_wkly?: number;
  travel_work_credential?: boolean;
  owner_operator?: boolean;
  local_market_saturated?: boolean;
  entry_gates?: string[];
}

// Training-ladder roster — typical credentials offered by/through Man-Tra-Con /
// John A. Logan / Rend Lake / IBEW Local 702 / other regional providers.
// Faithful port of console-api `_TRAINING_LADDER_ROSTER` (refresh annually).
const MANTRACON_TRAINING_LADDER_ROSTER: TrainingLadderSeed[] = [
  { id: "nabcep_solar", name: "NABCEP solar installer (residential/commercial)", supersector_code: "1012", ladder: "SEI / employer-direct → NABCEP PV Installation Professional", typical_journey_wage_wkly: 1040, training_duration: "8-16 weeks", local_employer_override: 30, notes: "Distinct from CEJA Climate Works (separate row below). NABCEP is the IPS / Solar Energy International credential for residential + small-commercial installers. Local NABCEP employer base in LWA-25 is verified but small: StraightUp Solar (Marion IL office), Tick Tock Energy (Effingham IL), plus smaller EnergySage-listed installers. NOTE: utility-scale solar work goes to IBEW 702 + IUOE 318 + LIUNA 773 union construction, not NABCEP installers — see ceja_climate_works." },
  { id: "ceja_climate_works", name: "CEJA Climate Works pre-apprenticeship → union building trades", supersector_code: "1012", ladder: "HIRE360 8-12wk pre-app (MC3, OSHA 10, GPRO) → IBEW 702 / IUOE 318 / LIUNA 773 / UA 553 / Carpenters apprenticeship → utility-scale construction journey", typical_journey_wage_wkly: 2200, training_duration: "8-12wk pre-app + 3-5yr trades apprenticeship", travel_work_credential: true, notes: "TRAVEL-WORK PATHWAY — the legitimate CEJA-funded ladder. HIRE360 is the Southern IL Climate Works grantee. Graduates feed IBEW Local 702, IUOE Local 318, LIUNA Local 773, UA Local 553, Carpenters/Ironworkers — NOT NABCEP installers. Family-supporting wages with per-diem; the trade-off is the traveling lifestyle. Registered-apprenticeship completion rates run below 35% nationally; 5-year completion ~50-65%. Pitch it honestly as a 5-year ~50% gauntlet that pays family-supporting wages to those who complete." },
  { id: "ceja_wind", name: "CEJA wind technician (travel-work)", supersector_code: "1011", ladder: "Pre-app + GWO BST/BTT certifications → wind-tech entry", typical_journey_wage_wkly: 1500, training_duration: "12-20 weeks (incl. GWO BST + BTT)", local_employer_override: 0, travel_work_credential: true, notes: "TRAVEL-WORK PATHWAY. No utility-scale wind farms operate in Southern IL; the IL wind belt is Central + Northern. Graduates work the broader US wind belt — Iowa, Oklahoma, Texas, North Dakota — plus emerging East Coast offshore. The trade-off is the traveling lifestyle, not the wage. NOT a phantom credential — US wind-tech is one of the fastest-growing BLS occupations." },
  { id: "ceja_lineworker", name: "Lineworker (IBEW 702)", supersector_code: "1021", ladder: "Pre-app → 7×1,000hr apprenticeship periods (~3.5yr) → IBEW outside lineman journey", typical_journey_wage_wkly: 2621, training_duration: "~3.5 years apprenticeship (seven 1,000-hour periods)", notes: "Highest-wage clean-energy ladder + IBEW 702 (W. Frankfort) is local. Real family-supporting path — Big Muddy Solar's 124MW construction in Jackson Co. is hiring Local 702 lineworkers. Journey wage per published IBEW 702 outside-lineman wage sheet effective Jan 2025 (ibew702.org)." },
  { id: "electrician", name: "Electrician (IBEW 702)", supersector_code: "1012", ladder: "Pre-app → 5yr apprenticeship → IBEW journey", typical_journey_wage_wkly: 1680, training_duration: "5 years apprenticeship", notes: "IBEW Local 702 covers most of LWA-25. Strong local construction demand. Hits the family-supporting threshold at journey-out exactly." },
  { id: "cdl_class_a", name: "CDL Class A (truck driver, W-2)", supersector_code: "1021", ladder: "JALC or Rend Lake 4-8wk CDL school", typical_journey_wage_wkly: 1000, training_duration: "4-8 weeks", entry_gates: ["dot_physical", "drug_screen", "cdl_class_a"], notes: "Local W-2 jobs (FedEx Marion hub, Walgreens Distribution Mt. Vernon, Aisin logistics) pay $22-28/hr. Regional OTR $35-45/hr but takes drivers away from family. Local rate sub-1A+2C; OTR rate breaks the family-supporting frame the other way. See coal_hauler_ownerop for owner-operator economics." },
  { id: "coal_hauler_ownerop", name: "Coal-hauler owner-operator (Knight Hawk Prairie Eagle → Cora terminal)", supersector_code: "1021", ladder: "CDL Class A school + truck financing + DOT authority", typical_journey_wage_wkly: 2980, training_duration: "4-8wk CDL + 0-1yr W-2 OTR experience before financing a truck", local_employer_override: 40, owner_operator: true, entry_gates: ["dot_physical", "drug_screen", "cdl_class_a"], notes: "Knight Hawk's Prairie Eagle Mine (Perry Co., ~30 mi short-haul) routes via owner-operator truck to the Cora Marine Terminal (Rockwood IL). Wage column shows GROSS (~$155k OOIDA-survey); NET after expenses $22-27k for short-haul respondents, $40-70k for experienced operators. Cohort risk: 60-70hr weeks, heavy maintenance burden, market volatility, coal-dust lung exposure. Small operators have failed at these margins." },
  { id: "cna", name: "CNA (Certified Nursing Asst.)", supersector_code: "1025", ladder: "4-6 week certification", typical_journey_wage_wkly: 640, training_duration: "4-6 weeks", notes: "Easy to place into — Memorial Hospital, SIH, nursing homes all hire CNAs. BUT pays $14-17/hr — below single-adult living wage. Risk of getting stuck training for low-wage care-economy jobs because they're easy to place into." },
  { id: "lpn", name: "LPN (Licensed Practical Nurse)", supersector_code: "1025", ladder: "12-month diploma program", typical_journey_wage_wkly: 1000, training_duration: "12 months", notes: "Significant step up from CNA. Common ladder rung. Still single-adult-only territory." },
  { id: "rn_adn", name: "RN (ADN, Associate Degree)", supersector_code: "1025", ladder: "2yr ADN at JALC + NCLEX (+ optional 1yr local before travel-agency)", typical_journey_wage_wkly: 1380, training_duration: "2 years", notes: "Memorial Hospital + SIH + Marion VA all hire ADN-RNs. Strong local demand at $32-38/hr starting. CRITICAL PATH UP: after 1 year local floor experience, RNs become eligible for travel-nurse agencies paying $60-110/hr blended ($130-200k+/yr). The 'land at SIH for 1 year then go travel' play is the highest-dollar 2-year-credential path in the region." },
  { id: "welding", name: "Welder (structural / pipe)", supersector_code: "1013", ladder: "JALC 12-18mo welding program + AWS certs", typical_journey_wage_wkly: 1240, training_duration: "12-18 months", notes: "Manufacturing demand at Continental Tire, Aisin, Penn Aluminum. Family-supporting at journey-out. Pipefitter (Local 160 Mt. Vernon) goes higher." },
  { id: "industrial_maint", name: "Industrial maintenance / mechatronics", supersector_code: "1013", ladder: "JALC 18-24mo mechatronics program", typical_journey_wage_wkly: 1320, training_duration: "18-24 months", notes: "Continental Tire is the anchor employer. Strong local demand + clears family-supporting threshold." },
  { id: "it_support", name: "IT support (Network+/Security+)", supersector_code: "1022", ladder: "Stacked CompTIA certs", typical_journey_wage_wkly: 1080, training_duration: "6-12 months", notes: "Local employer base is tiny — Information sector has ~50-200 jobs in LWA-25. The ceiling is low locally; better framed as a 'work-from-anywhere' ladder than a 'land at a local employer' ladder." },
  { id: "underground_coal_miner", name: "Underground coal miner (UMWA scale, Sugar Camp / Pond Creek / Knight Hawk)", supersector_code: "1011", ladder: "MSHA Part 48 surface (40hr) + underground (24hr) certification → on-job training under journey miner", typical_journey_wage_wkly: 1400, total_package_wkly: 2000, training_duration: "64hr MSHA Part 48 cert + 6-12mo apprentice / red-hat → full miner", local_employer_override: 800, entry_gates: ["msha_part_48_certification", "physical_fitness", "drug_screen", "no_claustrophobia", "often_family_connection_to_enter"], notes: "Sugar Camp (Franklin Co.) + Pond Creek / Mach #1 (Williamson + Franklin) + Knight Hawk Prairie Eagle (Perry Co.) are the active LWA-25-region mines hiring underground positions. UMWA scale ~$28-40/hr base; productivity bonuses + 50-60hr weeks can push take-home to $80-110k/yr. Entry gate: MSHA Part 48 cert + physical/drug screen; many mines recruit through family connections. Within-credential employer-level variance is large (Knight Hawk lowest-paying + 'family' culture; Foresight pays better but mandates OT to home-life conflict)." },
  { id: "river_barge_crew", name: "River-barge deckhand → mate → pilot (Cora / Mississippi + Ohio reach)", supersector_code: "1021", ladder: "USCG Merchant Mariner Credential (MMC) entry-level → deckhand → 360 days sea-time → mate test → pilot test", typical_journey_wage_wkly: 1500, training_duration: "MMC + TWIC processing 2-6 months entry; sea-time 360 days for mate; multi-year ladder to pilot", local_employer_override: 60, travel_work_credential: true, entry_gates: ["uscg_merchant_mariner_credential", "twic_card", "drug_screen", "physical", "swim_test"], notes: "TRAVEL-WORK PATHWAY. Employers in the Upper Miss + Ohio reach: ACBL, Marquette Transportation, Ingram Marine, Madison Coal & Supply, Calumet Marine. 'Pure hawsepipe industry' — wheelhouse advancement is connection-gated. Entry deckhand ~$52k/yr base; mate/engineer $300-500/day; pilot $125k+. Wage clears 1A+2C at mate/engineer/pilot rungs; lifestyle cost (long rotations, divorce risk, injury) is the structural trade-off." },
  { id: "il_doc_officer", name: "IL DOC correctional officer (Pinckneyville / Big Muddy / Vienna / Shawnee)", supersector_code: "9091", ladder: "Civil service exam (CMS) → background investigation → 5-week DOC training academy at Logan CC", typical_journey_wage_wkly: 1430, total_package_wkly: 1900, training_duration: "5-week academy + 6mo probationary period", local_employer_override: 1200, entry_gates: ["physical_fitness", "background_check", "drug_screen", "civil_service_exam", "post_academy"], notes: "IL DOC carries a structural staffing crisis (~25% overall vacancy, ~28-29% corrections-officer vacancy). One of the few reliably-open family-supporting state jobs in the region. Attrition is mandatory-OT-driven. Pension + benefits keep people; mandatory-OT family-time loss is the dominant exit driver. Physical fitness test is a real wash-out." },
  { id: "idot_highway_maintainer", name: "IDOT Highway Maintainer (District 9)", supersector_code: "9091", ladder: "CMS civil service exam → CDL Class A → IDOT field training", typical_journey_wage_wkly: 1200, total_package_wkly: 1620, training_duration: "CDL 4-8 weeks + IDOT field training 3-6 months", local_employer_override: 80, local_market_saturated: true, entry_gates: ["cdl_class_a", "civil_service_exam", "drug_screen", "dot_physical"], notes: "State civil service job covering road plowing, pavement repair, sign maintenance, snow removal. Hiring is attrition-only. Family-supporting all-in with state pension + benefits + storm-response OT. The CDL gate is the biggest entry barrier." },
  { id: "hvac_union_sheet_metal", name: "HVAC sheet metal (SMART Local 268 union)", supersector_code: "1012", ladder: "SMART Local 268 5-year apprenticeship — apprentice → journey sheet metal worker", typical_journey_wage_wkly: 1863, total_package_wkly: 2857, training_duration: "5-year apprenticeship (10,000hr OJT + 1,000hr classroom)", entry_gates: ["aptitude_test", "drug_screen", "basic_math"], notes: "Verified 2025 wage sheet (Local 268, covers 36 Southern IL counties): JOURNEY $46.57/hr check + $24.86 benefits = $71.43/hr total package. Coverage includes all of LWA-25. Major commercial/industrial HVAC work: Continental Tire, Aisin, SIH + Marion VA, SIU, school installs, GD-OTS environmental controls. UA Local 553 covers HVAC mechanical/refrigeration on the pipefitter side." },
  { id: "hvac_residential_nonunion", name: "HVAC residential (non-union, small shop)", supersector_code: "1012", ladder: "EPA 608 certification + 1-2yr OJT at residential service shop", typical_journey_wage_wkly: 880, training_duration: "EPA 608 + 1-2yr OJT to service tech", local_employer_override: 150, entry_gates: ["epa_608_universal_certification", "drug_screen", "valid_drivers_license"], notes: "DISTINCT from the union sheet-metal track. Small residential/light-commercial shops pay $18-25/hr; below 1A+2C unless owner of shop or specialize. Clearing 1A+2C requires shop ownership, commercial specialization, relocation, or crossing to union. Placement easy but wage ceiling is the constraint." },
  { id: "auto_mechanic_hs_cte", name: "Auto mechanic (HS CTE → dealership / garage)", supersector_code: "1024", ladder: "HS CTE program → ASE certifications stacked", typical_journey_wage_wkly: 760, training_duration: "2-yr HS CTE program (free) + ASE certs over working career", local_employer_override: 250, entry_gates: ["drug_screen", "valid_drivers_license"], notes: "Local placement WORKS — HS CTE grads land at small-town shops + dealerships. Local wage FAILS 1A+2C: rural shops $15-22/hr; MSA dealership flag-rate $22-30/hr; city dealerships $30-40+/hr. Flat-rate compensation structurally hurts rural shops. Clearing 1A+2C requires flag-rate at high-volume dealership, specialization, shop ownership, or relocation." },
  { id: "diesel_mechanic", name: "Diesel mechanic (Kaskaskia / Rend Lake / SIC programs)", supersector_code: "1024", ladder: "Community college 1-2yr diesel-tech AAS or 8-month certificate", typical_journey_wage_wkly: 1166, training_duration: "8mo-2yr program", local_employer_override: 80, entry_gates: ["drug_screen", "valid_drivers_license", "cdl_class_a_bumps_wage"], notes: "The credential LOOKS mapped on paper but the local ENTRY-LEVEL slot pipeline is broken: big captive shops hire experienced from within; railroads hire centrally; ag-equipment dealers prefer 3-5yr experience. Typical path: train locally → travel for first job to log 3-5yr experience → maybe return later. Mobile/field-service techs earn $65-90k vs local rural $38-45k (20-30% travel premium). PHANTOM locally at entry / TRAVEL-WORK to build experience." },
  { id: "aisin_production_tech", name: "Aisin production technician (Marion — Aisin Mfg / Electronics / Light Metals)", supersector_code: "1013", ladder: "HS diploma → Aisin onboarding + on-job training", typical_journey_wage_wkly: 900, training_duration: "2-4wk onboarding + ongoing skills progression", local_employer_override: 2000, entry_gates: ["hs_diploma", "drug_screen", "basic_skills_assessment"], notes: "Major Williamson Co. employer — 2,000+ jobs across three Aisin entities in Marion. Production-tech roles start $18-22/hr; lead + skilled trades $24-30/hr; with overtime + 2nd-shift premium can clear 1A+2C. Mild entry gate. Maps well to HS CTE manufacturing tracks + JALC industrial-maintenance/mechatronics." },
  { id: "hotel_hospitality_mgmt", name: "Hotel / hospitality management (HS or bachelor's path)", supersector_code: "1027", ladder: "HS CTE hospitality OR community-college AAS OR bachelor's hospitality management", typical_journey_wage_wkly: 725, training_duration: "HS CTE 2yr (free) OR AAS 2yr OR bachelor's 4yr ($30-80k tuition debt)", local_employer_override: 25, local_market_saturated: true, entry_gates: ["hs_diploma_or_bachelors", "drug_screen", "customer_service_experience_typical"], notes: "A degree in hotel management does not have good ROI. BLS OEWS SOC 11-9081 (Lodging Managers) May 2024: bachelor's entry median $37,668; experienced still below 1A+2C. Tuition debt + $37k entry wage = ROI fails the family-supporting test. LWA-25 lodging-manager market is small + saturated (~20-25 GM slots, attrition-only entry)." },
  { id: "continental_tire_production", name: "Continental Tire production operator (Mt. Vernon)", supersector_code: "1013", ladder: "HS diploma → Continental Tire onboarding + tire-build certification", typical_journey_wage_wkly: 1000, training_duration: "4-8wk onboarding + tire-build certification", local_employer_override: 3667, entry_gates: ["hs_diploma", "drug_screen", "physical", "mechanical_aptitude_test"], notes: "JEFFERSON CO. ANCHOR — 3,667 jobs in Mt. Vernon, the largest single employer south of Peoria. Production operator + tire builder roles start $20-25/hr; with overtime + 2nd-shift premium + skilled-trades progression clears 1A+2C. One of the few LWA-25 employers where the production rung itself can be family-supporting." },
];

export interface TrainingLadderRow {
  id: string;
  name: string;
  ladder: string;
  training_duration: string;
  typical_journey_wage_wkly: number;
  typical_journey_wage_hrly: number;
  total_package_wkly?: number;
  supersector_name: string;
  supersector_code: string;
  local_sector_employment: number;
  local_sector_share_pct: number;
  local_sector_avg_weekly_wage: number;
  demand_signal: string;
  vs_single_adult_livable_wkly: number;
  vs_family_livable_wkly: number;
  verdict: string;
  verdict_color: string;
  notes: string;
  entry_gates?: string[];
}
export interface TrainingAlignment {
  ladders: TrainingLadderRow[];
  livable_wage_jackson_il: {
    single_adult_wkly: number;
    single_adult_hrly: number;
    family_1a2c_wkly: number;
    family_1a2c_hrly: number;
    source: string;
  };
  source: string;
}

/** Cross-reference each training ladder against the local QCEW supersector mix +
 *  the MIT living-wage benchmark. Pure derivation — faithful port of console-api
 *  `_training_demand_alignment`. */
function trainingDemandAlignment(
  qcewBlock: IndustryMix | null,
): TrainingAlignment {
  const qcewByCode = new Map<string, IndustryRow>();
  for (const s of qcewBlock?.top_supersectors ?? []) qcewByCode.set(s.code, s);
  const lwaTotalEmp = qcewBlock?.total_employment ?? 0;
  const livable1a0c = MIT_LIVING_WAGE_JACKSON_IL_1A0C_WKLY;
  const livable1a2c = MIT_LIVING_WAGE_JACKSON_IL_1A2C_WKLY;

  const ladders: TrainingLadderRow[] = MANTRACON_TRAINING_LADDER_ROSTER.map(
    (tl) => {
      const qcewRow = qcewByCode.get(tl.supersector_code);
      let sectorEmp: number;
      let credentialSpecific: boolean;
      if (tl.local_employer_override != null) {
        sectorEmp = tl.local_employer_override;
        credentialSpecific = true;
      } else {
        sectorEmp = qcewRow?.total_employment ?? 0;
        credentialSpecific = false;
      }
      const sectorWage = qcewRow?.avg_weekly_wage ?? 0;
      const wage = tl.total_package_wkly ?? tl.typical_journey_wage_wkly;

      let demand: string;
      if (credentialSpecific) {
        if (sectorEmp === 0) demand = "NONE";
        else if (sectorEmp < 15) demand = "VERY LOW";
        else if (sectorEmp < 50) demand = "MODEST";
        else if (sectorEmp < 200) demand = "MODERATE";
        else demand = "HIGH";
      } else {
        if (sectorEmp === 0) demand = "NONE";
        else if (sectorEmp < 1000) demand = "VERY LOW";
        else if (sectorEmp < 3000) demand = "LOW";
        else if (sectorEmp < 10000) demand = "MODERATE";
        else demand = "HIGH";
      }

      let verdict: string;
      let verdictColor: string;
      if (tl.owner_operator) {
        verdict = "OWNER-OPERATOR · GROSS-MISLEADS · NET-GRIND";
        verdictColor = "warn";
      } else if (tl.travel_work_credential) {
        verdict = "TRAVEL-WORK · wage clears / lifestyle cost is high";
        verdictColor = "warn";
      } else if (tl.id === "cdl_class_a") {
        verdict = "FAMILY-TIME CONFLICT";
        verdictColor = "warn";
      } else if (demand === "NONE") {
        verdict = "PHANTOM PIPELINE";
        verdictColor = "danger";
      } else if (demand === "VERY LOW") {
        if (credentialSpecific) {
          verdict = "LOCAL · SATURATED — pipeline exists, absorbs ~1-2 grads/yr";
          verdictColor = "warn";
        } else {
          verdict = "PHANTOM PIPELINE";
          verdictColor = "danger";
        }
      } else if (wage < livable1a0c) {
        verdict = "LOCAL · WAGE-SUPPRESSED — fails single-adult LW";
        verdictColor = "danger";
      } else if (wage < livable1a2c) {
        verdict = "LOCAL · WAGE-SUPPRESSED — clears single-adult, fails 1A+2C";
        verdictColor = "warn";
      } else if (tl.local_market_saturated) {
        verdict =
          "LOCAL · FAMILY-SUPPORTING · SATURATED — wage clears but supply > demand";
        verdictColor = "warn";
      } else {
        verdict = "LOCAL · FAMILY-SUPPORTING";
        verdictColor = "good";
      }

      const sectorDisplay = credentialSpecific
        ? "Credential-specific (out-of-region only)"
        : (qcewRow?.name ?? "—");

      return {
        id: tl.id,
        name: tl.name,
        ladder: tl.ladder,
        training_duration: tl.training_duration,
        typical_journey_wage_wkly: wage,
        typical_journey_wage_hrly: Math.round((wage / 40) * 100) / 100,
        total_package_wkly: tl.total_package_wkly,
        supersector_name: sectorDisplay,
        supersector_code: tl.supersector_code,
        local_sector_employment: sectorEmp,
        local_sector_share_pct: lwaTotalEmp
          ? Math.round((sectorEmp / lwaTotalEmp) * 1000) / 10
          : 0,
        local_sector_avg_weekly_wage: sectorWage,
        demand_signal: demand,
        vs_single_adult_livable_wkly: Math.round(wage - livable1a0c),
        vs_family_livable_wkly: Math.round(wage - livable1a2c),
        verdict,
        verdict_color: verdictColor,
        notes: tl.notes,
        entry_gates: tl.entry_gates ?? [],
      };
    },
  );

  return {
    ladders,
    livable_wage_jackson_il: {
      single_adult_wkly: livable1a0c,
      single_adult_hrly: Math.round((livable1a0c / 40) * 100) / 100,
      family_1a2c_wkly: livable1a2c,
      family_1a2c_hrly: Math.round((livable1a2c / 40) * 100) / 100,
      source: `MIT Living Wage Calculator, Jackson County IL (${MIT_LIVING_WAGE_YEAR} values, refresh annually via livingwage.mit.edu/counties/17077)`,
    },
    source:
      "Local sector employment + avg weekly wage from BLS QCEW (latest published quarter, " +
      "from the industry_mix block on this page). Training journey-out wages from the local " +
      "advisory roster — typical figures; individual outcomes vary. Verdicts compare typical " +
      "journey-out wage to the MIT Living Wage benchmark for Jackson County (1 adult + 2 " +
      "children). PHANTOM PIPELINE = local employer base is essentially zero, so the credential " +
      "has nowhere to land locally even if wages would clear the bar.",
  };
}

/** Man-Tra-Con / SIWIB (LWA-25) payload — 5-county aggregate. Faithful port of
 *  console-api `public_mantracon()`. The `gdots_subaward_lanes*` fields are the
 *  heavy USAspending subaward crawls; they are `null` here (the page hides those
 *  tables when null and renders the full static supply-chain section regardless). */
export interface MantraconData {
  ts: string;
  indicators: Record<string, Indicator>;
  lwa_aggregate: LwaAggregate;
  lwa_labor_force_series: Array<{ date: string; value: number }>;
  lwa_unemployment_series: Array<{ date: string; value: number }>;
  business_opportunities: BusinessOps;
  top_federal_recipients?: MantraconTopRecipientsBlock;
  industry_mix?: IndustryMix;
  labor_truth?: LaborTruth;
  training_alignment?: TrainingAlignment;
  gdots_subaward_lanes?: null;
  gdots_subaward_lanes_bulk?: null;
}

export async function getMantraconData(): Promise<MantraconData> {
  const cfg = MANTRACON_CONFIG;
  const stateFips = cfg.stateFips ?? "17";

  const [indicators, business, topRecipients, industryMix, laborTruth, perCountySeries] =
    await Promise.all([
      fetchFredIndicators(cfg.fredSeries),
      usaspendingBlock({ countyFips: cfg.countyFips, recipientCity: null, stateFips }),
      mantraconTopRecipients(cfg.countyFips, { stateFips }),
      qcewSupersectorBlock(cfg.countyFips, MANTRACON_FIPS_NAME),
      acsLaborTruth({ countyFips: cfg.countyFips, stateFips, year: 2023 }),
      Promise.all(
        MANTRACON_COUNTY_FRED.map(async (c) => {
          const [ur, lf] = await Promise.all([
            fredSeriesRecent(c.ur),
            fredSeriesRecent(c.lf),
          ]);
          return { ur, lf };
        }),
      ),
    ]);

  const aggr = lwaWeightedAggregate(perCountySeries);
  const trainingAlignment = trainingDemandAlignment(industryMix);

  return {
    ts: new Date().toISOString(),
    indicators,
    lwa_aggregate: {
      labor_force: aggr.latest_lf,
      labor_force_date: aggr.latest_lf_date,
      unemployment_rate_weighted: aggr.latest_ur,
      unemployment_rate_date: aggr.latest_ur_date,
      county_count: MANTRACON_FIPS.length,
    },
    lwa_labor_force_series: aggr.lf_series,
    lwa_unemployment_series: aggr.ur_series,
    business_opportunities: business ?? EMPTY_BUSINESS_OPS,
    top_federal_recipients: topRecipients,
    industry_mix: industryMix ?? undefined,
    labor_truth: laborTruth ?? undefined,
    training_alignment: trainingAlignment,
    gdots_subaward_lanes: null,
    gdots_subaward_lanes_bulk: null,
  };
}
