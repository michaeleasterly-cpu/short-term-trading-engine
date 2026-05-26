/**
 * Public Carbondale, IL economic-development page.
 *
 * Tier 1: FRED county / MSA series for Jackson County, Carbondale-Marion
 * MSA (CBSA 16060), and Illinois state context. No auth.
 *
 * Tier 2-6 (Census ACS, Building Permits Survey, USAspending, IL state
 * scrapers, SIU enrollment, Zillow/Amtrak) land in subsequent commits
 * behind the same /api/public/carbondale endpoint.
 */
export const dynamic = "force-dynamic";
export const revalidate = 0;

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "https://console-api-production-4576.up.railway.app";

interface CarbondaleData {
  ts: string;
  indicators: Record<string, { value: number; date: string }>;
  unemployment_series: Array<{ date: string; value: number }>;
  labor_force_series: Array<{ date: string; value: number }>;
}

async function fetchCarbondale(): Promise<CarbondaleData | null> {
  try {
    const res = await fetch(`${API_BASE}/api/public/carbondale`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as CarbondaleData;
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

interface Card {
  key: string;
  label: string;
  value: string;
  sub?: string;
  tone: Tone;
  detail: string;
}

function fmtNum(n: number, dec = 0): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: dec });
}

function fmtCurr(n: number): string {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(0)}M`;
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

function buildSections(d: CarbondaleData): Array<{ id: string; title: string; subtitle: string; cards: Card[] }> {
  const ind = d.indicators;
  const get = (k: string) => ind[k]?.value;
  const getDate = (k: string) => ind[k]?.date;

  const cards = (...arr: Array<Card | null>): Card[] => arr.filter(Boolean) as Card[];

  // ─── Jobs (county + MSA) ────────────────────────────────────
  const jacksonUR = get("crb_jackson_unemployment_rate");
  const msaUR = get("crb_msa_unemployment_rate");
  const ilUR = get("il_unemployment_rate");
  const lf = get("crb_msa_labor_force");
  const psjobs = get("crb_msa_private_service_jobs");
  const hourly = get("crb_msa_avg_hourly_earnings");
  const weekly = get("crb_msa_avg_weekly_earnings");

  const jobsCards: Array<Card | null> = [
    jacksonUR !== undefined ? {
      key: "j_ur", label: "Unemployment (Jackson Co.)", value: `${jacksonUR.toFixed(1)}%`,
      sub: `as of ${ageOf(getDate("crb_jackson_unemployment_rate")!)}`,
      tone: jacksonUR < 4 ? "good" : jacksonUR < 6 ? "ok" : jacksonUR < 8 ? "warn" : "bad",
      detail: "Jackson County, IL unemployment rate. Source: US Bureau of Labor Statistics, Local Area Unemployment Statistics (LAUS); FRED series ILJAURN. Monthly since 1990.",
    } : null,
    msaUR !== undefined ? {
      key: "m_ur", label: "Unemployment (Carbondale-Marion MSA)", value: `${msaUR.toFixed(1)}%`,
      sub: `MSA-wide; ${ageOf(getDate("crb_msa_unemployment_rate")!)}`,
      tone: msaUR < 4 ? "good" : msaUR < 6 ? "ok" : msaUR < 8 ? "warn" : "bad",
      detail: "Carbondale-Marion, IL MSA (CBSA 16060: Jackson + Williamson counties). Source: BLS LAUS; FRED series LAUMT171606000000003.",
    } : null,
    ilUR !== undefined ? {
      key: "i_ur", label: "Unemployment (Illinois)", value: `${ilUR.toFixed(1)}%`,
      sub: "state-wide reference",
      tone: ilUR < 4 ? "good" : ilUR < 6 ? "ok" : ilUR < 8 ? "warn" : "bad",
      detail: "Illinois state unemployment rate for context vs Jackson County / Carbondale MSA. Source: BLS LAUS; FRED series ILUR.",
    } : null,
    lf !== undefined ? {
      key: "lf", label: "MSA Labor Force", value: fmtNum(lf),
      sub: `as of ${ageOf(getDate("crb_msa_labor_force")!)}`,
      tone: "ok",
      detail: "Total civilian labor force in the Carbondale-Marion MSA. Source: BLS LAUS; FRED series LAUMT171606000000006.",
    } : null,
    psjobs !== undefined ? {
      key: "psjobs", label: "Private Service Jobs (MSA)", value: fmtNum(psjobs * 1000),
      sub: "all-employees, seasonally adjusted",
      tone: "ok",
      detail: "All employees in private service-providing industries in the Carbondale-Marion MSA. Source: BLS Current Employment Statistics (CES); FRED series SMU17160600800000001SA. Includes retail, healthcare, hospitality, finance, professional services.",
    } : null,
    hourly !== undefined ? {
      key: "hourly", label: "Avg Hourly Earnings (MSA)", value: `$${hourly.toFixed(2)}`,
      sub: `total private; ${ageOf(getDate("crb_msa_avg_hourly_earnings")!)}`,
      tone: "ok",
      detail: "Average hourly earnings of all employees in the total-private sector of the Carbondale-Marion MSA. Source: BLS CES; FRED series SMU17160600500000003SA. Note: this series was discontinued after 2022.",
    } : null,
    weekly !== undefined ? {
      key: "weekly", label: "Avg Weekly Earnings (MSA)", value: `$${weekly.toFixed(0)}`,
      sub: "total private",
      tone: "ok",
      detail: "Average weekly earnings (total private) in the Carbondale-Marion MSA. Source: BLS CES; FRED series SMU17160600500000011SA.",
    } : null,
  ];

  // ─── People + income ────────────────────────────────────────
  const pop = get("crb_msa_population");
  const medHH = get("crb_jackson_median_hh_income");
  const pi = get("crb_jackson_personal_income");
  const gdp = get("crb_jackson_real_gdp");

  const peopleCards: Array<Card | null> = [
    pop !== undefined ? {
      key: "pop", label: "MSA Population", value: fmtNum(pop),
      sub: `as of ${ageOf(getDate("crb_msa_population")!)}`,
      tone: "ok",
      detail: "Resident population estimate for the Carbondale-Marion MSA. Source: US Census Bureau Population Estimates; FRED series CRBPOP. Annual.",
    } : null,
    medHH !== undefined ? {
      key: "medhh", label: "Median Household Income (Jackson Co.)", value: fmtCurr(medHH),
      sub: `as of ${ageOf(getDate("crb_jackson_median_hh_income")!)}`,
      tone: medHH > 60000 ? "good" : medHH > 45000 ? "ok" : "warn",
      detail: "Median household income estimate for Jackson County, IL. Source: Census Bureau Small Area Income and Poverty Estimates (SAIPE); FRED series MHIIL17077A052NCEN. Annual, ~12-month lag.",
    } : null,
    pi !== undefined ? {
      key: "pi", label: "Personal Income (Jackson Co.)", value: fmtCurr(pi * 1000),
      sub: `total; ${ageOf(getDate("crb_jackson_personal_income")!)}`,
      tone: "ok",
      detail: "Total personal income (all sources) for Jackson County residents. Source: US Bureau of Economic Analysis (BEA); FRED series PI17077. Annual, expressed in thousands of dollars.",
    } : null,
    gdp !== undefined ? {
      key: "gdp", label: "Real GDP (Jackson Co., all ind.)", value: fmtCurr(gdp * 1000),
      sub: `as of ${ageOf(getDate("crb_jackson_real_gdp")!)}`,
      tone: "ok",
      detail: "Real Gross Domestic Product, all industries, Jackson County. Source: BEA Regional Economic Accounts; FRED series REALGDPALL17077. Annual, chained dollars.",
    } : null,
  ];

  // ─── Housing ────────────────────────────────────────────────
  const dom = get("crb_msa_housing_days_on_market");
  const newList = get("crb_msa_housing_new_listings_mom");
  const priceInc = get("crb_msa_housing_price_inc_yoy");

  const housingCards: Array<Card | null> = [
    dom !== undefined ? {
      key: "dom", label: "Median Days on Market", value: `${fmtNum(dom)} days`,
      sub: `MSA; ${ageOf(getDate("crb_msa_housing_days_on_market")!)}`,
      tone: dom < 30 ? "good" : dom < 60 ? "ok" : dom < 90 ? "warn" : "bad",
      detail: "Median days a home is listed before going off-market in the Carbondale-Marion MSA. Source: Realtor.com via FRED; series MEDDAYONMAR16060.",
    } : null,
    newList !== undefined ? {
      key: "newlist", label: "New Listings MoM Change", value: `${newList > 0 ? "+" : ""}${newList.toFixed(0)}`,
      sub: "month-over-month",
      tone: "ok",
      detail: "Month-over-month change in new home listings in the Carbondale-Marion MSA. Source: Realtor.com via FRED; series NEWLISCOUMM16060.",
    } : null,
    priceInc !== undefined ? {
      key: "priceinc", label: "Listings with Price Increases (YoY)", value: `${priceInc > 0 ? "+" : ""}${priceInc.toFixed(0)}`,
      sub: "year-over-year",
      tone: "ok",
      detail: "Year-over-year change in count of listings where the asking price was increased. Source: Realtor.com via FRED; series PRIINCCOUYY16060.",
    } : null,
  ];

  // ─── Hardship / safety net ──────────────────────────────────
  const snap = get("crb_jackson_snap_recipients");
  const poverty = get("crb_jackson_poverty_universe");
  const singleParent = get("crb_jackson_single_parent_pct");

  const hardshipCards: Array<Card | null> = [
    snap !== undefined ? {
      key: "snap", label: "SNAP Recipients (Jackson Co.)", value: fmtNum(snap),
      sub: `as of ${ageOf(getDate("crb_jackson_snap_recipients")!)}`,
      tone: "warn",
      detail: "Number of SNAP (food stamp) benefit recipients in Jackson County, IL. Source: Census SAIPE; FRED series CBR17077ILA647NCEN. Annual. A direct measure of food-insecurity-level economic hardship.",
    } : null,
    poverty !== undefined ? {
      key: "pov", label: "Poverty Universe (Jackson Co.)", value: fmtNum(poverty),
      sub: `all ages; ${ageOf(getDate("crb_jackson_poverty_universe")!)}`,
      tone: "warn",
      detail: "Number of persons in Jackson County for whom poverty status was determined (denominator for poverty-rate calculations). Source: Census SAIPE; FRED series PUAAIL17077A647NCEN. Annual.",
    } : null,
    singleParent !== undefined ? {
      key: "sp", label: "Single-Parent Household Share", value: `${singleParent.toFixed(1)}%`,
      sub: `of households with children; ${ageOf(getDate("crb_jackson_single_parent_pct")!)}`,
      tone: singleParent > 35 ? "warn" : "ok",
      detail: "Single-parent households as a percentage of households with children, Jackson County. Source: Census ACS via FRED; series S1101SPHOUSE017077. Annual.",
    } : null,
  ];

  return [
    { id: "jobs", title: "Jobs & wages", subtitle: "How is the local labor market doing?", cards: cards(...jobsCards) },
    { id: "people", title: "People & income", subtitle: "Who lives here, and how is the economy supporting them?", cards: cards(...peopleCards) },
    { id: "housing", title: "Housing market", subtitle: "Carbondale-Marion MSA housing-market conditions.", cards: cards(...housingCards) },
    { id: "hardship", title: "Hardship signals", subtitle: "Indicators of economic stress at the household level.", cards: cards(...hardshipCards) },
  ].filter(s => s.cards.length > 0);
}

function topHeadline(d: CarbondaleData): { headline: string; subhead: string; tone: Tone } {
  const ur = d.indicators["crb_jackson_unemployment_rate"]?.value;
  const lf = d.indicators["crb_msa_labor_force"]?.value;
  const lfSeries = d.labor_force_series;

  if (ur === undefined) {
    return { headline: "Carbondale Snapshot", subhead: "Local economic indicators for Jackson County + the Carbondale-Marion MSA.", tone: "ok" };
  }
  // Simple headline based on unemployment rate
  if (ur < 4) {
    return { headline: "Strong labor market", subhead: `Jackson County unemployment is ${ur.toFixed(1)}% — historically low. ${lfSeries.length > 12 ? `Labor force at ${fmtNum(lf || 0)}.` : ""}`, tone: "good" };
  }
  if (ur < 6) {
    return { headline: "Healthy labor market", subhead: `Jackson County unemployment at ${ur.toFixed(1)}% — within normal range.`, tone: "ok" };
  }
  if (ur < 8) {
    return { headline: "Softening labor market", subhead: `Jackson County unemployment at ${ur.toFixed(1)}% — above the national norm. Worth watching.`, tone: "warn" };
  }
  return { headline: "Stressed labor market", subhead: `Jackson County unemployment at ${ur.toFixed(1)}% — recession-level.`, tone: "bad" };
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
  const TICK_COUNT = 4;
  const tickIdxs = Array.from({ length: TICK_COUNT }, (_, i) =>
    Math.round(((i + 0.5) / TICK_COUNT) * (series.length - 1))
  );
  const fmtMonthYear = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
  };
  return (
    <svg viewBox="0 0 800 260" preserveAspectRatio="none" style={{ width: "100%", height: 260 }}>
      <line x1="0" y1={lineY(4)} x2="800" y2={lineY(4)} stroke="oklch(55% 0.16 142)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(4) - 5} fill="oklch(50% 0.16 142)" fontSize="11" fontFamily="ui-sans-serif">Full-employment line · 4%</text>
      <line x1="0" y1={lineY(6)} x2="800" y2={lineY(6)} stroke="oklch(58% 0.15 60)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(6) - 5} fill="oklch(50% 0.15 60)" fontSize="11" fontFamily="ui-sans-serif">Watch line · 6%</text>
      <polyline fill="none" stroke="oklch(45% 0.16 220)" strokeWidth="2" points={pts} />
      {tickIdxs.map(idx => {
        const p = series[idx];
        if (!p) return null;
        const x = (idx / Math.max(1, series.length - 1)) * 780 + 10;
        return (
          <g key={idx}>
            <line x1={x} y1="220" x2={x} y2="226" stroke="#8a857c" strokeWidth="0.5" />
            <text x={x} y="245" fill="#5a564d" fontSize="11" fontFamily="ui-sans-serif" textAnchor="middle">
              {fmtMonthYear(p.date)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default async function CarbondalePage() {
  const data = await fetchCarbondale();
  const sections = data ? buildSections(data) : [];
  const top = data ? topHeadline(data) : null;

  return (
    <html lang="en">
      <head>
        <title>Carbondale, IL · Economic Snapshot</title>
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
          {!data && (
            <div style={{ padding: 40, textAlign: "center", color: "#8a857c" }}>
              Sorry — the Carbondale data feed isn&apos;t responding right now. Try again in a minute.
            </div>
          )}

          {data && top && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src="/logo-icon.svg" alt="Packet Void Labs" width={28} height={28} />
                <div style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.08em", color: "#8a857c" }}>
                  Carbondale, IL · Economic Snapshot
                </div>
              </div>
              <h1 style={{ fontSize: 48, fontWeight: 600, lineHeight: 1.05, margin: "8px 0 8px 0", color: TONE_COLOR[top.tone] }}>
                {top.headline}
              </h1>
              <div style={{ fontSize: 18, color: "#3d3a33", maxWidth: 720 }}>{top.subhead}</div>
              <div style={{ fontSize: 12, color: "#8a857c", marginTop: 8 }}>
                Updated {data.ts.slice(0, 16).replace("T", " ")} UTC. Jackson County, IL + Carbondale-Marion MSA (CBSA 16060) + Illinois state context.
              </div>

              {sections.map(section => (
                <section key={section.id} style={{ marginTop: 40 }}>
                  <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
                  <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
                    {section.title}
                  </h2>
                  <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16 }}>{section.subtitle}</div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 16 }}>
                    {section.cards.map(c => (
                      <div key={c.key} style={{
                        background: "white",
                        border: "1px solid #d8d2c4",
                        borderLeft: `6px solid ${TONE_COLOR[c.tone]}`,
                        borderRadius: 6,
                        padding: 16,
                      }}>
                        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 8 }}>
                          {c.label}
                        </div>
                        <div style={{ fontSize: 26, fontWeight: 500, color: TONE_COLOR[c.tone], lineHeight: 1.1, marginBottom: 6 }}>
                          {c.value}
                        </div>
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
                <>
                  <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", margin: "40px 0 24px" }} />
                  <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8, color: "#1f1d18" }}>
                    Carbondale-Marion MSA unemployment — last 5 years
                  </h2>
                  <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16 }}>
                    The MSA-wide unemployment rate. Below 4% (green dotted) is full-employment territory; above 6% (yellow dotted) warrants attention.
                  </div>
                  <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
                    <URChart series={data.unemployment_series} />
                  </div>
                </>
              )}

              <div style={{ marginTop: 40, fontSize: 12, color: "#8a857c", lineHeight: 1.6 }}>
                <strong>Where the data comes from:</strong> all indicators pulled from FRED
                (Federal Reserve Economic Data, St. Louis Fed) — which aggregates BLS Local
                Area Unemployment Statistics, BLS Current Employment Statistics, Census Bureau
                Population Estimates, Census Small Area Income and Poverty Estimates, BEA Regional
                Economic Accounts, Census American Community Survey, and Realtor.com housing
                data. Monthly series refresh ~1-2 months after the reference period; annual
                series lag 6-18 months. Updated nightly via our data pipeline.
              </div>

              <div style={{ marginTop: 24, fontSize: 12, color: "#5a564d", lineHeight: 1.7 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>
                  Coverage notes
                </div>
                <ul style={{ margin: "0 0 10px 18px", padding: 0 }}>
                  <li><strong>Jackson County, IL</strong> (FIPS 17077) — where the City of Carbondale sits.</li>
                  <li><strong>Carbondale-Marion, IL MSA</strong> (CBSA 16060) — Jackson + Williamson counties. The MSA is the federal statistical unit for sub-state labor and housing data.</li>
                  <li><strong>Illinois state context</strong> — included for comparison. Note Illinois state averages skew toward Chicago and may diverge significantly from Southern Illinois conditions.</li>
                </ul>
                <p style={{ margin: 0 }}>
                  This is a public snapshot of widely-used federal economic gauges for the
                  Carbondale area. Tier 2-6 enhancements (Census ACS demographics, building
                  permits, federal contract awards, IL sales-tax revenue, SIU enrollment,
                  Zillow housing) ship in subsequent commits.
                </p>
              </div>
            </>
          )}
        </div>
      </body>
    </html>
  );
}
