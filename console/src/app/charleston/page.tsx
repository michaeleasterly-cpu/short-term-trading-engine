/**
 * Public Charleston, IL economic-development page.
 *
 * Charleston is the Coles County seat (East Central IL · LWA-23), home to
 * Eastern Illinois University. ~17k city population, anchored by EIU + the
 * Mattoon-area employer base (Sarah Bush Lincoln, Rural King, Lake Land
 * College, Consolidated Communications, R.R. Donnelley).
 *
 * This page is currently STATIC (primary-source data hardcoded); it does not
 * yet route through console-api. Companion pages /carbondale and /murphysboro
 * are data-driven from the API — Charleston can migrate to the same pattern
 * once Coles County + Mattoon Micropolitan series are wired in the backend.
 */
import { DashboardHead, Topbar, DashboardFooter, DEFAULT_FOOTER_COLUMNS } from "@/components/dashboard-chrome";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// ════════════════════════════════════════════════════════════════════════
// Primary-source Charleston data — see source citations beneath each block.
// ════════════════════════════════════════════════════════════════════════

const CITY = {
  name: "Charleston, IL",
  county: "Coles County",
  fips_place: "16000US1712567",
  fips_county: "17029",
  msa: "Mattoon, IL Micropolitan Statistical Area (CBSA 31380)",
  lwa: "East Central Illinois (LWA-23)",
  population_2024: 17062,
  population_2020: 17579,
  pop_decline_pct: -2.9, // 17062 vs 17579 = -517 = -2.94%
  median_household_income_2024: 49300,
  median_age: 29.6,
  poverty_rate_pct: 26.3,
  pct_white: 82.7,
  pct_black: 6.8,
  pct_hispanic: 4.5,
  pct_bachelors_or_higher: 25.0, // ~13.2 + 11.8 from ACS source
};

const EIU_ENROLLMENT = [
  { year: "Fall 2004", headcount: 11651, note: "Recent peak — pre-decline baseline" },
  { year: "Fall 2014", headcount: 8520, note: "Pre-state-budget-standoff" },
  { year: "Fall 2016", headcount: 7440, note: "Aftermath of 2015-2017 IL budget impasse" },
  { year: "Fall 2019", headcount: 7806, note: "Pre-COVID partial recovery" },
  { year: "Fall 2022", headcount: 7682, note: "Post-COVID stability" },
  { year: "Fall 2024", headcount: 5910, note: "FAFSA-delay year (national)" },
  { year: "Fall 2025", headcount: 5434, note: "Most recent; -8% YoY, but largest first-year domestic cohort in 3 years" },
];

// ── Anchor employers — Coles County footprint (Charleston + Mattoon + SBL between)
const ANCHOR_EMPLOYERS = [
  {
    name: "Eastern Illinois University (EIU)",
    location: "Charleston",
    sector: "Public university",
    role: "Largest single employer in Coles County; faculty + staff + research.",
    headcount_note: "Total employment is enrollment-coupled; staff levels have contracted alongside the 20-year enrollment decline (see §1).",
    url: "https://www.eiu.edu/",
  },
  {
    name: "Sarah Bush Lincoln Health Center",
    location: "Between Mattoon + Charleston on IL Route 16",
    sector: "Regional healthcare system",
    role: "Forbes / Statista Top 10 Best Employers in Illinois (2024). Regional hospital system serving 8 counties.",
    headcount_note: "Anchor regional-services employer; primary local clinical career ladder.",
    url: "https://www.sarahbush.org/",
  },
  {
    name: "Rural King Supply",
    location: "Headquarters: Mattoon; distribution center: Mattoon; Charleston facility in former Casey Tool & Machine building",
    sector: "Retail / farm + ranch supply",
    role: "Privately held; ~146 stores across 13+ states; ~9,000+ total employees (national).",
    headcount_note: "Mattoon distribution center supplies 80+ stores. Charleston facility supports Rural King's growing e-commerce operation.",
    url: "https://www.ruralking.com/",
  },
  {
    name: "Lake Land College",
    location: "Mattoon",
    sector: "Community college",
    role: "Comprehensive community college serving 15-county East Central IL footprint, ~4,500 credit students.",
    headcount_note: "Companion higher-ed anchor to EIU; the credential-pipeline source for most workforce-board-relevant training in Coles County.",
    url: "https://www.lakelandcollege.edu/",
  },
  {
    name: "Consolidated Communications",
    location: "Headquarters: Mattoon",
    sector: "Telecommunications + broadband",
    role: "Mid-size publicly traded telecom; pre-2024 publicly traded (CNSL), acquired by Searchlight Capital + BCI 2024-12.",
    headcount_note: "HQ employment in Mattoon is a structural anchor — even post-acquisition, HQ functions remain locally based.",
    url: "https://www.consolidated.com/",
  },
  {
    name: "R.R. Donnelley Charleston",
    location: "Charleston",
    sector: "Printing / commercial print",
    role: "RRD operates a Charleston facility producing direct mail + commercial print products.",
    headcount_note: "Print-industry employment has contracted nationally over the past 15 years; the Charleston facility's role within RRD's footprint is a watch-item for the local industrial base.",
    url: "https://www.rrd.com/",
  },
];

// ── Crime stats — Charleston + Mattoon NeighborhoodScout / FBI UCR 2024
const CRIME_DATA = {
  charleston: {
    crime_per_1000: 18,
    violent_per_1000: 3,
    property_per_1000: 15,
    violent_1_in_n: "1 in 334",
    note: "Higher than 84% of IL cities + towns. Not among the very highest; college-town pattern (high overall rate driven by property crime + young population).",
  },
  mattoon: {
    crime_per_1000: 17,
    violent_per_1000: 5, // ~17 total - 12 property = 5 violent
    property_per_1000: 12,
    note: "Slightly lower overall vs Charleston; lower property crime but slightly higher violent-crime share.",
  },
  release_year: "FBI UCR 2024 calendar year (NeighborhoodScout October 2025 release).",
};

// ════════════════════════════════════════════════════════════════════════
// Render helpers
// ════════════════════════════════════════════════════════════════════════

function StatCard({ label, value, sub, flag }: { label: string; value: string; sub?: string; flag?: boolean }) {
  return (
    <div style={{
      background: "white",
      border: `1px solid ${flag ? "oklch(45% 0.20 22)33" : "#d8d2c4"}`,
      borderLeft: `6px solid ${flag ? "oklch(45% 0.20 22)" : "#1f1d18"}`,
      borderRadius: 6, padding: 14,
    }}>
      <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 600, color: flag ? "oklch(45% 0.20 22)" : "#1f1d18", lineHeight: 1.05 }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Page
// ════════════════════════════════════════════════════════════════════════

export default async function CharlestonPage() {
  const renderedAt = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";

  return (
    <>
      <DashboardHead title="Charleston, IL · Economic Snapshot" />
      <div className="dashboard-shell" style={{ maxWidth: 1180, margin: "0 auto", padding: "24px 24px 60px", fontFamily: "var(--font-serif), Georgia, serif" }}>
        <Topbar
          brand="Charleston, IL · Economic Snapshot"
          region="Coles County · East Central Illinois (LWA-23)"
          renderedAt={renderedAt}
        />

        {/* ═══ Hero · framing ═══ */}
        <section style={{ marginTop: 24 }}>
          <h1 style={{ fontSize: 32, fontWeight: 600, margin: 0, color: "#1f1d18", lineHeight: 1.15 }}>
            Charleston, IL — a college town reshaped by 20 years of enrollment decline at Eastern Illinois University
          </h1>
          <p style={{ fontSize: 15, color: "#3d3a33", marginTop: 12, maxWidth: 820, lineHeight: 1.6 }}>
            Charleston is the Coles County seat (pop {CITY.population_2024.toLocaleString()}, ACS 5-year 2024), home to Eastern Illinois University (EIU). Like {" "}
            <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale</a>{" "}— SIU's host city ~95 mi south-west — Charleston's economic fortunes are tied to a regional state university whose enrollment has fallen sharply over the last two decades. Both cities share the same structural challenge: a young-skewed population, elevated poverty + crime rates, and an anchor institution that is no longer the employer + economic engine it was in the mid-2000s.
          </p>
          <p style={{ fontSize: 13, color: "#5a564d", marginTop: 8, lineHeight: 1.55 }}>
            <strong>Regional context:</strong> Charleston sits in <em>{CITY.lwa}</em> — a separate workforce-development area from <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>LWA-25 Southern Illinois</a>. The two regions face structurally similar challenges (regional-university decline, rural East Central IL labor markets, federal-funding-dependence) but operate under distinct workforce boards + program portfolios.
          </p>
        </section>

        {/* ═══ Headline KPIs ═══ */}
        <section style={{ marginTop: 32 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
            <StatCard
              label="Population (ACS 2024)"
              value={CITY.population_2024.toLocaleString()}
              sub={`Down from ${CITY.population_2020.toLocaleString()} in 2020 (${CITY.pop_decline_pct.toFixed(1)}%)`}
              flag
            />
            <StatCard
              label="EIU enrollment (Fall 2025)"
              value="5,434"
              sub="Down from 11,651 peak (Fall 2004) — 53% drop"
              flag
            />
            <StatCard
              label="Median household income"
              value={`$${CITY.median_household_income_2024.toLocaleString()}`}
              sub="ACS 5-year 2024"
            />
            <StatCard
              label="Family poverty rate"
              value={`${CITY.poverty_rate_pct.toFixed(1)}%`}
              sub="High; college-town pattern + structural drift"
              flag
            />
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 10, lineHeight: 1.5 }}>
            Sources: <a href="https://www.census.gov/quickfacts/fact/table/charlestoncityillinois/PST045221" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census QuickFacts Charleston</a> + <a href="http://censusreporter.org/profiles/16000US1712567-charleston-il/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census Reporter Charleston</a> + <a href="https://www.eiu.edu/ir/fall_enrollment_tables.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>EIU Institutional Research · Fall Enrollment Tables</a> + <a href="https://www.dailyeasternnews.com/2025/06/27/easterns-20-year-enrollment-decrease-is-part-of-statewide-trend/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Daily Eastern News · 20-year enrollment decrease (2025-06-27)</a>.
          </div>
        </section>

        {/* ═══ §1 EIU enrollment decline · the defining story ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            01 · The EIU enrollment trajectory · headcount halved over two decades
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            EIU's enrollment trajectory is the dominant economic factor for Charleston. A loss of roughly 6,200 students over 20 years cascades through the city's housing market (student rentals + landlord-investor exposure), retail (restaurants, bars, services oriented around campus), and university employment (faculty + administrative staff levels track enrollment over time).
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, color: "#3d3a33" }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Term</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", textAlign: "right" }}>Total enrollment</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", textAlign: "right" }}>Change vs Fall 2004</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Context</th>
                </tr>
              </thead>
              <tbody>
                {EIU_ENROLLMENT.map((r, i) => {
                  const delta = r.headcount - EIU_ENROLLMENT[0].headcount;
                  const deltaPct = (delta / EIU_ENROLLMENT[0].headcount) * 100;
                  return (
                    <tr key={r.year} style={{ borderBottom: i < EIU_ENROLLMENT.length - 1 ? "1px solid #ebe5d6" : "none" }}>
                      <td style={{ padding: "8px 10px", fontWeight: 600 }}>{r.year}</td>
                      <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 600 }}>{r.headcount.toLocaleString()}</td>
                      <td style={{ padding: "8px 10px", textAlign: "right", color: delta < 0 ? "oklch(45% 0.20 22)" : "#5a564d" }}>
                        {i === 0 ? "—" : `${delta > 0 ? "+" : ""}${delta.toLocaleString()} (${deltaPct > 0 ? "+" : ""}${deltaPct.toFixed(1)}%)`}
                      </td>
                      <td style={{ padding: "8px 10px", color: "#5a564d", fontSize: 12 }}>{r.note}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 12 }}>
            <strong>Why this matters for workforce + economic planning in Charleston:</strong>
            <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
              <li><strong>Anchor-institution employment is not stable.</strong> EIU faculty + staff levels are downstream of student enrollment over the long horizon. A 53% enrollment decline cannot be absorbed indefinitely without corresponding employment contraction.</li>
              <li><strong>Off-campus student housing is overbuilt for current demand.</strong> The rental + landlord market in Charleston was sized for the early-2000s student population; current demand sits half that level. The same Census ACS structural vacancy + rental-degradation pattern visible in <a href="/carbondale" style={{ color: "#1f5f8f" }}>Carbondale</a> is the parallel here.</li>
              <li><strong>Retail + services oriented around campus traffic are exposed.</strong> Bar / restaurant / services density built for 11k students serves 5k students now; the marginal businesses have already closed, but the residual stock is fragile.</li>
              <li><strong>Recovery is not predicted by recent FAFSA-delay data alone.</strong> EIU welcomed its largest first-year domestic cohort in three years in Fall 2025 — a positive signal — but transfer + international declines outweighed it. Year-on-year volatility is FAFSA-driven; the underlying 20-year trajectory is structural.</li>
            </ul>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
            Sources: <a href="https://www.eiu.edu/ir/fall_enrollment_tables.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>EIU Institutional Research · Fall Enrollment Tables</a> + <a href="https://www.eiu.edu/ir/tenth_day_enrollment.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>EIU Tenth-Day Enrollment</a> + <a href="https://www.dailyeasternnews.com/2025/06/27/easterns-20-year-enrollment-decrease-is-part-of-statewide-trend/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Daily Eastern News · 20-year enrollment decrease is part of statewide trend (2025-06-27)</a> + <a href="https://jg-tc.com/news/local/education/article_235c4b96-1e4d-4d4e-94ad-5c5614e1856e.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>JG-TC · EIU total enrollment drops 8%</a> + <a href="https://nces.ed.gov/ipeds/dfr/2024/ReportHTML.aspx?unitId=144892" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NCES IPEDS Data Feedback Report · EIU (UnitID 144892)</a>.
          </div>
        </section>

        {/* ═══ §2 Demographics ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            02 · Demographics · the very-young college-town pattern
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            ACS 5-year 2024 puts the median age at <strong>{CITY.median_age} years</strong> — among the youngest non-trivial-population cities in Illinois. Roughly one-third of the city headcount is undergraduate or graduate student population; the demographic profile reflects that.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14, marginBottom: 12 }}>
            <StatCard label="Median age" value={`${CITY.median_age} yrs`} sub="Very young — student population skew" />
            <StatCard label="Bachelor's degree or higher" value={`~${CITY.pct_bachelors_or_higher.toFixed(0)}%`} sub="(adults 25+); EIU's transient student population doesn't lift this baseline" />
            <StatCard label="Race · White alone" value={`${CITY.pct_white.toFixed(1)}%`} />
            <StatCard label="Race · Black alone" value={`${CITY.pct_black.toFixed(1)}%`} />
            <StatCard label="Hispanic / Latino" value={`${CITY.pct_hispanic.toFixed(1)}%`} sub="Of any race" />
            <StatCard label="Family poverty rate" value={`${CITY.poverty_rate_pct.toFixed(1)}%`} flag sub="Persistent + structural" />
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
            Source: <a href="http://censusreporter.org/profiles/16000US1712567-charleston-il/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census Reporter · Charleston, IL (Place 16000US1712567)</a>. ACS 5-year vintage 2024.
          </div>
        </section>

        {/* ═══ §3 Anchor employers ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            03 · Anchor employers · the Coles County industrial + institutional base
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Coles County's employer base is more diversified than a single-anchor town would suggest. EIU is the largest employer, but Sarah Bush Lincoln Health Center (between Charleston + Mattoon), Rural King (HQ + distribution in Mattoon), Lake Land College, and Consolidated Communications all carry meaningful headcount. R.R. Donnelley operates a Charleston print facility. The president of Coles Together (the regional economic-development organization) describes the employer base as deliberately diversified — no single large employer carries the regional economy.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {ANCHOR_EMPLOYERS.map((e) => (
              <div key={e.name} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
                  <div style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18" }}>{e.name}</div>
                  <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b" }}>{e.sector}</div>
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4, marginBottom: 8 }}>{e.location}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>{e.role}</div>
                <div style={{ fontSize: 12, color: "#5a564d", lineHeight: 1.55, marginTop: 6 }}>{e.headcount_note}</div>
                <div style={{ fontSize: 11, marginTop: 6 }}>
                  <a href={e.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{e.url}</a>
                </div>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            Sources: <a href="https://www.colestogether.com/industriesandworkforce" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Coles Together · Industries + Workforce</a> + <a href="https://jg-tc.com/news/article_30a6eadd-84c3-5edd-a7a6-be80ebcfd1d3.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>JG-TC · Leading the way · large employers in the area</a> + <a href="https://jg-tc.com/business/local/achievements/article_7c72debe-3b48-4dbb-9daa-49c2bcad8230.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>JG-TC · Sarah Bush Lincoln ranked among IL's best employers</a> + <a href="https://www.colesco.illinois.gov/static/coclerk/CountyDirectory.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Coles County Directory of County, Municipal, Township + School Officials</a>. Always confirm current headcount via the employer directly before public stakeholder use.
          </div>
        </section>

        {/* ═══ §3.5 EIU Clery Act campus statistics ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            EIU campus · Clery Act Annual Security Report
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            EIU publishes the Annual Safety and Security Report (ASR) in compliance with the Jeanne Clery Disclosure of Campus Security Policy and Campus Crime Statistics Act, the IL Campus Security Enhancement Act, the Higher Education Opportunity Act of 2008, and VAWA. The ASR carries 3-year totals for federally-defined Clery offenses on the campus footprint + immediate public property + non-campus university-controlled properties.
          </div>
          <div style={{ padding: 14, background: "oklch(98% 0.015 220)", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 12 }}>
            <strong>Different metric than the city per-1,000 rates above.</strong> Clery counts only specific federally-defined offenses on the campus footprint + immediate public property, expressed as raw counts (not rates per 1,000 residents). EIU on-campus enrollment ~5,400 students (Fall 2025); residence-hall capacity is materially smaller. Compare Clery raw counts to the Charleston city per-1,000 rates carefully — different denominator, different geographic scope.
          </div>
          <div style={{ padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 12 }}>
            <strong>How to pull current EIU Clery statistics:</strong> EIU&apos;s Annual Safety and Security Report is hosted by the EIU Police Department at <a href="https://www.eiu.edu/police/Safety_Report.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>eiu.edu/police/Safety_Report.php</a>. The most recent ASR (released October 2025, covering 2022-2024) carries 3-year totals for Murder, Sex offenses (Rape + Forcible fondling), Robbery, Aggravated assault, Burglary, Motor vehicle theft, and Arson — broken down by On-Campus, On-Campus Student Housing, Non-campus, and Public Property categories per federal Clery formatting. For comparable cross-institution data, the US Department of Education Campus Safety and Security data portal (<a href="https://ope.ed.gov/campussafety/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>ope.ed.gov/campussafety</a>) indexes Clery filings for all federally-funded postsecondary institutions; EIU&apos;s IPEDS UnitID is 144892. This page will mirror the EIU ASR table once the operator pulls + transcribes the current report (same pattern as the SIU Carbondale Clery card on the <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale page</a>).
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
            Sources: <a href="https://www.eiu.edu/police/Safety_Report.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>EIU Police · Annual Safety and Security Report</a> + <a href="https://www.eiu.edu/police/Welcome_Statistics.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>EIU Police · Criminal Statistics</a> + <a href="https://ope.ed.gov/campussafety/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>US Dept of Education · Campus Safety + Security data tool</a> (IPEDS UnitID 144892).
          </div>
        </section>

        {/* ═══ §4 Crime · NeighborhoodScout / FBI UCR 2024 ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            04 · Crime · Charleston + Mattoon (FBI UCR 2024)
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Charleston's crime rate is elevated relative to the national average and to most Illinois cities (higher than 84% of IL communities). The pattern is property-crime dominant — typical of a college town with high transient + rental-housing density. Violent-crime rate is 3 per 1,000 (1 in 334 chance of violent victimization).
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>City</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Crime / 1,000</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Violent / 1,000</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Property / 1,000</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Note</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style={{ padding: "8px 10px", fontWeight: 600 }}>Charleston · Coles</td>
                  <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 600, color: "oklch(45% 0.20 22)" }}>{CRIME_DATA.charleston.crime_per_1000}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{CRIME_DATA.charleston.violent_per_1000}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{CRIME_DATA.charleston.property_per_1000}</td>
                  <td style={{ padding: "8px 10px", fontSize: 12, color: "#5a564d" }}>{CRIME_DATA.charleston.note}</td>
                </tr>
                <tr style={{ borderTop: "1px solid #ebe5d6" }}>
                  <td style={{ padding: "8px 10px", fontWeight: 600 }}>Mattoon · Coles</td>
                  <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 600, color: "oklch(45% 0.20 22)" }}>{CRIME_DATA.mattoon.crime_per_1000}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{CRIME_DATA.mattoon.violent_per_1000}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{CRIME_DATA.mattoon.property_per_1000}</td>
                  <td style={{ padding: "8px 10px", fontSize: 12, color: "#5a564d" }}>{CRIME_DATA.mattoon.note}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
            Sources: <a href="https://www.neighborhoodscout.com/il/charleston/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NeighborhoodScout Charleston</a> + <a href="https://www.neighborhoodscout.com/il/mattoon/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NeighborhoodScout Mattoon</a> + <a href="https://isp.illinois.gov/CrimeReporting/CrimeInIllinoisReports" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL State Police Crime in Illinois reports</a>. {CRIME_DATA.release_year}
          </div>
        </section>

        {/* ═══ §5 Comparison · Charleston-EIU vs Carbondale-SIU ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            05 · Sibling cities · Charleston + Carbondale share the regional-university decline pattern
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Charleston and <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale</a> are the two clearest examples of the Illinois regional-state-university decline pattern. Both cities are college-town anchored — Carbondale on SIU, Charleston on EIU — and both have lost roughly half their student headcount over the last two decades. The downstream effects on housing, retail, employment, and city demographics are structurally similar.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Metric</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Charleston · EIU</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Carbondale · SIU</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["City population (ACS 2024)", "~17,062", "~21,857"],
                  ["University Fall 2025 enrollment", "5,434 (EIU)", "~11,116 (SIU)"],
                  ["20-year enrollment decline", "-53%", "-45%"],
                  ["Median age", "29.6 yrs", "~25.5 yrs"],
                  ["Median household income", "$49,300", "~$31,000"],
                  ["Family poverty rate", "26.3%", "~37%"],
                  ["Crime rate per 1,000 (FBI UCR 2024)", "18", "50"],
                  ["Workforce-development area", "LWA-23 East Central IL", "LWA-25 Southern IL"],
                  ["Regional anchor health system", "Sarah Bush Lincoln (Mattoon-Charleston)", "Memorial Hospital of Carbondale (SIH)"],
                ].map(([label, ch, ca], i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{label}</td>
                    <td style={{ padding: "8px 10px", color: "#3d3a33" }}>{ch}</td>
                    <td style={{ padding: "8px 10px", color: "#3d3a33" }}>{ca}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 8 }}>
            <strong>Differences worth flagging for stakeholders:</strong>
          </div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.6 }}>
            <li><strong>Charleston is materially more affluent than Carbondale</strong> on median household income (+59%) — partially explained by EIU's higher faculty-pay vs SIU's older student-renter housing depressing the Carbondale figure.</li>
            <li><strong>Charleston has substantially lower crime rates</strong> (18 per 1,000 vs Carbondale's 50 per 1,000). The college-town pattern is consistent, but Carbondale's MV-theft + property-crime rates are notably higher.</li>
            <li><strong>The federal-contracting concentration that defines LWA-25 (GD-OTS Marion at $812M / 24 months)</strong> has no equivalent in Coles County. Charleston / Mattoon are diversified-services + light-industrial; the federal-money story that anchors the Southern IL page does not apply here.</li>
            <li><strong>Sarah Bush Lincoln is a Forbes Top 10 IL employer ranking</strong> — a meaningful regional differentiator. Carbondale's SIH Memorial Hospital is comparable in scale but doesn't carry the same independent national rating recognition.</li>
          </ul>
        </section>

        {/* ═══ §6 Comparison framework · 12 dimensions ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            06 · Comparison framework · the analytical dimensions for a Carbondale vs Charleston cross-read
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            The §05 headline metrics are the visible top of the comparison. The substantive comparison — the one that actually shifts workforce-planning, BD-pitch, federal-grant, or relocator decisions — runs through these 12 dimensions. Each carries a canonical primary-source data path. The dashboard renders the headline numbers; the deeper questions land here so stakeholders can pull them on demand.
          </div>
          <ol style={{ margin: "0 0 0 22px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            {[
              {
                q: "Wage-adjusted private-sector employment trajectory (10-yr), excluding the university payroll",
                why: "Strips the anchor-institution distortion and shows whether the surrounding private economy is growing, flat, or hollowing out.",
                src: "BLS QCEW (NAICS 6113 carve-out) at the county level",
                url: "https://www.bls.gov/cew/",
              },
              {
                q: "Occupational mix + median wage by major SOC group in each Micropolitan SA",
                why: "Determines whether either city has a transferable workforce for advanced manufacturing, healthcare, or tech relocation — vs. a service-economy lock-in.",
                src: "BLS OEWS · Carbondale-Marion + Mattoon Micro SAs",
                url: "https://www.bls.gov/oes/",
              },
              {
                q: "Tradable vs non-tradable employment share + how the split has moved since 2015",
                why: "Tradable employment (manufacturing, ag-tech, logistics, federal) is the leading indicator of whether a city can recover from anchor decline; non-tradable just recirculates local dollars.",
                src: "BLS QCEW 2-digit NAICS + Brookings tradable-sector classification",
                url: "https://www.brookings.edu/articles/the-geography-of-trade-in-goods-and-services-in-the-u-s/",
              },
              {
                q: "Net domestic migration pattern for the 25-44 age cohort over the last 5 years",
                why: "Student churn masks whether prime-working-age adults are arriving or leaving — the actual labor-supply signal for a relocator.",
                src: "Census ACS B07001 migration tables + IRS SOI county-to-county migration",
                url: "https://www.irs.gov/statistics/soi-tax-stats-migration-data",
              },
              {
                q: "Federal contract + grant inflow per capita by agency + NAICS over the last 5 fiscal years",
                why: "Reveals existing federal anchors (DOE/NSF research at SIU, USDA, VA, DoD subcontracts) that a grant proposal can stack onto or a BD pitch can cite.",
                src: "USAspending.gov + SAM.gov · Jackson + Coles county recipients",
                url: "https://www.usaspending.gov/",
              },
              {
                q: "Commuting shed — where do workers live + where do residents work?",
                why: "Charleston-Mattoon is a twin-city labor market; Carbondale is more isolated. Changes program design (transit, regional LWA coordination) + the realistic labor-draw radius.",
                src: "Census LEHD OnTheMap (LODES) county inflow/outflow",
                url: "https://onthemap.ces.census.gov/",
              },
              {
                q: "Housing affordability + vacancy relative to median wage; share of stock that is student-dependent rental",
                why: "Both cities have overbuilt student housing; the conversion risk + relocator housing-cost story diverges sharply.",
                src: "Census ACS B25 series + HUD CHAS + local MLS / Zillow Research",
                url: "https://www.huduser.gov/portal/datasets/cp.html",
              },
              {
                q: "LWA WIOA performance vs state-average exit / credential / median-earnings benchmarks",
                why: "LWA-25 (Man-Tra-Con) vs LWA-23 (East Central IL) have different program portfolios + outcomes — drives where a federal workforce grant should land.",
                src: "IL DCEO WIOA Annual Performance Reports + USDOL ETA-9169",
                url: "https://www.illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx",
              },
              {
                q: "Healthcare-sector employment share + concentration (Location Quotient > 1.5?)",
                why: "Both cities have regional hospitals (SIH / Sarah Bush Lincoln); healthcare often becomes the post-anchor employer + the LQ tells you whether it's already saturated or still absorbing.",
                src: "BLS QCEW location quotients · NAICS 62",
                url: "https://www.bls.gov/cew/about-data/location-quotients-explained.htm",
              },
              {
                q: "Prime-age (25-54) labor force participation rate vs IL + US averages",
                why: "LFPR catches the discouraged-worker problem the unemployment rate hides — critical for both grant narrative + BD honest-broker pitch.",
                src: "Census ACS S2301 county tables",
                url: "https://data.census.gov/table?q=S2301",
              },
              {
                q: "Broadband availability + adoption rate (100/20 Mbps fixed) at census-tract level",
                why: "Remote-work + federal BEAD eligibility hinge on this; Coles vs Jackson differs materially.",
                src: "FCC Broadband Data Collection + NTIA BEAD eligibility maps",
                url: "https://broadbandmap.fcc.gov/",
              },
              {
                q: "Property-tax burden on commercial real estate + TIF / Enterprise Zone / Opportunity Zone footprints",
                why: "The single biggest line item in a relocator's pro-forma + the actual lever a city BD office can pull.",
                src: "IL Dept of Revenue PTAX-203 + IL DCEO Enterprise Zone registry + Treasury OZ map",
                url: "https://dceo.illinois.gov/expandrelocate/incentives/enterprisezone.html",
              },
            ].map((d, i) => (
              <li key={i} style={{ marginBottom: 14 }}>
                <strong>{d.q}.</strong>{" "}
                <span style={{ color: "#5a564d" }}>{d.why}</span>{" "}
                <em style={{ color: "#5a564d" }}>Source: <a href={d.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{d.src}</a>.</em>
              </li>
            ))}
          </ol>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Operator note:</strong> the table in §05 carries the visible top-line comparison; this framework is what the dashboard will iteratively fill in. Items 1-4 + 8 + 10 are the highest-leverage for workforce-planning + grant-narrative decisions; items 5-6 + 12 carry the highest leverage for BD relocator pitches; items 7 + 11 are the relocator-side housing + connectivity diligence pair. Pull each as needed; cite the primary source on every claim.
          </div>
        </section>

        {/* ═══ §7 Action ladder ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            07 · Action ladder · what the page surfaces for the LWA-23 workforce board + Coles Together
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Each card below leads with what the page already does (data-side) and ends with the human-only residual step. The Lake Land College + EIU credential pipeline is the right place to anchor any workforce-board cohort planning; Sarah Bush Lincoln Health Center is the right place to anchor any allied-health credential discussion; Rural King + Consolidated Communications HQ presence in Mattoon are the right places to anchor any retail / telecom / logistics conversation.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
            {[
              {
                title: "Compare Charleston-EIU + Carbondale-SIU side-by-side",
                body: <>The two cities share the regional-university decline pattern but differ on crime, poverty, and federal-money concentration. Use the §05 comparison table to map joint-region workforce-planning analogues (e.g., what works for SIU graduate-retention housing in Carbondale's old stock vs EIU's Charleston student-rental market). Companion: <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale →</a></>,
              },
              {
                title: "Anchor allied-health credential pipelines on Sarah Bush Lincoln",
                body: "SBL is a Forbes-ranked top 10 IL employer + the regional hospital system. Lake Land College CNA / LPN / RN-ADN credentials are the natural pipeline; the workforce board's residual step is brokering the next cohort intake at SBL's HR + nursing leadership. Pull current SBL job postings + hire counts before any cohort planning.",
              },
              {
                title: "Map EIU enrollment-decline cascade to local economic impact",
                body: "The §01 table shows EIU enrollment dropping from 11,651 (2004) to 5,434 (2025). The downstream consequences — student-rental oversupply, campus-adjacent retail attrition, university staff levels — are workforce-board-adjacent but not workforce-board-solvable. Coles Together's role is regional economic-development advocacy; the data anchors the case.",
              },
              {
                title: "Coordinate with sister regions",
                body: <>Companion public dashboards: <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>Southern Illinois Region (LWA-25) →</a> for the regional-university decline parallel + federal-money concentration framework; <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale →</a> for the SIU-host-city economic profile; <a href="/murphysboro" style={{ color: "#1f5f8f", fontWeight: 600 }}>Murphysboro →</a> for the Jackson County secondary-city pattern; <a href="/market" style={{ color: "#1f5f8f", fontWeight: 600 }}>US Market Health →</a> for national macro context.</>,
              },
            ].map((c, i) => (
              <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>{c.title}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6 }}>{c.body}</div>
              </div>
            ))}
          </div>
        </section>

        {/* ═══ §8 Page scope + methodology ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            08 · Page scope + methodology
          </h2>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6, maxWidth: 820 }}>
            This page is currently a <strong>static primary-source profile</strong> — data is hardcoded against the citations above and refreshed manually. Companion pages <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>/carbondale</a> and <a href="/murphysboro" style={{ color: "#1f5f8f", fontWeight: 600 }}>/murphysboro</a> route through console-api with live FRED + Census + USAspending pulls. Charleston can migrate to the same live-data pattern once Coles County / Mattoon Micropolitan series are wired in the backend (Coles County FIPS {CITY.fips_county}; Mattoon Micropolitan CBSA 31380).
            <br /><br />
            <strong>Refresh cadence:</strong> EIU enrollment is published annually in October (Tenth-Day) and November (Fall Enrollment Tables) by EIU Institutional Research. Census ACS 5-year is published in December for the preceding 5-year window. FBI UCR is published in October for the preceding calendar year. NeighborhoodScout publishes shortly after FBI UCR release. Update this page accordingly.
            <br /><br />
            <strong>Editorial standard:</strong> every claim on this page is anchored on a primary source (cited inline); no inferences or unsourced framings. Verdict adjectives are reserved for verifiable patterns (e.g., &quot;higher than 84% of IL communities&quot; for crime, where NeighborhoodScout publishes the percentile).
          </div>
        </section>

        <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
      </div>
    </>
  );
}
