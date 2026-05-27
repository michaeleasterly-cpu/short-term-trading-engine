/**
 * Public /mantracon page — Man-Tra-Con / SIWIB LWA-25 workforce dashboard.
 *
 * 5-county service area (Franklin, Jackson, Jefferson, Perry, Williamson).
 * Headline = labor-force-weighted UR across the LWA. Per-county detail.
 * Federal-contract business leads (USAspending) so the board can match
 * sectors with regional demand to local training pipelines.
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

interface TrainingLadder {
  id: string;
  name: string;
  ladder: string;
  training_duration: string;
  typical_journey_wage_wkly: number;
  typical_journey_wage_hrly: number;
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
}
interface TrainingAlignment {
  ladders: TrainingLadder[];
  livable_wage_jackson_il: {
    single_adult_wkly: number;
    single_adult_hrly: number;
    family_1a2c_wkly: number;
    family_1a2c_hrly: number;
    source: string;
  };
  source: string;
}

interface TopRecipient {
  name: string;
  amount: number;
  share_pct: number;
  alias_count: number;
  sba_status?: string;
  location_tag?: string;
  founder_note?: string;
  source_url?: string;
}
interface SdvosbSummary {
  count: number;
  local_count: number;
  out_of_region_count: number;
  total_dollars: number;
  total_share_pct: number;
}
interface TopRecipientsBlock {
  recipients: TopRecipient[];
  total_dollars: number;
  lookback_months: number;
  top1_share: number;
  top3_share: number;
  concentration_label: string;
  sdvosb_summary?: SdvosbSummary;
  source: string;
}

function sbaBadge(status: string | undefined): { label: string; bg: string; fg: string } {
  switch (status) {
    case "SDVOSB":      return { label: "SDVOSB",       bg: "oklch(96% 0.04 142)", fg: "oklch(35% 0.18 142)" };
    case "WOSB":        return { label: "WOSB",         bg: "oklch(96% 0.04 142)", fg: "oklch(35% 0.18 142)" };
    case "HUBZONE":     return { label: "HUBZone",      bg: "oklch(96% 0.04 142)", fg: "oklch(35% 0.18 142)" };
    case "8A":          return { label: "8(a)",         bg: "oklch(96% 0.04 142)", fg: "oklch(35% 0.18 142)" };
    case "LARGE":       return { label: "Large biz",    bg: "#f0ece1",              fg: "#5a564d" };
    case "UNVERIFIED":  return { label: "Verify @SAM.gov", bg: "oklch(97% 0.04 60)", fg: "oklch(40% 0.15 60)" };
    default:            return { label: "—",            bg: "#f0ece1",              fg: "#5a564d" };
  }
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
interface LaborTruthGeo {
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
interface LaborTruth {
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

interface CountyIndustrySnapshot {
  fips: string;
  name: string;
  total_employment: number;
  top_supersectors: Array<{ code: string; name: string; employment: number; avg_weekly_wage: number }>;
}
interface IndustryMix {
  as_of_quarter: string;
  top_supersectors: IndustryRow[];
  total_employment: number;
  by_county?: CountyIndustrySnapshot[];
  source: string;
}

interface MantraconData {
  ts: string;
  indicators: Record<string, { value: number; date: string }>;
  lwa_aggregate: {
    labor_force: number | null;
    labor_force_date: string | null;
    unemployment_rate_weighted: number | null;
    unemployment_rate_date: string | null;
    county_count: number;
  };
  lwa_labor_force_series: Array<{ date: string; value: number }>;
  lwa_unemployment_series: Array<{ date: string; value: number }>;
  business_opportunities: BusinessOps;
  top_federal_recipients?: TopRecipientsBlock;
  industry_mix?: IndustryMix;
  labor_truth?: LaborTruth;
  training_alignment?: TrainingAlignment;
}

function TrainingAlignmentSection({ ta, industryMixAvailable }: { ta: TrainingAlignment; industryMixAvailable: boolean }) {
  if (!ta.ladders.length) return null;
  // If the upstream QCEW fetch failed (empty industry_mix), every ladder will
  // get bogus "0 jobs / PHANTOM PIPELINE" verdicts. Render an explicit error
  // banner instead of pretending the verdicts are real.
  if (!industryMixAvailable) {
    return (
      <section style={{ marginTop: 40 }}>
        <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
        <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
          Training-to-demand alignment · data feed temporarily unavailable
        </h2>
        <div style={{ padding: 16, background: "oklch(97% 0.04 60)", border: "1px solid oklch(58% 0.15 60)33", borderLeft: "6px solid oklch(58% 0.15 60)", borderRadius: 6, fontSize: 14, color: "#3d3a33", lineHeight: 1.55 }}>
          The BLS QCEW industry-employment feed is currently unreachable from our
          server, so per-ladder demand verdicts (PHANTOM / FAMILY-SUPPORTING etc.) cannot
          be computed right now. Refresh in a few minutes — empty results are not
          cached, so the next page load will retry the BLS fetch. The training-ladder
          roster + livable-wage benchmarks below are still informative on their own.
        </div>
      </section>
    );
  }
  const lw = ta.livable_wage_jackson_il;
  const colorFor = (c: string) => c === "good" ? "oklch(45% 0.16 142)" : c === "warn" ? "oklch(48% 0.15 60)" : "oklch(45% 0.20 22)";
  const bgFor = (c: string) => c === "good" ? "oklch(96% 0.04 142)" : c === "warn" ? "oklch(97% 0.04 60)" : "oklch(96% 0.05 22)";
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Training-to-demand alignment · the single-mom test
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Workforce-development theater: grant comes in, training cohort starts, graduates
        hit the labor market — but does the credential they earned have local employers
        to hire them, at wages a single parent can raise two kids on? This cross-references
        every major regional training ladder against (a) actual local sector employment from
        BLS QCEW and (b) the MIT Living Wage benchmark for Jackson County. PHANTOM PIPELINE
        means the credential has nowhere to land locally — graduates relocate, commute, or
        never work in the field.
      </div>

      {/* Livable wage benchmark callout */}
      <div style={{ marginBottom: 20, padding: 14, background: "#fff", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d", marginBottom: 8 }}>
          Livable-wage benchmark · Jackson County, IL (MIT Living Wage Calculator)
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12, fontSize: 13 }}>
          <div>
            <div style={{ color: "#5a564d" }}>Single adult, no kids</div>
            <div style={{ fontSize: 20, fontWeight: 600, color: "#1f1d18" }}>${lw.single_adult_hrly}/hr</div>
            <div style={{ fontSize: 11, color: "#7a756b" }}>${lw.single_adult_wkly.toFixed(0)}/wk · ${(lw.single_adult_wkly * 52 / 1000).toFixed(0)}k/yr</div>
          </div>
          <div>
            <div style={{ color: "#5a564d" }}>1 adult + 2 kids (single-parent family)</div>
            <div style={{ fontSize: 20, fontWeight: 600, color: "oklch(45% 0.20 22)" }}>${lw.family_1a2c_hrly}/hr</div>
            <div style={{ fontSize: 11, color: "#7a756b" }}>${lw.family_1a2c_wkly.toFixed(0)}/wk · ${(lw.family_1a2c_wkly * 52 / 1000).toFixed(0)}k/yr</div>
          </div>
        </div>
        <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b" }}>{lw.source}</div>
      </div>

      {/* Training ladder grid */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
        {ta.ladders.map(l => (
          <div key={l.id} style={{
            background: "white",
            border: `1px solid ${colorFor(l.verdict_color)}33`,
            borderLeft: `6px solid ${colorFor(l.verdict_color)}`,
            borderRadius: 6, padding: 16,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18" }}>{l.name}</div>
                <div style={{ fontSize: 12, color: "#7a756b", marginTop: 2 }}>{l.ladder} · {l.training_duration}</div>
              </div>
              <div style={{
                fontSize: 11, fontWeight: 700, color: "white", background: colorFor(l.verdict_color),
                padding: "5px 10px", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.06em",
                whiteSpace: "nowrap",
              }}>
                {l.verdict}
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 14, marginTop: 12, padding: 12, background: bgFor(l.verdict_color), borderRadius: 4 }}>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>Journey wage</div>
                <div style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18" }}>${l.typical_journey_wage_hrly}/hr</div>
                <div style={{ fontSize: 11, color: "#5a564d" }}>${l.typical_journey_wage_wkly}/wk</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>vs single-adult LW</div>
                <div style={{ fontSize: 16, fontWeight: 600, color: l.vs_single_adult_livable_wkly >= 0 ? "oklch(45% 0.16 142)" : "oklch(45% 0.20 22)" }}>
                  {l.vs_single_adult_livable_wkly > 0 ? "+" : ""}${l.vs_single_adult_livable_wkly}/wk
                </div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>vs family LW (1A+2C)</div>
                <div style={{ fontSize: 16, fontWeight: 600, color: l.vs_family_livable_wkly >= 0 ? "oklch(45% 0.16 142)" : "oklch(45% 0.20 22)" }}>
                  {l.vs_family_livable_wkly > 0 ? "+" : ""}${l.vs_family_livable_wkly}/wk
                </div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>Local sector</div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18" }}>{l.supersector_name}</div>
                <div style={{ fontSize: 11, color: "#5a564d" }}>{l.local_sector_employment.toLocaleString()} jobs ({l.demand_signal})</div>
              </div>
            </div>
            <div style={{ marginTop: 10, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>{l.notes}</div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 12, fontSize: 11, color: "#7a756b", lineHeight: 1.55 }}>{ta.source}</div>
    </section>
  );
}

function TravelJobsSection() {
  // Travel-required family-supporting credentials. Static roster; refresh annually.
  // Wage figures sourced from union scale schedules + BLS OES + expert advisory.
  // Family-compatibility frames distinguish rotational/per-project travel (predictable
  // home time) from OTR trucking (chronic absence) — that's the "FAMILY-TIME
  // CONFLICT" classification on the CDL row of the Training Alignment section.
  type TravelRow = {
    name: string; cred: string; trainSource: string;
    wage_hrly: string; per_diem: string; annual_est: string;
    travel_pattern: string; family_compat: "GOOD" | "OK" | "TOUGH";
    note: string;
  };
  const rows: TravelRow[] = [
    {
      name: "Pipefitter / Steamfitter (UA Local 160)",
      cred: "5yr apprenticeship → journey",
      trainSource: "UA Local 160 (Mt. Vernon) pre-apprenticeship",
      wage_hrly: "$50-65/hr",
      per_diem: "$80-130/day",
      annual_est: "$110-160k+",
      travel_pattern: "Refinery/petrochem/power-plant outages; 4-12wk projects; predictable home weekends",
      family_compat: "OK",
      note: "Outage season concentrates work in spring/fall. Local 160 covers Mt. Vernon → Evansville → St. Louis radius — often within drivable-home range. Top-paying construction trade in the region.",
    },
    {
      name: "Boilermaker (Local 363)",
      cred: "4yr apprenticeship → journey",
      trainSource: "Boilermakers Local 363 pre-apprenticeship",
      wage_hrly: "$40-55/hr",
      per_diem: "$110-150/day",
      annual_est: "$95-140k+",
      travel_pattern: "Power-plant outages, refinery turnarounds; 2-8wk rotations",
      family_compat: "OK",
      note: "Less work as coal plants retire, but nuclear + petrochem outage work is steady. Strong per-diem + travel pay culture.",
    },
    {
      name: "Ironworker (Local 393 Marion)",
      cred: "3-4yr apprenticeship → journey",
      trainSource: "Ironworkers Local 393 (Marion) pre-apprenticeship",
      wage_hrly: "$40-50/hr",
      per_diem: "$80-110/day",
      annual_est: "$90-130k",
      travel_pattern: "Bridge + industrial steel; mix of local + 2-4hr radius projects",
      family_compat: "GOOD",
      note: "Local 393 hall is in Marion. Significant local work (interstate bridges, industrial construction). Travel mostly within driving distance of home.",
    },
    {
      name: "IBEW traveling card (Local 702 + sister locals)",
      cred: "Existing IBEW 702 journey",
      trainSource: "After IBEW Local 702 5yr apprenticeship",
      wage_hrly: "$45-65/hr",
      per_diem: "$100-160/day + truck allowance",
      annual_est: "$120-180k on travel work",
      travel_pattern: "Storm restoration, large industrial projects, data-center builds; varies by 'book' status",
      family_compat: "GOOD",
      note: "IBEW member can travel for higher-wage work when local book is slow. Storm-restoration after hurricanes pays $$$ for 2-6wk deployments. Coming back to home local when work is available.",
    },
    {
      name: "IUOE crane operator (Local 318)",
      cred: "3yr apprenticeship → journey",
      trainSource: "IUOE Local 318 pre-apprenticeship",
      wage_hrly: "$45-60/hr",
      per_diem: "$80-130/day",
      annual_est: "$110-150k",
      travel_pattern: "Wind farms, big construction, refinery outages; project-based",
      family_compat: "OK",
      note: "Local 318 staffed Big Muddy Solar construction (124 MW, Jackson Co.). Same union has wind-farm cranes in IA/TX wind belt — multi-week projects with per-diem.",
    },
    {
      name: "Wind turbine technician",
      cred: "GWO Basic Safety + 2yr AAS or vendor school",
      trainSource: "Highland Community College, Freeport IL or vendor (Vestas/GE/Siemens)",
      wage_hrly: "$28-45/hr base + travel pay",
      per_diem: "$80-130/day on travel work",
      annual_est: "$70-100k with overtime + travel",
      travel_pattern: "IL/IA/KS/TX wind belt; 1-4wk service trips; some rotational O&M (14-on 14-off)",
      family_compat: "OK",
      note: "Operator's note: the CEJA wind tech credential lives here, NOT as a local job. Wind belt is 4-8hr drive from LWA-25. Many techs do rotational shifts that keep half the month at home.",
    },
    {
      name: "Offshore wind technician (East Coast)",
      cred: "GWO + offshore-specific certs",
      trainSource: "GWO-certified school + offshore module",
      wage_hrly: "$35-55/hr + offshore premium",
      per_diem: "Vessel/housing provided + per diem",
      annual_est: "$85-130k",
      travel_pattern: "East Coast offshore wind farms (NY/MA/RI/VA); 2-3wk rotations onshore↔offshore",
      family_compat: "OK",
      note: "Brand-new US industry, exploding demand 2025-2030. Vineyard Wind, Revolution Wind, Sunrise Wind ramping. Rotational schedules = half the year at home.",
    },
    {
      name: "Locomotive engineer / conductor",
      cred: "Class I RR hire-and-train (BNSF/UP/CN/NS)",
      trainSource: "Direct hire by railroad — engineer school is paid",
      wage_hrly: "Starts ~$28/hr, journey $45-60/hr",
      per_diem: "Away-from-home meal allowance",
      annual_est: "$85-130k engineer with seniority",
      travel_pattern: "Pool service — turnaround trips to crew change point + return; not multi-week travel",
      family_compat: "OK",
      note: "Carbondale is on the UP Salem Sub + CN through Du Quoin. Crew terminals at Salem IL + Mounds IL. Schedules are irregular (on-call) but you're home most nights or every other night.",
    },
    {
      name: "Traveling RN (medical)",
      cred: "RN license + 1yr experience",
      trainSource: "ADN/BSN → 1yr at SIH/Memorial → agency contract",
      wage_hrly: "$60-110/hr (blended bill rate)",
      per_diem: "$1,400-2,800/wk lodging/meals stipend",
      annual_est: "$130-200k+ on travel contracts",
      travel_pattern: "13-week assignments anywhere in US; can stack 4×13wk + 8wk home",
      family_compat: "TOUGH",
      note: "Family-compatibility depends on family structure. Single parent traveling = childcare problem. Family staying together (RV family pattern) works. Highest dollar of any 2-yr-credential path.",
    },
    {
      name: "Power plant operator",
      cred: "NUS or vocational certificate + plant training",
      trainSource: "JALC Power Plant Operations program",
      wage_hrly: "$35-55/hr + shift premium",
      per_diem: "Local only (no travel)",
      annual_est: "$80-115k",
      travel_pattern: "Mostly LOCAL — IPP plants in Marion / Vienna / Tuscola hire from LWA-25 directly",
      family_compat: "GOOD",
      note: "Included here because it's family-supporting + uses similar industrial-controls credentialing as travel jobs. JALC's program is one of the strongest in IL. Local plants (Prairie State + several IPPs) have ongoing demand.",
    },
  ];
  const compatTone = (c: string) => c === "GOOD" ? "oklch(45% 0.16 142)" : c === "OK" ? "oklch(48% 0.15 60)" : "oklch(45% 0.20 22)";
  const compatBg = (c: string) => c === "GOOD" ? "oklch(96% 0.04 142)" : c === "OK" ? "oklch(97% 0.04 60)" : "oklch(96% 0.05 22)";
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Travel-required family-supporting opportunities
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Most of the training ladders above land in LOCAL employment. But several
        family-supporting credentials require travel — and the local training
        infrastructure exists to feed them. These pay more than any non-degreed
        local-employment path, often $90k-180k+ all-in. The trade-off is travel,
        but rotational schedules (e.g., 14-on 14-off offshore wind, IBEW project
        rotations, RR pool service) keep significant home time. The page calls
        out CDL OTR separately as &quot;FAMILY-TIME CONFLICT&quot; because long-haul
        trucking is chronic absence rather than rotational; the credentials below
        have better home-time structures.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
        {rows.map((r, i) => (
          <div key={i} style={{
            background: "white", border: `1px solid ${compatTone(r.family_compat)}33`,
            borderLeft: `6px solid ${compatTone(r.family_compat)}`,
            borderRadius: 6, padding: 16,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18" }}>{r.name}</div>
                <div style={{ fontSize: 12, color: "#7a756b", marginTop: 2 }}>
                  {r.cred} · Training: {r.trainSource}
                </div>
              </div>
              <div style={{
                fontSize: 11, fontWeight: 700, color: "white", background: compatTone(r.family_compat),
                padding: "5px 10px", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.06em",
                whiteSpace: "nowrap",
              }}>
                {r.family_compat === "GOOD" ? "FAMILY-FRIENDLY TRAVEL" : r.family_compat === "OK" ? "MANAGEABLE TRAVEL" : "TRAVEL-HEAVY"}
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 14, marginTop: 12, padding: 12, background: compatBg(r.family_compat), borderRadius: 4 }}>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>Wage</div>
                <div style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18" }}>{r.wage_hrly}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>Per diem / travel pay</div>
                <div style={{ fontSize: 13, color: "#1f1d18" }}>{r.per_diem}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>Annual all-in</div>
                <div style={{ fontSize: 15, fontWeight: 600, color: "oklch(35% 0.18 142)" }}>{r.annual_est}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#7a756b", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>Travel pattern</div>
                <div style={{ fontSize: 12, color: "#1f1d18" }}>{r.travel_pattern}</div>
              </div>
            </div>
            <div style={{ marginTop: 10, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>{r.note}</div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 16, padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <strong>The Mantracon / SIWIB strategic gap this fills:</strong> the
        existing CEJA wind technician + CEJA solar installer pipelines suffer
        from local-employer scarcity. But the credentials themselves are real and
        valuable on travel-supported work. Reframing the CEJA cohort outcome from
        &quot;land a local job&quot; to &quot;land a regional travel-pay job with
        predictable home time&quot; changes what success looks like. Pair with
        Big Muddy Solar (which IS hiring local IBEW/IUOE/LIUNA) for the
        local construction work + the broader regional travel circuit for ongoing
        income.
      </div>
      <div style={{ marginTop: 12, fontSize: 11, color: "#7a756b" }}>
        Wage figures are typical journey-out + travel-pay structures sourced from union scale schedules, BLS OES Carbondale-Marion MSA, and the expert advisory. Verify specific opportunities with the named union halls or schools.
      </div>
    </section>
  );
}

function AttractionPipelineSection() {
  // Static expert-derived strategy advisory; no live API needed.
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Anchor-employer attraction pipeline · the realistic targets
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Without new anchor employers paying above the livable-wage threshold, the
        training-alignment problem above can&apos;t be solved by training alone. Current
        large local employers in LWA-25 are concentrated in prisons (Marion FCI, IDOC),
        state agencies + the university (SIU + state university system), large healthcare
        (SIH / Memorial / Marion VA), and the Marion munitions plant (GD-OTS).
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <strong>The honesty caveat on current anchors:</strong> &quot;Large local employer&quot;
        isn&apos;t the same as &quot;family-supporting wages.&quot; The QCEW sector wage shown
        in the Industry Mix section above is an <em>average across all positions</em>
        in that sector — it blends faculty / doctors / executives with support staff /
        IT / clerical. The wage distribution within state agencies and the university
        skews top-heavy. Verify with role-specific data before pitching any specific
        employer as &quot;family-supporting&quot;:{" "}
        <a href="https://salaries.bettergov.org/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f", fontWeight: 600 }}>BetterGov Illinois Public Salaries Database</a>{" "}
        (search by employer and role){" "}·{" "}
        <a href="https://www.bls.gov/oes/current/oes_16060.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f", fontWeight: 600 }}>BLS OES Carbondale-Marion MSA</a>{" "}
        (median wage by occupation, all employers).
        {" "}<strong>The strategic answer is new anchor employers, not asking existing
        anchors to pay more.</strong>
      </div>

      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        To break the wage ceiling we need new anchors — and the realistic target
        list isn&apos;t Google or Microsoft; it&apos;s tier-2 firms hunting stranded
        power, federal agencies with relocation precedent, and university
        research-anchored programs.
      </div>

      {/* Data center attraction scorecard */}
      <h3 style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18", margin: "20px 0 8px 0" }}>
        Data center / hyperscaler attraction scorecard for LWA-25
      </h3>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
        {[
          { factor: "Stranded coal-plant interconnect", grade: "✓ STRONG", note: "Baldwin retirement = ~1,200MW of substation capacity in MISO-South. Ameren IL serves the area. Hyperscalers (and AI-training operators) value stranded-grid sites.", color: "oklch(45% 0.16 142)" },
          { factor: "Power utility — Egyptian Electric as Ameren alternative", grade: "✓ STRONG", note: "Egyptian Electric Cooperative Association (EECA, Murphysboro HQ) serves portions of 10 counties in the LWA-25 footprint and can be approached as an alternative to investor-owned Ameren for industrial-scale power purchase. Member-owned coops typically structure more flexible industrial rates than IOUs. For 100MW+ data-center loads, the wholesale supply comes from EECA's G&T parent (Southern Illinois Power Cooperative) + the MISO market — but EECA is the negotiation counterparty for retail-scale arrangements. The TVA + local-distribution-coop model served Google's Chattanooga DC.", color: "oklch(45% 0.16 142)" },
          { factor: "Local renewable supply pipeline", grade: "✓ EMERGING", note: "Arevon Energy's 124 MW Big Muddy Solar Project (Jackson County, commercial operation end of 2026, $200M private investment) is utility-scale solar feeding the local grid. For data-center recruitment, this is a concrete answer to the 'green PPA?' question — both Ameren-served and EECA-served sites can structure direct or virtual PPAs against Big Muddy generation.", color: "oklch(45% 0.16 142)" },
          { factor: "IL Data Center Investments Act", grade: "✓ STRONG", note: "Public Act 101-0031 — 20-year sales-tax exemption on equipment + property-tax abatement eligible, certified by DCEO. File certification before any RFP arrives.", color: "oklch(45% 0.16 142)" },
          { factor: "Water (cooling)", grade: "✓ STRONG", note: "Crab Orchard NWR, Kinkaid Lake, Mississippi River access. Sufficient for all but the largest installations.", color: "oklch(45% 0.16 142)" },
          { factor: "Land cost", grade: "✓ STRONG", note: "Undervalued vs Northern Virginia, Phoenix, Columbus.", color: "oklch(45% 0.16 142)" },
          { factor: "Power cost — Ameren vs Egyptian Electric Cooperative (EECA) head-to-head", grade: "~ MODERATE", note: "Ameren IL published industrial rate ~$0.08-0.09/kWh. EECA does not publish a comparable industrial-class per-kWh tariff in the same machine-readable way (member-coops negotiate large-power deals bespoke; see eeca.coop/member-services/rate-schedules/). Typical rural-coop industrial rates run 1-2¢/kWh below IOU — call it ~$0.06-0.08/kWh expected range, subject to negotiation. EECA's wholesale supplier Southern Illinois Power Cooperative (SIPC) owns coal + natural-gas generation PHYSICALLY LOCATED in Williamson and Washington counties (inside the LWA-25 footprint), plus long-term contracts for IL solar (White County) + IL wind (Paxton). That's a 'local generation for local load' pitch with minimal transmission distance — Northern VA can't claim that. Neither can compete with NoVa $0.06 on a paper-rate basis, but the bespoke-deal latitude + local-generation story plus the IL Data Center Act sales-tax exemption changes the all-in math.", color: "oklch(48% 0.15 60)" },
          { factor: "Federal IRA Energy Communities adder", grade: "✓ STRONG", note: "Franklin and Perry counties are coal-closure tracts. Solar/wind/storage projects sited here get IRA §48 +10pp ITC bonus on top of 30% base. Use for behind-the-meter generation co-located with DC.", color: "oklch(45% 0.16 142)" },
          { factor: "Fiber diversity", grade: "✗ WEAK", note: "Limited carrier diversity. Need to map FCC Broadband Map carriers and pitch fiber-construction grants alongside any major siting.", color: "oklch(45% 0.20 22)" },
          { factor: "Operations talent (200-person ops staff)", grade: "✗ WEAK", note: "SIU produces some IT capacity but no existing data-center workforce concentration. Mantracon + JALC + Rend Lake would need to stand up a DC-ops training program in parallel to any recruitment.", color: "oklch(45% 0.20 22)" },
        ].map((f, i) => (
          <div key={i} style={{ padding: "10px 0", borderTop: i === 0 ? "none" : "1px solid #ebe5d6", display: "grid", gridTemplateColumns: "1fr auto", gap: 12, alignItems: "baseline" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18" }}>{f.factor}</div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4, lineHeight: 1.5 }}>{f.note}</div>
            </div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "white", background: f.color, padding: "4px 8px", borderRadius: 3, whiteSpace: "nowrap" }}>{f.grade}</div>
          </div>
        ))}
      </div>

      {/* Target list */}
      <h3 style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18", margin: "24px 0 8px 0" }}>
        Realistic target list — recruit these, not those
      </h3>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Tier-2 data centers + AI-training operators</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.7 }}>
            <li><strong>QTS, CyrusOne, Stack Infrastructure, Compass, Aligned</strong> — tier-2 wholesale DC operators</li>
            <li><strong>CoreWeave, Lambda, Crusoe</strong> — AI-training operators explicitly hunting stranded-power sites</li>
            <li><strong>Switch, DataBank</strong> — colocation operators with Midwest expansion appetite</li>
            <li style={{ color: "#7a756b" }}><span style={{ textDecoration: "line-through" }}>Google, Microsoft, AWS, Meta</span> — these go to Loudoun/Phoenix/Columbus. Don&apos;t waste cycles.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Federal agency relocation candidates (short list)</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.7 }}>
            <li><strong>USDA ARS</strong> — agricultural research, SIU College of Ag is the anchor</li>
            <li><strong>USGS</strong> — Mississippi River science / Shawnee NF research</li>
            <li><strong>DOE Office of Fossil Energy &amp; Carbon Management</strong> — coal-country transition mandate</li>
            <li><strong>VA regional facilities expansion</strong> — Marion VA already exists; pitch VBA processing center co-location</li>
            <li>Full playbook + process detail in the <em>Federal agency relocation</em> subsection below.</li>
          </ul>
        </div>
      </div>

      {/* === Federal agency relocation — full playbook === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Federal agency relocation · the actual playbook
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Federal-agency relocation out of DC is real but rare, contentious, and structurally
        different post-2020-pandemic. Two precedents bracket the strategy: USDA ERS/NIFA →
        Kansas City (2019, controversial; retained the agencies) and BLM HQ → Grand Junction
        CO (2019, reversed 2021 after only 41 of 328 staff actually relocated). The lessons
        are unambiguous: <strong>relocation only works when the local site has a real talent
        pool, a credible university anchor, and a multi-year congressional champion. The
        local champion is the lever; everything else is consequence.</strong>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 20 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>What the agency itself evaluates</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Talent supply within driving distance</strong> — land-grant universities + technical colleges that produce the agency&apos;s specific workforce (e.g., USDA ARS wants AG-science PhDs)</li>
            <li><strong>Cost-of-living delta vs DC</strong> — USDA cited this as the #1 cost-driver. Southern IL wins this on paper vs essentially any DC alternative.</li>
            <li><strong>Co-location infrastructure</strong> — existing federal real estate (Marion VA, USACE Rend Lake) lowers the build-out friction.</li>
            <li><strong>Accessibility / connectivity</strong> — air-served (MWA, BLV, EVV), interstate (I-57, I-24, I-64), now Amtrak. The new station improves the case.</li>
            <li><strong>Mission fit with regional industry</strong> — coal-region for DOE FECM, ag-region for USDA ARS, water-systems region for USGS.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>What the local champion must deliver</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Congressional delegation alignment</strong> — IL-12 (Bost), IL senators (Durbin + Duckworth), House Appropriations Ag/Interior/Energy subcommittee allies. Need bipartisan cover for relocations specifically.</li>
            <li><strong>Governor + IL DCEO commitment</strong> — IL DCEO opens-relocate/locate-incentives playbook is the state vehicle. State Capitol-side champion needed.</li>
            <li><strong>SIU institutional partnership letter</strong> — explicit research-collaboration + facilities commitment from SIU as the anchor university (more on this below).</li>
            <li><strong>City + county zoning + utility commitments</strong> — site-ready, utilities provisioned, sales-tax abatement in place.</li>
            <li><strong>Avoid the BLM mistake</strong> — engage employees and unions FROM THE START. The Grand Junction reversal happened because of staff attrition + zero employee consultation.</li>
          </ul>
        </div>
      </div>

      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Named target agencies — what they need + why Southern IL fits
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
          {[
            {
              agency: "USDA ARS — Agricultural Research Service",
              size: "~7,000 staff nationally · ~110 research locations",
              fit_strong: "SIU College of Agricultural, Life & Physical Sciences is the natural anchor. Land-grant proximity (UIUC 3hr, SIU on-site). Ag talent pool. Cost-of-living delta vs DC is steep. Mission fit: row-crop + livestock research relevant to Midwest.",
              process: "Track ARS facility consolidation in the FY budget cycle. ARS has been actively rationalizing 1990s-era locations. Pitch a new regional lab focused on a Southern-IL-specific topic (cover crops, biofuels feedstock, livestock health). USDA cited 'proximity to land-grant universities' as the explicit win criterion in the 2019 KC selection.",
            },
            {
              agency: "USGS — US Geological Survey",
              size: "~8,500 staff · regional water/biology/minerals centers",
              fit_strong: "Mississippi River science is the SIU Center for Fisheries, Aquaculture, and Aquatic Sciences (CFAAS) sweet spot. Shawnee NF biology research already happens here informally. USGS Critical Minerals priority + SIU's existing $200K NSF/DOE grant on extracting rare-earth elements from abandoned coal mines is a perfect bridge.",
              process: "USGS doesn't do big bang relocations like USDA did; they expand existing regional centers when funded. Pitch is an EXPANSION of the existing USGS Illinois Water Science Center presence into Southern IL — co-located with SIU CFAAS + a new critical-minerals satellite tied to coal-mine remediation work.",
            },
            {
              agency: "DOE Office of Fossil Energy and Carbon Management (FECM)",
              size: "Office of ~200 + NETL national lab footprint",
              fit_strong: "Perfect mission fit. Coal-region transition is FECM&apos;s explicit congressional mandate. SIU has the rare-earth coal-mine extraction grant already. Franklin + Perry counties are IRA Energy Communities tracts (10pp ITC bonus). NETL (Morgantown WV + Pittsburgh PA) needs a Midwest field presence; Southern IL is the natural site.",
              process: "Push for an NETL field office (not full FECM HQ relocation — that won&apos;t happen). $5-15M facility, 30-80 staff, SIU faculty partnerships. File through the DOE-tracked Office of Communities (legacy DOE Office of Legacy Management has a similar mission).",
            },
            {
              agency: "USDA Forest Service research — Shawnee NF satellite",
              size: "USFS R&D has ~80 sites; Shawnee is a major Eastern NF",
              fit_strong: "Shawnee NF is the largest forest reservation in IL — 280k acres. USFS Northern Research Station (NRS) already covers IL with offices in Carbondale, IL — formally a NRS station exists at SIU. Expansion is incremental, not relocation.",
              process: "Lower-stakes target: expand the existing NRS Carbondale presence. SIU College of Ag + Forestry program is the anchor. Push for additional research positions tied to forest health / oak decline / fire-on-the-prairie research.",
            },
            {
              agency: "USDA Climate Hub — Midwest regional addition",
              size: "10 regional Climate Hubs nationally · ~25 staff each",
              fit_strong: "Midwest Climate Hub is currently at Iowa State University (Ames). A Southern IL co-location at SIU would extend the Hub's reach into the Ohio River Valley / Lower Midwest ag transition zone — distinct from Iowa's Northern Plains focus.",
              process: "USDA + NOAA partnership; Hub additions happen via Farm Bill appropriations cycle. Frame as 'Lower Mississippi / Ohio Valley Climate Hub'.",
            },
            {
              agency: "VA — VBA processing center expansion at Marion",
              size: "Marion VAMC already operational; add VBA claims processing",
              fit_strong: "Lowest-risk target. Marion VA is already the regional anchor for federal contracting (see Federal Money Concentration section). Adding a VBA (Veterans Benefits Administration) Regional Office or claims-processing center co-locates with existing infrastructure.",
              process: "VBA expansion happens at the appropriations level, not via formal &apos;relocation&apos;. Congressional ask through House Veterans Affairs Committee.",
            },
          ].map((a, i) => (
            <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 4 }}>{a.agency}</div>
              <div style={{ fontSize: 11, color: "#7a756b", marginBottom: 8 }}>{a.size}</div>
              <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 6 }}><strong>Why Southern IL fits:</strong> {a.fit_strong}</div>
              <div style={{ fontSize: 12, color: "#3d3a33" }}><strong>Process:</strong> {a.process}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 12, color: "#3d3a33", lineHeight: 1.55, marginBottom: 24 }}>
        <strong>Post-pandemic-telework caveat:</strong> federal-employee remote work has
        normalized since 2020, which CHANGED what relocation can deliver. Many agencies now
        operate hybrid; physically relocating an HQ no longer forces staff to a specific city.
        The successful play has shifted from "big bang HQ move" to "spin up a new regional
        center / satellite lab in the target city." Lower political cost, higher success
        rate, and you can grow it over time. Plan around the satellite-lab pattern.
      </div>

      {/* === University research-anchored programs === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        University research-anchored federal programs · SIU as the bid vehicle
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        SIU Carbondale is a <strong>Carnegie R1 research university</strong> (top tier of US
        research institutions) — the credential most federal research programs require to
        even compete. SIU is the bid vehicle through which the region can capture
        multi-decade, multi-million-dollar federal research investment that <em>creates
        $80-130k research-staff positions and graduate-student-to-permanent-staff
        pipelines</em>. These are family-supporting STEM jobs that wouldn&apos;t otherwise
        land in LWA-25 — and they&apos;re what the &quot;research-anchored&quot; line of the
        original Anchor Attraction strategy refers to. SIU already wins individual NSF/NIH/
        USDA grants — the strategic move is to win the BIG center-scale programs.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
        {[
          {
            program: "NSF Regional Innovation Engines",
            funding: "Up to $160M over 10 years (Type-2) · $1M / 2yr Type-1 prep grant",
            what: "NSF&apos;s flagship 'transform a region around a technology specialty' program. 29 semifinalists in the 2025 round. Each Engine builds a research-to-commercialization ecosystem around one key technology area.",
            fit: "SIU&apos;s coal-mine rare-earth extraction work + the broader 'critical minerals from legacy coal infrastructure' theme is exactly the kind of differentiated regional bet NSF wants. Other candidate themes: rural broadband + AI agriculture (with UIUC partnership); Mississippi River corridor environmental sensing.",
            process: "Need multi-sector regional coalition: SIU + UIUC + JALC + Rend Lake + Mantracon + IL DCEO + at least 3-5 industry partners. Start with the $1M Type-1 prep grant — apply for Type-2 after 24mo coalition-building.",
            url: "https://www.nsf.gov/funding/initiatives/regional-innovation-engines",
          },
          {
            program: "DOE / NETL — coal-region critical minerals",
            funding: "$5-50M individual grants; up to $200M for major demonstration projects",
            what: "DOE Office of Fossil Energy & Carbon Management funds research on extracting rare-earth elements + critical minerals from coal byproducts (acid mine drainage, fly ash, coal-mine tailings).",
            fit: "SIU already has a $200K seed grant in this exact space. Franklin + Perry + Saline + Williamson counties have hundreds of abandoned coal mines. The substrate is here, the credential is here, the federal mandate is here.",
            process: "Move from $200K seed → multi-million demonstration project → eventual production facility. Critical Materials Innovation Hub partnership is the model; DOE is actively seeking Midwest sites.",
            url: "https://www.energy.gov/fecm",
          },
          {
            program: "USDA Long-Term Agroecosystem Research (LTAR) network",
            funding: "$1-3M/year per site, indefinite duration",
            what: "USDA-ARS network of 18 long-term research sites studying agricultural ecosystems over decades. Each site is staffed with permanent research scientists + technicians.",
            fit: "Southern IL is the transition zone between Corn Belt and Mid-South / Ohio Valley agriculture — under-represented in the LTAR network. SIU's existing crop + soil research could anchor a new site.",
            process: "USDA-ARS proposes new LTAR additions through the Farm Bill cycle. Need SIU faculty PI + multi-decade commitment from the region.",
            url: "https://ltar.ars.usda.gov/",
          },
          {
            program: "NSF Engineering Research Centers (ERC)",
            funding: "$26-32M over 10 years per ERC",
            what: "Multi-university research consortia tackling Convergence Research Challenges. ~30 active ERCs nationally.",
            fit: "SIU would partner with a larger anchor (UIUC, Northwestern, U of Chicago). Possible themes: clean-coal-to-products, rare-earth recovery, agricultural-water remediation.",
            process: "Multi-year coalition building. SIU as one of 3-5 partner institutions; major university would be lead. Apply via NSF ENG directorate solicitations.",
            url: "https://www.nsf.gov/funding/opportunities/erc-engineering-research-centers",
          },
          {
            program: "NIH P30 / P50 Centers — biomedical research",
            funding: "$10-25M over 5 years per center, renewable",
            what: "NIH Institutional Center grants. P30 = Core Center (shared research infrastructure); P50 = Specialized Center (disease-focused research program).",
            fit: "SIU School of Medicine (Springfield campus) is the bid vehicle. Possible themes: rural-health disparities, opioid-epidemic research, telehealth in underserved communities. Aligns with HRSA HPSA designations of Southern IL.",
            process: "PI must have NIH R01 track record + institutional infrastructure. SIU SOM already has NIH-funded labs. Time horizon 18-36mo from concept to award.",
            url: "https://grants.nih.gov/funding/activity-codes",
          },
          {
            program: "ARPA-E — energy moonshots",
            funding: "$3-10M individual awards · 3-yr terms",
            what: "DOE's high-risk / high-reward energy R&D. Smaller per-award but more iterations.",
            fit: "Lower-probability shot but worth filing. Theme alignment: critical minerals + battery storage + carbon management. SIU's coal-byproduct work is competitive.",
            process: "Watch ARPA-E open solicitations 2-3 times/year. SIU PIs apply individually or with industry partner.",
            url: "https://arpa-e.energy.gov/",
          },
          {
            program: "FAA Air Traffic Collegiate Training Initiative (AT-CTI)",
            funding: "Indirect — graduates feed FAA hiring pipeline at premium pay",
            what: "SIU is an AT-CTI partner school. Graduates skip part of the FAA Academy and go to higher starting pay.",
            fit: "Underleveraged. The local feed could be much stronger if Mantracon promoted the pathway.",
            process: "Already in place — push enrollment + retention. FAA controller starting salary is $50-75k, journey $130-180k.",
            url: "https://www.faa.gov/about/office_org/headquarters_offices/ahr/job_opportunities/atc_recruitment",
          },
        ].map((p, i) => (
          <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 16, marginBottom: 4 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18" }}>{p.program}</div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "#1f5f8f", whiteSpace: "nowrap" }}>{p.funding}</div>
            </div>
            <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}><strong>What it is:</strong> {p.what}</div>
            <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}><strong>SIU / regional fit:</strong> {p.fit}</div>
            <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}><strong>Process:</strong> {p.process}</div>
            {p.url && <div style={{ fontSize: 11, marginTop: 4 }}><a href={p.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{p.url} →</a></div>}
          </div>
        ))}
      </div>

      <div style={{ marginTop: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          SIU&apos;s actual current research strengths (what to bid AROUND)
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Coal-region critical minerals</strong> — already has $200K NSF/DOE seed grant on rare-earth extraction from abandoned coal mines. THE differentiated bid theme.</li>
          <li><strong>Mississippi River / aquatic sciences</strong> — SIU Center for Fisheries, Aquaculture, and Aquatic Sciences (CFAAS) is regionally renowned.</li>
          <li><strong>Forestry / forest health</strong> — Shawnee NF adjacent; USFS Northern Research Station already has Carbondale presence.</li>
          <li><strong>Aviation</strong> — SIU Aviation Flight + FAA AT-CTI partnership — underleveraged.</li>
          <li><strong>Agriculture</strong> — College of Agricultural, Life &amp; Physical Sciences — natural USDA partner.</li>
          <li><strong>Medical / rural health</strong> — SIU School of Medicine (Springfield) is the NIH bid vehicle.</li>
          <li><strong>Workforce development research</strong> — partnership with JALC + Rend Lake creates a community-college-research consortium opportunity for DOL grants.</li>
        </ul>
      </div>

      {/* IL programs to file under */}
      <div style={{ marginTop: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Stack these IL state programs in any pitch
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>EDGE Tax Credit</strong> — income-tax credit against new jobs created</li>
          <li><strong>REV Illinois</strong> — electric vehicle / clean-energy specific (applies to battery storage co-located with DC)</li>
          <li><strong>High Impact Business</strong> designation — sales-tax exemption on building materials</li>
          <li><strong>Enterprise Zone</strong> designation — confirm with IL DCEO; Carbondale-Marion area should already have one</li>
          <li><strong>IL Data Center Investments Act</strong> — see scorecard above</li>
          <li><strong>SBA HUBZone</strong> — most LWA-25 census tracts qualify for set-aside boost on federal contracts</li>
          <li><strong>CDFI Capital Magnet Fund + New Markets Tax Credits</strong> — Carbondale &amp; Murphysboro both NMTC-eligible</li>
        </ul>
      </div>

      <div style={{ marginTop: 12, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Source: synthesized from local-BD expert advisory + IL DCEO program documentation. Refresh annually.
      </div>
    </section>
  );
}

function FederalConcentrationSection({ tr }: { tr: TopRecipientsBlock }) {
  if (!tr.recipients.length) return null;
  const top = tr.recipients[0];
  const topAmt = top.amount;
  // Heuristic — flag extreme concentration
  const isConcentrated = tr.top1_share >= 40;
  const formatM = (n: number) =>
    n >= 1_000_000_000 ? `$${(n / 1_000_000_000).toFixed(2)}B`
    : n >= 1_000_000 ? `$${(n / 1_000_000).toFixed(1)}M`
    : `$${(n / 1_000).toFixed(0)}k`;
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Where the federal money actually goes · community-leverage view
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Total federal contract dollars flowing into the 5-county LWA over the
        last {tr.lookback_months} months: <strong>{formatM(tr.total_dollars)}</strong>.
        Place-of-performance filter — these are firms doing the work locally, regardless of
        where they&apos;re headquartered. The asymmetry between federal-dollar flow and
        local-job creation is what gives the workforce board real CBA / apprenticeship /
        supplier-development leverage with the top recipients.
      </div>

      {/* Concentration headline */}
      <div style={{
        background: isConcentrated ? "oklch(96% 0.05 22)" : "#f0ece1",
        border: `1px solid ${isConcentrated ? "oklch(55% 0.20 22)33" : "#d8d2c4"}`,
        borderLeft: `6px solid ${isConcentrated ? "oklch(45% 0.20 22)" : "#5a564d"}`,
        borderRadius: 6, padding: 16, marginBottom: 20,
      }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", color: isConcentrated ? "oklch(40% 0.20 22)" : "#5a564d", marginBottom: 4 }}>
          Concentration · {tr.concentration_label.split("—")[0].trim()}
        </div>
        <div style={{ fontSize: 16, color: "#1f1d18", marginBottom: 8 }}>
          {tr.concentration_label.split("—")[1]?.trim() || tr.concentration_label}
        </div>
        <div style={{ fontSize: 14, color: "#3d3a33" }}>
          Top-1 recipient share: <strong>{tr.top1_share.toFixed(1)}%</strong> · Top-3: <strong>{tr.top3_share.toFixed(1)}%</strong>
        </div>
      </div>

      {/* Recipient table with share bars + SBA status badges */}
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
        {tr.recipients.map((r, i) => {
          const barPct = (r.amount / topAmt) * 100;
          const flag = i === 0 && r.share_pct >= 70;
          const badge = sbaBadge(r.sba_status);
          return (
            <div key={r.name} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6", padding: "12px 14px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span style={{ fontSize: 14, fontWeight: 600, color: flag ? "oklch(45% 0.20 22)" : "#1f1d18" }}>{r.name}</span>
                    {flag && <span style={{ fontSize: 10, padding: "2px 6px", background: "oklch(45% 0.20 22)", color: "white", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 700 }}>DOMINANT</span>}
                    {r.sba_status && r.sba_status !== "UNCLASSIFIED" && (
                      <span style={{ fontSize: 10, padding: "2px 6px", background: badge.bg, color: badge.fg, borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 700, border: `1px solid ${badge.fg}33` }}>
                        {badge.label}
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: "#7a756b", marginTop: 4 }}>
                    {r.share_pct.toFixed(1)}% of all federal contract $ in LWA-25
                    {r.location_tag && <span> · {r.location_tag}</span>}
                    {r.founder_note && <span> · {r.founder_note}</span>}
                  </div>
                  {r.source_url && (
                    <div style={{ fontSize: 11, marginTop: 4 }}>
                      <a href={r.source_url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>certification source →</a>
                    </div>
                  )}
                </div>
                <div style={{ fontSize: 15, fontWeight: 600, color: "#1f5f8f", whiteSpace: "nowrap" }}>{formatM(r.amount)}</div>
              </div>
              <div style={{ marginTop: 6, height: 4, background: "#ebe5d6", borderRadius: 2 }}>
                <div style={{ height: 4, width: `${barPct}%`, background: flag ? "oklch(45% 0.20 22)" : "oklch(45% 0.16 220)", borderRadius: 2 }} />
              </div>
            </div>
          );
        })}
      </div>

      {/* SDVOSB strategic callout — the Marion VA Veterans First story */}
      {tr.sdvosb_summary && tr.sdvosb_summary.count > 0 && (
        <div style={{ marginTop: 20, padding: 16, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            The Marion VA Veterans First contracting story
          </div>
          <div style={{ marginBottom: 10 }}>
            <strong>{tr.sdvosb_summary.count} of the top recipients</strong> in LWA-25 are
            confirmed Service-Disabled Veteran-Owned Small Businesses (SDVOSBs), capturing{" "}
            <strong>{formatM(tr.sdvosb_summary.total_dollars)}</strong> in federal contracts
            ({tr.sdvosb_summary.total_share_pct.toFixed(1)}% of regional total). Marion VA Medical
            Center&apos;s Veterans First Contracting Program is the single biggest non-DoD
            federal procurement channel in the region — and it&apos;s the highest-value SBA
            certification to pursue for any local firm wanting to win this work.
          </div>
          <div style={{ marginBottom: 10 }}>
            <strong style={{ color: "oklch(35% 0.18 22)" }}>The asymmetry:</strong> only{" "}
            <strong>{tr.sdvosb_summary.local_count} of {tr.sdvosb_summary.count}</strong> are
            local to Southern Illinois — the other{" "}
            <strong>{tr.sdvosb_summary.out_of_region_count}</strong> are headquartered in
            Florida, Kentucky, and North Carolina. The set-aside money is flowing, but to
            <em> out-of-region</em> veteran firms because the region doesn&apos;t have enough
            certified <em>local</em> SDVOSBs to absorb the demand.
          </div>
          <div style={{ marginBottom: 4 }}>
            <strong>What Mantracon / SIWIB can do about it:</strong>
          </div>
          <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
            <li>Stand up an &quot;SDVOSB certification on-ramp&quot; with the regional{" "}
              <a href="https://www.sba.gov/local-assistance/find/?type=Veterans%20Business%20Outreach%20Center" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Veterans Business Outreach Center (VBOC)</a>{" "}
              — help local veterans apply for SBA SDVOSB certification + bid for Marion VA work
            </li>
            <li>Partner with{" "}
              <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/sba-mentor-protege-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA Mentor-Protégé Program</a>{" "}
              — pair the existing out-of-region SDVOSBs (Above Group, Jett&apos;s, SDV Office) with local protégés so the work stays here
            </li>
            <li>Smith Hafeli is the proof-of-concept: a local Marion-headquartered SDVOSB winning $11.9M in 24 months. There&apos;s no reason 5-10 more local SDVOSBs couldn&apos;t exist with the right certification support.</li>
          </ul>
        </div>
      )}

      {/* Community leverage callout */}
      <div style={{ marginTop: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What the workforce board can do with this
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Community Benefit Agreement (CBA)</strong> — when a single recipient captures the majority of federal dollars in a region but employs only a fraction of local labor, the workforce board has standing to negotiate hiring commitments, apprenticeship slots, and local supplier-development. Precedents: Intel Ohio, Amazon HQ2 negotiations, Foxconn Wisconsin (revised).</li>
          <li><strong>Apprenticeship pipeline</strong> — federal contractors with prevailing-wage requirements are natural anchors for registered apprenticeships. Partner with the dominant recipient on a Mantracon-hosted pre-apprenticeship for the skill ladders they consume (machinist, electrician, industrial maintenance, quality tech).</li>
          <li><strong>Tier-2 supplier development</strong> — large primes use out-of-region subcontractors. Identify which work could be done by HUBZone-certified local firms (Franklin/Perry/parts-of-Jackson qualify) and broker the relationships.</li>
          <li><strong>Federal contracting set-asides</strong> — the more local firms that show up in this list, the more federal money stays in the regional payroll. SBA HUBZone + 8(a) + WOSB certifications are the on-ramp.</li>
        </ul>
      </div>

      <div style={{ marginTop: 12, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>{tr.source}</div>
    </section>
  );
}

function LaborTruthSection({ lt }: { lt: LaborTruth }) {
  if (!lt.geos.length) return null;
  const agg = lt.aggregate;
  const stateLFPR = lt.benchmarks.il_state_lfpr;
  const stateEP = lt.benchmarks.il_state_ep;
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        The true labor picture · beyond the headline unemployment rate
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The headline unemployment rate only counts people <em>actively looking for work</em>.
        It misses every working-age person who has stopped looking, gone on disability, dropped
        into the cash/informal economy, or is otherwise &quot;not in the labor force.&quot;
        That&apos;s a politician-friendly number — these three metrics tell the real story.
      </div>

      {/* Headline LWA-5 stats vs IL state */}
      {agg && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 16, marginBottom: 24 }}>
          {[
            { label: "Labor force participation", value: `${agg.lfpr}%`, sub: `IL state: ${stateLFPR}% · gap ${agg.gap_lfpr_vs_state > 0 ? "+" : ""}${agg.gap_lfpr_vs_state}pp`, color: agg.gap_lfpr_vs_state < -3 ? "oklch(45% 0.20 22)" : "#1f1d18" },
            { label: "Employment-to-population", value: `${agg.ep_ratio}%`, sub: `IL state: ${stateEP}% · gap ${agg.gap_ep_vs_state > 0 ? "+" : ""}${agg.gap_ep_vs_state}pp`, color: agg.gap_ep_vs_state < -3 ? "oklch(45% 0.20 22)" : "#1f1d18" },
            { label: "Headline UE rate", value: `${agg.ue_rate}%`, sub: "what politicians cite", color: "#1f1d18" },
            { label: "Not in labor force", value: agg.not_in_labor_force.toLocaleString(), sub: `${agg.not_lf_pct}% of working-age — the invisible population`, color: "oklch(45% 0.20 22)" },
          ].map((s, i) => (
            <div key={i} style={{ background: "white", border: `1px solid ${s.color === "#1f1d18" ? "#d8d2c4" : s.color + "33"}`, borderLeft: `6px solid ${s.color}`, borderRadius: 6, padding: 16 }}>
              <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 6 }}>{s.label}</div>
              <div style={{ fontSize: 28, fontWeight: 600, color: s.color, lineHeight: 1.05 }}>{s.value}</div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>{s.sub}</div>
            </div>
          ))}
        </div>
      )}

      {/* Per-county table */}
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#f0ece1", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d" }}>
              <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 600 }}>County</th>
              <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>Pop 16+</th>
              <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>Headline UE</th>
              <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>LFPR</th>
              <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>E/P ratio</th>
              <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>NOT in LF</th>
            </tr>
          </thead>
          <tbody>
            {lt.geos.map((g, i) => {
              const nm = g.name.split(",")[0].replace(" County", "");
              return (
                <tr key={g.fips} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                  <td style={{ padding: "12px 14px", fontWeight: 600 }}>{nm}</td>
                  <td style={{ padding: "12px 14px", textAlign: "right" }}>{g.pop_16plus.toLocaleString()}</td>
                  <td style={{ padding: "12px 14px", textAlign: "right", color: "#5a564d" }}>{g.ue_rate?.toFixed(1)}%</td>
                  <td style={{ padding: "12px 14px", textAlign: "right", color: g.gap_lfpr_vs_state < -5 ? "oklch(45% 0.20 22)" : "#1f1d18", fontWeight: 600 }}>
                    {g.lfpr.toFixed(1)}%<span style={{ fontSize: 11, color: "#7a756b", marginLeft: 4 }}>({g.gap_lfpr_vs_state > 0 ? "+" : ""}{g.gap_lfpr_vs_state}pp)</span>
                  </td>
                  <td style={{ padding: "12px 14px", textAlign: "right", color: g.gap_ep_vs_state < -5 ? "oklch(45% 0.20 22)" : "#1f1d18", fontWeight: 600 }}>
                    {g.ep_ratio.toFixed(1)}%<span style={{ fontSize: 11, color: "#7a756b", marginLeft: 4 }}>({g.gap_ep_vs_state > 0 ? "+" : ""}{g.gap_ep_vs_state}pp)</span>
                  </td>
                  <td style={{ padding: "12px 14px", textAlign: "right" }}>
                    <strong>{g.not_in_labor_force.toLocaleString()}</strong><span style={{ fontSize: 11, color: "#7a756b", marginLeft: 4 }}>({g.not_lf_pct.toFixed(1)}%)</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 12, fontSize: 12, color: "#5a564d", lineHeight: 1.55, maxWidth: 760 }}>
        <strong>How to read this:</strong> The headline unemployment rate stays low because once
        someone stops looking, they vanish from the math. LFPR + E/P ratio capture the entire
        working-age population (16+) including everyone not currently job-searching. The
        &quot;NOT in LF&quot; column is the closest legitimate count of the invisible population
        — people not employed, not unemployed-by-official-definition, not in school.
        IL state benchmark: LFPR {stateLFPR}% · E/P {stateEP}%. US national: LFPR {lt.benchmarks.us_national_lfpr}% · E/P {lt.benchmarks.us_national_ep}%.
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b" }}>{lt.source}</div>
    </section>
  );
}

async function fetchData(): Promise<MantraconData | null> {
  try {
    const res = await fetch(`${API_BASE}/api/public/mantracon`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as MantraconData;
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

function urTone(ur: number | null | undefined): Tone {
  if (ur == null) return "ok";
  if (ur < 4) return "good";
  if (ur < 6) return "ok";
  if (ur < 8) return "warn";
  return "bad";
}

function fmtNum(n: number): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function fmtMoney(n: number): string {
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

const COUNTY_LABELS: Record<string, string> = {
  jackson: "Jackson (Carbondale, Murphysboro)",
  franklin: "Franklin (Benton, West Frankfort)",
  jefferson: "Jefferson (Mt. Vernon)",
  perry: "Perry (Du Quoin, Pinckneyville)",
  williamson: "Williamson (Marion, Herrin, Carterville)",
};

function CountyTable({ d }: { d: MantraconData }) {
  const counties = ["jackson", "franklin", "jefferson", "perry", "williamson"];
  return (
    <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
        <thead>
          <tr style={{ background: "#f0ece1", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d" }}>
            <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 600 }}>County</th>
            <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>Unemployment</th>
            <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600 }}>Labor Force</th>
            <th style={{ textAlign: "right", padding: "10px 14px", fontWeight: 600, width: 110 }}>As of</th>
          </tr>
        </thead>
        <tbody>
          {counties.map((c, i) => {
            const ur = d.indicators[`crb_${c}_unemployment_rate`];
            const lf = d.indicators[`crb_${c}_labor_force`];
            const tone = urTone(ur?.value);
            return (
              <tr key={c} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                <td style={{ padding: "12px 14px" }}>
                  <div style={{ fontWeight: 600, color: "#1f1d18" }}>{c.charAt(0).toUpperCase() + c.slice(1)} County</div>
                  <div style={{ fontSize: 12, color: "#7a756b" }}>{COUNTY_LABELS[c]}</div>
                </td>
                <td style={{ padding: "12px 14px", textAlign: "right", fontWeight: 600, color: TONE_COLOR[tone] }}>
                  {ur ? `${ur.value.toFixed(1)}%` : "—"}
                </td>
                <td style={{ padding: "12px 14px", textAlign: "right", color: "#1f1d18" }}>
                  {lf ? fmtNum(lf.value) : "—"}
                </td>
                <td style={{ padding: "12px 14px", textAlign: "right", fontSize: 12, color: "#7a756b" }}>
                  {ur ? ageOf(ur.date) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function URTrendChart({ series }: { series: Array<{ date: string; value: number }> }) {
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
  return (
    <svg viewBox="0 0 800 260" preserveAspectRatio="none" style={{ width: "100%", height: 260 }}>
      <line x1="0" y1={lineY(4)} x2="800" y2={lineY(4)} stroke="oklch(55% 0.16 142)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(4) - 5} fill="oklch(50% 0.16 142)" fontSize="11" fontFamily="ui-sans-serif">Full-employment · 4%</text>
      <line x1="0" y1={lineY(6)} x2="800" y2={lineY(6)} stroke="oklch(58% 0.15 60)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(6) - 5} fill="oklch(50% 0.15 60)" fontSize="11" fontFamily="ui-sans-serif">Watch · 6%</text>
      <polyline fill="none" stroke="oklch(45% 0.16 220)" strokeWidth="2" points={pts} />
      {tickIdxs.map(idx => {
        const p = series[idx]; if (!p) return null;
        const x = (idx / Math.max(1, series.length - 1)) * 780 + 10;
        const dt = new Date(p.date).toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
        return (
          <g key={idx}>
            <line x1={x} y1="220" x2={x} y2="226" stroke="#8a857c" strokeWidth="0.5" />
            <text x={x} y="245" fill="#5a564d" fontSize="11" fontFamily="ui-sans-serif" textAnchor="middle">{dt}</text>
          </g>
        );
      })}
    </svg>
  );
}

function IndustryMixByCountySection({ mix }: { mix: IndustryMix }) {
  if (!mix.by_county || mix.by_county.length === 0) return null;
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Industry mix by county
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 760 }}>
        Each county in the LWA-25 has a different economic identity. This drilldown
        shows the top employers-by-NAICS-supersector inside each county so board
        members representing a specific jurisdiction can see their county's
        story — and so workforce strategy can be tailored county-by-county
        rather than averaged across the region.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
        {mix.by_county.map(c => {
          const maxEmp = Math.max(...c.top_supersectors.map(s => s.employment));
          return (
            <div key={c.fips} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
                <h3 style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18", margin: 0 }}>{c.name} County</h3>
                <div style={{ fontSize: 12, color: "#7a756b" }}>FIPS 17{c.fips}</div>
              </div>
              <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 12 }}>
                Total covered employment: <strong>{c.total_employment.toLocaleString()}</strong>
              </div>
              {c.top_supersectors.map((s, i) => {
                const barPct = (s.employment / maxEmp) * 100;
                return (
                  <div key={s.code} style={{ paddingTop: i === 0 ? 0 : 8, borderTop: i === 0 ? "none" : "1px solid #ebe5d6", marginTop: i === 0 ? 0 : 8 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                      <div style={{ color: "#1f1d18", fontWeight: 500 }}>{s.name}</div>
                      <div style={{ color: "#5a564d" }}>{s.employment.toLocaleString()} · ${s.avg_weekly_wage}/wk</div>
                    </div>
                    <div style={{ height: 3, background: "#ebe5d6" }}>
                      <div style={{ height: 3, width: `${barPct}%`, background: "oklch(45% 0.16 220)" }} />
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
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
        Industry mix · who actually employs people in {scope}
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 760 }}>
        Total covered employment by NAICS supersector — the single best view of
        where regional jobs actually are. Wages shown are the QCEW average
        weekly wage across all ownerships in that sector. Use this to (a) bias
        WIOA training cohorts to high-employment + high-wage sectors,
        (b) identify sectors where wages signal employer competition for talent,
        and (c) recognize what sectors a new employer would be slotting into.
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

function BusinessLeadsSection({ b }: { b: BusinessOps }) {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Business lead opportunities · federal contracts
      </h2>
      <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 760 }}>
        Where federal dollars are already flowing into the 5-county LWA. Use these
        sectors to (a) target employer recruitment that matches existing federal
        demand, (b) align WIOA training cohorts to the in-demand NAICS codes, and
        (c) help local primes find subcontracting opportunities at SAM.gov.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
        <div>
          <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
            Top NAICS in LWA-25 (last {b.totals.lookback_months} months)
          </h3>
          {b.top_naics.length === 0 ? (
            <div style={{ color: "#7a756b", fontSize: 13 }}>No NAICS data returned by USAspending for this period.</div>
          ) : (
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
              {b.top_naics.map((n, i) => (
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
            Largest federal awards · place-of-performance LWA-25
          </h3>
          {b.top_awards.length === 0 ? (
            <div style={{ color: "#7a756b", fontSize: 13 }}>No federal contract awards in this 5-county window.</div>
          ) : (
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
              {b.top_awards.slice(0, 8).map((a, i) => (
                <div key={i} style={{
                  padding: "10px 14px", borderTop: i === 0 ? "none" : "1px solid #ebe5d6", fontSize: 13,
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                    <div style={{ fontWeight: 600, color: "#1f1d18", flex: 1 }}>{a.recipient || "—"}</div>
                    <div style={{ fontWeight: 600, color: "#1f5f8f", whiteSpace: "nowrap" }}>{fmtMoney(a.amount)}</div>
                  </div>
                  <div style={{ fontSize: 12, color: "#5a564d", marginTop: 2 }}>{a.agency || "—"}</div>
                  {a.description && (
                    <div style={{ fontSize: 12, color: "#7a756b", marginTop: 4 }}>{a.description}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div style={{ marginTop: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33" }}>
        <strong style={{ color: "#1f1d18" }}>Where to go for live opportunities:</strong>
        <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
          <li>
            <a href={b.sam_gov_search_link} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>
              SAM.gov active opportunities filtered to Illinois →
            </a>{" "}
            (sort by closing date; export to share with local primes)
          </li>
          <li>
            <a href="https://www.usaspending.gov/state/Illinois" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>
              USAspending — Illinois detail
            </a>{" "}
            (deep historical view to find prime-contractor relationships in the region)
          </li>
          <li>
            <a href="https://www.sba.gov/funding-programs/contracting-assistance-programs" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>
              SBA contracting-assistance programs (HUBZone, 8(a), WOSB)
            </a>{" "}
            — Franklin, Perry & parts of Jackson Co. carry HUBZone status
          </li>
        </ul>
      </div>
    </section>
  );
}

export default async function MantraconPage() {
  const data = await fetchData();
  if (!data) {
    return (
      <html lang="en"><body style={{ fontFamily: "system-ui", padding: 40, color: "#5a564d" }}>
        Sorry — the workforce-board data feed isn&apos;t responding right now. Try again in a minute.
      </body></html>
    );
  }
  const ag = data.lwa_aggregate;
  // Drive headline from LFPR gap to IL state — captures the full picture of
  // labor utilization, not just U-3 unemployment which masks discouraged workers.
  // The labor_truth section below makes this concrete; the headline should
  // agree with that synthesis, not contradict it.
  const lfprGap = data.labor_truth?.aggregate?.gap_lfpr_vs_state ?? null;
  let tone: Tone = "ok";
  let headline = "LWA-25 Workforce Snapshot";
  if (lfprGap != null) {
    if (lfprGap >= 0)        { tone = "good"; headline = `Strong regional labor market`; }
    else if (lfprGap >= -3)  { tone = "ok";   headline = `Healthy regional labor market`; }
    else if (lfprGap >= -6)  { tone = "warn"; headline = `Softening regional labor market`; }
    else                     { tone = "bad";  headline = `Structurally weak regional labor market`; }
  }

  return (
    <html lang="en">
      <head>
        <title>Man-Tra-Con · SIWIB · LWA-25 Workforce Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
        <style>{`
          :root { color-scheme: light; }
          * { box-sizing: border-box; }
          html, body { margin: 0; padding: 0; background: #f7f5f1; color: #1f1d18; font-family: "IBM Plex Sans", system-ui, sans-serif; line-height: 1.5; }
          a { color: #1f5f8f; }
          .container { max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }
        `}</style>
      </head>
      <body>
        <div className="container">
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/logo-icon.svg" alt="Packet Void Labs" width={28} height={28} />
            <div style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.08em", color: "#8a857c" }}>
              Man-Tra-Con · SIWIB · LWA-25 Workforce Dashboard
            </div>
          </div>
          <h1 style={{ fontSize: 44, fontWeight: 600, lineHeight: 1.05, margin: "8px 0 8px 0", color: TONE_COLOR[tone] }}>
            {headline}
          </h1>
          <div style={{ fontSize: 17, color: "#3d3a33", maxWidth: 760 }}>
            {lfprGap != null && ag.unemployment_rate_weighted != null ? (
              <>
                Headline UE rate <strong>{ag.unemployment_rate_weighted.toFixed(1)}%</strong> looks fine — but labor-force participation runs <strong>{Math.abs(lfprGap).toFixed(1)}pp below Illinois</strong>. The headline misses everyone who has stopped looking. See the true labor picture below.
              </>
            ) : (
              "Five-county Southern Illinois Workforce Development Board service area (Franklin, Jackson, Jefferson, Perry, Williamson)."
            )}
          </div>
          <div style={{ fontSize: 12, color: "#8a857c", marginTop: 8 }}>
            Page rendered {data.ts.slice(0, 16).replace("T", " ")} UTC. Workforce metrics from BLS LAUS via FRED, monthly (1-2 month lag). Federal awards from USAspending.gov.
          </div>

          <div style={{ marginTop: 16, padding: 14, background: "#fff", border: "1px solid #d8d2c4", borderRadius: 6 }}>
            <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d", marginBottom: 8 }}>
              Data freshness · each block live-fetched on every page load
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10, fontSize: 12 }}>
              <div><strong>BLS LAUS labor market:</strong><br /><span style={{ color: "#5a564d" }}>through {data.indicators?.crb_jackson_unemployment_rate?.date ?? "—"} · refreshes monthly</span></div>
              <div><strong>BLS QCEW industry mix:</strong><br /><span style={{ color: "#5a564d" }}>{data.industry_mix?.as_of_quarter ?? "—"} · refreshes quarterly (~7mo lag)</span></div>
              <div><strong>Census ACS labor utilization:</strong><br /><span style={{ color: "#5a564d" }}>{data.labor_truth?.year ?? "2023"} 5-year estimates · refreshes annually (Dec)</span></div>
              <div><strong>Federal awards (USAspending):</strong><br /><span style={{ color: "#5a564d" }}>{data.business_opportunities?.totals?.lookback_months ?? 24}-month rolling · refreshes continuously</span></div>
            </div>
          </div>

          <section style={{ marginTop: 32 }}>
            <h2 style={{ fontSize: 20, fontWeight: 600, margin: "0 0 12px 0", color: "#1f1d18" }}>
              County-by-county labor market
            </h2>
            <CountyTable d={data} />
          </section>

          {data.lwa_unemployment_series.length > 0 && (
            <section style={{ marginTop: 32 }}>
              <h2 style={{ fontSize: 20, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
                LWA-25 weighted unemployment · last 5 years
              </h2>
              <div style={{ fontSize: 13, color: "#5a564d", marginBottom: 12 }}>
                Labor-force-weighted average across the 5 counties. Calculated from BLS LAUS monthly data — the same series each county council uses.
              </div>
              <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
                <URTrendChart series={data.lwa_unemployment_series} />
              </div>
            </section>
          )}

          {data.labor_truth && <LaborTruthSection lt={data.labor_truth} />}

          {data.industry_mix && <IndustryMixSection mix={data.industry_mix} scope="the LWA-25 (5-county region)" />}

          {data.industry_mix && <IndustryMixByCountySection mix={data.industry_mix} />}

          <BusinessLeadsSection b={data.business_opportunities} />

          {data.top_federal_recipients && <FederalConcentrationSection tr={data.top_federal_recipients} />}

          {data.training_alignment && (
            <TrainingAlignmentSection
              ta={data.training_alignment}
              industryMixAvailable={!!data.industry_mix?.top_supersectors?.length}
            />
          )}

          <TravelJobsSection />

          <AttractionPipelineSection />

          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              How a board member can move on this
            </h2>
            <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 760 }}>
              Concrete next steps the data above supports.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
              {[
                {
                  title: "Align WIOA training to in-demand NAICS",
                  body: "The top-NAICS list above shows where federal dollars are already buying labor in the LWA. Bias annual WIOA training-cohort planning toward credentials that map to those NAICS codes — graduates land in sectors with active local demand instead of speculative future hires.",
                },
                {
                  title: "Recruit second-tier primes",
                  body: "Largest-awards list identifies primes already winning in the LWA. Ask staff to flag which ones use out-of-region subs; that's the wedge for a HUBZone-status local sub to pitch as a tier-2.",
                },
                {
                  title: "CEJA clean-energy alignment",
                  body: "Man-Tra-Con's $2.3M CEJA grant trains residents for clean-energy jobs. Cross-reference EPA / DOE / USDA Rural Energy awards above against the credentialing pipeline — the graduates need somewhere to land.",
                },
                {
                  title: "Coordinate with city pages",
                  body: (
                    <>
                      <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale →</a>{" "}
                      and{" "}
                      <a href="/murphysboro" style={{ color: "#1f5f8f", fontWeight: 600 }}>Murphysboro →</a>{" "}
                      share the Jackson County substrate with city-specific housing, hardship,
                      and federal-awards framing.{" "}
                      <a href="/market" style={{ color: "#1f5f8f", fontWeight: 600 }}>US Market Health →</a>{" "}
                      for the national macro backdrop.
                    </>
                  ),
                },
              ].map((c, i) => (
                <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
                  <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>{c.title}</div>
                  <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>{c.body}</div>
                </div>
              ))}
            </div>
          </section>

          <div style={{ marginTop: 40, fontSize: 12, color: "#8a857c", lineHeight: 1.6 }}>
            <strong>Sources:</strong> County labor-market data — US Bureau of Labor
            Statistics Local Area Unemployment Statistics (LAUS) via the St. Louis
            Fed (FRED). Federal contract awards — USAspending.gov (Treasury / OMB).
            SAM.gov for active solicitations. SBA HUBZone & 8(a) program info from sba.gov.
            <br /><br />
            <strong>Coverage:</strong> LWA-25 = Franklin, Jackson, Jefferson, Perry,
            Williamson. This is the Southern Illinois Workforce Development Board
            (SIWIB) service area as administered by Man-Tra-Con Corp.,
            3117 Civic Circle Boulevard, Suite B, Marion, IL 62959.
            <br /><br />
            <strong>Caveats:</strong> Monthly BLS LAUS series are 1-2 months lagged.
            USAspending federal-awards data reflects what has been reported by
            agencies — there is reporting lag, and prime-award place-of-performance
            does not capture subcontract flow.
          </div>
        </div>
      </body>
    </html>
  );
}
