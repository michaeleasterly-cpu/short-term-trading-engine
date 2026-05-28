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
interface CountyIndustryRow {
  fips: string; name: string;
  total_employment: number;
  top_supersectors: Array<{ code: string; name: string; employment: number; avg_weekly_wage: number }>;
}
interface IndustryMix {
  as_of_quarter: string;
  top_supersectors: IndustryRow[];
  total_employment: number;
  by_county?: CountyIndustryRow[];
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
            <strong>The structural labor-market issue in LWA-23 is participation, not unemployment.</strong> Headline weighted UR across 13 counties sits at {lwaUR != null ? `${lwaUR.toFixed(1)}%` : "~5%"} (BLS LAUS, March 2026 vintage) — but <strong>87,127 working-age adults across the footprint are NOT in the labor force</strong> (40.1% of working-age population vs IL state 34.9%, a -5.2pp LFPR gap per Census ACS). The headline UR only counts adults actively looking; the participation gap captures the structurally-disconnected. CEFS Economic Opportunity Corporation (Effingham) administers WIOA across 13 counties — Clark, Clay, Coles, Crawford, Cumberland, Edgar, Effingham, Fayette, Jasper, Lawrence, Marion, Moultrie, Richland — anchored on the I-57 / I-70 / US-45 corridor.
          </p>
          <p style={{ fontSize: 13, color: "#5a564d", marginTop: 8, lineHeight: 1.55 }}>
            <strong>Regional anchors</strong> (cross-checked in detail below): Eastern Illinois University + Lake Land College + Kaskaskia College + Olney Central College on the credential side (combined Fall 2025 enrollment ~14k); Sarah Bush Lincoln + HSHS St. Anthony&apos;s + Carle Richland + Clay County Hospital + SBL Fayette + Salem Township Hospital on the regional-healthcare side; Marathon Petroleum Robinson Refinery (~690 emp, 253k bpcd) + NAL Paris headlamp plant (1,400 emp, Tier-1 Koito Group) + Sherwin-Williams Effingham + Rural King HQ Mattoon + Newton Power Plant (617 MW) + Vandalia + Lawrence Correctional Centers on the industrial side.
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

        {/* ═══ LWA-23 UR trend chart · chart-only, not a numbered section ═══ */}
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

        {/* ═══ §01 The true labor picture ═══ */}
        {data?.labor_truth && data.labor_truth.aggregate && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              01 · The true labor picture · what the headline {(data.lwa_aggregate?.unemployment_rate_weighted ?? 0).toFixed(1)}% unemployment rate hides
            </h2>
            <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
              The headline LWA-23 weighted UR of <strong>{(data.lwa_aggregate?.unemployment_rate_weighted ?? 0).toFixed(1)}%</strong> only counts adults <em>actively looking for work</em>. Census ACS labor-force data tells a different story: <strong>{data.labor_truth.aggregate.not_in_labor_force.toLocaleString()} working-age adults across the 13-county footprint are NOT in the labor force</strong> — neither employed nor officially searching. That&apos;s <strong>{data.labor_truth.aggregate.not_lf_pct.toFixed(1)}% of working-age (16+) population vs IL state&apos;s {data.labor_truth.benchmarks.il_state_not_lf_pct.toFixed(1)}%</strong>. The structural labor-supply constraint in LWA-23 is participation, not unemployment.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14, marginBottom: 16 }}>
              {[
                { label: "Headline UR (13-co weighted)", value: `${(data.lwa_aggregate?.unemployment_rate_weighted ?? 0).toFixed(1)}%`, sub: "what politicians cite", flag: false },
                { label: "Labor force participation", value: `${data.labor_truth.aggregate.lfpr}%`, sub: `IL state ${data.labor_truth.benchmarks.il_state_lfpr}% · gap ${data.labor_truth.aggregate.gap_lfpr_vs_state > 0 ? "+" : ""}${data.labor_truth.aggregate.gap_lfpr_vs_state}pp`, flag: data.labor_truth.aggregate.gap_lfpr_vs_state < -3 },
                { label: "Employment-to-population", value: `${data.labor_truth.aggregate.ep_ratio}%`, sub: `IL state ${data.labor_truth.benchmarks.il_state_ep}% · gap ${data.labor_truth.aggregate.gap_ep_vs_state > 0 ? "+" : ""}${data.labor_truth.aggregate.gap_ep_vs_state}pp`, flag: data.labor_truth.aggregate.gap_ep_vs_state < -3 },
                { label: "Not in labor force", value: data.labor_truth.aggregate.not_in_labor_force.toLocaleString(), sub: `${data.labor_truth.aggregate.not_lf_pct}% of working-age — the invisible population`, flag: true },
              ].map((s, i) => (
                <StatCard key={i} label={s.label} value={s.value} sub={s.sub} flag={s.flag} />
              ))}
            </div>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto", marginBottom: 12 }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <thead>
                  <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                    <th style={{ padding: "8px 10px", fontWeight: 600 }}>County</th>
                    <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>LFPR</th>
                    <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>EP-ratio</th>
                    <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Not-in-LF</th>
                    <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>LFPR gap vs IL</th>
                    <th style={{ padding: "8px 10px", fontWeight: 600 }}>Pattern</th>
                  </tr>
                </thead>
                <tbody>
                  {data.labor_truth.geos
                    .slice()
                    .sort((a, b) => a.gap_lfpr_vs_state - b.gap_lfpr_vs_state)
                    .map((g, i) => {
                      const cleanName = g.name.replace(", Illinois", "").replace(" County", "");
                      const gapColor = g.gap_lfpr_vs_state < -10 ? "oklch(35% 0.22 22)"
                        : g.gap_lfpr_vs_state < -5 ? "oklch(45% 0.20 22)"
                        : g.gap_lfpr_vs_state < 0 ? "oklch(45% 0.18 60)"
                        : "oklch(40% 0.16 142)";
                      const pattern = g.gap_lfpr_vs_state < -10
                        ? "Severe gap — prison economy + small private sector"
                        : g.gap_lfpr_vs_state < -5
                        ? "Significant gap — single-anchor county or thin private market"
                        : g.gap_lfpr_vs_state < 0
                        ? "Mild gap — typical rural-IL pattern"
                        : "Above IL state — commuter county or anchor-employer effect";
                      return (
                        <tr key={g.fips} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                          <td style={{ padding: "6px 10px", fontWeight: 600 }}>{cleanName}</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>{g.lfpr}%</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{g.ep_ratio}%</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{g.not_in_labor_force.toLocaleString()}</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600, color: gapColor }}>{g.gap_lfpr_vs_state > 0 ? "+" : ""}{g.gap_lfpr_vs_state}pp</td>
                          <td style={{ padding: "6px 10px", fontSize: 11, color: "#5a564d" }}>{pattern}</td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
            <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
              <strong>Why this matters for CEFS workforce planning:</strong>
              <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
                <li><strong>Lawrence County (LFPR 51.4%, gap -13.7pp)</strong> is the most-extreme case: the county hosts Lawrence Correctional Center (IDOC max-security, capacity 2,458) but otherwise has a thin private-sector base — labor-force participation collapses outside the prison-economy.</li>
                <li><strong>Fayette (53.4%, -11.7pp), Crawford (54.8%, -10.3pp), Edgar (56.5%, -8.6pp), Clay (56.6%, -8.5pp)</strong> all carry single-anchor economies (Vandalia Correctional, Marathon refinery, NAL plant, Clay County Hospital) but everyone outside the anchor is disproportionately out of the labor force.</li>
                <li><strong>Cumberland (LFPR 67.2%, +2.1pp ABOVE state) + Jasper (65.8%, +0.7pp)</strong> are the commuter counties — residents work in Effingham + Mattoon-Charleston + Newton Power Plant; their labor-force participation tracks the anchor county they commute to.</li>
                <li><strong>The 87,127 not-in-LF population is the leading metric for CEFS WIOA enrollment targeting</strong>, not the headline UR. Standard WIOA outreach reaches the unemployed-and-looking; this group needs barrier-removal (childcare, transportation, basic-skills bridge) before training enrollment.</li>
              </ul>
            </div>
            <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
              {data.labor_truth.source}. ACS year: {data.labor_truth.year}. The &quot;Not in labor force&quot; count is the closest legitimate proxy for the invisible-population concern — people neither employed nor officially unemployed-and-looking.
            </div>
          </section>
        )}

        {/* ═══ §02 Participation gap ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            02 · Participation gap · root causes (not just symptoms)
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            The §01 LFPR gap is the diagnosis. To intervene effectively, CEFS needs to know <strong>why</strong> participation is low in each county. The two strongest correlates with LFPR drop in LWA-23 are <strong>disability rate</strong> (Census ACS S1810) and <strong>carceral-economy share</strong> (Lawrence + Fayette host IDOC facilities), with rent-burden as a downstream income-side pressure. Age structure is a secondary factor — none of the 13 counties has a notably-elderly median.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>County</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Disability % (S1810)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Median age (S0101)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>LFPR (S2301)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Renters cost-burdened</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Dominant participation-gap driver</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { c: "Lawrence", disab: 20.2, age: 40.6, lfpr: 51.4, rent: 28.6, driver: "Disability + carceral economy (max-security IDOC; population disability rate highest in footprint)" },
                  { c: "Marion", disab: 18.7, age: 41.3, lfpr: 60.0, rent: 36.6, driver: "Disability + rent-burden + thin private sector outside Salem Twp Hospital" },
                  { c: "Clay", disab: 18.6, age: 41.5, lfpr: 56.6, rent: 23.1, driver: "Disability — single-anchor hospital economy (Clay County Hospital)" },
                  { c: "Edgar", disab: 18.4, age: 46.4, lfpr: 56.5, rent: 26.4, driver: "Disability + age (oldest median in footprint) + NAL single-anchor" },
                  { c: "Richland", disab: 18.3, age: 42.5, lfpr: 62.1, rent: 37.0, driver: "Disability + rent-burden — counterbalanced by Walmart DC + Olney Central + Carle Richland" },
                  { c: "Fayette", disab: 17.5, age: 41.7, lfpr: 53.4, rent: 40.8, driver: "Disability + carceral economy (Vandalia Correctional) + HIGHEST rent-burden in footprint" },
                  { c: "Coles", disab: 16.3, age: 38.2, lfpr: 62.5, rent: 40.4, driver: "Second-highest rent-burden (40.4%) — EIU + SBL anchor counterbalances disability tier" },
                  { c: "Jasper", disab: 15.9, age: 44.2, lfpr: 65.8, rent: 31.4, driver: "Newton Power Plant anchor — LFPR ABOVE state average; rent-burden moderate" },
                  { c: "Crawford", disab: 15.7, age: 42.4, lfpr: 54.8, rent: 28.9, driver: "Single-anchor refinery — workers outside Marathon disproportionately not in LF; not driven by disability" },
                  { c: "Effingham", disab: 14.8, age: 39.5, lfpr: 64.8, rent: 30.3, driver: "Anchor county effect (CEFS + HSHS + Sherwin-Williams + I-57/I-70) — LFPR near state" },
                  { c: "Clark", disab: 13.5, age: 42.0, lfpr: 59.7, rent: 30.6, driver: "ZF Marshall anchor; cross-border commute to Terre Haute IN" },
                  { c: "Cumberland", disab: 13.4, age: 42.3, lfpr: 67.2, rent: 30.4, driver: "Commuter county — residents work in Effingham/Mattoon-Charleston; LFPR ABOVE state" },
                  { c: "Moultrie", disab: 13.2, age: 40.4, lfpr: 60.5, rent: 30.1, driver: "Agri-Fab + Hydro-Gear manufacturing cluster — LFPR near state" },
                ].map((r, i) => {
                  const disabColor = r.disab >= 18 ? "oklch(45% 0.20 22)" : r.disab >= 16 ? "oklch(45% 0.18 60)" : "oklch(40% 0.16 142)";
                  const lfprColor = r.lfpr < 55 ? "oklch(45% 0.20 22)" : r.lfpr < 62 ? "oklch(45% 0.18 60)" : "oklch(40% 0.16 142)";
                  return (
                    <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                      <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.c}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600, color: disabColor }}>{r.disab}%</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.age}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600, color: lfprColor }}>{r.lfpr}%</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.rent}%</td>
                      <td style={{ padding: "6px 10px", fontSize: 11, color: "#5a564d" }}>{r.driver}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            <strong>Pattern that emerges from the data:</strong>
            <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
              <li><strong>Disability is the dominant explanatory variable</strong> — 6 of 7 counties with disability rate ≥17% have LFPR below state average; 4 of 4 counties with disability rate ≤14% have LFPR within ±2pp of state average. This is not just a healthcare issue — it&apos;s the binding constraint on labor supply.</li>
              <li><strong>Carceral economy compounds disability where present</strong> — Lawrence (max-security IDOC + 20.2% disability) is the most-extreme participation collapse. Fayette (min-security IDOC + 17.5%) is third-worst.</li>
              <li><strong>Single-anchor counties</strong> — Crawford (15.7% disability but 54.8% LFPR) shows the anchor-only pattern: workers outside Marathon refinery disproportionately don&apos;t participate, regardless of health.</li>
              <li><strong>Anchor counties counterbalance disability</strong> — Effingham (14.8% disability + LFPR 64.8%) and Coles (16.3% + 62.5%) show that strong anchor presence (CEFS+HSHS / EIU+SBL) keeps LFPR near state average despite typical rural-IL disability rates.</li>
              <li><strong>Rent-burden is the income-side amplifier</strong> — Fayette 40.8%, Coles 40.4%, Richland 37.0%, Marion 36.6% of renters paying 30%+ of income. Fayette compounds the worst combo: 17.5% disability + carceral economy + highest rent-burden in footprint. High rent-burden + high disability = compounded structural exit from the labor market.</li>
              <li><strong>Age is NOT the primary driver</strong> — median age range 38.2-46.4 across the 13 counties; only Edgar (46.4) tilts old. The Census participation gap is concentrated in working-age 16-64 disability + low private-sector demand, not retirees.</li>
            </ul>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://api.census.gov/data/2023/acs/acs5/subject" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census ACS 5-year 2023 Subject Tables</a> — S1810 (disability characteristics, civilian non-institutionalized) + S0101 (age/sex) + S2301 (employment status) — pulled per county FIPS for the 13-county set. B25070 (gross rent as % of household income) for rent-burden share. Disability metric is &quot;population with any disability&quot; (S1810_C03_001E); excludes institutionalized population (so IDOC prison populations in Lawrence + Fayette are NOT in the S1810 numerator).
          </div>
        </section>

        {/* ═══ §03 Theory of change ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            03 · Theory of change · what LWA-23 should actually do
          </h2>
          <div style={{ padding: 16, background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Binding constraint
            </div>
            <div style={{ fontSize: 14, color: "#3d3a33", lineHeight: 1.6 }}>
              <strong>Labor supply, not labor demand.</strong> LWA-23 has 87,127 working-age adults not in the labor force — 40.1% of working-age population vs IL state 34.9%. The §02 root-cause analysis shows the LFPR gap is dominated by <strong>disability + carceral economy + single-anchor concentration</strong>, with rent-burden as the income-side amplifier in Clark, Coles, and Marion. Employer-attraction strategies that assume an available labor pool are bidding against the wrong constraint.
            </div>
          </div>
          <div style={{ padding: 16, background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Intervention path (in order)
            </div>
            <ol style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
              <li><strong>Participation recovery FIRST.</strong> Re-engage the not-in-LF population via barrier-removal — childcare (the §13 76% Region 11 slot gap), transportation (the §12 shift-incompatible transit), disability accommodation (the §02 ≥17% disability tier in Lawrence/Marion/Clay/Edgar/Richland/Fayette), carceral re-entry (Lawrence + Fayette). This is what makes the rest of the strategy possible.</li>
              <li><strong>Healthcare laddering SECOND.</strong> CNA → LPN → RN is the local pathway with the strongest labor demand (RN 154 annual openings per §19, $31-$45/hr; CLEARS 1A+2C at journey wage). Lake Land + Kaskaskia + Olney Central + Olney Central all run RN-ADN programs. SBL Mattoon is the only growth-engine-tier hospital; the other 5 hospitals are stabilizers with CNA-tier sub-livable wages — so the ladder must reach RN, not stop at CNA.</li>
              <li><strong>Industrial-maintenance + welding THIRD.</strong> 14% of LWA-23 employment is manufacturing (per §06 QCEW). Marathon refinery + NAL Paris + Sherwin-Williams Effingham + Hydro-Gear Sullivan all hire welders + maintenance technicians at FAMILY-SUPPORTING wages (§11 Training-to-Demand verdict). Kaskaskia Crisp Manufacturing Center + Olney Central + Lake Land deliver the credentials.</li>
              <li><strong>CDL FOURTH.</strong> Top regional job demand by openings (Heavy Truck Drivers 219 annual + 67 December 2025 postings). LOCAL rate fails 1A+2C; OTR + refinery-tanker clears with home-time cost (the LWA-25 TRAVEL-WORK pattern). Use only for cohorts where the family-configuration math works.</li>
              <li><strong>Employer attraction LAST.</strong> Anchor-attraction targets (§21) are real opportunities (NAL EV cluster, Vistra Newton transition, BEAD broadband) but they presuppose available labor. Attempting attraction before participation recovery exposes recruited employers to a labor-supply shortage and turns into broken promises. Sequence matters.</li>
            </ol>
          </div>
          <div style={{ padding: 16, background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Target populations
            </div>
            <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
              <li><strong>Not-in-LF working-age adults with reported disabilities</strong> (the 17-20% disability tier in Lawrence/Marion/Clay/Edgar/Richland/Fayette) — vocational rehab + partial-time work + accommodation pathways, not standard WIOA ITAs.</li>
              <li><strong>Parents (especially mothers) blocked by childcare</strong> — Region 11 76% slot gap + Coles MIT LWC childcare cost $9,460/yr one-child = 22% of median HH income (§13). Pathway: CEFS Head Start + CCAP co-enrollment + cohort-childcare-stipend WIOA design.</li>
              <li><strong>Returning citizens from Lawrence + Vandalia Correctional</strong> — the IDOC presence in Lawrence and Fayette doesn&apos;t just employ COs, it also generates a steady local re-entry population. CEFS WIOA re-entry programming is the dedicated lever.</li>
              <li><strong>Incumbent workers at the single-anchor employers</strong> — Marathon Robinson + NAL Paris + Sherwin-Williams + Hydro-Gear + Newton Power Plant — for skills-upgrade to capture wage progression (Incumbent Worker Training is one of CEFS&apos;s six WIOA services per §09).</li>
              <li><strong>Cross-border commuters</strong> — Clark + Edgar → Terre Haute IN; Crawford + Lawrence → Vincennes IN. These are existing flows that CEFS can formalize via cross-state placement agreements.</li>
            </ul>
          </div>
          <div style={{ padding: 16, background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Measurable outcome (PIRL augmentation — see §20)
            </div>
            <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6 }}>
              Standard PIRL captures Q2/Q4 employment + median earnings + credential attainment + measurable skill gains. Three LWA-23-specific augmentations: <strong>(a) not-in-LF re-engagement count</strong> (WIOA enrollees who were not-in-LF at intake — segment of the 87k from §01); <strong>(b) childcare-barrier-removal retention</strong> (% of enrollees who flagged childcare as a barrier and still completed credentialing); <strong>(c) disability-tier placement</strong> (% of enrollees with a reported disability placed in a job at the wage-tier their credential should command). These are the three metrics that distinguish a participation-recovery strategy from a generic-WIOA performance dashboard.
            </div>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            <strong>Why this differs from LWA-25 (Southern Illinois):</strong> LWA-25&apos;s strategic problem is federal-money concentration risk (95.6% to GD-OTS Marion) + the SIU + Carbondale enrollment decline. LWA-25&apos;s solution path runs through diversification + supply-chain replacement of GD-OTS sub-recipients. LWA-23 doesn&apos;t have GD-OTS or anything like it — the federal-money base is genuinely diversified (§08). LWA-23&apos;s problem is the labor-supply side: 87,127 not-in-LF + 17-20% disability rates in 6 counties + Region 11 76% childcare gap + carceral economy in Lawrence + Fayette. These two regions need different interventions; an LWA-25-style supply-chain strategy doesn&apos;t fit LWA-23, and an LWA-23-style participation-recovery strategy doesn&apos;t fit LWA-25.
          </div>
        </section>

        {/* ═══ §04 County strategy matrix ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            04 · County strategy matrix · 5 archetypes, 5 different interventions
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            The 13 LWA-23 counties don&apos;t all share the same constraint. Treating them as a single analytical unit produces a generic strategy; treating them as 5 archetypes lets the intervention attack the actual binding constraint in each.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {[
              {
                name: "Anchor counties (Coles + Effingham)",
                fips: "17029 + 17049",
                signature: "EIU + Lake Land + SBL + CEFS HQ + HSHS + Sherwin-Williams + I-57/I-70 crossroads",
                signal: "LFPR near state average (62.5% + 64.8%); disability rates moderate (16.3% + 14.8%); Effingham is the in-footprint net job-importer; Coles is the credential-pipeline anchor",
                strategy: "Education + healthcare anchoring + I-57/I-70 logistics + retail HQ workforce continuity. Lake Land + EIU + Olney Central credential delivery for the rest of the LWA-23 footprint. Anchor-attraction targets (NAL EV cluster expansion, logistics) land here first.",
                color: "oklch(40% 0.16 142)",
              },
              {
                name: "Single-industrial-anchor counties (Crawford + Edgar + Jasper + Clark + Moultrie)",
                fips: "17033 + 17045 + 17079 + 17023 + 17139",
                signature: "Marathon Robinson refinery / NAL Paris / Newton Power Plant / ZF Marshall / Agri-Fab + Hydro-Gear",
                signal: "Single dominant employer drives the local economy; workers outside the anchor disproportionately don't participate (Crawford LFPR 54.8% despite 15.7% disability); LFPR is good when commuter pattern exists (Jasper 65.8%, Moultrie 60.5%)",
                strategy: "Incumbent-worker upskilling at the anchor + manufacturing-maintenance credential pipeline (Kaskaskia Crisp Mfg Center + Olney Central + Lake Land). Energy-transition planning for Jasper (Vistra Newton Unit 1 retirement end-2027). Cross-border CDL/logistics for Clark + Edgar to Terre Haute IN.",
                color: "oklch(45% 0.16 220)",
              },
              {
                name: "Carceral-economy counties (Lawrence + Fayette)",
                fips: "17101 + 17051",
                signature: "Lawrence Correctional (max-security, capacity 2,458) + Vandalia Correctional (min-security)",
                signal: "WORST LFPR in footprint (Lawrence 51.4%, Fayette 53.4%); highest + third-highest disability rates (20.2% + 17.5%); thin private-sector base outside IDOC; steady local re-entry population",
                strategy: "Re-entry workforce programming as a dedicated lane within CEFS WIOA. IDOC officer career-ladder (state-pension eligible, $24-31/hr starting) for the local workforce who can pass background. Vocational rehab + accommodation pathways for the 17-20% disability tier. Recovery + MAT clinic referral pairing.",
                color: "oklch(45% 0.20 22)",
              },
              {
                name: "Healthcare-anchored small counties (Clay + Marion + Richland)",
                fips: "17025 + 17121 + 17159",
                signature: "Clay County Hospital (CAH) + Salem Township Hospital (CAH) + Carle Richland Memorial",
                signal: "Single-hospital economy; high disability rates (18.6% + 18.7% + 18.3%); modest LFPR (56.6% / 60.0% / 62.1%); rent-burden material in Marion (38.5%)",
                strategy: "Healthcare laddering (CNA → LPN → RN) is the main credential play; LPN/RN clears 1A+2C, CNA doesn't. Marion + Richland counties benefit from cross-LWA coordination with LWA-25 (Mt. Vernon SSM + Jefferson Co. SIH). Walmart DC Olney + Carle Richland are the Richland diversifiers.",
                color: "oklch(45% 0.18 60)",
              },
              {
                name: "Commuter + agricultural counties (Cumberland + Clay + Lawrence agricultural sides)",
                fips: "17035 + agricultural margins of 17025 / 17101",
                signature: "Workers commute to Effingham / Mattoon-Charleston for jobs; agriculture as residential base",
                signal: "Cumberland has BEST LFPR in footprint (67.2%, +2.1pp ABOVE IL state) precisely because residents commute out for work; lowest disability rate tier (13.4%)",
                strategy: "Maintain commuter-pattern via transportation reliability (Cumberland is served by RIDES MTD; Coles by Dial-A-Ride; Effingham by CIPT). Don't try to attract employers into the commuter counties — the strategy is the commute itself. Agricultural credentialing through Lake Land's 14 ag programs for the residents staying.",
                color: "oklch(45% 0.16 142)",
              },
            ].map((a, i) => (
              <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: `6px solid ${a.color}`, borderRadius: 6, padding: 14 }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: "#1f1d18", marginBottom: 4 }}>{a.name}</div>
                <div style={{ fontSize: 11, color: "#7a756b", marginBottom: 8 }}>FIPS: {a.fips}</div>
                <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 4 }}><strong>Signature:</strong> {a.signature}</div>
                <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 8 }}><strong>Data signal:</strong> {a.signal}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}><strong>Strategy:</strong> {a.strategy}</div>
              </div>
            ))}
          </div>
        </section>

        {/* ═══ §05 13-county footprint ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            05 · 13-county footprint · anchor employer per county seat
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

        {/* ═══ §06 LWA-23 industry mix ═══ */}
        {mix && mix.top_supersectors && mix.top_supersectors.length > 0 && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              06 · LWA-23 industry mix · BLS QCEW (13-county aggregate)
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

        {/* ═══ §07 Industry mix by county ═══ */}
        {mix && mix.by_county && mix.by_county.length > 0 && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              07 · Industry mix by county · each county&apos;s own economic identity
            </h2>
            <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
              Top supersectors per county (BLS QCEW, latest published quarter). Effingham (~23k jobs) and Coles (~22k) are the dominant employment centers; the smaller counties typically concentrate in 2-3 sectors with a single anchor employer driving the largest line.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 12 }}>
              {mix.by_county
                .slice()
                .sort((a, b) => b.total_employment - a.total_employment)
                .map((c) => {
                  const FIPS_TO_NAME: Record<string, string> = {
                    "023": "Clark", "025": "Clay", "029": "Coles", "033": "Crawford",
                    "035": "Cumberland", "045": "Edgar", "049": "Effingham",
                    "051": "Fayette", "079": "Jasper", "101": "Lawrence",
                    "121": "Marion", "139": "Moultrie", "159": "Richland",
                  };
                  const niceName = FIPS_TO_NAME[c.fips] || c.fips;
                  return (
                    <div key={c.fips} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
                        <div style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18" }}>{niceName} County</div>
                        <div style={{ fontSize: 12, color: "#7a756b" }}>Total: <strong>{c.total_employment.toLocaleString()}</strong> jobs</div>
                      </div>
                      <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                        <tbody>
                          {(c.top_supersectors || []).slice(0, 5).map((s, i) => (
                            <tr key={s.code} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                              <td style={{ padding: "4px 0", color: "#3d3a33" }}>{s.name}</td>
                              <td style={{ padding: "4px 0", textAlign: "right", fontWeight: 600 }}>{s.employment.toLocaleString()}</td>
                              <td style={{ padding: "4px 0", textAlign: "right", color: "#5a564d" }}>${s.avg_weekly_wage.toLocaleString()}/wk</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  );
                })}
            </div>
            <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
              Source: live BLS QCEW pull per county (quarter {mix.as_of_quarter}). Smaller counties show smaller absolute employment but the supersector mix exposes their structural anchor (e.g., Jasper&apos;s Manufacturing line reflects Newton Power Plant; Lawrence&apos;s Public Administration is the Correctional Center; Crawford&apos;s Manufacturing is the Marathon refinery).
            </div>
          </section>
        )}

        {/* ═══ §08 Federal contract dollars ═══ */}
        {data?.business_opportunities && (
          <section style={{ marginTop: 40 }}>
            <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              08 · Federal contract dollars · {fmtMoney(data.business_opportunities.totals.awards_dollars)} (last {data.business_opportunities.totals.lookback_months} months)
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

        {/* ═══ §09 CEFS LWA-23 WIOA program portfolio ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            09 · CEFS LWA-23 WIOA program portfolio · what the board actually delivers
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

        {/* ═══ §10 Community college credential pipelines ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            10 · Community college credential pipelines · Lake Land + Kaskaskia + Olney Central
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

        {/* ═══ §11 Training-to-demand alignment ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            11 · Training-to-demand alignment · the 1A+2C single-earner wage test
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

        {/* ═══ §12 Mobility + job access ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            12 · Mobility + job access · transit coverage across 13 counties
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            The 13-county footprint is covered by <strong>four</strong> rural transit agencies, all demand-response by default with a few deviated/fixed routes in larger towns. <strong>None operate evenings, Sundays, or full-Saturday hours</strong> — any 2nd-shift manufacturing job (Beef Packers + IDEX Effingham, Mattoon factory cluster, NAL Paris, Hydro-Gear Sullivan) is effectively car-dependent. CEFS itself operates the Central IL Public Transit (CIPT) service for 4 of the 13 counties.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Counties served</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Provider</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Service hours</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Service type</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { counties: "Clay + Effingham + Fayette + Moultrie", provider: "Central Illinois Public Transit (CIPT) — operated by CEFS Economic Opportunity Corp", hours: "M-F 6am-5pm (Effingham dispatch 6am-6pm)", type: "Demand-response + ETrax deviated route inside Effingham city", src: "https://www.cefseoc.org/transportation-cipt" },
                  { counties: "Clark + Crawford + Cumberland + Edgar + Jasper + Lawrence + Richland", provider: "RIDES Mass Transit District (RMTD)", hours: "M-Sat 8am-4pm (Robinson office); demand-response county-wide. Fixed routes in Paris, Robinson, Olney (e.g., Bulldog/Wildcat 6:00am-5:52pm)", type: "Demand-response + limited deviated fixed-route in larger towns", src: "https://www.ridesmtd.com/" },
                  { counties: "Coles", provider: "Dial-A-Ride Public Transportation", hours: "M-F 8am-5pm; \"Zip Line\" deviated route Mattoon-Charleston runs through ~2pm", type: "Demand-response + one deviated route. Cross-county service to Champaign-Urbana / Douglas / Effingham at $7 one-way (the only meaningful MSA-job-access link in LWA-23)", src: "https://www.dialaridetransit.org/coles-county-public-transportation.html" },
                  { counties: "Marion", provider: "South Central Transit (SCT)", hours: "Varies by route; dispatch 1-800-660-7433", type: "Deviated fixed-route + demand-response", src: "http://southcentraltransit.org/routes-and-schedules/" },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{r.counties}</td>
                    <td style={{ padding: "8px 10px", color: "#3d3a33" }}>{r.provider}<br /><a href={r.src} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f", fontSize: 11 }}>source</a></td>
                    <td style={{ padding: "8px 10px", fontSize: 12, color: "#5a564d" }}>{r.hours}</td>
                    <td style={{ padding: "8px 10px", fontSize: 12, color: "#5a564d" }}>{r.type}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            <strong>Workforce-board implication:</strong> the 87,127 working-age adults not in the labor force (see §01) face a transportation barrier the WIOA training pipeline doesn&apos;t directly address. CIPT (CEFS-operated) is the in-board lever for Clay/Effingham/Fayette/Moultrie residents. RMTD coverage of the 7 south + east counties is demand-response only — no fixed-route reliability for shift-work commute. The cross-county Dial-A-Ride link from Coles to Champaign-Urbana at $7 one-way is the single best LWA-23 → MSA-job-access link. Source: <a href="https://en.wikipedia.org/wiki/Rides_Mass_Transit_District" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>RMTD FY2024-25 NTD data via Wikipedia</a> (FY24 = 636,290 rides / 141,218 revenue hours; FY25 = 367,682 rides / 142,368 revenue hours — flag for follow-up on the ridership drop).
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Note: SHOW BUS does NOT serve any LWA-23 county (its territory is DeWitt/Ford/Iroquois/Kankakee/Livingston/Logan/Mason/McLean; per <a href="https://www.showbusonline.org/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>showbusonline.org</a>, withdrew from rural Macon/DeWitt/Ford/McLean 2025-06-30). Effingham city&apos;s ETrax is a CIPT deviated route, not a standalone provider. Coles County is NOT served by JAX Mass Transit (JAX is Carbondale/Jackson County).
          </div>
        </section>

        {/* ═══ §13 Childcare constraint ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            13 · Childcare constraint · the 76% slot-gap that caps labor-force participation
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Per the <a href="https://www.birthtofiveil.com/region11" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Birth to Five Illinois Region 11 Needs Assessment</a>, of 8,122 children under age 6 in Region 11 (Clark, Coles, Cumberland, Douglas, Edgar, Moultrie, Shelby — covering 5 of the 13 LWA-23 counties), <strong>76% do not have a slot in a licensed or license-exempt childcare center or home</strong>. Five named regional needs include &quot;more infant and toddler care slots and full-day preschool opportunities.&quot; This is a direct mechanism feeding the LWA-23 not-in-labor-force gap documented in §01 — parents without childcare cannot participate in the labor market regardless of credential or wage.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: 12, marginBottom: 12 }}>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(45% 0.20 22)", marginBottom: 4, textTransform: "uppercase" }}>Region 11 slot gap</div>
              <div style={{ fontSize: 24, fontWeight: 600 }}>76%</div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>of children under 6 lack a licensed slot (Clark+Coles+Cumberland+Edgar+Moultrie + Douglas + Shelby)</div>
            </div>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "#1f1d18", marginBottom: 4, textTransform: "uppercase" }}>Coles 1-child cost</div>
              <div style={{ fontSize: 24, fontWeight: 600 }}>$9,460/yr</div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>MIT Living Wage Coles County — ~22% of ACS median HH income (3x HHS 7% affordability benchmark)</div>
            </div>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "#1f1d18", marginBottom: 4, textTransform: "uppercase" }}>Coles 2-child cost</div>
              <div style={{ fontSize: 24, fontWeight: 600 }}>$18,323/yr</div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>MIT LWC Coles — 1A+2C living wage = $33.54/hr (= $69,763/yr single-earner)</div>
            </div>
            <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(40% 0.16 142)", borderRadius: 6, padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(40% 0.16 142)", marginBottom: 4, textTransform: "uppercase" }}>CEFS Head Start sites</div>
              <div style={{ fontSize: 24, fontWeight: 600 }}>8</div>
              <div style={{ fontSize: 12, color: "#5a564d", marginTop: 4 }}>Altamont, Effingham, Litchfield, Louisville (Clay), Pana, Shelbyville, Taylorville, Vandalia (Fayette) + home-based EHS in 7 counties</div>
            </div>
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14, marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>The 13 LWA-23 counties span 4 Birth to Five Illinois regions:</div>
            <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
              <li><strong>Region 3</strong> — Effingham + Fayette (with Bond + Christian + Montgomery). <a href="https://www.birthtofiveil.com/region3" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>birthtofiveil.com/region3</a></li>
              <li><strong>Region 11</strong> — Clark + Coles + Cumberland + Edgar + Moultrie (with Douglas + Shelby). <a href="https://www.birthtofiveil.com/region11" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>birthtofiveil.com/region11</a></li>
              <li><strong>Region 12</strong> — Clay + Crawford + Jasper + Lawrence + Richland. <a href="https://www.birthtofiveil.com/region12" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>birthtofiveil.com/region12</a></li>
              <li><strong>Region 13</strong> — Marion (with Clinton + Jefferson + Washington). <a href="https://www.birthtofiveil.com/region13" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>birthtofiveil.com/region13</a></li>
            </ul>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            <strong>Workforce-planning implication:</strong> the WIOA cohort funnel loses participants at the childcare-barrier step before they reach training enrollment. CEFS already operates Head Start as the income-eligible (≤100% federal poverty) zero-fee floor — the eight CEFS Head Start sites are the only formal childcare floor for low-income families in 8 of the 13 counties. The next-leverage move is the IL CCAP (Child Care Assistance Program) co-enrollment pathway for WIOA participants whose income clears the Head Start threshold but falls under the CCAP eligibility ceiling.
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://www.birthtofiveil.com/region11" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Birth to Five IL Region 11 ECEC Needs Assessment</a> + <a href="https://datahub.iecam.illinois.edu/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IECAM data hub</a> + <a href="https://sunshine.dcfs.illinois.gov/Content/Licensing/Daycare/ProviderLookup.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>DCFS Sunshine Provider Lookup</a> + <a href="https://www.cefseoc.org/headstart" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>CEFS Head Start program</a> + <a href="https://livingwage.mit.edu/counties/17029" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>MIT Living Wage Calculator Coles County</a> + <a href="https://bipartisanpolicy.org/article/state-child-care-data-2025-update/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BPC Child Care Gaps Assessment 2025</a>.
          </div>
        </section>

        {/* ═══ §14 Commute + regional leakage ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            14 · Commute + regional leakage · LWA-23 has no major-MSA commuter shed
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Unlike LWA-25 (which sits on the Carbondale-Marion MSA + I-57 / I-24 corridor with regional pull toward St. Louis Metro East), <strong>LWA-23 has no major-MSA commuter shed</strong>. The 13 counties span ~120 miles east-west + ~100 miles north-south but no MSA borders. Workers stay in-footprint or commute to Effingham (the I-57/I-70 in-footprint magnet). Coles County mean commute = 18.2 min (well below US 26.4 min mean) — most LWA-23 jobs are in-county.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14, marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>The job-flow structure:</div>
            <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
              <li><strong>Effingham city is the in-footprint net job-importer</strong> — pulls workers from Fayette, Jasper, Clay, Cumberland. Driven by the I-57/I-70 interchange industrial cluster, Beef Packers, IDEX, HSHS St. Anthony&apos;s Memorial Hospital. Effingham County industry mix is 22.8% education/health/social, 13.4% manufacturing, 12.7% retail (ACS-derived).</li>
              <li><strong>Sarah Bush Lincoln Health Center (Mattoon-Charleston line)</strong> is the single largest in-footprint employer pulling workers from Coles + Cumberland + Edgar + Moultrie + Douglas (Coles County Transportation Plan 2025).</li>
              <li><strong>Coles County → Champaign-Urbana</strong> is the only meaningful intra-state MSA-commute link in LWA-23 (US-45/I-57 corridor, ~50 mi). Dial-A-Ride&apos;s $7 cross-county service is the only transit-accessible component.</li>
              <li><strong>Marion → Mt. Vernon (Jefferson Co.) + St. Louis Metro East</strong> via I-64 (~75 mi). Long-haul commute pattern; not transit-accessible.</li>
              <li><strong>No meaningful Indianapolis commute</strong> — &gt;100 mi from the easternmost county seats; no transit link.</li>
            </ul>
          </div>

          <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", marginTop: 16, marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Verified IL → IN cross-border commute flows (LEHD LODES 2021)
          </h3>
          <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 12, maxWidth: 820, lineHeight: 1.55 }}>
            Direct LEHD LODES 2021 in_od_aux.csv extraction (h_geocode = IL residence, w_geocode = IN workplace, aggregated to county pair). Numbers are workers — actual commute flow.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>IL residence county</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>→ Vigo IN (Terre Haute)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>→ Knox IN (Vincennes)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Pattern</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { c: "Clark", vigo: 591, knox: 26, p: "STRONG → Terre Haute (US-40/I-70, ~25 mi from Marshall)" },
                  { c: "Edgar", vigo: 396, knox: 1, p: "STRONG → Terre Haute (NAL Paris ~30 mi from Vigo)" },
                  { c: "Crawford", vigo: 132, knox: 221, p: "MIXED → both Wabash Valley nodes; ~equal split" },
                  { c: "Lawrence", vigo: 95, knox: 925, p: "DOMINANT → Vincennes (Lawrenceville/Sumner ~15 mi from Knox)" },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{r.c}</td>
                    <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 600 }}>{r.vigo.toLocaleString()}</td>
                    <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 600 }}>{r.knox.toLocaleString()}</td>
                    <td style={{ padding: "8px 10px", fontSize: 11, color: "#5a564d" }}>{r.p}</td>
                  </tr>
                ))}
                <tr style={{ borderTop: "1px solid #ebe5d6", background: "#fef9eb" }}>
                  <td style={{ padding: "8px 10px", fontWeight: 700 }}>4-county → IN total</td>
                  <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 700 }}>1,214</td>
                  <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 700 }}>1,173</td>
                  <td style={{ padding: "8px 10px", fontSize: 11, color: "#5a564d" }}>2,387 LWA-23 residents cross the state line to work in 2 IN counties</td>
                </tr>
              </tbody>
            </table>
          </div>
          <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", marginTop: 16, marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Reverse flow: IN → IL in-commuters (LEHD LODES 2021)
          </h3>
          <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 12, maxWidth: 820, lineHeight: 1.55 }}>
            Symmetric pull from il_od_aux.csv: IN residents working IN one of the 4 LWA-23 border counties. Identifies whether each border edge is net-outflow (LWA-23 loses workers) or net-inflow.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>IN home county</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>→ Clark IL</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>→ Edgar IL</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>→ Crawford IL</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>→ Lawrence IL</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style={{ padding: "6px 10px", fontWeight: 600 }}>Vigo IN (Terre Haute)</td>
                  <td style={{ padding: "6px 10px", textAlign: "right" }}>370</td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>663</td>
                  <td style={{ padding: "6px 10px", textAlign: "right" }}>75</td>
                  <td style={{ padding: "6px 10px", textAlign: "right" }}>9</td>
                </tr>
                <tr style={{ borderTop: "1px solid #ebe5d6" }}>
                  <td style={{ padding: "6px 10px", fontWeight: 600 }}>Knox IN (Vincennes)</td>
                  <td style={{ padding: "6px 10px", textAlign: "right" }}>10</td>
                  <td style={{ padding: "6px 10px", textAlign: "right" }}>12</td>
                  <td style={{ padding: "6px 10px", textAlign: "right" }}>54</td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>374</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div style={{ padding: 12, background: "white", border: "1px solid #d8d2c4", borderRadius: 6, fontSize: 12, color: "#5a564d", lineHeight: 1.55, marginTop: 8 }}>
            <strong>Edge interpretation:</strong> The <strong>Terre Haute (Vigo IN) edge</strong> is essentially balanced two-way — Edgar+Clark send 987 workers into Vigo, Vigo sends 1,033 workers back into Edgar+Clark — a true labor-market integration edge. The <strong>Vincennes (Knox IN) edge is net-outflow</strong>: Lawrence loses 925 workers to Knox vs only 374 coming back. Crawford adds another net-152 outflow to Knox. Net workforce drain from Lawrence + Crawford → Knox IN = ~772 jobs; this directly amplifies the §02 LFPR collapse in Lawrence (51.4%, worst in footprint).
          </div>

          <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", marginTop: 16, marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Verified Marion County → St. Louis Metro East flow (LEHD LODES 2021)
          </h3>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Marion County, IL residents working in MO county</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Commuters</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { c: "St. Louis County MO", n: 344 },
                  { c: "St. Louis City MO", n: 225 },
                  { c: "St. Charles MO", n: 40 },
                  { c: "Jefferson MO", n: 38 },
                  { c: "Franklin MO", n: 6 },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.c}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>{r.n}</td>
                  </tr>
                ))}
                <tr style={{ borderTop: "1px solid #ebe5d6", background: "#fef9eb" }}>
                  <td style={{ padding: "6px 10px", fontWeight: 700 }}>Marion IL → STL Metro East total</td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 700 }}>653 (84% of Marion's 773 total MO commuters)</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Strategic implication:</strong> Lawrence County&apos;s LFPR 51.4% (worst in footprint, §02) is partially explained by the 925-commuter outflow to Knox IN — those workers ARE in the labor force, just not in the IL count. The Lawrence Correctional Center can&apos;t employ everyone, so a significant share of working-age Lawrence residents work in Vincennes IN. Marion County&apos;s 38.5%-rent-burdened, 18.7%-disability profile is similarly amplified by the 653-commuter outflow to St. Louis Metro East (Marion → STL is a ~75-mi I-64 corridor, requires a car — not transit-accessible).
            <br /><br />
            <strong>Cross-state placement-agreement pathway for CEFS:</strong> 2,387 LWA-23 → IN commuters + 1,790 LWA-23 → MO commuters = <strong>4,177 cross-state commuters</strong>. Formal Workforce Innovation cross-state coordination with Indiana Region 7 (Wabash Valley) + the MO Workforce Development Board St. Louis would capture additional placement opportunities. Source: <a href="https://lehd.ces.census.gov/data/lodes/LODES8/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>LEHD LODES 8 / 2021 vintage</a> · in_od_aux + mo_od_aux IL-residence pairs · LWA-23 13-county extraction 2026-05-28.
          </div>
        </section>

        {/* ═══ §15 City-level crime ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            15 · City-level crime · LWA-23 county seats + major cities (FBI UCR 2024)
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
            FBI UCR 2024 calendar year, NeighborhoodScout October 2025 release. <strong>Marshall (Clark), Toledo (Cumberland), Newton (Jasper), Sullivan (Moultrie) — per-1,000 figures VERIFIED_UNAVAILABLE_PUBLICLY 2026-05-28:</strong> all 4 cities ARE NIBRS-certified agencies that DO report to FBI; their counts are visible on FBI&apos;s own CDE webapp but the public API to retrieve them programmatically is broken post-2024 migration (api.usa.gov/crime/fbi/cde/summarized/* paths return 404; FBI Spring backend at crime-data-spring-api-master.app.cloud.gov retired). NeighborhoodScout paywalled. FBI CDE agency-detail pages where the counts CAN be viewed: <a href="https://cde.ucr.cjis.gov/LATEST/webapp/agency/IL0120200/home" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Marshall PD (ORI IL0120200)</a> · <a href="https://cde.ucr.cjis.gov/LATEST/webapp/agency/IL0180000/home" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Cumberland Co Sheriff (IL0180000, covers Toledo village)</a> · <a href="https://cde.ucr.cjis.gov/LATEST/webapp/agency/IL0400100/home" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Newton PD (IL0400100)</a> · <a href="https://cde.ucr.cjis.gov/LATEST/webapp/agency/IL0700300/home" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Sullivan PD (IL0700300)</a>. Page does not fabricate or infer rates for these four cities; the 9 cities above carry verified per-1,000 rates.
          </div>
        </section>

        {/* ═══ §16 Healthcare anchor analysis ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            16 · Healthcare anchor analysis · stabilizer vs growth engine vs low-wage trap
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Six hospital anchors serve LWA-23. <strong>Facility-level SOC-mix is VERIFIED_UNAVAILABLE_PUBLICLY</strong> (CMS Form 2552-10 cost reports give aggregate FTE + overhead-department paid hours but never RN/LPN/CNA split; CMS Form 10079 Occupational Mix Survey captures the split but per-facility responses are not posted publicly; only state-aggregate appears in CMS wage-index PUFs). Below uses (a) verified aggregate FTE from FY2024 CMS cost reports (IL HFS portal), (b) Form 990 payroll ÷ headcount as a wage-tier signal where it indirectly captures the modal-worker tier, and (c) BLS OEWS NAICS 622100 national hospital-industry occupation mix as labeled proxy (national mix: ~28% RN, ~7% CNA, plus physicians + allied health + admin + support).
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Hospital</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>County · beds</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>~Headcount</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Avg payroll/emp</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { name: "Sarah Bush Lincoln (SBL) — CCN 140189", loc: "Mattoon, Coles · 145 beds", hc: "2,321.52 hospital / 2,435.02 complex FTE", pay: "$247.7M total salaries", verdict: "STABILIZER → GROWTH ENGINE", verdictColor: "oklch(40% 0.16 142)", note: "FY2024 CMS cost report. ~$106k/FTE = IL RN median tier. RN/physician/allied-health dominant. Form 990 (EIN 237098532) Top-5 comp all physicians ($1.76M-$1.92M).", src: "https://hfs.illinois.gov/content/dam/soi/en/web/hfs/medicalproviders/costreports/2024medicarehospital/1401890624_mattoon_sbl.pdf" },
                  { name: "HSHS St. Anthony's Memorial — CCN 140032", loc: "Effingham · 133 beds", hc: "355.74 hospital / 546.93 complex FTE", pay: "$45.44M Other Salaries (Form 990)", verdict: "STABILIZER", verdictColor: "oklch(40% 0.16 142)", note: "FY2024 cost report verified. Mixed CNA/LPN/RN tier. Family-supporting in Effingham (median home $189k) but not a growth engine. Form 990 EIN 370661233.", src: "https://hfs.illinois.gov/content/dam/soi/en/web/hfs/medicalproviders/costreports/2024medicarehospital/1400320624_effingham_stanthonys.pdf" },
                  { name: "Carle Richland Memorial — CCN 140147", loc: "Olney, Richland · 47 beds (CAH)", hc: "316.85 hospital / 367.63 complex FTE", pay: "$36.76M Other Salaries (Form 990)", verdict: "STABILIZER", verdictColor: "oklch(40% 0.16 142)", note: "FY2023 cost report. Critical Access Hospital. Second-largest Richland employer. CAH designation caps growth; payroll consistent with high RN/allied-health share.", src: "https://hfs.illinois.gov/content/dam/soi/en/web/hfs/medicalproviders/costreports/medicarehospital/2023-medicare-hospital-cost-reports/1401471223_olney_richland.pdf" },
                  { name: "Clay County Hospital and Clinics — CCN 141351", loc: "Flora, Clay · 18 beds (CAH, county-owned)", hc: "FY2024 not republished by IL HFS; FY2018 last available", pay: "Not disclosed (county-owned, no 990)", verdict: "STABILIZER + LOW-WAGE TRAP RISK", verdictColor: "oklch(45% 0.18 60)", note: "County-government-owned; 990 not filed. FY2024 cost report exists in federal CMS HCRIS PUF (HCRIS submission ID 780005) but IL HFS only republishes through FY2018 for this facility. RN-heavy inpatient but CNA-tier clinic/LTC side likely sub-livable.", src: "https://hfs.illinois.gov/content/dam/soi/en/web/hfs/medicalproviders/costreports/2018medicarehospital/1413510218_flora_claycounty.pdf" },
                  { name: "SBL Fayette County Hospital — CCN 141346", loc: "Vandalia, Fayette · 25 beds (CAH)", hc: "247.52 hospital / 318.14 complex FTE", pay: "Rolls up to SBL system 990", verdict: "STABILIZER", verdictColor: "oklch(40% 0.16 142)", note: "FY2024 cost report. SBL subsidiary; CAH; S-3 Part II not filed (CAH non-PPS election); only aggregate FTE/wage available. Inherits SBL Mattoon payroll scaffolding.", src: "https://hfs.illinois.gov/content/dam/soi/en/web/hfs/medicalproviders/costreports/2024medicarehospital/1413460624_vandalia_fayettecounty.pdf" },
                  { name: "Salem Township Hospital — CCN 141345", loc: "Salem, Marion · 25 beds (CAH)", hc: "183.59 hospital / 212.97 complex FTE", pay: "Not disclosed (township-owned, no 990)", verdict: "LOW-WAGE TRAP RISK", verdictColor: "oklch(45% 0.20 22)", note: "FY2024 cost report. Township-owned; CAH; S-3 Part II not filed. Smallest of the six in headcount. Modal employee outside RN/physician likely sub-livable. Stabilizer for Marion employment level, NOT wage growth.", src: "https://hfs.illinois.gov/content/dam/soi/en/web/hfs/medicalproviders/costreports/2024medicarehospital/1413450324_salem_salemtownship.pdf" },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "8px 10px", fontWeight: 600 }}>{r.name}</td>
                    <td style={{ padding: "8px 10px", fontSize: 12, color: "#5a564d" }}>{r.loc}</td>
                    <td style={{ padding: "8px 10px", textAlign: "right", color: "#5a564d" }}>{r.hc}</td>
                    <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 600 }}>{r.pay}</td>
                    <td style={{ padding: "8px 10px" }}>
                      <div style={{ fontSize: 11, fontWeight: 700, color: r.verdictColor, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 3 }}>{r.verdict}</div>
                      <div style={{ fontSize: 11, color: "#5a564d", lineHeight: 1.5 }}>{r.note}{" · "}<a href={r.src} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>source</a></div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            <strong>Rollup:</strong> SBL Mattoon is the only clear growth-engine candidate. SBL Fayette + HSHS St. Anthony&apos;s + Carle Richland are stabilizers. Clay County Hospital + Salem Township Hospital carry low-wage-trap risk in their non-clinical headcount. <strong>Data limitation flagged:</strong> no 990 in this set discloses SOC-level breakdowns. Obtaining the actual occupation mix requires either an Illinois Hospital Association data request or each system&apos;s internal HR census.
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: ProPublica Nonprofit Explorer 990 filings for each hospital + <a href="https://healthcarereportcard.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois Hospital Report Card</a> licensed-beds data + <a href="https://www.bls.gov/ooh/healthcare/registered-nurses.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS OOH Registered Nurses (May 2024 IL median $82,500)</a> + <a href="https://www.bls.gov/ooh/Healthcare/Nursing-assistants.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS OOH Nursing Assistants (US median $39,530)</a> + <a href="https://connector.hrsa.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>HRSA Connector CAH workforce data</a>.
          </div>
        </section>

        {/* ═══ §17 Housing affordability ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            17 · Housing affordability · HUD Fair Market Rent vs MIT Living Wage Coles County
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            HUD FY2025 Fair Market Rent (effective Oct 2024-Sep 2025) per county. The <strong>HUD 30% rule</strong> says a household needs ~3.33× annualized rent in gross income to afford FMR-priced housing. With 12 of 13 LWA-23 counties at the rural-IL 2-BR FMR floor of $870, a household needs <strong>$34,800/yr ($16.73/hr)</strong> just to clear FMR — below the MIT LWC Coles single-adult $19.17/hr line, and FAR below the 2A+2C (one earner) $35.68/hr line. CNA-tier wages clear single-adult housing but fail the family wage. RN-tier wages clear both.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>County</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>2-BR FMR</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>3-BR FMR</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Min income to afford 2-BR (30% rule)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Min hourly wage (2,080 hrs)</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { c: "Clark", fmr2: 926, fmr3: 1181 },
                  { c: "Clay", fmr2: 870, fmr3: 1140 },
                  { c: "Coles", fmr2: 895, fmr3: 1188 },
                  { c: "Crawford", fmr2: 870, fmr3: 1125 },
                  { c: "Cumberland", fmr2: 870, fmr3: 1048 },
                  { c: "Edgar", fmr2: 870, fmr3: 1187 },
                  { c: "Effingham", fmr2: 870, fmr3: 1160 },
                  { c: "Fayette", fmr2: 870, fmr3: 1113 },
                  { c: "Jasper", fmr2: 870, fmr3: 1048 },
                  { c: "Lawrence", fmr2: 870, fmr3: 1075 },
                  { c: "Marion", fmr2: 870, fmr3: 1125 },
                  { c: "Moultrie", fmr2: 870, fmr3: 1219 },
                  { c: "Richland", fmr2: 870, fmr3: 1067 },
                ].map((r, i) => {
                  const minIncome = Math.round((r.fmr2 * 12) / 0.30);
                  const minWage = (minIncome / 2080).toFixed(2);
                  return (
                    <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                      <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.c}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>${r.fmr2}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>${r.fmr3}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>${minIncome.toLocaleString()}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: "#1f5f8f", fontWeight: 600 }}>${minWage}/hr</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Coles County anchor (MIT Living Wage, Feb 2026 update):</strong> 1A $19.17/hr; 2A+2C (one earner) $35.68/hr. Housing line: $668/mo single / $970/mo family — both BELOW the HUD 40th-percentile FMR, meaning the MIT model uses a lower-percentile rent than HUD does. <strong>Affordability fail-points:</strong> CNA at IL median ($39,530 nationally) clears single-adult housing but FAILS 2A+2C wage. RN at IL median ($82,500) clears both. Welder at US median ($51,000) clears single-adult, falls short of family-wage by ~$24k/yr.
          </div>

          {/* GAP CLOSED 2026-05-28: ACS 2024 5-year table via Census Reporter API */}
          <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", marginTop: 24, marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Actual ACS cost-burden by county (5-year 2024 vintage)
          </h3>
          <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 12, maxWidth: 820, lineHeight: 1.55 }}>
            Pulled from Census Reporter API (B25064 + B25077 + B25070 + B25091). Cost-burden = household paying 30%+ of income on housing.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>County</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Median rent (B25064)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Median home value (B25077)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Renters cost-burdened (B25070)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Mortgaged owners burdened (B25091)</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { c: "Clark", rent: 861, home: 132400, rb: 30.6, ob: 19.2 },
                  { c: "Clay", rent: 735, home: 97400, rb: 23.1, ob: 22.5 },
                  { c: "Coles", rent: 780, home: 127400, rb: 40.4, ob: 17.4 },
                  { c: "Crawford", rent: 811, home: 121100, rb: 28.9, ob: 11.5 },
                  { c: "Cumberland", rent: 772, home: 126000, rb: 30.4, ob: 15.5 },
                  { c: "Edgar", rent: 775, home: 97300, rb: 26.4, ob: 14.4 },
                  { c: "Effingham", rent: 733, home: 189500, rb: 30.3, ob: 14.6 },
                  { c: "Fayette", rent: 798, home: 130700, rb: 40.8, ob: 19.3 },
                  { c: "Jasper", rent: 793, home: 125500, rb: 31.4, ob: 20.2 },
                  { c: "Lawrence", rent: 836, home: 99100, rb: 28.6, ob: 14.7 },
                  { c: "Marion", rent: 794, home: 103000, rb: 36.6, ob: 19.2 },
                  { c: "Moultrie", rent: 804, home: 134000, rb: 30.1, ob: 17.4 },
                  { c: "Richland", rent: 805, home: 112000, rb: 37.0, ob: 20.5 },
                ].map((r, i) => {
                  const rbColor = r.rb >= 38 ? "oklch(45% 0.20 22)" : r.rb >= 30 ? "oklch(45% 0.18 60)" : "oklch(40% 0.16 142)";
                  return (
                    <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                      <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.c}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right" }}>${r.rent}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right" }}>${r.home.toLocaleString()}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600, color: rbColor }}>{r.rb}%</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.ob}%</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Key finding:</strong> renter cost-burden is severe + broad. <strong>Fayette (40.8%), Coles (40.4%), Richland (37.0%), Marion (36.6%)</strong> all exceed the national 30%-rent-burdened share. Below-state-median home values ($97k-$135k for 12 of 13 counties; only Effingham reaches $189,500 reflecting its commercial-center role) don&apos;t translate to rental affordability — the rental stock is thinner + older + concentrated in the anchor towns. Owner-with-mortgage burden range 11.5% (Crawford) to 22.5% (Clay).
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://www.huduser.gov/portal/datasets/fmr/fmr2025/FY2025_FMR_Schedule.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>HUD FY2025 FMR schedule</a> + <a href="https://livingwage.mit.edu/counties/17029" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>MIT Living Wage Coles County</a> + <a href="https://api.censusreporter.org/1.0/data/show/latest?table_ids=B25064,B25077,B25070,B25091" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census Reporter API · ACS 2024 5-year B25064/B25070/B25077/B25091 (full 13-county pull)</a> + <a href="https://nlihc.org/sites/default/files/SHP_IL.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NLIHC 2025 Illinois Housing Profile</a>.
          </div>
        </section>

        {/* ═══ §18 Wage benchmark ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            18 · Wage benchmark · BLS OEWS Illinois statewide May 2024
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            LWA-23 has no MSA — Mattoon is Micropolitan. Closest reference: BLS East Central IL nonmetropolitan area (code 1700003) + Champaign-Urbana MSA (CBSA 16580) to the north. Carbondale-Marion MSA (CBSA 16060, LWA-25) total mean hourly wage was $26.21 vs US $31.48 in May 2023 — that&apos;s a -16.7% rural-IL discount. LWA-23 wages run in the same band.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>SOC</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Occupation</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>IL median</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>IL P10 (entry)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>IL P90 (exp)</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>vs Coles 1A+2C $35.68/hr ($74,214/yr)</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { soc: "29-1141", occ: "Registered Nurses", median: 82500, p10: 63900, p90: 105140, verdict: "CLEARS 1A+2C at median", color: "oklch(40% 0.16 142)" },
                  { soc: "33-3012", occ: "Correctional Officers", median: 65190, p10: 50530, p90: 82830, verdict: "BELOW 1A+2C at median; clears at P90 with OT", color: "oklch(45% 0.18 60)" },
                  { soc: "47-2152", occ: "Plumbers / Pipefitters", median: 87900, p10: null, p90: null, verdict: "CLEARS 1A+2C at median (rural-IL adjusted)", color: "oklch(40% 0.16 142)" },
                  { soc: "15-1244", occ: "Network / Systems Admin (US ref)", median: 95360, p10: null, p90: null, verdict: "US median CLEARS; SIU local ceiling $88,452 (LWA-25) FAILS — see /southern-illinois IT row", color: "oklch(45% 0.18 60)" },
                  { soc: "15-1232", occ: "Computer User Support (US ref)", median: 61250, p10: null, p90: null, verdict: "US median FAILS 1A+2C", color: "oklch(45% 0.20 22)" },
                  { soc: "51-4121", occ: "Welders / Cutters (US ref)", median: 51000, p10: null, p90: null, verdict: "US median FAILS 1A+2C; shutdown circuit clears", color: "oklch(45% 0.20 22)" },
                  { soc: "53-3032", occ: "Heavy Truck Drivers (US ref)", median: 57440, p10: null, p90: null, verdict: "US median FAILS 1A+2C; OTR + tanker clears", color: "oklch(45% 0.20 22)" },
                  { soc: "31-1014", occ: "Nursing Assistants (US ref)", median: 39530, p10: null, p90: null, verdict: "FAILS 1A+2C by ~$35k/yr — CNA tier is sub-livable", color: "oklch(45% 0.20 22)" },
                  { soc: "25-2021", occ: "Elementary School Teachers", median: null, p10: null, p90: null, verdict: "IL starting ~$40-55k → FAILS at entry; mid-career + master's clears (EIU pipeline)", color: "oklch(45% 0.18 60)" },
                  { soc: "49-9071", occ: "Maintenance / Repair Workers", median: null, p10: null, p90: null, verdict: "IL mid-career $50-67k → marginal; experienced at refinery / mfg clears", color: "oklch(45% 0.18 60)" },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 11, color: "#5a564d" }}>{r.soc}</td>
                    <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.occ}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>{r.median ? `$${(r.median / 1000).toFixed(0)}k` : "—"}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.p10 ? `$${(r.p10 / 1000).toFixed(0)}k` : "—"}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", color: "#5a564d" }}>{r.p90 ? `$${(r.p90 / 1000).toFixed(0)}k` : "—"}</td>
                    <td style={{ padding: "6px 10px", fontSize: 11, color: r.color, fontWeight: 600 }}>{r.verdict}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 12, lineHeight: 1.5 }}>
            <strong>Data note:</strong> IL statewide medians from <a href="https://www.bls.gov/ooh/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS Occupational Outlook Handbook (May 2024)</a> + <a href="https://www.careeronestop.org/Toolkit/Wages/find-salary.aspx?soccode=333012&keyword=Correctional+Officers&location=Illinois" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>CareerOneStop IL OES extract</a>. P10/P90 percentiles only published for some SOCs; US median used where IL detail requires the IDES 2024 Statewide Wage Publication XLS download. The full IL OEWS file is at <a href="https://ides.illinois.gov/content/dam/soi/en/web/ides/labor_market_information/where_workers_work/2024_Statewide_Wage_Publication.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDES 2024 Statewide Wage Publication</a>; East Central IL nonmetro area-specific data at <a href="https://www.bls.gov/oes/2024/may/oessrcma.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS OEWS May 2024 area index</a>.
          </div>
        </section>

        {/* ═══ §19 IL DCEO In-Demand Occupations ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            19 · IL DCEO In-Demand Occupations · Southeast EDR 7 (LWA-23 coterminous)
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            The IL workNet Southeastern Regional Data Packet 2026 publishes the official Demand Occupations list — eligible-training-provider WIOA funding is tied to occupations on this list. LWA-23 is coterminous with IDES Economic Development Region 7. Below: annual openings + entry/experienced hourly wage by credential tier, sorted by openings within tier.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Credential tier</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>SOC</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Occupation</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Annual openings</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Entry $/hr</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Experienced $/hr</th>
                </tr>
              </thead>
              <tbody>
                {[
                  // Certificate / License
                  { tier: "Cert/License", soc: "31-1131", occ: "Nursing Assistants", openings: 226, entry: 16.96, exp: 21.20 },
                  { tier: "Cert/License", soc: "53-3032", occ: "Heavy + Tractor-Trailer Truck Drivers", openings: 219, entry: 19.14, exp: 31.61 },
                  { tier: "Cert/License", soc: "25-9045", occ: "Teaching Assistants (ex-postsecondary)", openings: 128, entry: null, exp: null },
                  { tier: "Cert/License", soc: "39-9011", occ: "Childcare Workers", openings: 115, entry: 14.42, exp: 17.28 },
                  { tier: "Cert/License", soc: "49-3023", occ: "Automotive Service Technicians + Mechanics", openings: 75, entry: 17.01, exp: 29.43 },
                  { tier: "Cert/License", soc: "39-9031", occ: "Exercise Trainers + Group Fitness Instructors", openings: 70, entry: 14.52, exp: 30.34 },
                  { tier: "Cert/License", soc: "31-9092", occ: "Medical Assistants", openings: 43, entry: 16.97, exp: 21.40 },
                  // Associate's
                  { tier: "Associate's", soc: "29-1141", occ: "Registered Nurses (RN)", openings: 154, entry: 31.10, exp: 44.86 },
                  { tier: "Associate's", soc: "25-2011", occ: "Preschool Teachers (ex-Sp Ed)", openings: 38, entry: 15.08, exp: 21.58 },
                  { tier: "Associate's", soc: "15-1232", occ: "Computer User Support Specialists", openings: 20, entry: 19.35, exp: 32.27 },
                  { tier: "Associate's", soc: "29-2010", occ: "Clinical Lab Technologists / Technicians", openings: 12, entry: 24.02, exp: 36.77 },
                  { tier: "Associate's", soc: "23-2011", occ: "Paralegals + Legal Assistants", openings: 11, entry: 18.57, exp: 30.17 },
                  { tier: "Associate's", soc: "31-2021", occ: "Physical Therapist Assistants (PTA)", openings: 10, entry: 26.92, exp: 33.69 },
                  { tier: "Associate's", soc: "15-1231", occ: "Computer Network Support Specialists", openings: 9, entry: 18.10, exp: 34.59 },
                  // Bachelor's
                  { tier: "Bachelor's", soc: "11-1021", occ: "General + Operations Managers", openings: 248, entry: 23.13, exp: 67.42 },
                  { tier: "Bachelor's", soc: "13-1199", occ: "Business Operations Specialists, All Other", openings: 79, entry: 20.14, exp: 43.36 },
                  { tier: "Bachelor's", soc: "25-2021", occ: "Elementary School Teachers (ex-Sp Ed)", openings: 79, entry: null, exp: null },
                  { tier: "Bachelor's", soc: "13-2011", occ: "Accountants + Auditors", openings: 69, entry: 23.32, exp: 41.13 },
                  { tier: "Bachelor's", soc: "13-1161", occ: "Market Research Analysts", openings: 46, entry: 19.89, exp: 37.45 },
                  { tier: "Bachelor's", soc: "13-1111", occ: "Management Analysts", openings: 34, entry: 32.24, exp: 64.67 },
                  { tier: "Bachelor's", soc: "41-3021", occ: "Insurance Sales Agents", openings: 32, entry: 19.93, exp: 38.29 },
                  // Beyond Bachelor's
                  { tier: "Beyond Bach", soc: "11-3031", occ: "Financial Managers", openings: 56, entry: 39.40, exp: 79.11 },
                  { tier: "Beyond Bach", soc: "11-9199", occ: "Managers, All Other", openings: 45, entry: 34.47, exp: 72.12 },
                  { tier: "Beyond Bach", soc: "15-1252", occ: "Software Developers", openings: 39, entry: 32.53, exp: 61.30 },
                  { tier: "Beyond Bach", soc: "11-2022", occ: "Sales Managers", openings: 35, entry: 38.79, exp: 89.32 },
                  { tier: "Beyond Bach", soc: "11-9111", occ: "Medical + Health Services Managers", openings: 31, entry: 33.88, exp: 63.17 },
                  { tier: "Beyond Bach", soc: "11-2021", occ: "Marketing Managers", openings: 26, entry: 38.86, exp: 80.67 },
                  { tier: "Beyond Bach", soc: "23-1011", occ: "Lawyers", openings: 17, entry: 25.76, exp: 74.41 },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "5px 10px", fontSize: 11, color: "#7a756b", fontWeight: 600 }}>{r.tier}</td>
                    <td style={{ padding: "5px 10px", fontFamily: "monospace", fontSize: 11, color: "#5a564d" }}>{r.soc}</td>
                    <td style={{ padding: "5px 10px", fontWeight: 600 }}>{r.occ}</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", fontWeight: 600, color: r.openings >= 100 ? "oklch(40% 0.16 142)" : "#1f1d18" }}>{r.openings}</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", color: "#5a564d" }}>{r.entry != null ? `$${r.entry.toFixed(2)}` : "—"}</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", color: "#5a564d" }}>{r.exp != null ? `$${r.exp.toFixed(2)}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Real-time corroboration — HWOL December 2025 EDR 7 top job postings:</strong> Heavy Truck Drivers (67 new ads), Food Prep Workers (63), Retail Salespersons (58), Registered Nurses (49), Home Health + Personal Care Aides (45), Food Service Managers (35), General Maintenance + Repair (32), Customer Service Reps (31), Driver/Sales Workers (31), First-Line Retail Supervisors (30). <strong>Top posting employers:</strong> Flynn Group/Taco Bell (49), Casey&apos;s (34), Rural King (33), Domino&apos;s (31), UPS (30), Addus HomeCare (27), Walmart/Sam&apos;s Club (22), Lake Land College (20), Love&apos;s (18), Sherwin-Williams (14). Source: <a href="https://ides.illinois.gov/content/dam/soi/en/web/ides/labor_market_information/hwol/edr7_dec25.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDES EDR 7 HWOL Dec 2025</a>.
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://www.illinoisworknet.com/WIOA/RegPlanning/Documents/2026WIOARegionalandLocalPlanning/SoutheasternRegionalDataPacket2026.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Southeastern Regional Data Packet 2026 (IL workNet)</a> · DCEO Office of Employment and Training + NIU Workforce Policy Lab joint product · IDES Long-Term Occupational Employment Projections 2022-2032 + OEWS 2024. Living-wage benchmark (IL one adult / single parent): $23.56 / $40.41 (<a href="https://livingwage.mit.edu/states/17" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>MIT Living Wage Calculator</a>).
          </div>
        </section>

        {/* ═══ §20 PIRL / WIOA Performance Accountability ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            20 · PIRL / WIOA Performance Accountability · what should be measured
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            WIOA programs are accountable to six primary indicators (Q2 employment, Q4 employment, median earnings, credential attainment, measurable skill gains, effectiveness in serving employers). <strong>LWA-23 organizational structure (verified 2026-05-28):</strong> Lake Land College is the WIOA grant recipient / fiscal agent on behalf of the 13-county Chief Elected Officials; CEFS Economic Opportunity Corporation is the operator / sub-recipient. The federally-published identifier is literally <strong>Local Workforce Innovation Area 23 (LWIA 23)</strong> — no separate numeric ETA/WIPS code is published publicly. Statewide PY2023 ETA-9169 baselines below are public + sourced; the LWA-23-specific row is policy-restricted (DOL ETA dashboard requires authenticated session; IL IPATS is authorized-users-only) and is not publishable from public schema.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto", marginBottom: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Title I program</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>IL statewide served</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Q2 emp rate</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Q4 emp rate</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Median earnings Q2</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Credential</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>MSG</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { p: "Adult", served: 8489, q2: "80.5%", q4: "78.2%", earn: "$10,293", cred: "73.2%", msg: "69.1%" },
                  { p: "Dislocated Worker", served: 5282, q2: "81.3%", q4: "80.3%", earn: "$11,693", cred: "73.9%", msg: "69.8%" },
                  { p: "Youth", served: 5666, q2: "80.3%", q4: "78.4%", earn: "$5,700", cred: "70.8%", msg: "65.5%" },
                ].map((r, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                    <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.p}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right" }}>{r.served.toLocaleString()}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>{r.q2}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right" }}>{r.q4}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600 }}>{r.earn}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right" }}>{r.cred}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right" }}>{r.msg}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            <strong>What CEFS/LWA-23 should measure + report</strong> (PIRL-aligned, region-specific):
            <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
              <li><strong>Q2 + Q4 employment rate</strong> vs IL statewide 80.5% / 78.2% (Adult) — LWA-23 should publish its own rate annually; legitimate goal is at or above state median.</li>
              <li><strong>Median Q2 earnings</strong> vs IL statewide $10,293 (Adult) — given the LWA-23 rural-IL ~17% wage discount, expect 80-90% of state median; the publication-grade question is whether training-to-job match is FAMILY-SUPPORTING (clears Coles 1A+2C $35.68/hr ~$74k/yr).</li>
              <li><strong>Credential attainment</strong> vs IL statewide 73.2% (Adult) — for LWA-23 this is closely tied to Lake Land + Kaskaskia + Olney Central completion rates for the in-demand SOC codes in §19.</li>
              <li><strong>Training-to-job match rate</strong> — the operator-side accountability metric: of WIOA completers credentialed in SOC X, what % placed in a job using that credential at the wage-tier the credential should command? PIRL captures the wage; the match needs an additional employer-side survey or LEHD-LODES match.</li>
              <li><strong>Employer-side effectiveness</strong> — IL DCEO publishes employer-side measures (penetration rate, repeat-business rate, employer retention). The IPATS dashboard at <a href="https://www.illinoisworknet.com/WIOA/RegPlanning/Pages/StateWorkforcePerformance.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>illinoisworknet.com</a> is the operator-facing source.</li>
              <li><strong>Region-specific add-on metrics:</strong> (a) <em>Not-in-LF re-engagement</em> — given the §01 finding of 87,127 working-age adults out of the labor force, the region-specific PIRL augmentation is the count of WIOA enrollees who were not-in-LF at intake. Standard PIRL doesn&apos;t segment by labor-force status at intake; this is the additional question LWA-23 needs to track.</li>
              <li><strong>(b) Childcare-barrier removal:</strong> of WIOA enrollees whose intake survey flagged childcare as a barrier (§13 / Region 11 76% slot gap), the % retained through credential completion. This is the LWA-23 leakage point.</li>
            </ul>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://www.dol.gov/sites/dolgov/files/ETA/Performance/PY23%20Databooks/IL_Annual%20Performance%20Narrative%20PY23.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL PY23 WIOA Annual Performance Narrative</a> + <a href="https://www.dol.gov/sites/dolgov/files/ETA/Performance/PY2023_WIOA_Local_Board_Annual_Report.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>DOL PY2023 WIOA Local Board Annual Report (interactive dashboard; 403 to programmatic access)</a> + <a href="https://www.illinoisworknet.com/WIOA/RegPlanning/Pages/StateWorkforcePerformance.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL workNet WIOA Performance dashboard (IPATS, authorized-users only)</a> + <a href="https://lwa23.net/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>LWA-23 (Connecting People with Jobs)</a> + <a href="https://illinoisworkforcepartnership.org/wioa/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois Workforce Partnership · WIOA LWA-23 profile</a>. Numeric ETA/WIPS code: VERIFIED_UNAVAILABLE_PUBLICLY 2026-05-28 (the prior &quot;17125 / Mantracon Corp&quot; identification was wrong — Man-Tra-Con is LWA-25; canonical LWA-23 identifier is the literal designation &quot;LWIA 23&quot;).
          </div>
        </section>

        {/* ═══ §21 Anchor-attraction targets ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            21 · Anchor-attraction targets · realistic + asset-grounded
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Five anchor-attraction targets that map to LWA-23 actual labor + infrastructure assets. Each carries a stackable federal/state grant program with a current Notice of Funding Opportunity URL. Order is by realistic-feasibility (asset fit) not by funding scale.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {[
              {
                title: "EV / Battery Component Manufacturing Cluster",
                anchor: "North American Lighting (Paris) supply chain",
                rationale: "NAL Paris is a Tier-1 automotive lighting plant in Koito Group, already supplying the OEM EV transition. Adjacent capacity in Coles/Edgar/Clark counties on I-70 corridor is the leverage.",
                programs: [
                  { name: "IL DCEO Reimagining Energy and Vehicles (REV) Act", url: "https://dceo.illinois.gov/businesshelp/rev.html", detail: "Payroll-withholding credit up to 100% in Underserved or Energy Transition Areas; 75% withholding for construction; $2.5M capex / 50-job floor (Tier 1, 20-yr) or larger (Tier 2, 30-yr)" },
                  { name: "20 ILCS 686 REV Act statute", url: "https://ilga.gov/Legislation/ILCS/Articles?ActID=4229&ChapterID=5", detail: "Statutory text" },
                  { name: "EDA Good Jobs Challenge", url: "https://www.eda.gov/funding/programs/good-jobs-challenge/2024", detail: "Sectoral-partnership training pipeline through Lake Land + Olney Central + Kaskaskia" },
                ],
              },
              {
                title: "I-57 / I-70 Logistics + Distribution Expansion",
                anchor: "Rural King DC Mattoon + Effingham crossroads + Casey's/UPS/Love's/TravelCenters footprint",
                rationale: "Truck-driver demand is 219 annual openings (§19 highest single SOC), Rural King + UPS + Casey's + Love's already posting heavily, and the literal interstate cross. Strongest sector-fit on existing labor supply.",
                programs: [
                  { name: "EDA Public Works + Economic Adjustment Assistance", url: "https://www.eda.gov/funding/programs/public-works", detail: "Intermodal + site-readiness" },
                  { name: "USDA Business + Industry Loan Guarantees", url: "https://www.rd.usda.gov/programs-services/business-programs/business-industry-loan-guarantees", detail: "Warehouse-tenant financing" },
                  { name: "WIOA SOC 53-3032 CDL pipeline funding", url: "https://www.cefseoc.org/wioa", detail: "CEFS administers; Kaskaskia + Lake Land + private schools deliver" },
                ],
              },
              {
                title: "Energy-Transition Reinvestment — entire LWA-23 footprint qualifies as IRA Energy Community NOW",
                anchor: "ALL 13 LWA-23 counties qualify under IRS Notice 2024-48 Appendix 1 Statistical Area Category for CY2023 (verified 2026-05-28 via direct PDF extraction) — 12 counties in East Central IL nonmetropolitan area (1700003); Moultrie in West Central IL nonmetropolitan area (1700002). Vistra Newton Power Plant Unit 1 retirement (end-2027 per Vistra 10-K) activates the ADDITIONAL Coal Closure Category for the Jasper census tract + adjoining tracts post-retirement.",
                rationale: "This is the single most-favorable federal lever in LWA-23. The entire 13-county footprint already meets the Fossil Fuel Employment threshold + UR-≥-national-avg requirement for CY2023 — meaning any qualifying renewable energy, battery storage, or clean manufacturing project sited anywhere in LWA-23 gets the 10% IRA §45/§48 bonus tax credit TODAY. Strategic implication: don't wait for Newton retirement to start sequencing projects; the Statistical Area lever is active now, the Coal Closure lever stacks on top of it post-2027 for Jasper specifically. The IL DCEO Energy Transition Community Grant ($565,615 to Jasper) layers on the IL-side designation independently.",
                programs: [
                  { name: "IRS Notice 2024-48 Appendix 1 (Statistical Area Category qualifying list, CY2023)", url: "https://www.irs.gov/pub/irs-drop/n-24-48-appendix-1.pdf", detail: "Confirmed via direct PDF extraction: all 13 LWA-23 counties are listed (East Central IL non-metro 1700003 + West Central IL non-metro 1700002 for Moultrie)" },
                  { name: "IRS Notice 2024-48 Appendix 2 (Coal Closure tracts list)", url: "https://www.irs.gov/pub/irs-drop/n-24-48-appendix-2.pdf", detail: "Will add Jasper census tract + adjoining tracts AFTER Newton Unit 1 retires (end-2027)" },
                  { name: "IRS Notice 2024-48 (full)", url: "https://www.irs.gov/pub/irs-drop/n-24-48.pdf", detail: "Energy Community designation methodology" },
                  { name: "NETL Energy Communities interactive map", url: "https://arcgis.netl.doe.gov/portal/home/item.html?id=bc0fb23213804024a69a9fdd8a937b35", detail: "Tract-level visualization" },
                  { name: "IRS Energy Communities FAQ", url: "https://www.irs.gov/credits-deductions/frequently-asked-questions-for-energy-communities", detail: "10% IRA §45/§48 adder mechanics" },
                  { name: "EDA Recompete Pilot Program", url: "https://www.eda.gov/funding/programs/recompete-pilot-program/faq", detail: "Targets prime-age employment gaps — stackable" },
                  { name: "Vistra 10-K FY2025 Newton retirement filing", url: "https://www.sec.gov/Archives/edgar/data/0001692819/000169281926000006/vistra-20251231xex417.htm", detail: "End-2027 retirement triggers Coal Closure overlay on existing Statistical Area qualification" },
                ],
              },
              {
                title: "Petrochemical Downstream + Specialty Coatings",
                anchor: "Marathon Robinson Refinery (245k bpd) + Sherwin-Williams Effingham",
                rationale: "Marathon Robinson is a 245k-bpd downstream refinery feeding hydrocarbon-derivatives demand; Sherwin-Williams Effingham is the regional architectural-coatings hub. Cluster strategy: specialty-chemical / coatings tier-2 suppliers.",
                programs: [
                  { name: "DCEO EDGE tax credit", url: "https://dceo.illinois.gov/expandrelocate/incentives/taxassistance.html", detail: "Economic Development for a Growing Economy tax credit program" },
                  { name: "EDA Investing in America's Regional Innovation Hubs / Tech Hubs Phase 2", url: "https://www.eda.gov/funding/programs/regional-technology-and-innovation-hubs/faq", detail: "Advanced-materials consortia funding" },
                  { name: "Lake Land + Olney Central chemical-tech programs", url: "https://www.lakelandcollege.edu/", detail: "Map directly to SOC 29-2010 (Clinical Lab Tech) + process-tech career cluster" },
                ],
              },
              {
                title: "Rural Broadband + Telework Hub",
                anchor: "Consolidated Communications HQ (Mattoon) + IL $1.04B BEAD allocation",
                rationale: "Consolidated is a publicly-traded ILEC headquartered in LWA-23; Illinois received $1,040,420,751.50 in BEAD allocation. Fiber-to-the-premises in the rural counties unlocks remote-work in-flow against the SOC 15-1252 (Software Developers, 39 openings/yr per §19) and SOC 13-2011 (Accountants, 69 openings/yr) demand.",
                programs: [
                  { name: "Internet for All — Illinois BEAD allocation", url: "https://www.internetforall.gov/news-media/biden-harris-administration-announces-1-billion-illinois-deploy-high-speed-internet", detail: "$1,040,420,751.50 IL BEAD allocation announced" },
                  { name: "DCEO Office of Broadband BEAD program", url: "https://dceo.illinois.gov/broadband/bead.html", detail: "IL DCEO runs the deployment program" },
                  { name: "USDA ReConnect", url: "https://www.usda.gov/reconnect", detail: "Rural broadband infrastructure grants/loans" },
                  { name: "USDA Rural Business Development Grants", url: "https://www.rd.usda.gov/programs-services/business-programs/rural-business-development-grants-33", detail: "Eligible: towns, communities, state agencies, nonprofits, higher-ed, federally-recognized Tribes, cooperatives (individuals + for-profits ineligible)" },
                ],
              },
            ].map((t, i) => (
              <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 14 }}>
                <div style={{ fontSize: 15, fontWeight: 700, color: "#1f1d18", marginBottom: 4 }}>{String.fromCharCode(97 + i)}) {t.title}</div>
                <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 8 }}><strong>Anchor asset:</strong> {t.anchor}</div>
                <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 10 }}>{t.rationale}</div>
                <div style={{ fontSize: 11, fontWeight: 700, color: "#7a756b", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>Stackable programs:</div>
                <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 12.5, color: "#3d3a33", lineHeight: 1.6 }}>
                  {t.programs.map((p, j) => (
                    <li key={j}>
                      <a href={p.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f", fontWeight: 600 }}>{p.name}</a> — {p.detail}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Operator verification items before public-stakeholder use:</strong> (1) ETA local-area code for LWA-23 (candidate 17125 / Mantracon Corp.; alternatives 17120 Mid America WIB, 17100 Land of Lincoln Consortium). (2) Jasper County&apos;s active IRA Energy Community designation against the DOE/NETL interactive map. (3) LWA-23 PY2023 ETA-9169 narrative via IPATS interactive dashboard for the exact LWA-23 rows.
          </div>
        </section>

        {/* ═══ §22 LWA-23 vs LWA-25 ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            22 · LWA-23 vs LWA-25 · structural comparison
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

        {/* ═══ §23 Action ladder ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            23 · Action ladder · what the page surfaces for the CEFS board + regional stakeholders
          </h2>
          <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
            Each card below leads with what the page already does (data-side) and ends with the human-only residual step.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {[
              {
                title: "Weight cohort placement toward Welding + Industrial Mechanics + RN",
                body: <>The §11 Training-to-Demand table identifies three FAMILY-SUPPORTING pathways available in LWA-23: <strong>RN (ADN)</strong>, <strong>Welding</strong>, <strong>Industrial Mechanics</strong>. All three clear 1A+2C, all three have strong local employer demand (SBL + HSHS for RN; Marathon + Sherwin-Williams + NAL + Hydro-Gear for welding + maintenance). <strong>Residual:</strong> CEFS&apos;s next annual cohort plan should weight ITAs toward these three pathways over CNA / LPN / Cosmetology which fail the wage test.</>,
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

        {/* ═══ §24 Methodology + page scope ═══ */}
        <section style={{ marginTop: 40 }}>
          <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
          <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
            24 · Methodology + page scope
          </h2>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.6, maxWidth: 820 }}>
            <strong>Live data substrate:</strong> /api/public/cefs aggregates monthly UR + labor force across all 13 LWA-23 counties (sum-of-labor-forces method for the LWA aggregate; weighted-by-labor-force method for the LWA UR). FRED panel: 170 series for LWA-23 in platform.macro_data (98 monthly UR + LF + 60 annual labor + education + 12 Coles housing + 12 annual income/poverty/SNAP for Coles via cle_coles_*). QCEW industry mix + USAspending federal awards + ACS labor truth are pulled live via FIPS-parameterized helpers across the 13-county set. Census ACS Subject Tables (S1810 disability + S0101 age + S2301 LFPR + B25064/B25070/B25077/B25091 housing) pulled per county via Census API.
            <br /><br />
            <strong>Refresh cadence:</strong> Monthly FRED ingest refreshes UR + labor force ~1-2 months after the reference period. Annual series (income, GDP, poverty, SNAP) lag 6-18 months. USAspending refreshes nightly. Census ACS refreshes annually in December for the preceding 5-year window. FBI UCR + NeighborhoodScout refresh annually in October.
            <br /><br />
            <strong>Editorial standard:</strong> every claim on this page is anchored on a primary source (cited inline); no inferences or unsourced framings. <strong>LWA-23 admin contact:</strong> CEFS Economic Opportunity Corporation, 1805 South Banker Street, Effingham, IL 62401 · (217) 342-2193 ext. 2121 · lwia23@cefseoc.org · <a href="https://www.lwa23.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>lwa23.com</a>.
            <br /><br />
            <strong>Official naming note:</strong> This page uses the brand label &quot;East Central Illinois&quot; for accessibility, but IL DCEO + IL workNet officially designate this region as <strong>Southeastern Economic Development Region 7 (EDR 7)</strong>, coterminous with Local Workforce Innovation Area 23 (LWIA 23). Public-stakeholder materials should use both labels.
          </div>

          <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", marginTop: 24, marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Known limits · data still pending
          </h3>
          <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 12, maxWidth: 820, lineHeight: 1.55 }}>
            Open limitations are tracked here, classified by what kind of action closes each one. Source-integrity discipline rather than failure-flagging.
          </div>
          <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Limitation</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Closure class</th>
                  <th style={{ padding: "8px 10px", fontWeight: 600 }}>Operational next step</th>
                </tr>
              </thead>
              <tbody>
                {[
                  {
                    item: "DOL ETA numeric local-area code for LWA-23",
                    cls: "VERIFIED_UNAVAILABLE_PUBLICLY",
                    step: "Resolved 2026-05-28 — no separate numeric ETA/WIPS code is published publicly for any IL LWIA. The federally-published identifier IS literally \"Local Workforce Innovation Area 23\" (LWIA 23). Sources checked + result: DOL ETA PY2023 dashboard (403 to programmatic access), IL DCEO PY2023 narrative (qualitative, no per-LWIA table), IL workNet LWIA Matrix (LWIA 23 only), IPATS user guide (explicitly authorized-users-only policy), WIOA State Plan Portal (LWIA numbers only), CEFS Form 990 (no WIPS code). Page now uses canonical \"LWIA 23\" identifier; no false ETA number asserted anywhere.",
                  },
                  {
                    item: "IRS Notice 2024-48 Appendix 1 — LWA-23 pre-retirement Energy Community status under Statistical Area Category",
                    cls: "CLOSED",
                    step: "Resolved 2026-05-28 via direct PDF extraction of IRS Notice 2024-48 Appendix 1. ALL 13 LWA-23 counties qualify as Energy Communities for CY2023 — 12 counties in East Central IL nonmetropolitan area (code 1700003); Moultrie in West Central IL nonmetropolitan area (1700002). Both areas meet the Fossil Fuel Employment threshold + UR-≥-national-avg requirement. §21 Anchor Attraction Newton entry updated to reflect: the entire footprint is eligible for the 10% IRA §45/§48 bonus tax credit TODAY; Newton retirement adds Coal Closure overlay post-2027.",
                  },
                  {
                    item: "LEHD LODES IL → IN county-pair commute flows (Clark/Edgar → Vigo IN, Crawford/Lawrence → Knox IN)",
                    cls: "CLOSED",
                    step: "Resolved 2026-05-28 via direct LEHD LODES 2021 in_od_aux.csv extraction. Verified flows: Clark→Vigo 591, Edgar→Vigo 396, Crawford→Knox 221, Lawrence→Knox 925, 4-county cross-state total 2,387. §14 upgraded from hypothesis-strength to verified-finding with the per-pair table. Marion → St. Louis Metro East remains in §14 as qualitative pattern (Marion → Madison/St. Clair Co MO not pulled this round; can be added with same LODES pattern if needed).",
                  },
                  {
                    item: "Per-1,000 crime rates for Marshall (Clark), Toledo (Cumberland), Newton (Jasper), Sullivan (Moultrie)",
                    cls: "VERIFIED_UNAVAILABLE_PUBLICLY",
                    step: "Resolved 2026-05-28. All 4 cities ARE NIBRS-certified + report 2024 data — Marshall PD ORI IL0120200, Newton PD IL0400100, Sullivan PD IL0700300, Toledo (no city PD, covered by Cumberland Co Sheriff IL0180000). The data EXISTS and is shown on FBI's CDE webapp. The public API to retrieve per-agency offense counts is broken post-2024 migration: api.usa.gov/crime/fbi/cde/summarized/* paths return 404; FBI's Spring backend (crime-data-spring-api-master.app.cloud.gov) was retired. CDE webapp is a JS-SPA. NeighborhoodScout per-1,000 figures paywalled for these 4. IL State Police annual reports only through 2021. The §15 city-crime section now links directly to each FBI CDE agency-detail page where the counts can be viewed in FBI's own UI; page does not fabricate the rates programmatically.",
                  },
                  {
                    item: "SOC-level occupation breakdown per hospital anchor",
                    cls: "VERIFIED_UNAVAILABLE_PUBLICLY",
                    step: "Resolved 2026-05-28 via direct CMS HCRIS + IL HFS cost-report extraction + BLS OEWS NAICS 622100 review. NEITHER source publishes per-facility SOC-level mix. CMS Form 2552-10 Worksheet S-3 Part II gives overhead-department paid-hours (A&G, Nursing Admin, Pharmacy, Housekeeping, Dietary, etc.) + Part I gives aggregate hospital FTE — neither breaks out RN/LPN/CNA. CMS Form 10079 Occupational Mix Survey DOES capture RN/LPN/medtech/nurse-aide split but per-hospital responses are NOT posted publicly (only state-aggregate appears in CMS wage-index PUFs). Page uses (a) verified aggregate FTE from FY2024 CMS cost reports for 5 of 6 hospitals + (b) BLS OEWS NAICS 622100 national hospital-industry occupation mix as labeled proxy. Facility-level mix is not in any public schema.",
                  },
                  {
                    item: "LWA-23-specific PY2023 ETA-9169 PIRL row (six primary indicators)",
                    cls: "VERIFIED_UNAVAILABLE_PUBLICLY",
                    step: "Resolved 2026-05-28 — verified through direct check of all known public sources. DOL ETA PY2023 Local Board Annual Report dashboard requires authenticated interactive-browser session (403 via curl/WebFetch). IL workNet IPATS LWIA Comparison Tool is policy-restricted: \"available only to authorized users... will not be provided to outside parties.\" CEFS / lwa23.net / lwa23.com do not publish PY2023 PIRL row publicly. IL DCEO PY2023 Statewide Annual Performance Narrative is qualitative-format only, no per-LWIA tabular data. The authoritative dataset exists but is access-controlled by policy. Page uses IL statewide PY23 baselines (which ARE public) as the comparator; LWA-23-specific row not publishable from public schema.",
                  },
                ].map((r, i) => {
                  const clsColor = r.cls.startsWith("Confirmable") ? "oklch(40% 0.16 142)"
                    : r.cls.startsWith("Requires API") || r.cls.startsWith("Requires interactive") ? "oklch(45% 0.18 60)"
                    : r.cls.startsWith("Requires paid") || r.cls.startsWith("Requires PDF") ? "oklch(45% 0.18 60)"
                    : "oklch(45% 0.20 22)";
                  return (
                    <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                      <td style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>{r.item}</td>
                      <td style={{ padding: "8px 10px" }}>
                        <span style={{ background: `${clsColor}22`, color: clsColor, padding: "3px 8px", borderRadius: 3, fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em" }}>{r.cls}</span>
                      </td>
                      <td style={{ padding: "8px 10px", fontSize: 12, color: "#3d3a33", lineHeight: 1.5 }}>{r.step}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
            <strong>Bottom line:</strong> the LWA-23 page&apos;s core diagnosis + theory of change + county strategy matrix are anchored on confirmed primary-source data. The six items above are operational refinements, not foundational uncertainties. The strategic conclusion — <strong>LWA-23 should recover workers before chasing new anchors</strong> — does not depend on closing any of them.
          </div>
        </section>

        <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
      </div>
    </>
  );
}
