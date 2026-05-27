/**
 * Public /murphysboro page — Murphysboro, IL economic snapshot.
 *
 * Murphysboro is the Jackson County seat, 8 mi W of Carbondale, pop ~7.6k.
 * Shares the Jackson County / Carbondale-Marion MSA FRED substrate with
 * /carbondale; differentiation comes from city-specific USAspending awards
 * (recipient_city=MURPHYSBORO) layered over the county-wide context.
 */
export const dynamic = "force-dynamic";
export const revalidate = 0;

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "https://console-api-production-4576.up.railway.app";

interface BusinessOps {
  top_awards: Array<{
    amount: number; recipient: string; agency: string; description: string;
    naics_code: string | null; naics_desc: string | null;
    start_date: string; end_date: string;
  }>;
  top_naics: Array<{ code: string; name: string; amount: number }>;
  totals: { awards_count: number; awards_dollars: number; lookback_months: number };
  sam_gov_search_link: string;
}

interface IndustryRow {
  code: string;
  name: string;
  total_employment: number;
  private_employment: number;
  public_employment: number;
  avg_weekly_wage: number;
  annual_pay_equivalent: number;
}
interface IndustryMix {
  as_of_quarter: string;
  top_supersectors: IndustryRow[];
  total_employment: number;
  source: string;
}

interface CityDemographics {
  name: string;
  place_fips: string;
  year: number;
  population: number | null;
  median_age: number | null;
  pct_bachelors_plus: number | null;
  acs_unemployment_rate: number | null;
  median_household_income: number | null;
  poverty_rate_families: number | null;
  median_home_value: number | null;
  median_gross_rent: number | null;
  pct_owner_occupied: number | null;
  pct_renter_occupied: number | null;
  pct_foreign_born: number | null;
  mean_commute_minutes: number | null;
  pct_white_alone: number | null;
  pct_black_alone: number | null;
  pct_hispanic_or_latino: number | null;
  source: string;
}

interface DemoDelta {
  abs_change: number;
  pct_change: number;
  prior_value: number;
}
interface DemographicsTrend {
  current: CityDemographics;
  comparison_years: [number, number];
  deltas: Record<string, DemoDelta>;
}
interface HealthComponent {
  key: string;
  label: string;
  value: string;
  score: number | null;
  weight: number;
  rationale: string;
}
interface HealthScore {
  score: number | null;
  label: string;
  components: HealthComponent[];
  methodology: string;
}

function labelColor(label: string): { fg: string; bg: string } {
  switch (label) {
    case "Healthy":   return { fg: "oklch(40% 0.16 142)", bg: "oklch(96% 0.04 142)" };
    case "Stable":    return { fg: "oklch(40% 0.16 142)", bg: "oklch(96% 0.04 142)" };
    case "At-Risk":   return { fg: "oklch(40% 0.15 60)",  bg: "oklch(97% 0.04 60)"  };
    case "Distressed":return { fg: "oklch(40% 0.20 22)",  bg: "oklch(96% 0.05 22)"  };
    case "Crisis":    return { fg: "oklch(35% 0.22 22)",  bg: "oklch(94% 0.06 22)"  };
    default:          return { fg: "#5a564d", bg: "#f0ece1" };
  }
}

function ScoreBar({ score }: { score: number }) {
  return (
    <div style={{ position: "relative", height: 10, background: "#ebe5d6", borderRadius: 5, marginTop: 10 }}>
      <div style={{
        position: "absolute", top: 0, left: 0, height: 10, width: `${score}%`,
        background: score >= 60 ? "oklch(55% 0.16 142)" : score >= 40 ? "oklch(58% 0.15 60)" : "oklch(55% 0.20 22)",
        borderRadius: 5,
      }} />
    </div>
  );
}

function HealthScoreSection({ health, cityShortName }: { health: HealthScore; cityShortName: string }) {
  if (health.score == null) return null;
  const tone = labelColor(health.label);
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Community Health Score · {cityShortName}
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 720 }}>
        A single 0-100 composite synthesizing six hardship-vs-resilience signals from the Census ACS.
        Methodology inspired by the EIG Distressed Communities Index — HS-dropout rate is the
        most heavily-weighted predictor of long-term distress in published research.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 24, alignItems: "start" }}>
        <div style={{ background: tone.bg, border: `2px solid ${tone.fg}33`, borderRadius: 8, padding: 24, textAlign: "center" }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", color: tone.fg, marginBottom: 6 }}>
            Health Score
          </div>
          <div style={{ fontSize: 72, fontWeight: 700, color: tone.fg, lineHeight: 1 }}>
            {health.score!.toFixed(0)}
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 2 }}>out of 100</div>
          <div style={{
            display: "inline-block", marginTop: 12, padding: "6px 14px",
            background: tone.fg, color: "white", borderRadius: 4, fontSize: 13, fontWeight: 600,
            textTransform: "uppercase", letterSpacing: "0.06em",
          }}>{health.label}</div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 14, lineHeight: 1.5 }}>
            80+ Healthy · 60+ Stable · 40+ At-Risk · 20+ Distressed · &lt;20 Crisis
          </div>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 12 }}>
            Component breakdown
          </div>
          {health.components.map(c => (
            <div key={c.key} style={{ marginBottom: 14 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18" }}>{c.label}</div>
                <div style={{ fontSize: 18, fontWeight: 600, color: c.score == null ? "#7a756b" : c.score >= 60 ? "oklch(45% 0.16 142)" : c.score >= 40 ? "oklch(48% 0.15 60)" : "oklch(45% 0.20 22)" }}>
                  {c.score != null ? c.score.toFixed(0) : "—"}<span style={{ fontSize: 12, color: "#7a756b", fontWeight: 400 }}> / 100</span>
                </div>
              </div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 2 }}>{c.value}</div>
              {c.score != null && <ScoreBar score={c.score} />}
              <details style={{ marginTop: 6, fontSize: 11, color: "#7a756b" }}>
                <summary style={{ cursor: "pointer" }}>Why this matters</summary>
                <div style={{ marginTop: 4 }}>{c.rationale}</div>
              </details>
            </div>
          ))}
        </div>
      </div>
      <div style={{ marginTop: 12, fontSize: 11, color: "#7a756b", lineHeight: 1.5, maxWidth: 720 }}>
        <strong>Methodology:</strong> {health.methodology} The HS-dropout rate is included because it is the strongest single predictor of long-term economic distress in EIG / CDC SVI / Opportunity Insights research.
      </div>
    </section>
  );
}

interface MurphysboroData {
  ts: string;
  indicators: Record<string, { value: number; date: string }>;
  unemployment_series: Array<{ date: string; value: number }>;
  business_opportunities_city: BusinessOps;
  business_opportunities_county: BusinessOps;
  industry_mix?: IndustryMix;
  city_demographics?: CityDemographics;
  demographics_trend?: DemographicsTrend;
  health_score?: HealthScore;
}

const TREND_GOOD_UP = new Set(["population", "median_household_income"]);
const TREND_GOOD_DOWN = new Set(["poverty_rate_families", "acs_unemployment_rate"]);

function trendTone(key: string, pct_change: number): { color: string; bg: string } {
  if (TREND_GOOD_UP.has(key)) {
    return pct_change > 0
      ? { color: "oklch(40% 0.16 142)", bg: "oklch(96% 0.04 142)" }
      : { color: "oklch(40% 0.20 22)", bg: "oklch(96% 0.05 22)" };
  }
  if (TREND_GOOD_DOWN.has(key)) {
    return pct_change < 0
      ? { color: "oklch(40% 0.16 142)", bg: "oklch(96% 0.04 142)" }
      : { color: "oklch(40% 0.20 22)", bg: "oklch(96% 0.05 22)" };
  }
  return { color: "#3d3a33", bg: "#f0ece1" };
}

function DemographicsSection({ d, cityShortName, trend }: { d: CityDemographics; cityShortName: string; trend?: DemographicsTrend }) {
  if (!d.population) return null;
  const fmtPct = (v: number | null) => v == null ? "—" : `${v.toFixed(1)}%`;
  const fmtMoneyL = (v: number | null) => v == null ? "—" : `$${v.toLocaleString()}`;
  const priorYear = trend?.comparison_years?.[1];
  const PP_VARS = new Set(["poverty_rate_families", "acs_unemployment_rate", "pct_owner_occupied"]);

  const renderTrend = (key: string) => {
    const dl = trend?.deltas?.[key];
    if (!dl || priorYear == null) return null;
    const display = PP_VARS.has(key)
      ? `${dl.abs_change > 0 ? "+" : ""}${dl.abs_change.toFixed(1)}pp vs ${priorYear}`
      : `${dl.pct_change > 0 ? "+" : ""}${dl.pct_change.toFixed(1)}% vs ${priorYear}`;
    const tone = trendTone(key, dl.pct_change);
    return (
      <div style={{ fontSize: 12, color: tone.color, background: tone.bg, padding: "2px 6px", borderRadius: 3, marginTop: 8, fontWeight: 600, display: "inline-block" }}>{display}</div>
    );
  };

  const stats: Array<{ key: string | null; label: string; value: string; sub?: string }> = [
    { key: "population",              label: "Population",                          value: d.population!.toLocaleString(), sub: `ACS 5y ${d.year}` },
    { key: "median_age",              label: "Median age",                          value: d.median_age != null ? `${d.median_age.toFixed(1)} yrs` : "—", sub: d.median_age != null && d.median_age >= 38 ? "established / family-skew" : d.median_age != null && d.median_age < 30 ? "very young" : "near US median" },
    { key: null,                      label: "Bachelor's degree or higher (25+)",   value: fmtPct(d.pct_bachelors_plus) },
    { key: "median_household_income", label: "Median household income",             value: fmtMoneyL(d.median_household_income) },
    { key: "poverty_rate_families",   label: "Family poverty rate",                 value: fmtPct(d.poverty_rate_families) },
    { key: "acs_unemployment_rate",   label: "ACS unemployment (25+)",              value: fmtPct(d.acs_unemployment_rate), sub: "5y avg, narrower than LAUS" },
    { key: "median_home_value",       label: "Median home value",                   value: fmtMoneyL(d.median_home_value), sub: "owner-occupied" },
    { key: "median_gross_rent",       label: "Median gross rent",                   value: fmtMoneyL(d.median_gross_rent) },
    { key: "pct_owner_occupied",      label: "% owner-occupied",                    value: fmtPct(d.pct_owner_occupied), sub: d.pct_renter_occupied != null ? `${d.pct_renter_occupied.toFixed(1)}% renter` : undefined },
    { key: "mean_commute_minutes",    label: "Mean commute time",                   value: d.mean_commute_minutes != null ? `${Math.round(d.mean_commute_minutes)} min` : "—", sub: "one-way" },
  ];

  const headlineKeys: Array<[string, string, string]> = [
    ["population",              "Population",          "city headcount"],
    ["median_household_income", "Median HH income",    "per family unit"],
    ["poverty_rate_families",   "Family poverty",      "of families"],
    ["acs_unemployment_rate",   "ACS unemployment",    "ages 25+"],
  ];

  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Demographics · {cityShortName}
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 720 }}>
        Census American Community Survey 5-year estimates for the {cityShortName}
        municipality (not the broader county).
      </div>

      {trend?.deltas && priorYear != null && (
        <div style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", margin: "0 0 4px 0", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Direction of travel · {priorYear} → {d.year}
          </h3>
          <div style={{ fontSize: 13, color: "#5a564d", marginBottom: 12, maxWidth: 720 }}>
            How {cityShortName} has changed since the prior Census ACS5 release. Green = improvement, red = deterioration, grey = direction-agnostic.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
            {headlineKeys.map(([key, label, sub]) => {
              const dl = trend.deltas[key];
              if (!dl) return null;
              const isPP = PP_VARS.has(key);
              const display = isPP
                ? `${dl.abs_change > 0 ? "+" : ""}${dl.abs_change.toFixed(1)}pp`
                : `${dl.pct_change > 0 ? "+" : ""}${dl.pct_change.toFixed(1)}%`;
              const tone = trendTone(key, dl.pct_change);
              return (
                <div key={key} style={{ background: tone.bg, border: `1px solid ${tone.color}33`, borderRadius: 6, padding: 14 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: tone.color, marginBottom: 4 }}>{label}</div>
                  <div style={{ fontSize: 28, fontWeight: 600, color: tone.color, lineHeight: 1.05 }}>{display}</div>
                  <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>{sub}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 16 }}>
        {stats.map((s, i) => (
          <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
            <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 6 }}>{s.label}</div>
            <div style={{ fontSize: 22, fontWeight: 500, color: "#1f1d18", lineHeight: 1.1, marginBottom: 4 }}>{s.value}</div>
            {s.sub && <div style={{ fontSize: 12, color: "#7a756b" }}>{s.sub}</div>}
            {s.key && renderTrend(s.key)}
          </div>
        ))}
      </div>
      <div style={{ marginTop: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 8 }}>
          Race / ethnicity composition
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12, fontSize: 14 }}>
          <div><strong>{fmtPct(d.pct_white_alone)}</strong> <span style={{ color: "#5a564d" }}>White alone</span></div>
          <div><strong>{fmtPct(d.pct_black_alone)}</strong> <span style={{ color: "#5a564d" }}>Black or African American alone</span></div>
          <div><strong>{fmtPct(d.pct_hispanic_or_latino)}</strong> <span style={{ color: "#5a564d" }}>Hispanic or Latino (any race)</span></div>
          <div><strong>{fmtPct(d.pct_foreign_born)}</strong> <span style={{ color: "#5a564d" }}>Foreign-born</span></div>
        </div>
      </div>
      <div style={{ marginTop: 12, fontSize: 12, color: "#7a756b" }}>{d.source}</div>
    </section>
  );
}

function IndustryMixSection({ mix, scope }: { mix: IndustryMix; scope: string }) {
  if (!mix.top_supersectors.length) return null;
  const maxEmp = Math.max(...mix.top_supersectors.map(s => s.total_employment));
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Industry mix · who employs people in {scope}
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 720 }}>
        Murphysboro residents work across the whole Jackson County labor shed.
        These NAICS supersectors are the employer-mix the city is feeding labor
        to today — and the recruitment leverage list for sectors a new employer
        would be joining.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1.6fr 90px 110px 120px", gap: 0, padding: "10px 14px", background: "#f0ece1", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d", fontWeight: 600 }}>
          <div>Supersector</div>
          <div style={{ textAlign: "right" }}>Employment</div>
          <div style={{ textAlign: "right" }}>Avg/week</div>
          <div style={{ textAlign: "right" }}>≈Annual</div>
        </div>
        {mix.top_supersectors.map((row, i) => {
          const barPct = (row.total_employment / maxEmp) * 100;
          return (
            <div key={row.code} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
              <div style={{ display: "grid", gridTemplateColumns: "1.6fr 90px 110px 120px", gap: 0, padding: "12px 14px", fontSize: 14, alignItems: "center" }}>
                <div>
                  <div style={{ fontWeight: 600, color: "#1f1d18" }}>{row.name}</div>
                  <div style={{ fontSize: 11, color: "#7a756b", marginTop: 2 }}>
                    Private {row.private_employment.toLocaleString()} ·{" "}
                    Public {row.public_employment.toLocaleString()}
                  </div>
                </div>
                <div style={{ textAlign: "right", fontWeight: 600 }}>{row.total_employment.toLocaleString()}</div>
                <div style={{ textAlign: "right" }}>${row.avg_weekly_wage.toLocaleString()}</div>
                <div style={{ textAlign: "right", color: "#5a564d" }}>${(row.annual_pay_equivalent / 1000).toFixed(0)}k</div>
              </div>
              <div style={{ height: 3, background: "#ebe5d6" }}>
                <div style={{ height: 3, width: `${barPct}%`, background: "oklch(45% 0.16 220)" }} />
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 12, fontSize: 12, color: "#7a756b" }}>
        Quarter: <strong>{mix.as_of_quarter}</strong>. Total covered employment in {scope}: <strong>{mix.total_employment.toLocaleString()}</strong>. {mix.source}
      </div>
    </section>
  );
}

async function fetchData(): Promise<MurphysboroData | null> {
  try {
    const res = await fetch(`${API_BASE}/api/public/murphysboro`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as MurphysboroData;
  } catch {
    return null;
  }
}

type Tone = "good" | "ok" | "warn" | "bad";
const TONE_COLOR: Record<Tone, string> = {
  good: "oklch(55% 0.16 142)",
  ok:   "oklch(55% 0.16 142)",
  warn: "oklch(58% 0.15 60)",
  bad:  "oklch(55% 0.20 22)",
};

function fmtNum(n: number, dec = 0): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: dec });
}
function fmtCurr(n: number): string {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}
function ageOf(d: string): string {
  const date = new Date(d + "T00:00:00Z");
  const now = new Date();
  const days = Math.floor((now.getTime() - date.getTime()) / 86400000);
  if (days < 60) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 24) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

interface Card { key: string; label: string; value: string; sub?: string; tone: Tone; detail: string; }

function buildCards(d: MurphysboroData): Array<{ id: string; title: string; subtitle: string; cards: Card[] }> {
  const ind = d.indicators;
  const get = (k: string) => ind[k]?.value;
  const dt = (k: string) => ind[k]?.date;

  const jacksonUR = get("crb_jackson_unemployment_rate");
  const msaUR = get("crb_msa_unemployment_rate");
  const ilUR = get("il_unemployment_rate");
  const lf = get("crb_jackson_labor_force");
  const hourly = get("crb_msa_avg_hourly_earnings");
  const weekly = get("crb_msa_avg_weekly_earnings");

  const pop = get("crb_msa_population");
  const medHH = get("crb_jackson_median_hh_income");
  const pi = get("crb_jackson_personal_income");
  const gdp = get("crb_jackson_real_gdp");

  const dom = get("crb_msa_housing_days_on_market");
  const newList = get("crb_msa_housing_new_listings_mom");
  const priceInc = get("crb_msa_housing_price_inc_yoy");

  const snap = get("crb_jackson_snap_recipients");
  const poverty = get("crb_jackson_poverty_universe");
  const singleParent = get("crb_jackson_single_parent_pct");

  const arr = (...cs: Array<Card | null>): Card[] => cs.filter(Boolean) as Card[];

  return [
    {
      id: "jobs", title: "Jobs & wages",
      subtitle: "Murphysboro sits inside Jackson County and the Carbondale-Marion MSA — labor market reads off both.",
      cards: arr(
        jacksonUR !== undefined ? { key: "j_ur", label: "Unemployment · Jackson County", value: `${jacksonUR.toFixed(1)}%`, sub: `${ageOf(dt("crb_jackson_unemployment_rate")!)}`, tone: jacksonUR < 4 ? "good" : jacksonUR < 6 ? "ok" : jacksonUR < 8 ? "warn" : "bad", detail: "Jackson County unemployment rate (Murphysboro is the county seat). Source: BLS LAUS / FRED ILJAURN." } : null,
        msaUR !== undefined ? { key: "m_ur", label: "Unemployment · MSA", value: `${msaUR.toFixed(1)}%`, sub: "Carbondale-Marion MSA", tone: msaUR < 4 ? "good" : msaUR < 6 ? "ok" : msaUR < 8 ? "warn" : "bad", detail: "Carbondale-Marion, IL MSA (CBSA 16060). Source: BLS LAUS / FRED LAUMT171606000000003." } : null,
        ilUR !== undefined ? { key: "i_ur", label: "Unemployment · Illinois", value: `${ilUR.toFixed(1)}%`, sub: "state-wide reference", tone: ilUR < 4 ? "good" : ilUR < 6 ? "ok" : ilUR < 8 ? "warn" : "bad", detail: "Illinois state-wide rate for comparison. Source: BLS LAUS / FRED ILUR." } : null,
        lf !== undefined ? { key: "lf", label: "Labor Force · Jackson County", value: fmtNum(lf), sub: `${ageOf(dt("crb_jackson_labor_force")!)}`, tone: "ok", detail: "Jackson County civilian labor force. Source: BLS LAUS / FRED ILJALFN." } : null,
        hourly !== undefined ? { key: "h", label: "Avg Hourly Earnings · MSA", value: `$${hourly.toFixed(2)}`, sub: "total private", tone: "ok", detail: "Average hourly earnings (total private) in the Carbondale-Marion MSA. Source: BLS CES / FRED SMU17160600500000003SA." } : null,
        weekly !== undefined ? { key: "w", label: "Avg Weekly Earnings · MSA", value: `$${weekly.toFixed(0)}`, sub: "total private", tone: "ok", detail: "Average weekly earnings (total private) in the MSA. Source: BLS CES / FRED SMU17160600500000011SA." } : null,
      ),
    },
    {
      id: "people", title: "People & income",
      subtitle: "Population is reported at the MSA level (Census). Income data is county-level (BEA + Census SAIPE).",
      cards: arr(
        pop !== undefined ? { key: "pop", label: "MSA Population", value: fmtNum(pop), sub: ageOf(dt("crb_msa_population")!), tone: "ok", detail: "Carbondale-Marion MSA population estimate. Murphysboro itself is ~7,600 (2020 Census). Source: Census Population Estimates / FRED CRBPOP." } : null,
        medHH !== undefined ? { key: "mh", label: "Median HH Income · Jackson Co.", value: fmtCurr(medHH), sub: ageOf(dt("crb_jackson_median_hh_income")!), tone: medHH > 60000 ? "good" : medHH > 45000 ? "ok" : "warn", detail: "Median household income, Jackson County. Source: Census SAIPE / FRED MHIIL17077A052NCEN." } : null,
        pi !== undefined ? { key: "pi", label: "Personal Income · Jackson Co.", value: fmtCurr(pi * 1000), sub: ageOf(dt("crb_jackson_personal_income")!), tone: "ok", detail: "Total personal income for Jackson County residents (thousands of dollars). Source: BEA / FRED PI17077." } : null,
        gdp !== undefined ? { key: "gdp", label: "Real GDP · Jackson Co.", value: fmtCurr(gdp * 1000), sub: "all industries", tone: "ok", detail: "Real GDP, all industries, Jackson County (thousands of chained dollars). Source: BEA / FRED REALGDPALL17077." } : null,
      ),
    },
    {
      id: "housing", title: "Housing market",
      subtitle: "MSA-level Realtor.com data — covers Murphysboro listings alongside Carbondale.",
      cards: arr(
        dom !== undefined ? { key: "dom", label: "Median Days on Market", value: `${fmtNum(dom)} days`, sub: ageOf(dt("crb_msa_housing_days_on_market")!), tone: dom < 30 ? "good" : dom < 60 ? "ok" : dom < 90 ? "warn" : "bad", detail: "Median days listed before going off-market in the MSA. Source: Realtor.com via FRED MEDDAYONMAR16060." } : null,
        newList !== undefined ? { key: "nl", label: "New Listings MoM", value: `${newList > 0 ? "+" : ""}${newList.toFixed(0)}`, sub: "month-over-month", tone: "ok", detail: "MoM change in new listings in the MSA. Source: Realtor.com via FRED NEWLISCOUMM16060." } : null,
        priceInc !== undefined ? { key: "pi2", label: "Price Increases (YoY)", value: `${priceInc > 0 ? "+" : ""}${priceInc.toFixed(0)}`, sub: "year-over-year", tone: "ok", detail: "YoY change in listings where asking price was raised. Source: Realtor.com via FRED PRIINCCOUYY16060." } : null,
      ),
    },
    {
      id: "hardship", title: "Hardship signals",
      subtitle: "Jackson County household-stress indicators (smallest jurisdiction with reliable annual stats).",
      cards: arr(
        snap !== undefined ? { key: "snap", label: "SNAP Recipients · Jackson Co.", value: fmtNum(snap), sub: ageOf(dt("crb_jackson_snap_recipients")!), tone: "warn", detail: "SNAP recipients in Jackson County. Source: Census SAIPE / FRED CBR17077ILA647NCEN." } : null,
        poverty !== undefined ? { key: "pv", label: "Poverty Universe · Jackson Co.", value: fmtNum(poverty), sub: "denominator for poverty rate", tone: "warn", detail: "Persons in Jackson County for whom poverty status was determined. Source: Census SAIPE / FRED PUAAIL17077A647NCEN." } : null,
        singleParent !== undefined ? { key: "sp", label: "Single-Parent HH Share", value: `${singleParent.toFixed(1)}%`, sub: "of households with kids", tone: singleParent > 35 ? "warn" : "ok", detail: "Single-parent households as a share of households with children, Jackson County. Source: Census ACS / FRED S1101SPHOUSE017077." } : null,
      ),
    },
  ].filter(s => s.cards.length > 0);
}

function URChart({ series }: { series: Array<{ date: string; value: number }> }) {
  if (!series.length) return null;
  const values = series.map(p => p.value);
  const min = Math.max(0, Math.min(...values) - 1);
  const max = Math.max(...values) + 1;
  const range = max - min || 1;
  const pts = series.map((p, i) => {
    const x = (i / Math.max(1, series.length - 1)) * 780 + 10;
    const y = 220 - ((p.value - min) / range) * 200;
    return `${x},${y}`;
  }).join(" ");
  const lineY = (v: number) => 220 - ((v - min) / range) * 200;
  const ticks = Array.from({ length: 4 }, (_, i) => Math.round(((i + 0.5) / 4) * (series.length - 1)));
  return (
    <svg viewBox="0 0 800 260" preserveAspectRatio="none" style={{ width: "100%", height: 260 }}>
      <line x1="0" y1={lineY(4)} x2="800" y2={lineY(4)} stroke="oklch(55% 0.16 142)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(4) - 5} fill="oklch(50% 0.16 142)" fontSize="11" fontFamily="ui-sans-serif">Full-employment · 4%</text>
      <line x1="0" y1={lineY(6)} x2="800" y2={lineY(6)} stroke="oklch(58% 0.15 60)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(6) - 5} fill="oklch(50% 0.15 60)" fontSize="11" fontFamily="ui-sans-serif">Watch · 6%</text>
      <polyline fill="none" stroke="oklch(45% 0.16 220)" strokeWidth="2" points={pts} />
      {ticks.map(idx => {
        const p = series[idx]; if (!p) return null;
        const x = (idx / Math.max(1, series.length - 1)) * 780 + 10;
        const dt = new Date(p.date).toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
        return <g key={idx}><line x1={x} y1="220" x2={x} y2="226" stroke="#8a857c" strokeWidth="0.5" /><text x={x} y="245" fill="#5a564d" fontSize="11" fontFamily="ui-sans-serif" textAnchor="middle">{dt}</text></g>;
      })}
    </svg>
  );
}

function fmtMoney(n: number) {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}

function BusinessLeadsTwo({ city, county }: { city: BusinessOps; county: BusinessOps }) {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Business lead opportunities · federal contracts
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 760 }}>
        Federal contract dollars flowing into Murphysboro specifically (top) and
        Jackson County broadly (bottom). Use these to pitch local employers on
        sectors where federal demand is already proven in the region, and to find
        primes for HUBZone-status subcontract pitches.
      </div>

      <div style={{ marginBottom: 24 }}>
        <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
          Awards with recipient city = Murphysboro · last {city.totals.lookback_months} months
        </h3>
        {city.top_awards.length === 0 ? (
          <div style={{ padding: 16, background: "white", border: "1px solid #d8d2c4", borderRadius: 6, fontSize: 13, color: "#7a756b" }}>
            No federal contract awards reported with recipient_city = MURPHYSBORO in this window.
            This is normal for a small city — look at the Jackson County view below to spot regional primes who could be invited to base subcontracting work in Murphysboro.
          </div>
        ) : (
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
            {city.top_awards.slice(0, 8).map((a, i) => (
              <div key={i} style={{ padding: "10px 14px", borderTop: i === 0 ? "none" : "1px solid #ebe5d6", fontSize: 13 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <div style={{ fontWeight: 600, color: "#1f1d18", flex: 1 }}>{a.recipient || "—"}</div>
                  <div style={{ fontWeight: 600, color: "#1f5f8f" }}>{fmtMoney(a.amount)}</div>
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", marginTop: 2 }}>{a.agency}</div>
                {a.description && <div style={{ fontSize: 12, color: "#7a756b", marginTop: 4 }}>{a.description}</div>}
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
        <div>
          <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
            Top NAICS · Jackson County (last {county.totals.lookback_months}mo)
          </h3>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
            {county.top_naics.slice(0, 8).map((n, i) => (
              <div key={n.code} style={{ display: "flex", justifyContent: "space-between", padding: "10px 14px", borderTop: i === 0 ? "none" : "1px solid #ebe5d6", fontSize: 14 }}>
                <div>
                  <div style={{ fontWeight: 600 }}>{n.name}</div>
                  <div style={{ fontSize: 11, color: "#7a756b" }}>NAICS {n.code}</div>
                </div>
                <div style={{ fontWeight: 600, color: "#1f5f8f" }}>{fmtMoney(n.amount)}</div>
              </div>
            ))}
          </div>
        </div>
        <div>
          <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
            Largest awards · Jackson Co. (any city)
          </h3>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
            {county.top_awards.slice(0, 8).map((a, i) => (
              <div key={i} style={{ padding: "10px 14px", borderTop: i === 0 ? "none" : "1px solid #ebe5d6", fontSize: 13 }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <div style={{ fontWeight: 600, color: "#1f1d18", flex: 1 }}>{a.recipient || "—"}</div>
                  <div style={{ fontWeight: 600, color: "#1f5f8f" }}>{fmtMoney(a.amount)}</div>
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", marginTop: 2 }}>{a.agency}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ marginTop: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33" }}>
        <strong>Where to go for active solicitations:</strong>
        <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
          <li><a href={city.sam_gov_search_link} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SAM.gov active opportunities · Illinois filter →</a></li>
          <li><a href="https://www.usaspending.gov/state/Illinois" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USAspending · Illinois state deep view</a></li>
          <li>Murphysboro qualifies for HUBZone preference in parts of the city — local primes can register at <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/hubzone-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA HUBZone</a> for set-aside contract eligibility.</li>
        </ul>
      </div>
    </section>
  );
}

export default async function MurphysboroPage() {
  const data = await fetchData();
  if (!data) {
    return (
      <html lang="en"><body style={{ fontFamily: "system-ui", padding: 40, color: "#5a564d" }}>
        Sorry — the Murphysboro data feed isn&apos;t responding right now. Try again in a minute.
      </body></html>
    );
  }
  const sections = buildCards(data);
  const ur = data.indicators["crb_jackson_unemployment_rate"]?.value;
  const tone: Tone = ur == null ? "ok" : ur < 4 ? "good" : ur < 6 ? "ok" : ur < 8 ? "warn" : "bad";
  const headline =
    ur == null ? "Murphysboro Snapshot" :
    ur < 4 ? "Strong local labor market" :
    ur < 6 ? "Healthy local labor market" :
    ur < 8 ? "Softening local labor market" :
    "Stressed local labor market";

  return (
    <html lang="en">
      <head>
        <title>Murphysboro, IL · Economic Snapshot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
        <style>{`
          :root { color-scheme: light; }
          * { box-sizing: border-box; }
          html, body { margin: 0; padding: 0; background: #f7f5f1; color: #1f1d18; font-family: "IBM Plex Sans", system-ui, sans-serif; line-height: 1.5; }
          a { color: #1f5f8f; }
          .container { max-width: 1000px; margin: 0 auto; padding: 32px 20px 64px; }
        `}</style>
      </head>
      <body>
        <div className="container">
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/logo-icon.svg" alt="Packet Void Labs" width={28} height={28} />
            <div style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.08em", color: "#8a857c" }}>
              Murphysboro, IL · Economic Snapshot
            </div>
          </div>
          <h1 style={{ fontSize: 46, fontWeight: 600, lineHeight: 1.05, margin: "8px 0 8px 0", color: TONE_COLOR[tone] }}>
            {headline}
          </h1>
          <div style={{ fontSize: 17, color: "#3d3a33", maxWidth: 720 }}>
            {ur != null
              ? <>Jackson County unemployment at <strong>{ur.toFixed(1)}%</strong>. Murphysboro is the Jackson County seat, 8 mi W of Carbondale, in the Carbondale-Marion MSA.</>
              : <>Jackson County seat. Carbondale-Marion MSA.</>}
          </div>
          <div style={{ fontSize: 12, color: "#8a857c", marginTop: 8 }}>
            Updated {data.ts.slice(0, 16).replace("T", " ")} UTC. County / MSA / state series via BLS LAUS, BEA, Census, Realtor.com (FRED). Federal awards via USAspending.gov.
          </div>

          {sections.map(section => (
            <section key={section.id} style={{ marginTop: 40 }}>
              <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
              <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0" }}>{section.title}</h2>
              <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16 }}>{section.subtitle}</div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 16 }}>
                {section.cards.map(c => (
                  <div key={c.key} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: `6px solid ${TONE_COLOR[c.tone]}`, borderRadius: 6, padding: 16 }}>
                    <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 8 }}>{c.label}</div>
                    <div style={{ fontSize: 26, fontWeight: 500, color: TONE_COLOR[c.tone], lineHeight: 1.1, marginBottom: 6 }}>{c.value}</div>
                    {c.sub && <div style={{ fontSize: 12, color: "#7a756b", marginBottom: 10 }}>{c.sub}</div>}
                    <details style={{ fontSize: 12, color: "#7a756b" }}>
                      <summary style={{ cursor: "pointer", userSelect: "none" }}>Source &amp; details</summary>
                      <div style={{ marginTop: 6 }}>{c.detail}</div>
                    </details>
                  </div>
                ))}
              </div>
            </section>
          ))}

          {data.unemployment_series.length > 0 && (
            <section style={{ marginTop: 40 }}>
              <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
              <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>Jackson County unemployment · last 5 years</h2>
              <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16 }}>
                Murphysboro tracks the Jackson County series. Below 4% is full employment; above 6% warrants attention.
              </div>
              <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
                <URChart series={data.unemployment_series} />
              </div>
            </section>
          )}

          {data.health_score && <HealthScoreSection health={data.health_score} cityShortName="Murphysboro" />}

          {data.city_demographics && <DemographicsSection d={data.city_demographics} cityShortName="Murphysboro" trend={data.demographics_trend} />}

          {data.industry_mix && <IndustryMixSection mix={data.industry_mix} scope="Jackson County" />}

          <BusinessLeadsTwo city={data.business_opportunities_city} county={data.business_opportunities_county} />

          <div style={{ marginTop: 40, padding: 20, background: "#f0ece1", borderRadius: 6, fontSize: 14, color: "#3d3a33" }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Related views
            </div>
            <div style={{ marginBottom: 6 }}>
              <a href="/carbondale" style={{ fontWeight: 600 }}>Carbondale, IL →</a>{" "}
              <span style={{ color: "#5a564d" }}>— same Jackson County / MSA substrate, framed for city BD work.</span>
            </div>
            <div style={{ marginBottom: 6 }}>
              <a href="/mantracon" style={{ fontWeight: 600 }}>Man-Tra-Con · SIWIB · LWA-25 →</a>{" "}
              <span style={{ color: "#5a564d" }}>— 5-county workforce-board view (Franklin, Jackson, Jefferson, Perry, Williamson) with training-pipeline alignment.</span>
            </div>
            <div>
              <a href="/market" style={{ fontWeight: 600 }}>US Market Health →</a>{" "}
              <span style={{ color: "#5a564d" }}>— national macro / recession watch backdrop.</span>
            </div>
          </div>

          <div style={{ marginTop: 24, fontSize: 12, color: "#8a857c", lineHeight: 1.6 }}>
            <strong>Sources:</strong> US Bureau of Labor Statistics (LAUS, CES),
            US Bureau of Economic Analysis (Regional Economic Accounts),
            US Census Bureau (Population Estimates, SAIPE, ACS), Realtor.com,
            USAspending.gov, SAM.gov. Aggregated via the St. Louis Fed (FRED).
            <br /><br />
            <strong>Coverage caveat:</strong> Most series are reported at Jackson
            County or Carbondale-Marion MSA scale rather than at the Murphysboro
            municipal level — the smallest jurisdiction with reliable BLS / BEA /
            Census coverage in this region is the county. Sub-county Census Place
            data (ACS) for Murphysboro CDP will be added in a future iteration.
          </div>
        </div>
      </body>
    </html>
  );
}
