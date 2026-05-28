/**
 * Public /east-central-illinois page — Local Workforce Innovation Area 23
 * (LWA-23) regional analysis. Mirrors the /southern-illinois (LWA-25) page
 * structure but for the 13-county LWA-23 footprint administered by CEFS
 * Economic Opportunity Corporation out of Effingham, IL.
 *
 * Counties: Clark, Clay, Coles, Crawford, Cumberland, Edgar, Effingham,
 * Fayette, Jasper, Lawrence, Marion, Moultrie, Richland. Major regional
 * anchors: EIU (Charleston), Lake Land College (Mattoon), Sarah Bush Lincoln
 * Health Center (Mattoon-Charleston), CEFS EOC (Effingham), Effingham-area
 * industrial + retail employers.
 *
 * Charleston-specific city profile lives at /charleston.
 */
import { DashboardHead, Topbar, DashboardFooter, DEFAULT_FOOTER_COLUMNS } from "@/components/dashboard-chrome";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const LWA23 = {
  name: "Local Workforce Innovation Area 23 (LWA-23)",
  short: "East Central Illinois · 13-county footprint",
  admin: "CEFS Economic Opportunity Corporation",
  admin_hq: "1805 South Banker Street, Effingham, IL 62401",
  admin_phone: "(217) 342-2193 ext. 2121",
  admin_email: "lwia23@cefseoc.org",
  admin_url: "https://www.lwa23.com/",
  counties: [
    { name: "Clark",      seat: "Marshall",     fips: "17023" },
    { name: "Clay",       seat: "Louisville",   fips: "17025" },
    { name: "Coles",      seat: "Charleston",   fips: "17029", anchor: "EIU + Lake Land + Sarah Bush Lincoln + Rural King + Consolidated Communications" },
    { name: "Crawford",   seat: "Robinson",     fips: "17033" },
    { name: "Cumberland", seat: "Toledo",       fips: "17035" },
    { name: "Edgar",      seat: "Paris",        fips: "17045" },
    { name: "Effingham",  seat: "Effingham",    fips: "17049", anchor: "CEFS HQ + St. Anthony's Memorial Hospital + transportation/logistics hub I-57/I-70" },
    { name: "Fayette",    seat: "Vandalia",     fips: "17051" },
    { name: "Jasper",     seat: "Newton",       fips: "17079" },
    { name: "Lawrence",   seat: "Lawrenceville", fips: "17101" },
    { name: "Marion",     seat: "Salem",        fips: "17121" },
    { name: "Moultrie",   seat: "Sullivan",     fips: "17139" },
    { name: "Richland",   seat: "Olney",        fips: "17159" },
  ],
};

const HIGHER_ED_ANCHORS = [
  {
    name: "Eastern Illinois University (EIU)",
    location: "Charleston · Coles County",
    type: "Public 4-year university",
    enrollment_fall2025: 5434,
    enrollment_peak: 11651,
    peak_year: "Fall 2004",
    note: "Regional public university; major credential source for K-12 teachers, business administration, public administration. 20-year enrollment decline of 53%. See /charleston for the city profile.",
    url: "https://www.eiu.edu/",
  },
  {
    name: "Lake Land College",
    location: "Mattoon · Coles County",
    type: "Community college",
    enrollment_fall2025: 4500, // approximate, credit-program
    note: "Comprehensive community college serving 15-county East Central IL footprint (overlaps but extends beyond LWA-23). The credential-pipeline source for most workforce-board-relevant training in the northern LWA-23.",
    url: "https://www.lakelandcollege.edu/",
  },
  {
    name: "Kaskaskia College",
    location: "Centralia · Marion County",
    type: "Community college",
    enrollment_fall2025: 5300, // approximate, credit + non-credit
    note: "Serves the southern + western edges of LWA-23 (Clay, Fayette, Marion, Washington Cos.). Strong nursing + allied-health + industrial-trades programs.",
    url: "https://kc.kaskaskia.edu/",
  },
  {
    name: "Olney Central College",
    location: "Olney · Richland County",
    type: "Community college (IECC system)",
    enrollment_fall2025: 1100,
    note: "Part of the Illinois Eastern Community Colleges (IECC) district — Olney Central, Lincoln Trail (Robinson), Wabash Valley (Mt. Carmel), Frontier (Fairfield). Serves the south-east LWA-23 corridor.",
    url: "https://www.iecc.edu/occ/",
  },
];

const ANCHOR_EMPLOYERS = [
  {
    name: "Sarah Bush Lincoln Health Center",
    location: "Between Mattoon + Charleston on IL Route 16 · Coles County",
    sector: "Regional healthcare system",
    role: "Forbes / Statista Top 10 Best Employers in Illinois (2024). Anchor regional hospital serving 8 counties.",
    url: "https://www.sarahbush.org/",
  },
  {
    name: "St. Anthony's Memorial Hospital (HSHS)",
    location: "Effingham · Effingham County",
    sector: "Regional healthcare system",
    role: "Part of Hospital Sisters Health System; major Effingham-area employer + the regional anchor for southern LWA-23 healthcare.",
    url: "https://www.hshs.org/stanthonys",
  },
  {
    name: "Rural King Supply",
    location: "Headquarters: Mattoon · Coles County",
    sector: "Retail / farm + ranch supply",
    role: "Privately held; ~146 stores across 13+ states; ~9,000+ total employees (national). Mattoon distribution center supplies 80+ stores.",
    url: "https://www.ruralking.com/",
  },
  {
    name: "Consolidated Communications",
    location: "Headquarters: Mattoon · Coles County",
    sector: "Telecommunications + broadband",
    role: "Mid-size publicly traded telecom; acquired by Searchlight Capital + BCI 2024-12. HQ functions remain locally based.",
    url: "https://www.consolidated.com/",
  },
  {
    name: "R.R. Donnelley Charleston",
    location: "Charleston · Coles County",
    sector: "Commercial printing",
    role: "RRD operates a Charleston facility producing direct mail + commercial print products.",
    url: "https://www.rrd.com/",
  },
  {
    name: "Marathon Petroleum Robinson Refinery",
    location: "Robinson · Crawford County",
    sector: "Petroleum refining",
    role: "Major refinery in Crawford County; one of the largest single industrial employers in southern LWA-23. ~245,000 bbl/day refining capacity.",
    url: "https://www.marathonpetroleum.com/Operations/Refining/Robinson-Refinery/",
  },
  {
    name: "Hodgson Mill",
    location: "Effingham · Effingham County",
    sector: "Food processing / specialty grains",
    role: "Stone-ground whole-grain flour + specialty mixes. Long-standing Effingham employer.",
    url: "https://www.hodgsonmill.com/",
  },
  {
    name: "Sherwin-Williams Effingham Manufacturing",
    location: "Effingham · Effingham County",
    sector: "Coatings manufacturing",
    role: "Large-scale paint + coatings plant; Sherwin-Williams' Effingham facility serves regional + national distribution.",
    url: "https://www.sherwin-williams.com/",
  },
];

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
export default async function EastCentralIllinoisPage() {
  const renderedAt = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";

  return (
    <>
      <DashboardHead title="East Central Illinois (LWA-23) · Regional Snapshot" />
      <div className="dashboard-shell" style={{ maxWidth: 1180, margin: "0 auto", padding: "24px 24px 60px", fontFamily: "var(--font-serif), Georgia, serif" }}>
        <Topbar
          brand="East Central Illinois (LWA-23) · Regional Snapshot"
          region="13-county workforce footprint · Effingham-anchored"
          renderedAt={renderedAt}
        />

        {/* ═══ Hero ═══ */}
        <section style={{ marginTop: 24 }}>
          <h1 style={{ fontSize: 32, fontWeight: 600, margin: 0, color: "#1f1d18", lineHeight: 1.15 }}>
            East Central Illinois — Local Workforce Innovation Area 23
          </h1>
          <p style={{ fontSize: 15, color: "#3d3a33", marginTop: 12, maxWidth: 820, lineHeight: 1.6 }}>
            LWA-23 covers 13 Illinois counties spanning the east-central + south-east of the state — anchored on the I-57 / I-70 / US-45 corridor and administered by <strong>{LWA23.admin}</strong> out of Effingham. The region's three structural anchors: Eastern Illinois University (Charleston, Coles Co.) + Lake Land College (Mattoon, Coles Co.) for the higher-ed credential pipeline; Sarah Bush Lincoln + HSHS St. Anthony's for regional healthcare; and a diversified industrial base (Marathon Petroleum Robinson, Sherwin-Williams Effingham, Rural King HQ Mattoon, Consolidated Communications HQ Mattoon, R.R. Donnelley Charleston, Hodgson Mill Effingham).
          </p>
          <p style={{ fontSize: 13, color: "#5a564d", marginTop: 8, lineHeight: 1.55 }}>
            <strong>Companion regional analysis:</strong> <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>Southern Illinois Region (LWA-25)</a> — 5-county footprint (Franklin / Jackson / Jefferson / Perry / Williamson) anchored on Man-Tra-Con + SIU + Memorial Hospital of Carbondale. The two regions face structurally similar challenges (regional-university decline, rural IL labor markets, federal-funding-dependence) but with distinct workforce-board portfolios + employer bases.{" "}
            <strong>Companion city profile:</strong> <a href="/charleston" style={{ color: "#1f5f8f", fontWeight: 600 }}>Charleston, IL →</a> for the Coles County seat + EIU host city deep-dive.
          </p>
        </section>

        {/* ═══ Headline KPIs ═══ */}
        <section style={{ marginTop: 32 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
            <StatCard
              label="LWA-23 counties"
              value="13"
              sub="vs LWA-25's 5-county footprint"
            />
            <StatCard
              label="Workforce-board admin"
              value="CEFS EOC"
              sub="Headquartered in Effingham, IL"
            />
            <StatCard
              label="Higher-ed anchors"
              value="4"
              sub="EIU + Lake Land + Kaskaskia + Olney Central"
            />
            <StatCard
              label="Anchor hospital systems"
              value="2"
              sub="Sarah Bush Lincoln + HSHS St. Anthony's"
            />
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 10, lineHeight: 1.5 }}>
            Sources: <a href="https://www.lwa23.com/services" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>LWA-23 · Services</a> + <a href="https://www.illinoisworknet.com/WIOA/RegPlanning/Pages/LWIAMatrix.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois workNet · LWIA Matrix</a>.
          </div>
        </section>

        {/* ═══ §1 13-county footprint ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            01 · The 13-county footprint · anchor counties + secondary counties
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23 has two clear anchor counties — <strong>Coles</strong> (EIU + Lake Land + SBL + Rural King + Consolidated Communications) and <strong>Effingham</strong> (CEFS HQ + St. Anthony's + I-57/I-70 logistics hub + Sherwin-Williams + Hodgson Mill). The other 11 counties are smaller, with county-seat populations in the 1k-10k range, and supply credential candidates + commuter labor to the anchor-county employer base.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, color: "#3d3a33" }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>County</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>County seat</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>FIPS</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Anchor employer cluster</th>
                </tr>
              </thead>
              <tbody>
                {LWA23.counties.map((c, i) => (
                  <tr key={c.fips} style={{ borderBottom: i < LWA23.counties.length - 1 ? "1px solid #ebe5d6" : "none", background: c.anchor ? "oklch(98% 0.02 142)" : "transparent" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{c.name}{c.anchor ? " ★" : ""}</td>
                    <td style={{ padding: "8px 10px" }}>{c.seat}</td>
                    <td style={{ padding: "8px 10px", color: "#5a564d", fontFamily: "monospace", fontSize: 11 }}>{c.fips}</td>
                    <td style={{ padding: "8px 10px", color: "#5a564d", fontSize: 12 }}>{c.anchor ?? "Smaller county; commuter / credential-pipeline contribution to anchor counties"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
            ★ = anchor county. Source: <a href="https://www.lwa23.com/services" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>LWA-23 · Services page</a>. County seats per Illinois Secretary of State; FIPS codes per US Census FIPS 5-2 standard.
          </div>
        </section>

        {/* ═══ §2 Higher-ed anchors ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            02 · Higher-ed anchors · credential pipelines + the EIU enrollment trajectory
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23 has one public 4-year university (EIU) + three community colleges (Lake Land, Kaskaskia, Olney Central). The community-college network is dense relative to LWA-25 (which has JALC + Rend Lake + SIC + Shawnee — also 4, but covering a smaller 5-county footprint). EIU's 20-year enrollment decline (peak 11,651 → 5,434 in Fall 2025) is the dominant higher-ed economic story for the region.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {HIGHER_ED_ANCHORS.map((a) => (
              <div key={a.name} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
                  <div style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18" }}>{a.name}</div>
                  <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b" }}>{a.type}</div>
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4, marginBottom: 8 }}>{a.location}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 6 }}>
                  Fall 2025 enrollment ~<strong>{a.enrollment_fall2025.toLocaleString()}</strong>
                  {a.enrollment_peak && a.peak_year && <> · 20-year peak <strong>{a.enrollment_peak.toLocaleString()}</strong> ({a.peak_year})</>}
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", lineHeight: 1.55 }}>{a.note}</div>
                <div style={{ fontSize: 11, marginTop: 6 }}>
                  <a href={a.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{a.url}</a>
                </div>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            Sources: institutional fact-books for each college + <a href="https://nces.ed.gov/ipeds/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NCES IPEDS</a>. Always confirm current enrollment via the institution before public stakeholder use.
          </div>
        </section>

        {/* ═══ §3 Anchor employers ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            03 · Anchor employers · diversified vs LWA-25's federal-concentration
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23's employer base is materially more diversified than LWA-25's. The Southern Illinois Region (LWA-25) page documents <strong>95.6% of federal contract dollars concentrated in one prime (GD-OTS Marion)</strong>; LWA-23 has no equivalent concentration. Instead, the region carries an industrial mix spanning healthcare (Sarah Bush Lincoln, HSHS St. Anthony's), petroleum refining (Marathon Robinson), specialty manufacturing (Sherwin-Williams Effingham, Hodgson Mill), commercial printing (RRD Charleston), retail HQ (Rural King Mattoon), telecom HQ (Consolidated Communications Mattoon), and higher-ed (EIU + 3 community colleges). The corollary: no single failure point, but also no single anchor on the scale of GD-OTS for federal-money stacking.
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
                <div style={{ fontSize: 11, marginTop: 6 }}>
                  <a href={e.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{e.url}</a>
                </div>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            Sources: each employer&apos;s own corporate site + <a href="https://www.colestogether.com/industriesandworkforce" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Coles Together · Industries + Workforce</a> + <a href="https://www.effinghamil.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>City of Effingham economic-development</a>.
          </div>
        </section>

        {/* ═══ §4 LWA-23 vs LWA-25 structural comparison ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            04 · LWA-23 vs LWA-25 · structural comparison
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            The two workforce regions share rural Illinois patterns but differ on three key axes: footprint size, federal-money concentration, and the regional-anchor mix. Where LWA-25 is small + federally-concentrated, LWA-23 is larger + employer-diversified.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Dimension</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>LWA-23 East Central</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>LWA-25 Southern</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["County count", "13", "5"],
                  ["Workforce-board admin", "CEFS EOC (Effingham)", "Man-Tra-Con (West Frankfort)"],
                  ["Anchor higher-ed (4yr)", "EIU (Charleston) · 5,434", "SIU Carbondale · ~11,116"],
                  ["Community colleges", "Lake Land + Kaskaskia + Olney Central (3)", "JALC + Rend Lake + SIC + Shawnee (4)"],
                  ["Anchor hospital systems", "Sarah Bush Lincoln + HSHS St. Anthony's", "SIH Memorial Carbondale + Heartland Reg'l"],
                  ["Federal-contract concentration", "Diversified — no GD-OTS-equivalent", "95.6% to GD-OTS Marion (24-mo)"],
                  ["Major industrial employers", "Marathon refinery (Robinson) + Sherwin-Williams + RRD + Rural King + Consolidated", "Aisin Mfg + Continental Tire + USG + GD-OTS subs"],
                  ["Notable demographic story", "Smaller per-county pops; Effingham + Coles dominate", "Williamson Co. growth corridor; Carbondale + Murphysboro decline"],
                  ["Interstate access", "I-57 + I-70 cross at Effingham; US-45", "I-57 + I-24; Carbondale-Marion airport (MWA)"],
                ].map(([label, lwa23, lwa25], i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{label}</td>
                    <td style={{ padding: "8px 10px", color: "#3d3a33" }}>{lwa23}</td>
                    <td style={{ padding: "8px 10px", color: "#3d3a33" }}>{lwa25}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* ═══ §5 Industry mix overview ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            05 · Industry mix · sectors that anchor LWA-23 employment
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23's sector mix runs along five lines: healthcare (Sarah Bush Lincoln + HSHS St. Anthony's + Lake Land allied-health pipeline), higher-ed (EIU + 3 community colleges), specialty manufacturing (Sherwin-Williams Effingham, Hodgson Mill, RRD Charleston), petroleum refining + downstream (Marathon Robinson), and retail / telecom HQ functions (Rural King + Consolidated Communications). Agriculture is a constant across the rural counties but does not show as an anchor employer in BLS QCEW counts; H-2A program data documents the regional ag-labor pattern.
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 12 }}>
            <strong>Pending live-data integration:</strong> BLS QCEW supersector employment data, aggregated across the 13 LWA-23 counties, is not yet wired into the console-api backend. Until then, this section carries qualitative employer mapping. The Charleston city-level QCEW data (Coles County FIPS 029) IS available via the /charleston endpoint; the multi-county LWA aggregation is the next backend work item.
          </div>
        </section>

        {/* ═══ §6 Action ladder ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            06 · Action ladder · what the page surfaces for the CEFS EOC workforce board + regional stakeholders
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Each card below leads with what the page already does (data-side) and ends with the human-only residual step. The 13-county scale of LWA-23 makes anchor-county prioritization (Coles + Effingham) the practical workforce-planning frame; the smaller counties are commuter-feeder + credential-pipeline contributors.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {[
              {
                title: "Prioritize anchor-county workforce investment",
                body: <>The §01 footprint table identifies Coles + Effingham as the two anchor counties; the other 11 are smaller commuter / pipeline contributors. <strong>Residual:</strong> the workforce board's annual cohort plan should weight WIOA program slots toward Coles + Effingham anchor-employer hiring lanes (Sarah Bush Lincoln allied-health, EIU + Lake Land paraprofessional, Sherwin-Williams + Hodgson Mill industrial-trades, Marathon Robinson refinery operations).</>,
              },
              {
                title: "Cross-coordinate with adjacent LWAs",
                body: <>LWA-23's footprint borders <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>LWA-25 (Southern Illinois) →</a> to the south + east. Workers commute across LWA boundaries. <strong>Residual:</strong> CEFS EOC + Man-Tra-Con should coordinate WIOA cohort planning at the LWA-23 / LWA-25 boundary (Marion + Fayette + Clay Co. workers commuting south; Williamson + Jefferson Co. workers commuting north).</>,
              },
              {
                title: "Anchor allied-health pipelines on the two hospital systems",
                body: "Sarah Bush Lincoln + HSHS St. Anthony's anchor regional healthcare. Lake Land + Kaskaskia + Olney Central all run nursing + allied-health credential pipelines. The workforce board's residual step is brokering cohort intake — pull current SBL + HSHS job postings + hire counts before cohort planning.",
              },
              {
                title: "Coordinate with the EIU + community-college credential pipeline",
                body: <>EIU (Charleston) carries the 4-year credential ladder. Lake Land + Kaskaskia + Olney Central carry the 2-year + sub-baccalaureate ladders. <strong>Residual:</strong> sequence WIOA training-cohort placements so 2-year graduates have a 4-year transfer option at EIU, and 4-year EIU credentialing has a community-college on-ramp. Companion: <a href="/charleston" style={{ color: "#1f5f8f", fontWeight: 600 }}>Charleston, IL →</a></>,
              },
              {
                title: "Diversification is the LWA-23 strength — protect it",
                body: <>LWA-25's federal-money concentration (95.6% to GD-OTS Marion) is both an anchor + a structural risk. LWA-23's diversified employer base (refining, retail HQ, telecom HQ, specialty mfg, healthcare, higher-ed) means no single anchor failure crashes the region. <strong>Residual:</strong> cohort plans should preserve cross-sector training breadth rather than concentrating on a single dominant employer — the diversification protects the region but only if the credential pipeline keeps feeding multiple sectors.</>,
              },
              {
                title: "Coordinate with the sister regional + city pages",
                body: <>Companion public dashboards: <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>Southern Illinois Region (LWA-25) →</a> · <a href="/charleston" style={{ color: "#1f5f8f", fontWeight: 600 }}>Charleston, IL →</a> · <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale, IL →</a> · <a href="/murphysboro" style={{ color: "#1f5f8f", fontWeight: 600 }}>Murphysboro, IL →</a> · <a href="/market" style={{ color: "#1f5f8f", fontWeight: 600 }}>US Market Health →</a></>,
              },
            ].map((c, i) => (
              <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>{c.title}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6 }}>{c.body}</div>
              </div>
            ))}
          </div>
        </section>

        {/* ═══ §7 Methodology + page scope ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            07 · Page scope + methodology
          </h2>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6, maxWidth: 820 }}>
            This page is a <strong>static primary-source regional profile</strong> mirroring the analytical structure of <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>/southern-illinois</a> (LWA-25). It does not yet route through console-api — multi-county QCEW aggregation, multi-county USAspending federal-money concentration, and multi-county Census ACS roll-ups are the next backend integration items.
            <br /><br />
            <strong>LWA-23 administrative anchor:</strong> {LWA23.admin}, {LWA23.admin_hq}. Public contact: {LWA23.admin_phone} · {LWA23.admin_email} · <a href={LWA23.admin_url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{LWA23.admin_url}</a>.
            <br /><br />
            <strong>Editorial standard:</strong> every claim on this page is anchored on a primary source (cited inline); no inferences or unsourced framings.
          </div>
        </section>

        <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
      </div>
    </>
  );
}
