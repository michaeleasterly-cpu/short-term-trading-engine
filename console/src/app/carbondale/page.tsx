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
import { DashboardHead, Topbar, DashboardFooter, DEFAULT_FOOTER_COLUMNS } from "@/components/dashboard-chrome";
import { getCarbondaleData, type CarbondaleData as RegionalCarbondaleData } from "@/lib/regional-data";

// Self-fetching daily-cached data layer (no console-api). A Vercel cron may
// force a fresh pull; otherwise the prerender refreshes every 24h.
export const revalidate = 86400;

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

interface LaborTruthGeo {
  name: string; fips: string;
  pop_16plus: number; in_labor_force: number; employed: number; unemployed: number; not_in_labor_force: number;
  lfpr: number; ep_ratio: number; not_lf_pct: number; ue_rate: number | null;
  gap_lfpr_vs_state: number; gap_ep_vs_state: number;
}
interface LaborTruth {
  geos: LaborTruthGeo[];
  aggregate: LaborTruthGeo | null;
  benchmarks: { il_state_lfpr: number; il_state_ep: number; il_state_not_lf_pct: number; us_national_lfpr: number; us_national_ep: number };
  year: number; source: string;
}

function LaborTruthCitySection({ lt, cityShortName }: { lt: LaborTruth; cityShortName: string }) {
  if (!lt.geos.length) return null;
  const g = lt.geos[0];
  const stateLFPR = lt.benchmarks.il_state_lfpr;
  const stateEP = lt.benchmarks.il_state_ep;
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        The true labor picture · beyond the headline unemployment rate
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 720, lineHeight: 1.55 }}>
        The headline unemployment rate only counts people <em>actively looking for work</em>.
        It misses every working-age person who has stopped looking, gone on disability, or
        otherwise dropped out of the labor force. {cityShortName}&apos;s real picture from ACS {lt.year}:
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14 }}>
        {[
          { label: "Headline UE rate", value: g.ue_rate != null ? `${g.ue_rate}%` : "—", sub: "what politicians cite", flag: false },
          { label: "Labor force participation", value: `${g.lfpr}%`, sub: `IL state: ${stateLFPR}% · gap ${g.gap_lfpr_vs_state > 0 ? "+" : ""}${g.gap_lfpr_vs_state}pp`, flag: g.gap_lfpr_vs_state < -3 },
          { label: "Employment-to-population", value: `${g.ep_ratio}%`, sub: `IL state: ${stateEP}% · gap ${g.gap_ep_vs_state > 0 ? "+" : ""}${g.gap_ep_vs_state}pp`, flag: g.gap_ep_vs_state < -3 },
          { label: "Not in labor force", value: g.not_in_labor_force.toLocaleString(), sub: `${g.not_lf_pct}% of working-age — the invisible population`, flag: true },
        ].map((s, i) => (
          <div key={i} style={{
            background: "white",
            border: `1px solid ${s.flag ? "oklch(45% 0.20 22)33" : "#d8d2c4"}`,
            borderLeft: `6px solid ${s.flag ? "oklch(45% 0.20 22)" : "#1f1d18"}`,
            borderRadius: 6, padding: 14,
          }}>
            <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 6 }}>{s.label}</div>
            <div style={{ fontSize: 26, fontWeight: 600, color: s.flag ? "oklch(45% 0.20 22)" : "#1f1d18", lineHeight: 1.05 }}>{s.value}</div>
            <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>{s.sub}</div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 12, fontSize: 12, color: "#5a564d", lineHeight: 1.55, maxWidth: 720 }}>
        <strong>How to read this:</strong> The headline UE rate stays low because once someone
        stops looking, they vanish from the math. LFPR + E/P ratio capture the entire
        working-age (16+) population. The &quot;Not in LF&quot; count is the closest legitimate
        proxy for the invisible-population concern — people neither employed nor officially
        unemployed-and-looking.
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b" }}>{lt.source}</div>
    </section>
  );
}

// The page renders the regional-data-layer payload directly. The local
// section-component interfaces above (BusinessOps, IndustryMix, CityDemographics,
// DemographicsTrend, HealthScore, LaborTruth) are structurally satisfied by the
// lib's exported types.
type CarbondaleData = RegionalCarbondaleData;

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
        most heavily-weighted predictor of long-term distress in published research. Lower scores
        signal more intervention need; higher scores signal underlying resilience.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 24, alignItems: "start" }}>
        {/* Headline score */}
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
        {/* Component breakdown */}
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


// Metrics whose UP direction is GOOD (green) for community well-being.
// For the rest we show grey (neutral) — these are direction-agnostic.
const TREND_GOOD_UP = new Set(["population", "median_household_income"]);
// Metrics whose DOWN direction is good (green when negative).
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
  const fmtMoney = (v: number | null) => v == null ? "—" : `$${v.toLocaleString()}`;
  const priorYear = trend?.comparison_years?.[1];
  // Vars that are percentages — render delta as ±X.Xpp; everything else as ±X.X%
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
    { key: "median_age",              label: "Median age",                          value: d.median_age != null ? `${d.median_age.toFixed(1)} yrs` : "—", sub: d.median_age != null && d.median_age < 30 ? "very young — youth-anchor" : d.median_age != null && d.median_age > 40 ? "older skew" : "near US median" },
    { key: null,                      label: "Bachelor's degree or higher (25+)",   value: fmtPct(d.pct_bachelors_plus), sub: d.pct_bachelors_plus != null && d.pct_bachelors_plus > 40 ? "highly educated workforce" : undefined },
    { key: "median_household_income", label: "Median household income",             value: fmtMoney(d.median_household_income) },
    { key: "poverty_rate_families",   label: "Family poverty rate",                 value: fmtPct(d.poverty_rate_families) },
    { key: "acs_unemployment_rate",   label: "ACS unemployment (25+)",              value: fmtPct(d.acs_unemployment_rate), sub: "5y avg, narrower than LAUS" },
    { key: "median_home_value",       label: "Median home value",                   value: fmtMoney(d.median_home_value), sub: "owner-occupied units" },
    { key: "median_gross_rent",       label: "Median gross rent",                   value: fmtMoney(d.median_gross_rent), sub: "renter-occupied units" },
    { key: "pct_owner_occupied",      label: "% owner-occupied",                    value: fmtPct(d.pct_owner_occupied), sub: d.pct_renter_occupied != null ? `${d.pct_renter_occupied.toFixed(1)}% renter` : undefined },
    { key: "mean_commute_minutes",    label: "Mean commute time",                   value: d.mean_commute_minutes != null ? `${Math.round(d.mean_commute_minutes)} min` : "—", sub: "one-way to work" },
  ];

  // Build the 4 highest-impact headline deltas for the "Direction of travel" strip
  const headlineKeys: Array<[string, string, string]> = [
    // [key, short label, sub explainer]
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
        municipality (not the broader county). Use this to know who actually
        lives here — and to make demographic-grounded pitches when courting
        employers, housing developers, or grant-makers.
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
        Total covered employment by NAICS supersector (BLS QCEW, latest published quarter).
        This is the answer to &ldquo;what kind of city is this for jobs?&rdquo; — and the
        leverage list for which sectors to court when recruiting employers.
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

function fmtMoney(n: number): string {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}

function BusinessLeadsBlock({ b }: { b: BusinessOps }) {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Business lead opportunities · federal contracts
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 720 }}>
        Federal contract dollars flowing into Jackson County, by sector. Use these
        to (a) pitch employers in matching NAICS to consider Carbondale, (b) help
        local primes find subcontracting wedges, and (c) target SAM.gov solicitations
        where regional demand is already proven.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
        <div>
          <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
            Top NAICS · Jackson Co. (last {b.totals.lookback_months}mo)
          </h3>
          {b.top_naics.length === 0 ? (
            <div style={{ color: "#7a756b", fontSize: 13 }}>No data returned.</div>
          ) : (
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
              {b.top_naics.slice(0, 8).map((n, i) => (
                <div key={n.code} style={{
                  display: "flex", justifyContent: "space-between", padding: "10px 14px",
                  borderTop: i === 0 ? "none" : "1px solid #ebe5d6", fontSize: 14,
                }}>
                  <div>
                    <div style={{ fontWeight: 600 }}>{n.name}</div>
                    <div style={{ fontSize: 11, color: "#7a756b" }}>NAICS {n.code}</div>
                  </div>
                  <div style={{ fontWeight: 600, color: "#1f5f8f" }}>{fmtMoney(n.amount)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
            Largest awards · place-of-performance Jackson Co.
          </h3>
          {b.top_awards.length === 0 ? (
            <div style={{ color: "#7a756b", fontSize: 13 }}>No data returned.</div>
          ) : (
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
              {b.top_awards.slice(0, 8).map((a, i) => (
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
      </div>
      <div style={{ marginTop: 16, padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13 }}>
        <strong>Where to act:</strong>{" "}
        <a href={b.sam_gov_search_link} target="_blank" rel="noopener noreferrer">SAM.gov · Illinois active opportunities →</a>
        {" · "}
        <a href="https://www.usaspending.gov/state/Illinois" target="_blank" rel="noopener noreferrer">USAspending · Illinois</a>
        {" · "}
        <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/hubzone-program" target="_blank" rel="noopener noreferrer">SBA HUBZone</a>
      </div>
    </section>
  );
}

async function fetchCarbondale(): Promise<CarbondaleData | null> {
  try {
    return await getCarbondaleData();
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

function topHeadline(d: CarbondaleData): { headline: string; subhead: string; tone: Tone; score: number | null; band: string | null } {
  // Headline is purely numeric (the composite score); the band label appears as a
  // methodology identifier in the subhead, not as a verdict in the banner. No
  // editorial language at the H1 level — reader interprets the number.
  const h = d.health_score;
  if (!h || h.score == null) {
    return {
      headline: "Economic profile",
      subhead: "Local indicators for Jackson County + the Carbondale-Marion MSA.",
      tone: "ok",
      score: null,
      band: null,
    };
  }
  const worst = [...h.components]
    .filter(c => c.score != null)
    .sort((a, b) => (a.score! - b.score!))[0];
  const worstLabel = worst ? worst.label.toLowerCase() : "";

  // Tone drives color of the numeric score, not a verdict word.
  const tone: Tone =
    h.score >= 80 ? "good" :
    h.score >= 60 ? "ok" :
    h.score >= 40 ? "warn" : "bad";

  const subhead = `Band: ${h.label} (composite of 6 hardship signals — HS-dropout, poverty, unemployment, income vs state, 5y pop trend, 5y income trend).${worstLabel ? ` Weakest component: ${worstLabel}.` : ""} Methodology + per-component scores below.`;

  return {
    headline: `Community Health Score · ${h.score.toFixed(0)} / 100`,
    subhead,
    tone,
    score: h.score,
    band: h.label,
  };
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

  const renderedAt = data ? data.ts.slice(0, 16).replace("T", " ") + " UTC" : "—";

  return (
    <html lang="en">
      <head>
        <DashboardHead title="Carbondale, IL · Economic Profile" />
      </head>
      <body>
        <div className="shell">
          <Topbar brand="Carbondale, IL · Economic Profile" region="Carbondale · Jackson County · IL" renderedAt={renderedAt} />

          {!data && (
            <div style={{ padding: 40, textAlign: "center", color: "var(--ink-3)" }}>
              Sorry — the Carbondale data feed isn&apos;t responding right now. Try again in a minute.
            </div>
          )}

          {data && top && (
            <>
              <header className="hero">
                <div>
                  <div className="eyebrow">Jackson County, IL · Carbondale-Marion MSA (CBSA 16060)</div>
                  <h1 className="serif" style={{ fontFamily: '"IBM Plex Serif", Georgia, serif', fontSize: 56, fontWeight: 500, lineHeight: 1.04, margin: "18px 0 18px", letterSpacing: "-0.02em", color: "var(--ink)", textWrap: "balance" }}>
                    {top.headline}
                  </h1>
                  <p className="lead" style={{ fontSize: 17, lineHeight: 1.5, color: "var(--ink-2)", maxWidth: "58ch", margin: 0 }}>{top.subhead}</p>
                </div>
                {top.score != null && (
                  <aside className="hero-side">
                    <div className="hero-stat">
                      <div className={`n ${top.tone === "bad" ? "neg" : top.tone === "warn" ? "warn" : top.tone === "good" ? "pos" : ""}`}>
                        {top.score.toFixed(0)}
                        <span style={{ fontSize: 18, color: "var(--ink-3)" }}> / 100</span>
                      </div>
                      <div className="label">Community Health Score<br />6-signal composite</div>
                    </div>
                    {top.band && (
                      <div className="hero-stat">
                        <div className="n" style={{ fontSize: 22 }}>{top.band}</div>
                        <div className="label">Methodology band<br />0–20 · 20–40 · 40–60 · 60–80 · 80–100</div>
                      </div>
                    )}
                  </aside>
                )}
              </header>

              <div className="freshness">
                <div className="fresh-cell">
                  <div className="k">Census ACS · demographics</div>
                  <div className="v">{data.city_demographics?.year ?? "2023"} 5-year</div>
                  <div className="sub">refreshes annually · Dec</div>
                </div>
                <div className="fresh-cell">
                  <div className="k">BLS LAUS · labor market</div>
                  <div className="v">Through {data.indicators?.crb_jackson_unemployment_rate?.date ?? "—"}</div>
                  <div className="sub">refreshes monthly</div>
                </div>
                <div className="fresh-cell">
                  <div className="k">BLS QCEW · industry mix</div>
                  <div className="v">{data.industry_mix?.as_of_quarter ?? "—"}</div>
                  <div className="sub">refreshes quarterly · ~7mo lag</div>
                </div>
                <div className="fresh-cell">
                  <div className="k">USAspending · federal $</div>
                  <div className="v">{data.business_opportunities?.totals?.lookback_months ?? 24}-month rolling</div>
                  <div className="sub">refreshes continuously</div>
                </div>
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

              {data.health_score && <HealthScoreSection health={data.health_score} cityShortName="Carbondale" />}

              {data.labor_truth && <LaborTruthCitySection lt={data.labor_truth} cityShortName="Carbondale" />}

              {data.city_demographics && <DemographicsSection d={data.city_demographics} cityShortName="Carbondale" trend={data.demographics_trend} />}

              {data.industry_mix && <IndustryMixSection mix={data.industry_mix} scope="Jackson County" />}

              {data.business_opportunities && <BusinessLeadsBlock b={data.business_opportunities} />}

              {/* SIU Carbondale campus — Clery Act 2024 ASR */}
              <section style={{ marginTop: 40 }}>
                <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
                <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
                  SIU campus · Clery Act Annual Security Report
                </h2>
                <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
                  SIU publishes the Annual Security and Fire Safety Report (ASR) in compliance with the Jeanne Clery Disclosure of Campus Security Policy and Campus Crime Statistics Act + the IL Campus Security Enhancement Act + HEOA 2008 + VAWA. The ASR carries 3-year totals for federally-defined Clery offenses on the campus footprint + immediate public property + non-campus university-controlled properties.
                </div>
                <div style={{ marginBottom: 12, padding: 12, background: "oklch(98% 0.015 220)", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6 }}>
                  <div style={{ fontSize: 12, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    SIU Carbondale campus · Clery Act 2024 ASR (released Sept 2025)
                  </div>
                  <div style={{ fontSize: 11.5, color: "#5a564d", marginBottom: 8, lineHeight: 1.5 }}>
                    Different metric than the per-1,000 city rates. Clery counts ONLY specific federally-defined offenses on the campus footprint + immediate public property as raw counts (not rates per 1,000 residents). Campus population ~11k students + staff; on-campus housing capacity ~5-6k students.
                  </div>
                  <div style={{ overflowX: "auto" }}>
                    <table style={{ width: "100%", fontSize: 11.5, borderCollapse: "collapse" }}>
                      <thead>
                        <tr style={{ background: "#ebe5d6", textAlign: "left" }}>
                          <th style={{ padding: "4px 6px", borderBottom: "1px solid #d8d2c4" }}>Clery offense</th>
                          <th style={{ padding: "4px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>2022 total</th>
                          <th style={{ padding: "4px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>2023 total</th>
                          <th style={{ padding: "4px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>2024 total</th>
                          <th style={{ padding: "4px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>2024 in student housing</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[
                          {label: "Murder / non-negligent manslaughter", y22:"0", y23:"0", y24:"0", h24:"0"},
                          {label: "Sex offense: rape", y22:"2", y23:"4", y24:"9", h24:"8"},
                          {label: "Sex offense: forcible fondling", y22:"1", y23:"1", y24:"0", h24:"0"},
                          {label: "Robbery", y22:"0", y23:"0", y24:"0", h24:"0"},
                          {label: "Aggravated assault", y22:"3", y23:"6", y24:"2", h24:"2"},
                          {label: "Burglary", y22:"10", y23:"4", y24:"10", h24:"1"},
                          {label: "Motor vehicle theft", y22:"5", y23:"0", y24:"2", h24:"0"},
                          {label: "Arson", y22:"0", y23:"0", y24:"0", h24:"0"},
                        ].map((r, i) => (
                          <tr key={r.label} style={{ borderBottom: i < 7 ? "1px solid #ebe5d6" : "none" }}>
                            <td style={{ padding: "3px 6px" }}>{r.label}</td>
                            <td style={{ padding: "3px 6px", textAlign: "right", color: "#5a564d" }}>{r.y22}</td>
                            <td style={{ padding: "3px 6px", textAlign: "right", color: "#5a564d" }}>{r.y23}</td>
                            <td style={{ padding: "3px 6px", textAlign: "right", fontWeight: 600 }}>{r.y24}</td>
                            <td style={{ padding: "3px 6px", textAlign: "right", color: "#5a564d" }}>{r.h24}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div style={{ fontSize: 11, color: "#3d3a33", marginTop: 8, lineHeight: 1.5 }}>
                    <strong>SIU campus three-year movement:</strong>
                    <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                      <li>Robbery, murder, arson: 0 in each of the three years.</li>
                      <li>Burglary: 10 → 4 → 10.</li>
                      <li>Aggravated assault: 3 → 6 → 2.</li>
                      <li>Motor vehicle theft: 5 → 0 → 2.</li>
                    </ul>
                  </div>
                  <div style={{ fontSize: 10.5, color: "#7a756b", marginTop: 6, lineHeight: 1.5 }}>
                    Source: <a href="https://safe.siu.edu/_common/documents/2024_asr.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SIU Department of Public Safety · 2024 Annual Security and Fire Safety Report</a> (released September 2025, covers calendar years 2022-2024). On-Campus Total includes On-Campus Student Housing as a subset.
                  </div>
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", lineHeight: 1.55 }}>
                  Companion: <a href="/charleston" style={{ color: "#1f5f8f", fontWeight: 600 }}>Charleston, IL → EIU Annual Safety + Security Report</a> for the parallel-college-town Clery comparison.
                </div>
              </section>

              <div className="sources" style={{ marginTop: 40, lineHeight: 1.6 }}>
                <b>Sources:</b> FRED (Federal Reserve Economic Data, St. Louis Fed),
                aggregating BLS LAUS, BLS CES, Census Population Estimates, Census SAIPE,
                BEA Regional Economic Accounts, Census ACS, and Realtor.com housing data.
                Monthly series refresh ~1–2 months after the reference period; annual series
                lag 6–18 months. Updated nightly.{" "}
                <b>Coverage:</b> Jackson County, IL (FIPS 17077) · Carbondale-Marion MSA
                (CBSA 16060 = Jackson + Williamson) · Illinois state context. Illinois
                state averages skew toward Chicago and may diverge from Southern Illinois.
              </div>

              <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
            </>
          )}
        </div>
      </body>
    </html>
  );
}
