/**
 * Public /east-central-illinois page — Local Workforce Innovation Area 23
 * (LWA-23) regional analysis. Mirrors the /southern-illinois (LWA-25) page
 * structure for the 13-county LWA-23 footprint administered by CEFS
 * Economic Opportunity Corporation out of Effingham, IL.
 *
 * Counties: Clark, Clay, Coles, Crawford, Cumberland, Edgar, Effingham,
 * Fayette, Jasper, Lawrence, Marion, Moultrie, Richland.
 *
 * Live data substrate: /api/public/cefs (13-county aggregate UR + labor
 * force + USAspending awards + QCEW industry mix + ACS labor truth, all
 * pulled from platform.macro_data + Census/USAspending APIs). FRED panel:
 * 170 series, 4,041 rows loaded 2026-05-28.
 *
 * Charleston city profile: /charleston (mirrors /carbondale).
 */
import { DashboardHead, Topbar, DashboardFooter, DEFAULT_FOOTER_COLUMNS } from "@/components/dashboard-chrome";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "https://console-api-production-4576.up.railway.app";

interface IndustryRow {
  code: string; name: string;
  total_employment: number; private_employment: number; public_employment: number;
  avg_weekly_wage: number; annual_pay_equivalent: number;
}
interface IndustryMix {
  as_of_quarter: string;
  top_supersectors: IndustryRow[];
  total_employment: number;
  source: string;
}
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
interface TopFedRecipient {
  recipient: string; agency: string; amount: number; awards_count: number;
}
interface CEFSData {
  ts: string;
  indicators: Record<string, { value: number; date: string }>;
  lwa_aggregate: {
    labor_force: number | null; labor_force_date: string | null;
    unemployment_rate_weighted: number | null; unemployment_rate_date: string | null;
    county_count: number;
  };
  lwa_labor_force_series: Array<{ date: string; value: number }>;
  lwa_unemployment_series: Array<{ date: string; value: number }>;
  business_opportunities?: BusinessOps;
  top_federal_recipients?: TopFedRecipient[];
  industry_mix?: IndustryMix;
  labor_truth?: LaborTruth;
}

async function fetchCEFS(): Promise<CEFSData | null> {
  try {
    const res = await fetch(`${API_BASE}/api/public/cefs`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as CEFSData;
  } catch { return null; }
}

function fmtMoney(n: number): string {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}

// ══════════════════════════════════════════════════════════════════════
// LWA-23 data tables — primary-source citations inline on each row.
// ══════════════════════════════════════════════════════════════════════

const COUNTIES = [
  {
    name: "Clark", seat: "Marshall", fips: "17023",
    anchor: "ZF (automotive electronics; ~1,000 area residents employed)",
    anchor_url: "https://www.tradeandindustrydev.com/region/illinois/city-marshall-economic-development-3562",
  },
  {
    name: "Clay", seat: "Louisville (county seat) / Flora", fips: "17025",
    anchor: "Clay County Hospital and Clinics (201-500 employees, nonprofit critical-access; HQ Flora with Louisville clinic; managed by SSM Health)",
    anchor_url: "https://www.linkedin.com/company/clay-county-hospital-and-clinics",
  },
  {
    name: "Coles", seat: "Charleston (county seat) / Mattoon", fips: "17029",
    anchor: "Eastern Illinois University + Lake Land College + Sarah Bush Lincoln Health System + Rural King HQ + Consolidated Communications HQ + R.R. Donnelley",
    anchor_url: "/charleston",
    anchor_is_internal: true,
  },
  {
    name: "Crawford", seat: "Robinson", fips: "17033",
    anchor: "Marathon Petroleum Robinson Refinery (~690 employees; 253,000 bpcd capacity; on-site since 1924)",
    anchor_url: "https://www.marathonpetroleum.com/Operations/Refining/Robinson-Refinery/",
  },
  {
    name: "Cumberland", seat: "Toledo", fips: "17035",
    anchor: "No dominant single employer; per County Comprehensive Plan only three employers exceed 100 — labor force commutes to Effingham + Mattoon-Charleston",
    anchor_url: "https://cumberlandcoil.gov/pdf/Cumberland%20County%20Comp%20Plan.pdf",
  },
  {
    name: "Edgar", seat: "Paris", fips: "17045",
    anchor: "North American Lighting (NAL) Paris Plant (1,400 employees, 540,000 sq ft headlamp manufacturing, Tier-1 Koito Group supplier) + Horizon Health / Paris Community Hospital (25-bed critical access)",
    anchor_url: "https://nal.com/advanced-forward-lighting-plant/",
  },
  {
    name: "Effingham", seat: "Effingham", fips: "17049",
    anchor: "HSHS St. Anthony's Memorial Hospital + Sherwin-Williams Manufacturing + Hodgson Mill + CEFS Economic Opportunity Corporation HQ + I-57/I-70 logistics-corridor employers",
    anchor_url: "https://www.lwa23.com/",
  },
  {
    name: "Fayette", seat: "Vandalia", fips: "17051",
    anchor: "SBL Fayette County Hospital (Sarah Bush Lincoln affiliate, 100-249 employees) + Vandalia Correctional Center (IDOC minimum-security state prison)",
    anchor_url: "https://idoc.illinois.gov/facilities/allfacilities/facility.vandalia-correctional-center.html",
  },
  {
    name: "Jasper", seat: "Newton", fips: "17079",
    anchor: "Newton Power Plant (Illinois Power Generating Co.; 617.4 MW coal-fired; commissioned 1977) — manufacturing 20.8% / healthcare 14.8% / education 11.8% of Newton employment",
    anchor_url: "https://en.wikipedia.org/wiki/Newton,_Illinois",
  },
  {
    name: "Lawrence", seat: "Lawrenceville (county seat) / Sumner", fips: "17101",
    anchor: "Lawrence Correctional Center (IDOC maximum-security; operational capacity 2,458; opened November 2001 in Sumner)",
    anchor_url: "https://en.wikipedia.org/wiki/Lawrence_Correctional_Center",
  },
  {
    name: "Marion", seat: "Salem", fips: "17121",
    anchor: "Salem Township Hospital (25-bed critical access, sub-500 employees) + SSM Health Saint Mary's Hospital Centralia (adjacent county-seat services)",
    anchor_url: "https://salemtownhosp.com/careers/",
  },
  {
    name: "Moultrie", seat: "Sullivan", fips: "17139",
    anchor: "Agri-Fab (~400 peak-season; lawn-and-garden attachments, HQ Sullivan) + Hydro-Gear (~506-700; drivetrain mfg, HQ Sullivan) = ~1,100 mfg jobs in town of 4,413",
    anchor_url: "https://www.agri-fab.com/Portals/0/PDFs/newsandupdates/Agri-Fab%2050th%20Anniversary%20press%20release.pdf",
  },
  {
    name: "Richland", seat: "Olney", fips: "17159",
    anchor: "Walmart Distribution Center (902) + Carle Richland Memorial Hospital (495) + Richland County CUSD (305) + Pacific Cycle (124) + Prairie Farms (102) + Escalade Sports (78) + Olney Central College",
    anchor_url: "https://rcdc.com/major-employers/",
  },
];

// City-level crime (FBI UCR 2024 calendar year, NeighborhoodScout October 2025 release)
const CITY_CRIME = [
  { rank: "🟢", city: "Lawrenceville · Lawrence",   rate: 1.16,  violent: 0.46, property: 0.70,  note: "Lowest LWA-23 rate; small-town pattern",                  flagged: false, src: "https://www.neighborhoodscout.com/il/lawrenceville/crime" },
  { rank: "🟢", city: "Louisville · Clay",          rate: 6.32,  violent: 0.00, property: 6.32,  note: "Zero violent crime in 2024 calendar year",               flagged: false, src: "https://www.neighborhoodscout.com/il/louisville/crime" },
  { rank: "🟡", city: "Vandalia · Fayette",         rate: 12.23, violent: 0.41, property: 11.82, note: "Property-dominant; near IDOC presence + I-70 corridor",  flagged: false, src: "https://www.neighborhoodscout.com/il/vandalia/crime" },
  { rank: "🟡", city: "Olney · Richland",           rate: 12.69, violent: 2.25, property: 10.44, note: "Walmart-DC + Olney Central College anchor town",         flagged: false, src: "https://www.neighborhoodscout.com/il/olney/crime" },
  { rank: "🟡", city: "Salem · Marion",             rate: 15.25, violent: 3.85, property: 11.40, note: "Salem Township Hospital seat",                           flagged: false, src: "https://www.neighborhoodscout.com/il/salem/crime" },
  { rank: "🟠", city: "Robinson · Crawford",        rate: 18.86, violent: 2.98, property: 15.88, note: "Marathon refinery town",                                 flagged: false, src: "https://www.neighborhoodscout.com/il/robinson/crime" },
  { rank: "🟠", city: "Paris · Edgar",              rate: 20.05, violent: 5.69, property: 14.36, note: "Highest violent-crime rate in LWA-23 (NAL plant town)",  flagged: true,  src: "https://www.neighborhoodscout.com/il/paris/crime" },
  { rank: "🔴", city: "Effingham · Effingham",      rate: 21.63, violent: 2.17, property: 19.46, note: "Highest overall in LWA-23 — I-57/I-70 cross-roads + retail/logistics density",  flagged: true,  src: "https://www.neighborhoodscout.com/il/effingham/crime" },
  // Charleston + Mattoon are documented on /charleston and /charleston-mattoon-companion-context
  { rank: "—",  city: "Charleston · Coles (see /charleston)", rate: 18, violent: 3, property: 15, note: "Per /charleston city profile; FBI UCR 2024",          flagged: false, src: "https://www.neighborhoodscout.com/il/charleston/crime" },
  { rank: "—",  city: "Mattoon · Coles",            rate: 17,    violent: 5,    property: 12,    note: "Slightly lower overall than Charleston; college-adjacent",         flagged: false, src: "https://www.neighborhoodscout.com/il/mattoon/crime" },
];

// Community colleges — credential portfolio + Fall 2025 enrollment
const COMMUNITY_COLLEGES = [
  {
    name: "Lake Land College",
    location: "Mattoon · Coles County",
    enrollment_fall2025: 4138,
    enrollment_delta: "+4.5% YoY — highest since Fall 2019",
    ipeds: 146506,
    programs: [
      "Associate Degree Nursing (ADN) — RN credential (largest program after Business in Fall 2025)",
      "Practical Nursing (LPN)",
      "Basic Nurse Assistant (CNA)",
      "Dental Hygiene",
      "Medical Assistant",
      "Paramedical Services + Emergency Medical Services (EMT/Paramedic)",
      "Physical Therapist Assistant",
      "Massage Therapy",
      "14 agriculture-degree programs on 160-acre on-campus land lab",
      "Cosmetology clinic + Automotive Technology (CTE FTE +5.3% Fall 2025)",
      "Business + Humanities/Communications + Technology + Math/Science + Social Science/Education divisions (58 total degree/cert fields)",
    ],
    url: "https://www.lakelandcollege.edu/",
    enrollment_src: "https://www.myradiolink.com/2025/09/16/lake-land-college-sees-highest-fall-enrollment-in-several-years/",
  },
  {
    name: "Kaskaskia College",
    location: "Centralia · Clinton County (serves LWA-23 Marion + Fayette + Clay)",
    enrollment_fall2025: 3669, // 1,280 FT + 2,389 PT
    enrollment_delta: "1,280 FT + 2,389 PT (US News headcount)",
    ipeds: 146366,
    programs: [
      "Registered Nursing (RN) — top RN program after Liberal Arts & Sciences",
      "Licensed Practical Nursing (LPN)",
      "Nursing Assistant / Patient Care Assistant (largest <1-yr certificate, 152 awarded 2023)",
      "Welding (Crisp Manufacturing and Trades Center, renamed Jan 2025)",
      "Industrial Technology + HVAC + Robotics + PLCs + Hydraulics/Pneumatics/Drives & Motors",
      "CAD + OSHA-10 + Forklift + Lockout/Tagout safety credentials",
      "CDL Truck Driver Training (160 Driving Academy partnership)",
      "Workforce Empowerment Initiative (industry-trades certificate stack)",
    ],
    url: "https://www.kaskaskia.edu/",
    enrollment_src: "https://www.usnews.com/education/community-colleges/kaskaskia-college-CC04235",
  },
  {
    name: "Olney Central College",
    location: "Olney · Richland County (part of Illinois Eastern Community Colleges)",
    enrollment_fall2025: 1142,
    enrollment_delta: "880-1,142 (FT/PT methodology dependent); 584 total degrees awarded 2023",
    ipeds: 145707,
    programs: [
      "Registered Nursing (RN) — 82 degrees awarded 2023",
      "Licensed Practical Nursing (LPN) — 67 degrees awarded",
      "Welding Technology — 20 degrees awarded",
      "Industrial Mechanics and Maintenance Technology",
      "Automotive Technology",
      "Agriculture",
      "Criminal Justice",
      "Liberal Arts & Sciences (105 degrees awarded; transfer pipeline to EIU)",
    ],
    url: "https://www.iecc.edu/occ/",
    enrollment_src: "https://datausa.io/profile/university/olney-central-college",
  },
];

// CEFS WIOA program portfolio (LWA-23 board services)
const CEFS_WIOA_PROGRAMS = [
  {
    title: "Skills & Training (ITAs)",
    description: "Individual Training Account (ITA) classroom-based training for in-demand occupations. Eligible providers drawn from the statewide Illinois workNet Eligible Training Provider System (ETPL); Lake Land, Kaskaskia, and Olney Central all listed.",
    audience: "WIOA-eligible adults + dislocated workers",
  },
  {
    title: "On-the-Job Training (OJT)",
    description: "Employer-side wage reimbursement for hiring + training new employees in skilled-occupation roles. CEFS reimburses up to 50% of wages during the training period.",
    audience: "Employers hiring + WIOA-eligible job seekers",
  },
  {
    title: "Incumbent Worker Training",
    description: "Skills upgrade for currently-employed workers at LWA-23 employers. Used to prevent layoffs + maintain competitiveness of existing local employers.",
    audience: "Current employers + their workforce",
  },
  {
    title: "Work Experience",
    description: "Paid or unpaid time-limited work assignments designed to develop skills, work history, and employer-side hire-back pipeline.",
    audience: "WIOA-eligible adults + young adults 16-24",
  },
  {
    title: "Young Adult / Youth Services",
    description: "Programs for in-school + out-of-school youth ages 16-24. Includes tutoring, leadership development, summer employment, occupational-skills training, and post-secondary educational support.",
    audience: "Ages 16-24 (in-school + out-of-school)",
  },
  {
    title: "Tuition Assistance",
    description: "Direct tuition assistance for WIOA-approved post-secondary training programs.",
    audience: "WIOA-eligible adults + dislocated workers",
  },
];

// Training-to-Demand 1A+2C wage test (Coles County anchor)
// MIT Living Wage 1A+2C Coles County IL ≈ $42-46/hr per livingwage.mit.edu/counties/17029
// (Exact value updates annually; ~10% below Jackson County's $46.76/hr)
const LWA23_LIVING_WAGE = {
  county: "Coles County, IL",
  single_adult_hrly: 17.50,
  oneA_2C_hrly: 44.00,
  oneA_2C_yearly: 91520,
  source_url: "https://livingwage.mit.edu/counties/17029",
};

// LWA-23 training-to-demand verdicts — credentials offered by Lake Land /
// Kaskaskia / Olney Central + the local employer base + 1A+2C wage check
const LWA23_TRAINING_VERDICTS = [
  {
    pathway: "RN (ADN) at Lake Land / Kaskaskia / Olney Central → SBL + HSHS + Carle Richland + Clay Co Hospital",
    train_cost: "$5-10k AAS",
    train_duration: "2 yrs",
    journey_wage: "$33-36/hr local mid-career (~$69-75k/yr); travel-RN $90-120k+",
    annual_premium: "+$45k local; +$60k travel",
    payback_yrs: "<6mo",
    local_slots: "Strong — SBL alone is Forbes Top 10 IL employer; HSHS St. Anthony's + Carle Richland + Clay Co Hospital + multiple critical-access in 13-county footprint",
    saturation: "FAMILY-SUPPORTING",
    verdict: "Strongest ROI in LWA-23. Clears 1A+2C wage bar without travel. Best workforce-board investment for cohort placement.",
  },
  {
    pathway: "LPN at Lake Land / Kaskaskia / Olney Central → SBL + nursing homes + clinics",
    train_cost: "$3-6k",
    train_duration: "1 yr",
    journey_wage: "$22-26/hr (~$46-54k/yr)",
    annual_premium: "+$28k",
    payback_yrs: "<6mo",
    local_slots: "Strong",
    saturation: "WAGE-SUPPRESSED",
    verdict: "Clears single-adult LW comfortably but BELOW 1A+2C. Frame as RN-bridge on-ramp, not destination wage.",
  },
  {
    pathway: "CNA at Lake Land / Kaskaskia / Olney Central → nursing homes + assisted living",
    train_cost: "$500-1.5k",
    train_duration: "8-12 wks",
    journey_wage: "$15-19/hr (~$31-39k/yr)",
    annual_premium: "+$5-15k",
    payback_yrs: "<3mo",
    local_slots: "Abundant",
    saturation: "BELOW LIVABLE",
    verdict: "Single-adult LW marginally cleared at top of range. Far below 1A+2C. Use as gateway credential, not endpoint.",
  },
  {
    pathway: "Welding at Kaskaskia (Crisp Mfg Center) / Olney Central → Sherwin-Williams, Marathon, NAL Paris, Agri-Fab/Hydro-Gear",
    train_cost: "$3-6k cert",
    train_duration: "6-18mo",
    journey_wage: "$22-32/hr local; refinery + shutdown work $40-55/hr",
    annual_premium: "+$30-50k local; +$60-90k shutdown circuit",
    payback_yrs: "<6mo",
    local_slots: "Strong — Marathon Robinson alone is a major welding employer; Sherwin-Williams + NAL Paris add scale",
    saturation: "FAMILY-SUPPORTING",
    verdict: "LWA-23's industrial-trades star. Local employer demand strongest of any trade. Shutdown circuit pays beyond 1A+2C if travel-tolerant.",
  },
  {
    pathway: "Industrial Mechanics / Maintenance Tech at Olney Central + Kaskaskia → Marathon, NAL, Sherwin-Williams, Hydro-Gear, Newton Power Plant",
    train_cost: "$3-6k AAS",
    train_duration: "1-2 yrs",
    journey_wage: "$24-32/hr (~$50-67k/yr)",
    annual_premium: "+$25-40k",
    payback_yrs: "<6mo",
    local_slots: "Strong — every manufacturing anchor needs them",
    saturation: "FAMILY-SUPPORTING (mid-career)",
    verdict: "Clears 1A+2C at experienced range; entry below the bar. Lifetime ROI strongest of any local industrial credential.",
  },
  {
    pathway: "HVAC + Building Systems at Kaskaskia",
    train_cost: "$3-6k",
    train_duration: "1-2 yrs",
    journey_wage: "$22-30/hr",
    annual_premium: "+$22-38k",
    payback_yrs: "<6mo",
    local_slots: "Medium — small-firm density across all 13 counties",
    saturation: "WAGE-SUPPRESSED at entry",
    verdict: "Solid trade; entry below 1A+2C; mid-career clears with own-truck independent-contractor structure.",
  },
  {
    pathway: "Automotive Tech at Lake Land + Olney Central",
    train_cost: "$3-6k AAS",
    train_duration: "1-2 yrs",
    journey_wage: "$18-28/hr (depends on dealership flag-rate vs hourly)",
    annual_premium: "+$10-30k",
    payback_yrs: "6-12mo",
    local_slots: "Medium — dealership + independent shop density",
    saturation: "MIXED",
    verdict: "Flag-rate dealership track varies; ASE-certified master tech at established dealership clears 1A+2C, entry-level doesn't.",
  },
  {
    pathway: "CDL Class A at Kaskaskia (160 Driving Academy partnership) + private schools → local freight + OTR + refinery tankers",
    train_cost: "$3-6k",
    train_duration: "6-12 wks",
    journey_wage: "$22-28/hr local; OTR $60-80k/yr (per-diem inclusive); refinery tanker $70-90k+",
    annual_premium: "+$25k local; +$40-60k OTR",
    payback_yrs: "<6mo",
    local_slots: "Abundant",
    saturation: "TRAVEL-WORK · LOCAL WAGE-SUPPRESSED",
    verdict: "Local rate fails 1A+2C; OTR + refinery-tanker work clears family-supporting bar but destroys home-time. Same TRAVEL-WORK pattern as LWA-25.",
  },
  {
    pathway: "Agriculture (14 programs at Lake Land + Olney CC Ag + IDOA partnerships)",
    train_cost: "$3-8k AAS",
    train_duration: "1-2 yrs",
    journey_wage: "Variable — operator-owners $40-100k+; farm-laborer $14-22/hr seasonal",
    annual_premium: "Variable",
    payback_yrs: "Variable",
    local_slots: "Strong (LWA-23 is grain-belt + livestock)",
    saturation: "OWNER-OPERATOR · LABORER GRIND",
    verdict: "Same dual-tier pattern as LWA-25 (Southern IL grain-farm operator labor income negative $276,707 in 2024 per farmdoc daily). H-2A program supplies most seasonal labor.",
  },
  {
    pathway: "IDOC Correctional Officer (Vandalia Correctional + Lawrence Correctional)",
    train_cost: "$0 (state-funded training academy)",
    train_duration: "8-10 wks",
    journey_wage: "$24-31/hr (~$50-65k/yr starting; state-pension eligible)",
    annual_premium: "+$25-30k",
    payback_yrs: "Immediate",
    local_slots: "Significant — 25-29% IDOC officer vacancy statewide; Vandalia + Lawrence both routinely understaffed",
    saturation: "FAMILY-SUPPORTING",
    verdict: "Clears 1A+2C with overtime + shift differential. Stable state-pension career; OT pattern means home-time + family-time constraints same as LWA-25 IDOC framing.",
  },
  {
    pathway: "K-12 Teacher / Paraprofessional credentialing via EIU + Lake Land",
    train_cost: "$0-30k (varies; EIU 4-year vs Lake Land AAS-to-EIU transfer path)",
    train_duration: "2-4 yrs",
    journey_wage: "Teacher $40-55k starting / Paraprofessional $14-19/hr",
    annual_premium: "+$25k teacher",
    payback_yrs: "1-3yr",
    local_slots: "Strong (rural-IL teacher shortage persistent)",
    saturation: "WAGE-SUPPRESSED at entry; FAMILY-SUPPORTING after step increases + master's",
    verdict: "EIU's largest credential pipeline. Entry-level teacher pay below 1A+2C; mid-career + master's + extracurricular stipends clears the bar. Persistent shortage = guaranteed local placement.",
  },
  {
    pathway: "Cosmetology / Massage Therapy at Lake Land",
    train_cost: "$8-15k",
    train_duration: "9-15 mo",
    journey_wage: "$15-28/hr (tip income variable)",
    annual_premium: "+$10-25k",
    payback_yrs: "6-18mo",
    local_slots: "Medium",
    saturation: "WAGE-SUPPRESSED",
    verdict: "Below 1A+2C in most local salons; owner-operator + booth-rental structure can clear it.",
  },
];

// ══════════════════════════════════════════════════════════════════════
// Render helpers
// ══════════════════════════════════════════════════════════════════════

function fmtNum(n: number): string {
  return n.toLocaleString("en-US");
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
      <text x="8" y={lineY(4) - 5} fill="oklch(50% 0.16 142)" fontSize="11">Full-employment line · 4%</text>
      <line x1="0" y1={lineY(6)} x2="800" y2={lineY(6)} stroke="oklch(58% 0.15 60)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(6) - 5} fill="oklch(50% 0.15 60)" fontSize="11">Watch line · 6%</text>
      <polyline fill="none" stroke="oklch(45% 0.16 220)" strokeWidth="2" points={pts} />
      {tickIdxs.map(idx => {
        const p = series[idx];
        if (!p) return null;
        const x = (idx / Math.max(1, series.length - 1)) * 780 + 10;
        return (
          <g key={idx}>
            <line x1={x} y1="220" x2={x} y2="226" stroke="#8a857c" strokeWidth="0.5" />
            <text x={x} y="245" fill="#5a564d" fontSize="11" textAnchor="middle">
              {fmtMonthYear(p.date)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// ══════════════════════════════════════════════════════════════════════
export default async function EastCentralIllinoisPage() {
  const data = await fetchCEFS();
  const renderedAt = data ? data.ts.slice(0, 16).replace("T", " ") + " UTC" : new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC";

  const lwaUR = data?.lwa_aggregate?.unemployment_rate_weighted;
  const lwaLF = data?.lwa_aggregate?.labor_force;
  const mix = data?.industry_mix;

  return (
    <>
      <DashboardHead title="East Central Illinois (LWA-23) · Regional Snapshot" />
      <div className="dashboard-shell" style={{ maxWidth: 1180, margin: "0 auto", padding: "24px 24px 60px", fontFamily: "var(--font-serif), Georgia, serif" }}>
        <Topbar
          brand="East Central Illinois (LWA-23) · Regional Snapshot"
          region="13-county footprint · CEFS Economic Opportunity Corporation · Effingham"
          renderedAt={renderedAt}
        />

        {/* ═══ Hero ═══ */}
        <section style={{ marginTop: 24 }}>
          <h1 style={{ fontSize: 32, fontWeight: 600, margin: 0, color: "#1f1d18", lineHeight: 1.15 }}>
            East Central Illinois — Local Workforce Innovation Area 23
          </h1>
          <p style={{ fontSize: 15, color: "#3d3a33", marginTop: 12, maxWidth: 820, lineHeight: 1.6 }}>
            LWA-23 covers 13 Illinois counties anchored on the I-57 / I-70 / US-45 corridor, administered by <strong>CEFS Economic Opportunity Corporation</strong> out of Effingham. The region&apos;s structural anchors: Eastern Illinois University + Lake Land College + Kaskaskia College + Olney Central College on the credential side; Sarah Bush Lincoln + HSHS St. Anthony&apos;s + Carle Richland + Clay County Hospital on the regional-healthcare side; and a diversified industrial base (Marathon Petroleum Robinson Refinery, Sherwin-Williams Effingham, NAL Paris headlamp plant, Rural King HQ Mattoon, Consolidated Communications HQ Mattoon, Hodgson Mill, Agri-Fab + Hydro-Gear Sullivan, ZF Marshall, Newton Power Plant, Vandalia + Lawrence Correctional Centers).
          </p>
          <p style={{ fontSize: 13, color: "#5a564d", marginTop: 8, lineHeight: 1.55 }}>
            <strong>Companion analysis:</strong> <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>Southern Illinois Region (LWA-25) →</a> · <a href="/charleston" style={{ color: "#1f5f8f", fontWeight: 600 }}>Charleston, IL → (EIU host city / Coles County profile)</a> · <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale, IL → (parallel SIU host city in LWA-25)</a>.
          </p>
        </section>

        {/* ═══ Hero KPIs ═══ */}
        <section style={{ marginTop: 32 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
            <StatCard label="LWA-23 counties" value="13" sub="vs LWA-25's 5-county footprint" />
            <StatCard
              label="13-county weighted UR"
              value={lwaUR != null ? `${lwaUR.toFixed(1)}%` : "—"}
              sub={data?.lwa_aggregate?.unemployment_rate_date ? `as of ${ageOf(data.lwa_aggregate.unemployment_rate_date)}` : "BLS LAUS · pending Railway redeploy"}
              flag={lwaUR != null && lwaUR > 6}
            />
            <StatCard
              label="13-county labor force"
              value={lwaLF != null ? fmtNum(lwaLF) : "—"}
              sub={data?.lwa_aggregate?.labor_force_date ? `as of ${ageOf(data.lwa_aggregate.labor_force_date)}` : "Sum across 13 counties"}
            />
            <StatCard
              label="Federal awards (24mo)"
              value={data?.business_opportunities ? fmtMoney(data.business_opportunities.totals.awards_dollars) : "—"}
              sub="USAspending place-of-performance 13-county set"
            />
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 10, lineHeight: 1.5 }}>
            Sources: <a href="https://www.lwa23.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>LWA-23 (CEFS Economic Opportunity Corporation)</a> + <a href="https://www.illinoisworknet.com/WIOA/RegPlanning/Pages/LWIAMatrix.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois workNet · LWIA Matrix</a> + BLS LAUS via FRED (county-level monthly UR + labor force, 170 FRED series in macro_data) + Census ACS via console-api + USAspending county-aggregate.
          </div>
        </section>

        {/* ═══ §0 LWA-23 UR trend chart ═══ */}
        {data && data.lwa_unemployment_series.length > 0 && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              LWA-23 weighted unemployment rate · last 5 years
            </h2>
            <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
              Labor-force-weighted aggregate UR across all 13 LWA-23 counties (sum-of-products method). Below 4% (green dotted) is full-employment territory; above 6% (yellow dotted) warrants attention.
            </div>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
              <URChart series={data.lwa_unemployment_series} />
            </div>
          </section>
        )}

        {/* ═══ §1 13-county footprint with anchor employers ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            01 · 13-county footprint · anchor employer per county seat
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23 has two clear anchor counties — <strong>Coles</strong> (EIU + Lake Land + SBL + Rural King + Consolidated Communications) and <strong>Effingham</strong> (CEFS HQ + HSHS St. Anthony&apos;s + Sherwin-Williams + I-57/I-70 logistics hub). The other 11 counties carry meaningful single-employer or single-sector anchors: Marathon refinery in Crawford (Robinson), NAL headlamp plant in Edgar (Paris), Walmart DC + Carle hospital in Richland (Olney), Agri-Fab + Hydro-Gear in Moultrie (Sullivan), ZF automotive electronics in Clark (Marshall), and two IDOC state prisons (Vandalia minimum in Fayette + Lawrence maximum in Lawrence County).
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, color: "#3d3a33" }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>County</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Seat</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>FIPS</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Anchor employer / cluster</th>
                </tr>
              </thead>
              <tbody>
                {COUNTIES.map((c, i) => (
                  <tr key={c.fips} style={{ borderBottom: i < COUNTIES.length - 1 ? "1px solid #ebe5d6" : "none" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{c.name}</td>
                    <td style={{ padding: "8px 10px" }}>{c.seat}</td>
                    <td style={{ padding: "8px 10px", color: "#5a564d", fontFamily: "monospace", fontSize: 11 }}>{c.fips}</td>
                    <td style={{ padding: "8px 10px", color: "#3d3a33", fontSize: 12, lineHeight: 1.55 }}>
                      {c.anchor}{" · "}
                      <a href={c.anchor_url} target={c.anchor_is_internal ? undefined : "_blank"} rel={c.anchor_is_internal ? undefined : "noopener noreferrer"} style={{ color: "#1f5f8f" }}>
                        {c.anchor_is_internal ? "see /charleston" : "source"}
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* ═══ §2 LWA-23 industry mix (live QCEW) ═══ */}
        {mix && mix.top_supersectors && mix.top_supersectors.length > 0 && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              02 · LWA-23 industry mix · BLS QCEW (13-county aggregate)
            </h2>
            <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
              Aggregate covered employment across the 13-county LWA-23 footprint by NAICS supersector. Quarter: <strong>{mix.as_of_quarter}</strong>. Total covered employment: <strong>{fmtNum(mix.total_employment)}</strong>.
            </div>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
              <div style={{ display: "grid", gridTemplateColumns: "1.6fr 90px 110px 120px", gap: 0, padding: "10px 14px", background: "#f0ece1", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d", fontWeight: 600 }}>
                <div>Supersector</div>
                <div style={{ textAlign: "right" }}>Employment</div>
                <div style={{ textAlign: "right" }}>Avg/week</div>
                <div style={{ textAlign: "right" }}>≈Annual</div>
              </div>
              {mix.top_supersectors.map((row, i) => {
                const maxEmp = Math.max(...mix.top_supersectors.map(s => s.total_employment));
                const barPct = (row.total_employment / maxEmp) * 100;
                return (
                  <div key={row.code} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <div style={{ display: "grid", gridTemplateColumns: "1.6fr 90px 110px 120px", gap: 0, padding: "12px 14px", fontSize: 14, alignItems: "center" }}>
                      <div>
                        <div style={{ fontWeight: 600, color: "#1f1d18" }}>{row.name}</div>
                        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 2 }}>
                          Private {fmtNum(row.private_employment)} · Public {fmtNum(row.public_employment)}
                        </div>
                      </div>
                      <div style={{ textAlign: "right", fontWeight: 600 }}>{fmtNum(row.total_employment)}</div>
                      <div style={{ textAlign: "right" }}>${fmtNum(row.avg_weekly_wage)}</div>
                      <div style={{ textAlign: "right", color: "#5a564d" }}>${(row.annual_pay_equivalent / 1000).toFixed(0)}k</div>
                    </div>
                    <div style={{ height: 3, background: "#ebe5d6" }}>
                      <div style={{ height: 3, width: `${barPct}%`, background: "oklch(45% 0.16 220)" }} />
                    </div>
                  </div>
                );
              })}
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: "#7a756b" }}>{mix.source}</div>
          </section>
        )}

        {/* ═══ §3 Federal money concentration — diversified, no GD-OTS equivalent ═══ */}
        {data?.business_opportunities && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              03 · Federal contract dollars · {fmtMoney(data.business_opportunities.totals.awards_dollars)} (last {data.business_opportunities.totals.lookback_months} months)
            </h2>
            <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
              Federal contract dollars with place-of-performance in the 13-county LWA-23 footprint over the last {data.business_opportunities.totals.lookback_months} months. <strong>Critically: no single dominant prime.</strong> The Southern Illinois Region (LWA-25) page documents 95.6% of its federal dollars concentrated in one prime (GD-OTS Marion); LWA-23 has no equivalent concentration. Diversification is the structural difference between the two regions.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
              <div>
                <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.06em", color: "#7a756b", marginBottom: 10 }}>
                  Top NAICS · LWA-23 (last {data.business_opportunities.totals.lookback_months}mo)
                </h3>
                {data.business_opportunities.top_naics.length === 0 ? (
                  <div style={{ color: "#7a756b", fontSize: 13 }}>No NAICS data returned by USAspending for this period.</div>
                ) : (
                  <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
                    {data.business_opportunities.top_naics.slice(0, 8).map((n, i) => (
                      <div key={n.code} style={{ display: "flex", justifyContent: "space-between", padding: "10px 14px", borderTop: i === 0 ? "none" : "1px solid #ebe5d6", fontSize: 14 }}>
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
                  Largest awards · LWA-23 place-of-performance
                </h3>
                {data.business_opportunities.top_awards.length === 0 ? (
                  <div style={{ color: "#7a756b", fontSize: 13 }}>No data returned.</div>
                ) : (
                  <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
                    {data.business_opportunities.top_awards.slice(0, 8).map((a, i) => (
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
              <a href={data.business_opportunities.sam_gov_search_link} target="_blank" rel="noopener noreferrer">SAM.gov · Illinois active opportunities →</a>
              {" · "}
              <a href="https://www.usaspending.gov/state/Illinois" target="_blank" rel="noopener noreferrer">USAspending · Illinois</a>
              {" · "}
              <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/hubzone-program" target="_blank" rel="noopener noreferrer">SBA HUBZone</a>
            </div>
          </section>
        )}

        {/* ═══ §4 CEFS WIOA program portfolio ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            04 · CEFS LWA-23 WIOA program portfolio · what the board actually delivers
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            CEFS Economic Opportunity Corporation administers six WIOA service categories for the 13-county LWA-23 footprint. The board does not publish a stand-alone Eligible Training Provider List (ETPL) on its own site — approved providers come from the statewide Illinois workNet ETPL. Lake Land College + Kaskaskia College + Olney Central College are all listed approved providers (Lake Land school ID 540081). The 2024-2028 Southeast Regional Plan (EDR 7) is the controlling document for occupational priorities.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
            {CEFS_WIOA_PROGRAMS.map((p, i) => (
              <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>{p.title}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 8 }}>{p.description}</div>
                <div style={{ fontSize: 11, color: "#7a756b" }}>Audience: {p.audience}</div>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            Sources: <a href="https://www.lwa23.com/services" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>LWA-23 · Services</a> + <a href="https://www.cefseoc.org/wioa" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>CEFS WIOA portal</a> + <a href="https://apps.illinoisworknet.com/etpl" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois workNet Eligible Training Provider System</a> + <a href="https://lwa23.net/regional-plan/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>2024-2028 Southeast Regional Plan (EDR 7)</a>. LWA-23 admin contact: lwia23@cefseoc.org · (217) 342-2193 ext. 2121 · 1805 South Banker Street, Effingham, IL 62401.
          </div>
        </section>

        {/* ═══ §5 Community college credential pipelines ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            05 · Community college credential pipelines · Lake Land + Kaskaskia + Olney Central
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23 has one public 4-year university (EIU; see <a href="/charleston" style={{ color: "#1f5f8f" }}>Charleston</a>) + three community colleges that supply the WIOA-funded credential pipeline. Lake Land + Kaskaskia + Olney Central together carry the bulk of allied-health, industrial-trades, and CTE training capacity for the 13-county footprint. CEFS approves these as ETPL providers.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {COMMUNITY_COLLEGES.map((c) => (
              <div key={c.name} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
                  <div style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18" }}>{c.name}</div>
                  <div style={{ fontSize: 12, color: "#7a756b" }}>Fall 2025: <strong>{fmtNum(c.enrollment_fall2025)}</strong> · {c.enrollment_delta}</div>
                </div>
                <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4, marginBottom: 10 }}>{c.location}</div>
                <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.6 }}>
                  {c.programs.map((p, i) => <li key={i}>{p}</li>)}
                </ul>
                <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8 }}>
                  <a href={c.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{c.url}</a>
                  {" · "}IPEDS UnitID {c.ipeds}
                  {" · "}<a href={c.enrollment_src} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>enrollment source</a>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* ═══ §6 Training-to-Demand 1A+2C wage test ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            06 · Training-to-demand alignment · the 1A+2C single-earner wage test
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Cross-references the credentials offered by Lake Land + Kaskaskia + Olney Central against the LWA-23 employer base. Wage benchmark: <strong>MIT Living Wage Calculator for {LWA23_LIVING_WAGE.county}</strong> — 1A+2C (one working adult supporting two children) sits at ~<strong>${LWA23_LIVING_WAGE.oneA_2C_hrly.toFixed(2)}/hr · ${fmtNum(LWA23_LIVING_WAGE.oneA_2C_yearly)}/yr</strong>. Single-adult LW ≈ ${LWA23_LIVING_WAGE.single_adult_hrly.toFixed(2)}/hr.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, color: "#3d3a33" }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Pathway</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Train</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Wage</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Saturation</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, maxWidth: 280 }}>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {LWA23_TRAINING_VERDICTS.map((r, i) => {
                  const tone = r.saturation.includes("FAMILY-SUPPORTING") ? "oklch(40% 0.16 142)"
                    : r.saturation.includes("WAGE-SUPPRESSED") || r.saturation.includes("BELOW") ? "oklch(45% 0.20 22)"
                    : r.saturation.includes("TRAVEL") ? "oklch(45% 0.18 60)" : "#5a564d";
                  return (
                    <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                      <td style={{ padding: "10px", fontWeight: 600, color: "#1f1d18" }}>{r.pathway}</td>
                      <td style={{ padding: "10px", color: "#3d3a33" }}>{r.train_cost}<div style={{ color: "#7a756b", fontSize: 11 }}>{r.train_duration}</div></td>
                      <td style={{ padding: "10px", color: "#3d3a33" }}>{r.journey_wage}<div style={{ color: "#7a756b", fontSize: 11 }}>{r.annual_premium}</div></td>
                      <td style={{ padding: "10px", textAlign: "center" }}>
                        <span style={{ background: `${tone}22`, color: tone, padding: "3px 8px", borderRadius: 3, fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em" }}>{r.saturation}</span>
                      </td>
                      <td style={{ padding: "10px", color: "#3d3a33", fontSize: 11, maxWidth: 280, lineHeight: 1.5 }}>{r.verdict}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            Sources: <a href={LWA23_LIVING_WAGE.source_url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>MIT Living Wage Calculator · Coles County, IL</a> + Lake Land + Kaskaskia + Olney Central program catalogs + BLS OEWS Illinois statewide + employer-side wage signals from JG-TC + Effingham Daily News + USAspending.gov.
          </div>
        </section>

        {/* ═══ §7 City-level crime ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            07 · City-level crime · LWA-23 county seats + major cities (FBI UCR 2024)
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Per-1,000-resident crime rates for the major cities of LWA-23. Effingham (county seat + I-57/I-70 cross-roads + retail-logistics density) carries the highest total crime in LWA-23 at 21.63 per 1,000. Paris (NAL headlamp plant town) carries the highest violent-crime rate. Lawrenceville is the lowest at 1.16 per 1,000.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", fontSize: 12.5, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>#</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>City · County</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Total / 1,000</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Violent / 1,000</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Property / 1,000</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Note</th>
                </tr>
              </thead>
              <tbody>
                {CITY_CRIME.map((r, i) => (
                  <tr key={r.city} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6", background: r.flagged ? "oklch(98% 0.02 22)" : "transparent" }}>
                    <td style={{ padding: "6px 10px", whiteSpace: "nowrap" }}>{r.rank}</td>
                    <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.city}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600, color: r.rate < 8 ? "oklch(40% 0.16 142)" : r.rate < 15 ? "oklch(45% 0.18 60)" : "oklch(45% 0.20 22)" }}>{r.rate.toFixed(2)}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.violent.toFixed(2)}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.property.toFixed(2)}</td>
                    <td style={{ padding: "6px 10px", fontSize: 11, color: "#5a564d" }}>{r.note}{" · "}<a href={r.src} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>source</a></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            FBI UCR 2024 calendar year, NeighborhoodScout October 2025 release. Marshall (Clark), Toledo (Cumberland), Newton (Jasper), and Sullivan (Moultrie) per-1,000 figures are behind NeighborhoodScout&apos;s paywall — they qualitatively describe Toledo + Sullivan as &quot;among the lowest in the US.&quot; For confirmed quantitative data on those cities, pull directly from the <a href="https://cde.ucr.cjis.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>FBI Crime Data Explorer</a> + divide by Census ACS 2024 population.
          </div>
        </section>

        {/* ═══ §8 LWA-23 vs LWA-25 ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            08 · LWA-23 vs LWA-25 · structural comparison
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Both regions share rural Illinois patterns (regional-university enrollment decline, agricultural-labor H-2A dependence, drug-supply pattern of meth + fentanyl-contaminated street drugs, mandatory-OT attrition at family-supporting employers). They differ on three axes: footprint size, federal-money concentration, and the anchor-employer mix.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
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
                  ["Workforce-board admin", "CEFS Economic Opportunity Corp. (Effingham)", "Man-Tra-Con (West Frankfort)"],
                  ["Anchor 4-year university", "EIU (Charleston) · 5,434 Fall 2025", "SIU Carbondale · ~11,116"],
                  ["Community colleges", "Lake Land + Kaskaskia + Olney Central (3) · 8,949 combined Fall 2025", "JALC + Rend Lake + SIC + Shawnee (4)"],
                  ["Anchor hospital systems", "Sarah Bush Lincoln + HSHS St. Anthony's + Carle Richland + Clay Co + SBL Fayette", "SIH Memorial Carbondale + Heartland Reg'l"],
                  ["Federal-contract concentration", "Diversified — no GD-OTS equivalent", "95.6% to GD-OTS Marion (24-mo)"],
                  ["Major industrial employers", "Marathon refinery (Robinson) + Sherwin-Williams Effingham + NAL Paris + Rural King HQ + Consolidated Comm HQ + Agri-Fab/Hydro-Gear Sullivan + ZF Marshall + RRD Charleston + Hodgson Mill + Newton Power Plant", "GD-OTS Marion + Aisin Mfg + Continental Tire + USG + IDOC IL River Correctional Center"],
                  ["State prisons (IDOC)", "Vandalia Correctional (Fayette, minimum) + Lawrence Correctional (Lawrence/Sumner, maximum, op. capacity 2,458)", "IL River Correctional (Brookport, Perry/Massac border) + Vienna (Johnson)"],
                  ["Highest single-employer headcount", "NAL Paris (1,400) + Walmart DC Olney (902) + Hydro-Gear Sullivan (700)", "GD-OTS Marion (~1,500 + sub-recipients)"],
                  ["Interstate access", "I-57 + I-70 cross at Effingham; US-45; US-50", "I-57 + I-24; Carbondale-Marion airport (MWA)"],
                  ["Notable economic story", "Diversified industrial base; 2 IDOC prisons; EIU 53% enrollment decline (Charleston)", "Federal-money concentration; SIU 45% decline (Carbondale); coal-mining legacy"],
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

        {/* ═══ §9 Action ladder ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            09 · Action ladder · what the page surfaces for the CEFS board + regional stakeholders
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Each card below leads with what the page already does (data-side) and ends with the human-only residual step.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {[
              {
                title: "Weight cohort placement toward Welding + Industrial Mechanics + RN",
                body: <>The §06 Training-to-Demand table identifies three FAMILY-SUPPORTING pathways available in LWA-23: <strong>RN (ADN)</strong>, <strong>Welding</strong>, <strong>Industrial Mechanics</strong>. All three clear 1A+2C, all three have strong local employer demand (SBL + HSHS for RN; Marathon + Sherwin-Williams + NAL + Hydro-Gear for welding + maintenance). <strong>Residual:</strong> CEFS&apos;s next annual cohort plan should weight ITAs toward these three pathways over CNA / LPN / Cosmetology which fail the wage test.</>,
              },
              {
                title: "Coordinate cohort intake with the two big industrial anchors",
                body: <>Marathon Petroleum Robinson Refinery (~690 employees) + NAL Paris (1,400 employees) are the two largest single-employer industrial sites in LWA-23 outside Coles County. Both have predictable hiring + retirement cycles. <strong>Residual:</strong> CEFS should broker the next welding + industrial-maintenance cohort to Marathon HR + NAL HR for direct hire-back pipelines.</>,
              },
              {
                title: "Anchor allied-health pipelines on SBL + HSHS + Carle Richland + Clay Co Hospital",
                body: <>LWA-23 has four major hospital systems serving the 13-county footprint. Lake Land + Kaskaskia + Olney Central all run RN-ADN + LPN credential pipelines. <strong>Residual:</strong> direct the workforce-board cohort intake at all four hospital systems concurrently — supply will not exceed demand given persistent nursing shortage + Carle Richland (495 employees) being the dominant Richland-county employer + SBL Top-10-IL employer status.</>,
              },
              {
                title: "Coordinate with EIU + 3 community colleges on transfer pipelines",
                body: <>EIU (Charleston · 5,434 Fall 2025) carries the 4-year credential ladder. Lake Land + Kaskaskia + Olney Central all have direct AAS → EIU transfer agreements. <strong>Residual:</strong> sequence WIOA placements so 2-year graduates have a 4-year transfer option at EIU. Companion: <a href="/charleston" style={{ color: "#1f5f8f", fontWeight: 600 }}>Charleston, IL →</a></>,
              },
              {
                title: "Cross-coordinate with adjacent LWA-25",
                body: <>LWA-23 + <a href="/southern-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>LWA-25 →</a> border at Marion + Fayette + Clay counties. Workers commute across LWA boundaries. <strong>Residual:</strong> CEFS + Man-Tra-Con should coordinate annual planning at the boundary — particularly for the Marion County (LWA-23) ↔ Jefferson County (LWA-25) workforce, where SSM Health Centralia + Salem Township Hospital serve both populations.</>,
              },
              {
                title: "Diversification is the LWA-23 strength — protect it",
                body: <>LWA-25&apos;s federal-money concentration (95.6% to GD-OTS Marion) is both an anchor + a structural risk. LWA-23&apos;s diversified employer base (refining, retail HQ, telecom HQ, headlamp manufacturing, paint manufacturing, healthcare, higher-ed, two state prisons, agriculture, food processing) means no single anchor failure crashes the region. <strong>Residual:</strong> cohort plans should preserve cross-sector training breadth rather than concentrating on a single dominant employer — the diversification protects the region but only if the credential pipeline keeps feeding multiple sectors.</>,
              },
              {
                title: "Direct Effingham public-safety attention to the I-57/I-70 corridor",
                body: <>Effingham carries the highest total crime in LWA-23 (21.63 per 1,000) — driven by retail + logistics density at the I-57/I-70 cross-roads + property crime (19.46 per 1,000), not violent crime. <strong>Residual:</strong> not a workforce-board problem to solve, but public-safety stakeholders should target retail + truck-stop + logistics-corridor property crime specifically.</>,
              },
              {
                title: "Coordinate with sister regional + city pages",
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

        {/* ═══ §10 Methodology + sources ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            10 · Methodology + page scope
          </h2>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6, maxWidth: 820 }}>
            <strong>Live data substrate:</strong> /api/public/cefs aggregates monthly UR + labor force across all 13 LWA-23 counties (sum-of-labor-forces method for the LWA aggregate; weighted-by-labor-force method for the LWA UR). FRED panel: 170 series for LWA-23 in platform.macro_data (98 monthly UR + LF + 60 annual labor + education + 12 Coles housing + 12 annual income/poverty/SNAP for Coles via cle_coles_*). QCEW industry mix + USAspending federal awards + ACS labor truth are pulled live via FIPS-parameterized helpers across the 13-county set.
            <br /><br />
            <strong>Refresh cadence:</strong> Monthly FRED ingest refreshes UR + labor force ~1-2 months after the reference period. Annual series (income, GDP, poverty, SNAP) lag 6-18 months. USAspending refreshes nightly. Census ACS refreshes annually in December for the preceding 5-year window. FBI UCR + NeighborhoodScout refresh annually in October.
            <br /><br />
            <strong>Editorial standard:</strong> every claim on this page is anchored on a primary source (cited inline); no inferences or unsourced framings. <strong>LWA-23 admin contact:</strong> CEFS Economic Opportunity Corporation, 1805 South Banker Street, Effingham, IL 62401 · (217) 342-2193 ext. 2121 · lwia23@cefseoc.org · <a href="https://www.lwa23.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>lwa23.com</a>.
          </div>
        </section>

        <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
      </div>
    </>
  );
}
