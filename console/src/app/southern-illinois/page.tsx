/**
 * Public /southern-illinois page — 5-county LWA-25 workforce + economic-development dashboard.
 *
 * 5-county service area (Franklin, Jackson, Jefferson, Perry, Williamson).
 * Headline = labor-force-weighted UR across the LWA. Per-county detail.
 * Federal-contract business leads (USAspending) so the board can match
 * sectors with regional demand to local training pipelines.
 */
import { DashboardHead, Topbar, DashboardFooter, DEFAULT_FOOTER_COLUMNS } from "@/components/dashboard-chrome";
import { getMantraconData } from "@/lib/regional-data";

// Self-fetching: regional data layer runs in Vercel (FRED + Census ACS + BLS
// QCEW + USAspending), no console-api / Railway. Daily ISR cache. Faithful TS
// port of console-api public_mantracon() — 5-county LWA-25 aggregate.
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

interface TrainingLadder {
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

interface GdotsSubawardLane {
  naics_code: string;
  naics_name: string;
  subaward_total_usd: number;
  subaward_count: number;
  prime_award_count: number;
  top_sub_recipients: Array<{ name: string; subaward_sum_usd: number }>;
  out_of_region_candidate: boolean;
}
interface GdotsSubawardLanes {
  rows: GdotsSubawardLane[];
  total_subaward_amount_usd: number;
  lookback_months: number;
  source_url: string;
  fetched_at: string;
}

interface GdotsSubawardLaneBulk {
  naics_code: string;
  naics_name: string;
  subaward_total_usd: number;
  subaward_count: number;
  top_sub_recipients: Array<{ name: string; state: string; uei: string; subaward_sum_usd: number }>;
  out_of_region_count: number;
  out_of_region_total_count: number;
  is_services_lane: boolean;
}
interface GdotsSubawardLanesBulk {
  rows: GdotsSubawardLaneBulk[];
  total_subaward_amount_usd: number;
  lookback_months: number;
  source_url: string;
  fetched_at: string;
}

interface PageData {
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
  gdots_subaward_lanes?: GdotsSubawardLanes | null;
  gdots_subaward_lanes_bulk?: GdotsSubawardLanesBulk | null;
}

// ─────────────────────────────────────────────────────────────────────
// LWA-25 town context score · INDEPENDENT calculation for Southern IL
// ─────────────────────────────────────────────────────────────────────
// Internal comparative index (0-100), normalized within the 10-town
// LWA-25 set only. Equal-weighted 4-dimension composite — calculated
// independently from page-local inputs; no /carbondale, /charleston,
// /murphysboro page scores are imported.
//   Safety        (weight 25) = per-town FBI UCR 2024 total crime per
//                               1,000 (NeighborhoodScout/AreaVibes as
//                               labelled FBI-UCR carriers; primary is
//                               FBI UCR 2024), inverted: lower = better.
//   Participation (weight 25) = county LFPR (ACS 2024 5-yr B23025-derived
//                               equivalent of S2301), direct: higher = better.
//   Health        (weight 25) = mean of inverted county disability rate
//                               (ACS B18101→S1810 equivalent) and inverted
//                               county age 65+ share (ACS B01001→S0101
//                               equivalent — REAL 65+%, not median-age
//                               proxy; this is a precision upgrade over
//                               LWA-23's median-age proxy).
//   Housing       (weight 25) = inverted county renter cost-burden share
//                               (ACS B25070).
// Min-max normalized within LWA-25 set. Adverse metrics inverted so
// higher composite = better-ranked within LWA-25.
// Severe-dimension flag (⚠): any single dimension under 25 surfaces
// explicitly so a Watch composite can't mask single-axis distress.
// Source-year note: ACS 2024 5-year (vintage 2020-2024).
const LWA25_CITY_SCORE_INPUTS: Array<{ town: string; county: string; crime_total: number; lfpr: number; disab: number; age65: number; rent_burden: number }> = [
  { town: "Carbondale",     county: "Jackson",    crime_total: 49.54, lfpr: 56.6, disab: 17.2, age65: 16.5, rent_burden: 50.2 },
  { town: "Marion",         county: "Williamson", crime_total: 33.66, lfpr: 58.8, disab: 19.1, age65: 19.6, rent_burden: 46.4 },
  { town: "Murphysboro",    county: "Jackson",    crime_total: 34.07, lfpr: 56.6, disab: 17.2, age65: 16.5, rent_burden: 50.2 },
  { town: "Mt. Vernon",     county: "Jefferson",  crime_total: 13.35, lfpr: 59.0, disab: 18.3, age65: 20.0, rent_burden: 43.3 },
  { town: "Benton",         county: "Franklin",   crime_total:  1.22, lfpr: 56.0, disab: 21.3, age65: 21.0, rent_burden: 52.2 },
  { town: "West Frankfort", county: "Franklin",   crime_total:  7.85, lfpr: 56.0, disab: 21.3, age65: 21.0, rent_burden: 52.2 },
  { town: "Herrin",         county: "Williamson", crime_total: 28.97, lfpr: 58.8, disab: 19.1, age65: 19.6, rent_burden: 46.4 },
  { town: "Carterville",    county: "Williamson", crime_total:  7.73, lfpr: 58.8, disab: 19.1, age65: 19.6, rent_burden: 46.4 },
  { town: "Pinckneyville",  county: "Perry",      crime_total:  3.42, lfpr: 49.0, disab: 21.1, age65: 20.3, rent_burden: 49.3 },
  { town: "Du Quoin",       county: "Perry",      crime_total:  5.17, lfpr: 49.0, disab: 21.1, age65: 20.3, rent_burden: 49.3 },
];

type Lwa25ScoreRow = {
  town: string;
  county: string;
  composite: number;
  grade: "Strong" | "Stable" | "Watch" | "Strained" | "Critical";
  safety: number;
  participation: number;
  health: number;
  health_disab: number;
  health_age65: number;
  housing: number;
  data_quality: string;
  severe_dimensions: string[];
};

function computeLwa25CityScores(rows: typeof LWA25_CITY_SCORE_INPUTS): Lwa25ScoreRow[] {
  const minMax = (vals: number[]) => ({ min: Math.min(...vals), max: Math.max(...vals) });
  const r_crime = minMax(rows.map(r => r.crime_total));
  const r_lfpr = minMax(rows.map(r => r.lfpr));
  const r_disab = minMax(rows.map(r => r.disab));
  const r_age65 = minMax(rows.map(r => r.age65));
  const r_rb = minMax(rows.map(r => r.rent_burden));

  const normAdverse = (val: number, range: { min: number; max: number }) =>
    range.max === range.min ? 50 : (100 * (range.max - val)) / (range.max - range.min);
  const normPositive = (val: number, range: { min: number; max: number }) =>
    range.max === range.min ? 50 : (100 * (val - range.min)) / (range.max - range.min);

  return rows.map((r): Lwa25ScoreRow => {
    const safety = normAdverse(r.crime_total, r_crime);
    const participation = normPositive(r.lfpr, r_lfpr);
    const health_disab = normAdverse(r.disab, r_disab);
    const health_age65 = normAdverse(r.age65, r_age65);
    const health = (health_disab + health_age65) / 2;
    const housing = normAdverse(r.rent_burden, r_rb);
    const compositeRaw = (safety + participation + health + housing) / 4;
    const composite = Math.round(compositeRaw);

    const grade: Lwa25ScoreRow["grade"] =
      composite >= 80 ? "Strong" :
      composite >= 65 ? "Stable" :
      composite >= 50 ? "Watch" :
      composite >= 35 ? "Strained" : "Critical";

    // Sub-component severe-flag check: Health splits into Disability + Age 65+
    // sub-components. Either can collapse to 0 (min-max worst-in-set) while
    // the dimension-level average masks it — e.g., Mt. Vernon Jefferson age65
    // 22 + disab 73 → mean 48 looks healthy at the dimension level but the
    // age-65+ sub-component is below the 25 threshold and should surface.
    const severe_dimensions: string[] = [];
    if (safety < 25) severe_dimensions.push("Safety");
    if (participation < 25) severe_dimensions.push("Participation");
    if (health < 25) severe_dimensions.push("Health");
    else if (health_disab < 25) severe_dimensions.push("Health (disability)");
    else if (health_age65 < 25) severe_dimensions.push("Health (age 65+)");
    if (housing < 25) severe_dimensions.push("Housing");

    return {
      town: r.town, county: r.county,
      composite,
      grade,
      safety: Math.round(safety),
      participation: Math.round(participation),
      health: Math.round(health),
      health_disab: Math.round(health_disab),
      health_age65: Math.round(health_age65),
      housing: Math.round(housing),
      data_quality: "4 of 4 dimensions · town safety + county proxies for participation + health + housing; housing measures cost burden only, not stock vintage",
      severe_dimensions,
    };
  });
}

const LWA25_CITY_SCORES: Lwa25ScoreRow[] = computeLwa25CityScores(LWA25_CITY_SCORE_INPUTS);

function Lwa25TownContextScoreSection() {
  const sorted = [...LWA25_CITY_SCORES].sort((a, b) => b.composite - a.composite);
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        LWA-25 town context score · internal comparative index (0-100, within LWA-25)
      </h2>
      <div style={{ fontSize: 12, color: "#7a756b", marginBottom: 10, fontStyle: "italic" }}>
        Internal comparative ranking within the 10-town LWA-25 set. Not an official public-health score, not a national or statewide benchmark, not a certification.
      </div>
      <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 14, maxWidth: 820, lineHeight: 1.55 }}>
        Equal-weighted 4-dimension composite computed in-page from FBI UCR 2024 (Safety) + ACS 2024 5-year county data (Participation, Health, Housing). <strong>Min-max normalized <em>across this 10-town LWA-25 set only</em></strong> — scores rank towns within LWA-25, not against IL or US benchmarks. <strong>This is calculated independently here</strong> and does not import any /carbondale, /murphysboro, or other community-page score value. Higher composite = better-ranked condition within LWA-25. Adverse metrics (crime, disability, age 65+, rent burden) inverted; only LFPR is direct. <strong>Severe-dimension flag (⚠):</strong> any single dimension under 25 is surfaced even when the composite band looks healthy. The Williamson corridor (Carterville, Herrin, Marion) ranks top-3 for the county-level dimensions; Carbondale and Murphysboro carry the highest single-axis distress (Carbondale&apos;s crime burden + the Jackson County rent-burden tier).
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto", marginBottom: 12 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Town · County</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Score</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Band</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Safety</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Particip.</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Health<br /><span style={{ fontSize: 10, fontWeight: 400, color: "#7a756b" }}>D = disab · A = 65+%</span></th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Housing<br /><span style={{ fontSize: 10, fontWeight: 400, color: "#7a756b" }}>(cost burden only)</span></th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Severe dim. (&lt;25)</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Data quality</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => {
              const gradeColor =
                r.grade === "Strong"   ? "oklch(40% 0.16 142)" :
                r.grade === "Stable"   ? "oklch(45% 0.14 142)" :
                r.grade === "Watch"    ? "oklch(45% 0.18 60)" :
                r.grade === "Strained" ? "oklch(45% 0.20 22)" :
                "oklch(35% 0.22 22)";
              return (
                <tr key={r.town} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                  <td style={{ padding: "6px 10px", fontWeight: 600 }}>{r.town} · {r.county}</td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 700, color: gradeColor }}>{r.composite}</td>
                  <td style={{ padding: "6px 10px" }}>
                    <span style={{ background: `${gradeColor}22`, color: gradeColor, padding: "2px 8px", borderRadius: 3, fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em" }}>{r.grade}</span>
                  </td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: r.safety < 25 ? 700 : 400, color: r.safety < 25 ? "oklch(45% 0.20 22)" : "#5a564d" }}>{r.safety}</td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: r.participation < 25 ? 700 : 400, color: r.participation < 25 ? "oklch(45% 0.20 22)" : "#5a564d" }}>{r.participation}</td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: r.health < 25 ? 700 : 400, color: r.health < 25 ? "oklch(45% 0.20 22)" : "#5a564d" }}>
                    <div>{r.health}</div>
                    <div style={{ fontSize: 10, marginTop: 2, color: "#7a756b", fontWeight: 400 }}>
                      <span style={{ color: r.health_disab < 25 ? "oklch(45% 0.20 22)" : "#7a756b", fontWeight: r.health_disab < 25 ? 600 : 400 }}>D{r.health_disab}</span>
                      {" · "}
                      <span style={{ color: r.health_age65 < 25 ? "oklch(45% 0.20 22)" : "#7a756b", fontWeight: r.health_age65 < 25 ? 600 : 400 }}>A{r.health_age65}</span>
                    </div>
                  </td>
                  <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: r.housing < 25 ? 700 : 400, color: r.housing < 25 ? "oklch(45% 0.20 22)" : "#5a564d" }}>{r.housing}</td>
                  <td style={{ padding: "6px 10px", fontSize: 11, color: r.severe_dimensions.length > 0 ? "oklch(45% 0.20 22)" : "#7a756b", fontWeight: r.severe_dimensions.length > 0 ? 600 : 400 }}>
                    {r.severe_dimensions.length > 0 ? `⚠ ${r.severe_dimensions.join(" + ")}` : "—"}
                  </td>
                  <td style={{ padding: "6px 10px", fontSize: 11, color: "#7a756b" }}>{r.data_quality}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 11, color: "#7a756b", lineHeight: 1.55 }}>
        <strong>Methodology:</strong> Composite = mean of 4 dimensions, each 0-100 after min-max normalization across <em>this 10-town LWA-25 set only</em> — scores rank towns within LWA-25, not against IL or US benchmarks. <strong>What 0 and 100 mean in this table:</strong> a dimension score of <strong>0 is the worst value within the 10-town LWA-25 set</strong> on that dimension (mathematically forced by min-max — e.g., Carbondale Safety 0 = highest crime in LWA-25 at 49.54/1k; Pinckneyville + Du Quoin Participation 0 = Perry County&apos;s 49.0% LFPR is the lowest in LWA-25; Benton + West Frankfort Health 0 = Franklin County&apos;s 21.3% disability and 21.0% age-65+ share are both the highest in LWA-25). A score of <strong>100 is the best value within the set</strong>. <strong>0 ≠ missing data.</strong> <strong>Safety</strong> (weight 25) = inverted FBI UCR 2024 total crime per 1,000 (NeighborhoodScout / AreaVibes as labelled FBI-UCR carriers). <strong>Economic participation</strong> (weight 25) = county Labor Force Participation Rate (ACS 2024 5-year, B23025-derived equivalent of S2301). <strong>Health/access burden</strong> (weight 25) = mean of <em>two sub-components shown inline as D (disability) · A (age 65+)</em>: inverted county disability rate (B18101→S1810 equivalent) and inverted county age 65+ share (B01001→S0101 equivalent — <em>this is the actual 65+ %, not a median-age proxy</em>). <strong>Housing</strong> (weight 25) = inverted county renter cost-burden share (ACS B25070). <strong>The Housing dimension measures rent-affordability / cost-burden only; it does NOT capture housing-stock age, vintage, or quality.</strong> Mt. Vernon scores 100 on Housing because Jefferson has the lowest renter cost-burden in LWA-25 (43.3%) — but Mt. Vernon&apos;s actual housing stock includes old pre-1970 single-family stock; the operator&apos;s lived signal on that gap is correct and not captured by the cost-burden measure (tracked in §19 Known Limits). <strong>Within-LWA-25 bands</strong> (relative to this set; not statewide or national thresholds): Strong 80-100, Stable 65-79, Watch 50-64, Strained 35-49, Critical 0-34. <strong>Severe-dimension flag (⚠):</strong> any dimension OR Health sub-component scoring under 25 is surfaced in its own column even when the composite band looks healthy — equal weighting can otherwise mask single-axis distress (e.g., Mt. Vernon Health D 73 · A 22 → dimension-mean 48 looks healthy, but the age-65+ sub-component is below 25 and the flag exposes it). <strong>All inputs are sourced from FBI UCR 2024 (via NeighborhoodScout/AreaVibes carriers) + Census ACS 2024 5-year</strong>; no community-page score values are imported. ACS source URL: <a href="https://api.censusreporter.org/1.0/data/show/acs2024_5yr?table_ids=B18101,B01002,B01001,B23025,B25064,B25070,B25077,B25091&amp;geo_ids=05000US17055,05000US17077,05000US17081,05000US17145,05000US17199" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census Reporter ACS 2024 5-yr · 5-county pull</a>.
      </div>
    </section>
  );
}

function KnownLimitsSection() {
  const items: Array<{ item: string; cls: string; step: string }> = [
    {
      item: "LWA-25-specific WIOA performance row for PY2023–PY2024 (six primary indicators)",
      cls: "PARTIALLY CLOSED · TARGETS RECOVERED + ACTUALS NOT YET PUBLISHED",
      step: "Alternate-source retry 2026-05-28 closed (a) LWIA-25 PY2024 + PY2025 negotiated targets (now in PIRL section) sourced from the IL DCEO PY2024 WIOA Annual Statewide Performance Report Narrative (released Nov 2025, pp.14-18; DCEO Local Area ID 17125, fiscal agent + operator: Man-Tra-Con Corp, Marion IL). PY2022–PY2024 LWIA-25 actuals: not located in any public source. IL DCEO PY24 narrative p.23 explicitly defers: \"Final adjusted levels of performance will not be made available until early 2026.\" Verified-blocked endpoints during retry: dol.gov/sites/dolgov/files/ETA/Performance/pdfs/PY2022/IL_PY22…pdf (403), siwdb.org meeting-minutes individual PDFs (404 on guessed paths; listing page enumerates dates but rendered HTML doesn't expose download URLs), DOL PY2023 Local Board Annual Report HTML (403 to programmatic access). WIPS (dol.gov/agencies/eta/performance/wips) remains an authenticated grantee submission system (login.gov + Rules of Behavior), not a public data portal.",
    },
    {
      item: "Town context score · town-level safety vs county-proxy participation/health/housing",
      cls: "COUNTY_PROXY_BY_DESIGN",
      step: "The LWA-25 town context score uses town-specific FBI UCR 2024 data for the Safety dimension and county-level Census ACS 2024 5-yr data (B18101 disability, B01001 age 65+, B23025 LFPR, B25070 rent burden) for the other three dimensions. Census ACS 5-year does not publish reliable place-level estimates for towns below ~20k population, so county-level data is the finest-grained reliable resolution available for 9 of 10 LWA-25 towns (only Carbondale exceeds 20k and could be replaced with town-level place pulls in a follow-on; the current implementation uses county-level Jackson values for consistency across the LWA-25 ranking). Each row carries an explicit data_quality label so the proxy structure is visible. This is a documented design choice, not a defect.",
    },
    {
      item: "Housing dimension measures cost burden, NOT stock vintage / quality",
      cls: "DIMENSION_SCOPE_LIMIT",
      step: "The Housing dimension on the §08 town context score is inverted county renter cost-burden share (ACS B25070) only. It does not measure housing-stock age, structural quality, or vintage. Mt. Vernon (Jefferson) scores 100 on Housing because Jefferson has the lowest renter cost-burden share in LWA-25 (43.3%) — but Mt. Vernon's actual housing stock is documented as pre-1970-dominant in the §11 housing-affordability section (B25034 year-structure-built shows this pattern even with the Continental Tire anchor failing to pull new residential construction). The 100 Housing score on Mt. Vernon reflects affordability only, not stock quality. Operator's lived signal on Mt. Vernon old housing is correct and not captured by the composite. A town-level housing-stock-vintage augmentation (B25034 at place level for towns >20k pop) would be additive but is not currently in the composite.",
    },
    {
      item: "ORI codes for the 10 LWA-25 reporting agencies",
      cls: "PUBLIC_IDENTIFIER_ONLY",
      step: "FBI CDE webapp (cde.ucr.cjis.gov) holds per-agency ORI codes and is the canonical public source — but its agency-detail pages are JavaScript SPAs (curl/WebFetch returns initial-render shell). The NeighborhoodScout / AreaVibes carriers used for the recovered town crime counts do not surface ORI codes in their public-facing pages. Known: Carbondale PD ORI is IL039015A from prior IL-UCR work. Other 9 agency ORIs are recoverable from FBI CDE agency-detail pages in a browser session if a stakeholder needs them for direct UCR cross-reference; the crime counts on the page are sourced and reproducible without them.",
    },
    {
      item: "GD-OTS Marion 95.6% federal-money concentration claim · refresh cadence",
      cls: "TIME-WINDOWED",
      step: "The 95.6% concentration is reported on the Federal Money Concentration section's stated 24-month USAspending lookback window. Concentration is window-dependent: a different lookback (12mo, 36mo, FY-bounded) produces a different share. Every dashboard refresh should re-pull USAspending place-of-performance for the 5 LWA-25 counties and re-state the concentration. Source: usaspending.gov advanced search by place-of-performance, county FIPS 17055/17077/17081/17145/17199.",
    },
  ];
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Known limits · data still pending or qualified
      </h2>
      <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 12, maxWidth: 820, lineHeight: 1.55 }}>
        Open limitations are tracked here, classified by what kind of action closes each one. Source-integrity discipline rather than failure-flagging; every row carries the attempted-source record.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Limitation</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Closure class</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Attempted-source record</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r, i) => {
              const clsColor = r.cls.startsWith("PARTIALLY CLOSED") || r.cls.startsWith("CLOSED") ? "oklch(40% 0.16 142)"
                : r.cls.startsWith("COUNTY_PROXY") || r.cls.startsWith("DIMENSION_SCOPE") || r.cls.startsWith("PUBLIC_IDENTIFIER") || r.cls.startsWith("TIME-WINDOWED") ? "oklch(45% 0.18 60)"
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
    </section>
  );
}

function Lwa25DceoOccupationsSection() {
  // IL workNet SouthernRegionalDataPacket2026 publishes the official Demand Occupations list
  // for EDR 8 (Southern Illinois) — coterminous with LWA-25 / Man-Tra-Con. WIOA eligible-training-
  // provider funding follows this list. Wages: IDES OEWS 2024 entry + experienced bands.
  // Annual openings: IDES Long-Term Occupational Projections 2022-2032.
  // Living-wage benchmarks here are IL statewide MIT-LWC (matches what the source packet uses);
  // for Jackson County reality check ($18.95 1A / $46.76 1A+2C), see Training-to-Demand
  // Alignment section above.
  const LW_1A = 23.56;
  const LW_2C = 40.41;
  const occupations: Array<{ tier: string; soc: string; occ: string; openings: number; entry: number | null; exp: number | null }> = [
    { tier: "Cert/License", soc: "31-1131", occ: "Nursing Assistants", openings: 306, entry: 16.91, exp: 22.06 },
    { tier: "Cert/License", soc: "53-3032", occ: "Heavy + Tractor-Trailer Truck Drivers", openings: 200, entry: 18.95, exp: 29.24 },
    { tier: "Cert/License", soc: "25-9045", occ: "Teaching Assistants (ex-postsecondary)", openings: 168, entry: null, exp: null },
    { tier: "Cert/License", soc: "39-9011", occ: "Childcare Workers", openings: 139, entry: 14.41, exp: 17.03 },
    { tier: "Cert/License", soc: "31-9092", occ: "Medical Assistants", openings: 110, entry: 17.26, exp: 21.85 },
    { tier: "Cert/License", soc: "49-3023", occ: "Automotive Service Technicians + Mechanics", openings: 78, entry: 16.82, exp: 27.94 },
    { tier: "Cert/License", soc: "39-9031", occ: "Exercise Trainers + Group Fitness Instructors", openings: 56, entry: 16.65, exp: 30.74 },
    { tier: "Associate's", soc: "29-1141", occ: "Registered Nurses (RN)", openings: 274, entry: 29.73, exp: 46.45 },
    { tier: "Associate's", soc: "25-2011", occ: "Preschool Teachers (ex-Sp Ed)", openings: 48, entry: 15.96, exp: 23.77 },
    { tier: "Associate's", soc: "31-2021", occ: "Physical Therapist Assistants (PTA)", openings: 33, entry: 23.70, exp: 34.76 },
    { tier: "Associate's", soc: "15-1232", occ: "Computer User Support Specialists", openings: 23, entry: 14.74, exp: 28.05 },
    { tier: "Associate's", soc: "23-2011", occ: "Paralegals + Legal Assistants", openings: 19, entry: 17.06, exp: 26.14 },
    { tier: "Associate's", soc: "29-2010", occ: "Clinical Lab Technologists / Technicians", openings: 17, entry: 22.09, exp: 36.33 },
    { tier: "Associate's", soc: "15-1231", occ: "Computer Network Support Specialists", openings: 15, entry: 18.20, exp: 31.90 },
    { tier: "Bachelor's", soc: "11-1021", occ: "General + Operations Managers", openings: 270, entry: 23.48, exp: 62.02 },
    { tier: "Bachelor's", soc: "25-2021", occ: "Elementary School Teachers (ex-Sp Ed)", openings: 106, entry: null, exp: null },
    { tier: "Bachelor's", soc: "13-1199", occ: "Business Operations Specialists, All Other", openings: 100, entry: 19.64, exp: 42.45 },
    { tier: "Bachelor's", soc: "13-2011", occ: "Accountants + Auditors", openings: 66, entry: 22.47, exp: 39.88 },
    { tier: "Bachelor's", soc: "13-1161", occ: "Market Research Analysts", openings: 51, entry: 18.07, exp: 33.83 },
    { tier: "Bachelor's", soc: "41-3021", occ: "Insurance Sales Agents", openings: 43, entry: 16.30, exp: 36.78 },
    { tier: "Bachelor's", soc: "13-1111", occ: "Management Analysts", openings: 37, entry: 30.83, exp: 63.79 },
    { tier: "Beyond Bach", soc: "11-3031", occ: "Financial Managers", openings: 63, entry: 31.90, exp: 70.57 },
    { tier: "Beyond Bach", soc: "11-9111", occ: "Medical + Health Services Managers", openings: 60, entry: 35.33, exp: 67.91 },
    { tier: "Beyond Bach", soc: "11-9199", occ: "Managers, All Other", openings: 58, entry: 32.86, exp: 72.70 },
    { tier: "Beyond Bach", soc: "15-1252", occ: "Software Developers", openings: 38, entry: 38.27, exp: 73.73 },
    { tier: "Beyond Bach", soc: "11-2022", occ: "Sales Managers", openings: 31, entry: 33.83, exp: 80.69 },
    { tier: "Beyond Bach", soc: "11-2021", occ: "Marketing Managers", openings: 30, entry: 28.58, exp: 60.66 },
    { tier: "Beyond Bach", soc: "23-1011", occ: "Lawyers", openings: 25, entry: 30.75, exp: 74.90 },
  ];
  const Mark = ({ pass }: { pass: boolean }) => (
    <span style={{ color: pass ? "oklch(40% 0.16 142)" : "oklch(45% 0.20 22)", fontWeight: 700 }}>{pass ? "✓" : "✗"}</span>
  );
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        IL DCEO In-Demand Occupations · EDR 8 Southern Illinois (LWA-25 coterminous)
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55 }}>
        The IL workNet Southern Regional Data Packet 2026 publishes the official Demand Occupations list — eligible-training-provider WIOA funding is tied to occupations on this list. LWA-25 is coterminous with IDES Economic Development Region 8 (Southern). Below: annual openings + entry/experienced hourly wage by credential tier, with each row scored against the IL statewide MIT Living Wage Calculator benchmarks (1A $23.56/hr · 1A+2C $40.41/hr). The packet uses the IL statewide benchmark; for the Jackson County reality check ($18.95 1A / $46.76 1A+2C), see Training-to-Demand Alignment above.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Credential tier</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>SOC</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Occupation</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Annual openings</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Entry $/hr<br /><span style={{ fontSize: 10, fontWeight: 400, color: "#7a756b" }}>1A · 2C</span></th>
              <th style={{ padding: "8px 10px", fontWeight: 600, textAlign: "right" }}>Experienced $/hr<br /><span style={{ fontSize: 10, fontWeight: 400, color: "#7a756b" }}>1A · 2C</span></th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Training ROI verdict</th>
            </tr>
          </thead>
          <tbody>
            {occupations.map((r, i) => {
              const entryClears1A = r.entry != null && r.entry >= LW_1A;
              const entryClears2C = r.entry != null && r.entry >= LW_2C;
              const expClears1A = r.exp != null && r.exp >= LW_1A;
              const expClears2C = r.exp != null && r.exp >= LW_2C;
              let verdict: string;
              let verdictColor: string;
              if (r.entry == null || r.exp == null) {
                verdict = "Missing wage data";
                verdictColor = "#7a756b";
              } else if (expClears2C) {
                verdict = "Strong ladder";
                verdictColor = "oklch(40% 0.16 142)";
              } else if (expClears1A) {
                verdict = "Viable single-adult";
                verdictColor = "oklch(45% 0.18 60)";
              } else {
                verdict = "Low-wage trap";
                verdictColor = "oklch(45% 0.20 22)";
              }
              return (
                <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                  <td style={{ padding: "5px 10px", fontSize: 11, color: "#7a756b", fontWeight: 600 }}>{r.tier}</td>
                  <td style={{ padding: "5px 10px", fontFamily: "monospace", fontSize: 11, color: "#5a564d" }}>{r.soc}</td>
                  <td style={{ padding: "5px 10px", fontWeight: 600 }}>{r.occ}</td>
                  <td style={{ padding: "5px 10px", textAlign: "right", fontWeight: 600, color: r.openings >= 100 ? "oklch(40% 0.16 142)" : "#1f1d18" }}>{r.openings}</td>
                  <td style={{ padding: "5px 10px", textAlign: "right", color: "#5a564d" }}>
                    <div>{r.entry != null ? `$${r.entry.toFixed(2)}` : "—"}</div>
                    {r.entry != null && <div style={{ fontSize: 10, marginTop: 2 }}><Mark pass={entryClears1A} /> · <Mark pass={entryClears2C} /></div>}
                  </td>
                  <td style={{ padding: "5px 10px", textAlign: "right", color: "#5a564d" }}>
                    <div>{r.exp != null ? `$${r.exp.toFixed(2)}` : "—"}</div>
                    {r.exp != null && <div style={{ fontSize: 10, marginTop: 2 }}><Mark pass={expClears1A} /> · <Mark pass={expClears2C} /></div>}
                  </td>
                  <td style={{ padding: "5px 10px" }}>
                    <span style={{ background: `${verdictColor}22`, color: verdictColor, padding: "2px 8px", borderRadius: 3, fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em", whiteSpace: "nowrap" }}>{verdict}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
        <strong>Training ROI readout · DCEO-demand list ≠ wage viability.</strong> The DCEO Demand Occupations list proves training eligibility + employer demand; it does not by itself prove that a credential leads to a family-supporting wage. Against IL statewide MIT LWC benchmarks (1A $23.56/hr · 1A+2C $40.41/hr):
        <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
          <li><strong>Strong ladders (experienced wage clears 1A+2C):</strong> RN (274 openings, $46.45 exp), Bus Ops Specialists (100 · $42.45), Mgmt Analysts (37 · $63.79), General/Ops Managers (270 · $62.02), and every Beyond-Bachelor&apos;s tier role (Financial Mgrs $70.57, Software Devs $73.73, Sales Mgrs $80.69, Medical Health Mgrs $67.91, Marketing Mgrs $60.66, Lawyers $74.90). RN at 274 annual openings is LWA-25&apos;s single highest-volume Strong ladder.</li>
          <li><strong>Viable single-adult only (clears 1A, fails 2C):</strong> Heavy Truck Drivers (200 openings · exp $29.24), Auto Tech (78 · $27.94), Exercise Trainers (56 · $30.74), PTA (33 · $34.76), Computer User Support (23 · $28.05), Paralegals (19 · $26.14), Clinical Lab Tech (17 · $36.33), Computer Net Support (15 · $31.90), Accountants (66 · $39.88 — just below 2C), Market Research (51 · $33.83), Insurance Sales (43 · $36.78). Preschool Teachers (48 · $23.77 exp barely clears 1A). Ladder-dependent: viable for a single earner, not a single parent of two.</li>
          <li><strong>Low-wage traps (experienced wage fails 1A):</strong> <em>Nursing Assistants (306 openings · exp $22.06)</em>, <em>Childcare Workers (139 · exp $17.03)</em>, <em>Medical Assistants (110 · exp $21.85)</em>. <strong>The top-3 Cert/License-tier openings by volume are all low-wage traps</strong> — exactly the credentials WIOA grants are easiest to fund and exactly the credentials least likely to lift a participant above single-adult living wage. This is the same wage-suppression pattern the page&apos;s training-to-demand alignment section already flags for Jackson County.</li>
          <li><strong>Missing wage data:</strong> Teaching Assistants (168 openings) + Elementary School Teachers (106 openings) — OEWS suppresses public-sector school wage rows. Verdict cannot be computed without IL TRS / district salary-schedule pulls.</li>
        </ul>
      </div>
      <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginTop: 12 }}>
        <strong>Real-time corroboration — HWOL December 2025 EDR 8 top job postings:</strong> Registered Nurses (129 new ads), Food Prep Workers (56), Heavy Truck Drivers (52), Retail Salespersons (47), Food Service Managers (40), Home Health + Personal Care Aides (40), Customer Service Reps (40), First-Line Retail Supervisors (26), General Maintenance + Repair (24), Cashiers (23). <strong>Top posting employers:</strong> Flynn Group/Pizza Hut/Taco Bell (44), State of Illinois (35), Casey&apos;s (29), Addus HomeCare (27), USPS (26), Kroger/Mariano&apos;s (26), Walmart/Sam&apos;s Club (25), SIH (25), SSM Health Care (21), Love&apos;s (19), SIU-Carbondale (15), Banterra Bank (15), H&amp;R Block (15). Source: <a href="https://ides.illinois.gov/content/dam/soi/en/web/ides/labor_market_information/hwol/edr8_dec25.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDES EDR 8 HWOL Dec 2025</a>.
      </div>
      <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
        Sources: <a href="https://www.illinoisworknet.com/WIOA/RegPlanning/Documents/2026WIOARegionalandLocalPlanning/SouthernRegionalDataPacket2026.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Southern Regional Data Packet 2026 · EDR 8 (IL workNet)</a> · DCEO Office of Employment and Training + NIU Workforce Policy Lab joint product · IDES Long-Term Occupational Employment Projections 2022-2032 + OEWS 2024 entry/experienced bands. Living-wage benchmark (IL one adult / single parent): $23.56 / $40.41 (<a href="https://livingwage.mit.edu/states/17" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>MIT Living Wage Calculator</a>). SOC codes are BLS SOC-2018 mappings (the source packet labels by occupation title; SOC codes are deterministic federal identifiers, not wage data).
      </div>
    </section>
  );
}

// Section-header banner used by the standardized LWA-25 report flow.
// Sits above each numbered section block to mirror LWA-23's "NN · Title" pattern.
function SectionHeader({ num, title }: { num: string; title: string }) {
  return (
    <h2 style={{
      fontSize: 22, fontWeight: 600, margin: "40px 0 8px 0",
      color: "#1f1d18", paddingTop: 16, borderTop: "2px solid #d8d2c4",
      scrollMarginTop: 60,
    }}>
      {num} · {title}
    </h2>
  );
}

function Lwa25TheoryOfChangeSection() {
  return (
    <section style={{ marginTop: 8, marginBottom: 8 }}>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.6 }}>
        <strong>Anchor-concentration-at-risk + structural gateways.</strong> LWA-25&apos;s binding constraint is not labor-force participation per se — it is <strong>federal-money concentration risk paired with stacked household gateways</strong>. GD-OTS Marion alone receives 95.6% of the 24-month federal-award flow into the 5-county footprint (§07). SIU Carbondale enrollment continues a multi-year decline that compounds the Jackson-County labor-supply pressure. Below those macro pressures, four household gateways (childcare, mobility, housing, mandatory-overtime employer culture — §10-§11, §16) determine whether any individual training cohort can actually reach a family-supporting wage.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid #1f1d18", borderRadius: 6, padding: 18, fontSize: 13.5, color: "#3d3a33", lineHeight: 1.65 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: "#1f1d18", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>The LWA-25 intervention sequence</div>
        <ol style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>De-risk the anchor concentration FIRST.</strong> Map GD-OTS Marion&apos;s $406M sub-award pool by NAICS lane (§07). For every lane currently sourced out-of-region, queue a Tier-2 introduction with a local-firm candidate. The local SDVOSB precedent already exists. This converts concentration from a permanent dependence into a temporary stepping-stone.</li>
          <li><strong>Bias training cohorts toward FAMILY-SUPPORTING + TRAVEL-WORK rungs SECOND.</strong> The §14 Training-to-Demand verdict already assigns every named ladder one of {`{PHANTOM / WAGE-SUPPRESSED / TRAVEL-WORK / FAMILY-SUPPORTING / SATURATED / OWNER-OP}`}. The §15 DCEO 1A+2C clearance confirms which of the federally-funded credentials actually clear single-adult and family-supporting living wage. WIOA cohort planning bias toward the green-verdict rows is the highest-leverage workforce-board lever.</li>
          <li><strong>Remove household gateways in parallel</strong> (NOT sequenced after — gateways are concurrent prerequisites). Childcare slot expansion (§10), mobility / transit-to-shift-work (§09), housing inventory in the Williamson corridor (§11), and mandatory-overtime negotiation with anchor employers (§16) all unlock the workforce supply the cohort-planning above presumes.</li>
          <li><strong>Anchor-attraction LAST in sequence but largest in horizon.</strong> Data-center recruitment (Williamson corridor power + EECA wholesale + Big Muddy Solar PPA), federal-retiree corridor capture (Marion-Herrin-Carterville inventory), CEJA solar buildout — all pursued from the standing the first three moves establish, not before.</li>
        </ol>
        <div style={{ marginTop: 14, padding: 12, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 4, fontSize: 12.5 }}>
          <strong>Contrast with LWA-23 (East Central Illinois).</strong> LWA-23&apos;s thesis is <em>participation recovery first</em> — labor-supply collapse driven by disability rates 17-20% and carceral economy in Lawrence + Fayette. LWA-25 is not labor-supply-collapsed in the same way; <em>concentration risk + gateway barriers</em> are the binding constraints. The two regions need different interventions in different order. See <a href="/east-central-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>/east-central-illinois →</a> for the parallel LWA-23 report.
        </div>
      </div>
    </section>
  );
}

function Lwa25CountyStrategyMatrixSection() {
  type Row = { archetype: string; counties: string; role: string; constraint: string; intervention: string; anchor: string };
  const rows: Row[] = [
    {
      archetype: "Williamson Corridor (Marion–Herrin–Carterville)",
      counties: "Williamson",
      role: "Newer-construction relocation corridor + GD-OTS Marion host + I-57/I-24 logistics + Marion VA",
      constraint: "Anchor-concentration dependence on GD-OTS (95.6% of 24-mo federal flow); housing-inventory bottleneck at the upper end of the corridor",
      intervention: "Supply-chain diversification away from GD-OTS via Tier-2 broker calls; data-center recruitment on Big Muddy Solar PPA + EECA wholesale rate; federal-retiree relocation cohort directed here over Jackson Co. for safety + inventory",
      anchor: "GD-OTS sub-award lanes (§07), Williamson safety scores top-3 in §08, EECA / SIPC power (§17)",
    },
    {
      archetype: "Jackson / SIU-Carbondale Corridor",
      counties: "Jackson",
      role: "SIU Carbondale university anchor + Murphysboro municipal seat + Big Muddy Solar host + young-skewed county (median age 32.4)",
      constraint: "SIU enrollment decline + Carbondale crime (highest in LWA-25 at 49.54/1k) + older housing stock + Jackson Co rent-burden 50.2% renters cost-burdened",
      intervention: "SIU graduate retention via housing-revitalization + climate-migration pitch; Big Muddy Solar PPA marketing; healthcare-laddering at SIH Memorial; municipal-broadband + middle-mile fiber as anchor-attraction differentiator",
      anchor: "SIU Carbondale enrollment trend, Carbondale + Murphysboro town context scores (§08), Big Muddy Solar (§17), housing affordability data (§11)",
    },
    {
      archetype: "Franklin / West Frankfort–Benton",
      counties: "Franklin",
      role: "Coal-legacy county + IRA Energy Community (coal-closure tract) + low-cost housing + highest LWA-25 county disability rate (21.3%) + highest rent-burden share (52.2%)",
      constraint: "Stacked health-and-housing distress (West Frankfort + Benton both surface ⚠ Health + Housing severe-dimension flags in §08); thin private-sector base outside coal-legacy + Vienna IDOC commute",
      intervention: "IRA §48 +10pp solar/storage adder for behind-the-meter generation; CEJA Climate Works cohorts directed here; manufactured-housing / Section 502 financing for housing-stock upgrade; targeted childcare-slot expansion (Region 26)",
      anchor: "IRA Energy Community designation (§07/§17), Franklin disability + rent-burden (§02), West Frankfort + Benton severe-dimension flags (§08)",
    },
    {
      archetype: "Jefferson / Mt. Vernon",
      counties: "Jefferson",
      role: "Continental Tire host (3,667 jobs) + manufacturing anchor + LWA-25&apos;s top-ranked town context score (Mt. Vernon 81 Strong)",
      constraint: "Single-anchor exposure (Continental Tire); Jefferson Co. age 65+ share at 20.0% — Mt. Vernon&apos;s only severe-dimension flag is the elderly-share component of Health; Mt. Vernon residential stock is pre-1970-dominant despite Continental presence",
      intervention: "Industrial-mechanics + welding cohort intake at Continental Tire; senior/retiree housing supply via HUD §202 + LIHTC senior; advanced-manufacturing supplier diversification around the Continental footprint",
      anchor: "Mt. Vernon town context score 81 / Strong with ⚠ Health-age dimension (§08), Continental Tire anchor (§06/§07)",
    },
    {
      archetype: "Perry / Du Quoin–Pinckneyville",
      counties: "Perry",
      role: "Lowest-LFPR county in footprint (49.0%) + IDOC IL River Correctional host + USG Pinckneyville + coal-legacy + IRA Energy Community",
      constraint: "Severe-low participation rate (Du Quoin + Pinckneyville both ⚠ Participation in §08, scoring composite Critical + Strained); thinnest private-sector base; longest commute distances",
      intervention: "Carceral re-entry workforce programming (IDOC officer career-ladder); CEJA solar siting opportunities; targeted transit-shift-work investment (FTA §5311); GD-OTS Tier-2 introduction for any local precision shop able to qualify",
      anchor: "Perry LFPR 49% (§02/§05), Du Quoin + Pinckneyville town context score (§08), IRA Energy Community + USG anchor (§07)",
    },
  ];
  return (
    <section style={{ marginTop: 8 }}>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.6 }}>
        Five distinct subregional archetypes within the LWA-25 5-county footprint. Each carries a different structural role, a different binding constraint, and a different best-fit intervention. The matrix below is the workforce board&apos;s allocation map — different cohorts, different placements, different funding strategies per archetype rather than a single uniform regional program.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
              <th style={{ padding: "8px 10px", fontWeight: 600, minWidth: 160 }}>Archetype · County(ies)</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Structural role</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Binding constraint</th>
              <th style={{ padding: "8px 10px", fontWeight: 600 }}>Best-fit intervention</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, minWidth: 130 }}>Data anchor</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6", verticalAlign: "top" }}>
                <td style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>
                  <div>{r.archetype}</div>
                  <div style={{ fontSize: 10, color: "#7a756b", fontWeight: 400, marginTop: 2 }}>{r.counties}</div>
                </td>
                <td style={{ padding: "8px 10px", color: "#3d3a33", lineHeight: 1.5 }}>{r.role}</td>
                <td style={{ padding: "8px 10px", color: "oklch(45% 0.20 22)", lineHeight: 1.5 }}>{r.constraint}</td>
                <td style={{ padding: "8px 10px", color: "oklch(40% 0.16 142)", lineHeight: 1.5 }}>{r.intervention}</td>
                <td style={{ padding: "8px 10px", fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>{r.anchor}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 11, color: "#7a756b", marginTop: 10, lineHeight: 1.5 }}>
        Archetypes derived from on-page evidence: GD-OTS concentration data (§07), Town Context Score severe-dimension flags (§08), ACS 2024 5-yr county disability + age + LFPR + rent-burden (§02 + §05), IRA Energy Community designations (§07/§17), and the local anchor-employer inventory referenced throughout the page. <strong>Not interchangeable with LWA-23&apos;s county archetypes.</strong>
      </div>
    </section>
  );
}

function TrainingROISection() {
  // ROI table for all named training pathways on the page. Saturation reflects
  // local-slot scarcity for cannabis top-rung, viticulture top-rung, and union
  // apprenticeships. Wage estimates pulled from the corresponding training/
  // travel-jobs sections. Slot estimates are advisory ranges; refine against
  // the workforce board PIRL data.
  type RoiRow = {
    pathway: string;
    train_cost: string;
    train_duration: string;
    journey_wage: string;
    annual_premium: string;  // vs $32k US-median single-earner baseline
    payback_yrs: string;
    local_slots: string;  // estimated annual openings region-wide
    saturation: "LOW" | "LOW-MED" | "MED" | "MED-HIGH" | "HIGH" | "EXTREME" | "PHANTOM";
    verdict: string;
  };
  const rows: RoiRow[] = [
    // === Family-supporting union trades (high-wage, low-slot, gated by apprenticeship) ===
    { pathway: "Lineworker IBEW 702 outside",
      train_cost: "Paid apprenticeship ($0 cost; you earn)", train_duration: "~3.5yr (7×1,000hr periods)",
      journey_wage: "$65.52/hr (~$136k/yr)",     annual_premium: "+$104k/yr",       payback_yrs: "Negative (paid during training)",
      local_slots: "~5-15/yr (IBEW 702 apprentice intake)", saturation: "HIGH",
      verdict: "Best ROI on the page IF you land an apprenticeship slot. Gated by union intake cycles." },
    { pathway: "Electrician IBEW 702 inside",
      train_cost: "Paid apprenticeship ($0 cost)", train_duration: "5yr",
      journey_wage: "$42-50/hr (~$92k/yr)",      annual_premium: "+$60k/yr",        payback_yrs: "Negative (paid during training)",
      local_slots: "~10-20/yr apprentice intake", saturation: "HIGH",
      verdict: "Excellent ROI. Single-adult LW cleared easily; 1A+2C threshold met with overtime. Gated by intake." },
    { pathway: "Pipefitter UA Local 553",
      train_cost: "Paid apprenticeship ($0 cost)", train_duration: "5yr",
      journey_wage: "$50-65/hr + per-diem (~$130k/yr all-in)", annual_premium: "+$98k/yr",       payback_yrs: "Negative",
      local_slots: "~5-15/yr (UA 553 intake; travel work expands range)", saturation: "HIGH",
      verdict: "Top-paying construction trade. Travel-tolerant lifestyle required." },
    { pathway: "Boilermaker Local 363 (Belleville/Highland IL)",
      train_cost: "Paid apprenticeship", train_duration: "4yr",
      journey_wage: "$40-55/hr + per-diem (~$120k/yr)", annual_premium: "+$88k/yr",        payback_yrs: "Negative",
      local_slots: "~3-8/yr (shrinking with coal-plant retirements)", saturation: "MED",
      verdict: "Family-supporting if you tolerate outage-driven travel. Sector contracting." },
    { pathway: "Crane operator IUOE Local 318",
      train_cost: "Paid apprenticeship", train_duration: "3yr",
      journey_wage: "$45-60/hr + per-diem (~$125k/yr)", annual_premium: "+$93k/yr",        payback_yrs: "Negative",
      local_slots: "~5-12/yr (boosted by Big Muddy Solar)", saturation: "MED",
      verdict: "Big Muddy Solar created near-term openings; ongoing through wind/data-center construction cycles." },
    // === Healthcare ladder ===
    { pathway: "CNA (Certified Nursing Asst.)",
      train_cost: "$500-1,500", train_duration: "4-6 weeks",
      journey_wage: "$14-17/hr (~$30k/yr)",      annual_premium: "-$2k/yr (BELOW baseline)", payback_yrs: "N/A — below baseline",
      local_slots: "Many (turnover-driven, 100s/yr)", saturation: "LOW",
      verdict: "Easy entry, low wage. Use ONLY as on-ramp to LPN→RN ladder, not as terminus." },
    { pathway: "LPN (Licensed Practical Nurse)",
      train_cost: "$8,000-15,000", train_duration: "12 months",
      journey_wage: "$25/hr (~$52k/yr)",         annual_premium: "+$20k/yr",        payback_yrs: "~0.5-1yr",
      local_slots: "Dozens/yr (SIH + Memorial + nursing homes)", saturation: "LOW-MED",
      verdict: "Fast ROI. Single-adult LW cleared; below 1A+2C without overtime." },
    { pathway: "RN (ADN, Associate Degree)",
      train_cost: "$10,000-20,000 tuition", train_duration: "2 years",
      journey_wage: "$32-38/hr local (~$72k/yr); travel-RN $130-200k+",
      annual_premium: "+$40k/yr local; +$130k/yr travel",
      payback_yrs: "<1yr (travel-RN); ~1yr (local)",
      local_slots: "Dozens/yr at SIH+Memorial+Marion VA + unlimited travel pool", saturation: "LOW",
      verdict: "Best single 2-year credential on the page. Travel-RN path is highest-dollar of any 2-yr credential in the region." },
    // === Manufacturing / industrial ===
    { pathway: "Welder (structural / pipe)",
      train_cost: "$5,000-15,000 (JALC 12-18mo)", train_duration: "12-18 months",
      journey_wage: "$31/hr local (~$64k); pipe welder traveling $50-70/hr + per-diem",
      annual_premium: "+$32k/yr local; +$80-100k traveling",
      payback_yrs: "~1yr local; ~3mo traveling",
      local_slots: "Dozens/yr (Continental, Aisin, Penn Aluminum)", saturation: "LOW-MED",
      verdict: "Strong. Local family-supporting at journey + Pipe-welder travel work goes to top-rung wages." },
    { pathway: "Industrial maintenance / mechatronics",
      train_cost: "$10,000-25,000 (JALC 18-24mo)", train_duration: "18-24 months",
      journey_wage: "$33/hr (~$69k/yr)",         annual_premium: "+$37k/yr",       payback_yrs: "~1yr",
      local_slots: "Dozens/yr (Continental anchor)", saturation: "LOW-MED",
      verdict: "Family-supporting, anchored on Continental Tire demand. Aisin + Penn Aluminum add depth." },
    // === Driving / logistics ===
    { pathway: "CDL Class A (truck driver)",
      train_cost: "$3,000-6,000 + 4-8wk lost income", train_duration: "4-8 weeks",
      journey_wage: "$22-28/hr local (~$50k); regional OTR $35-45/hr (~$80k+)",
      annual_premium: "+$18k local; +$48k OTR",
      payback_yrs: "<1yr",
      local_slots: "100s/yr (chronic turnover + national shortage)", saturation: "LOW",
      verdict: "FAMILY-TIME CONFLICT verdict applies — OTR pay clears family-supporting bar but destroys home time. Local rate doesn't clear 1A+2C." },
    // === Tech / IT — JALC offers two credential paths: Cyber-Security /
    // Information Assurance AAS (2-yr) + Computer Networking Certificate
    // (24 cr-hr, ~1 yr) that prep students to sit for CompTIA A+/Network+
    // /Security+/CCENT/CCNA exams. The credential itself is solid. The
    // local-employer market is what fails. ===
    { pathway: "IT support / cybersecurity (JALC AAS or Network+/Security+/A+ stacked)",
      train_cost: "$1,000-3,000 cert exams + self-study (cert track); $10-15k tuition (JALC AAS 2-yr)", train_duration: "6-12mo cert; 2yr AAS",
      journey_wage: "Local SIU ceiling $33,755-$88,452/yr (Classes 5032/5031); remote roles $60-120k+",
      annual_premium: "+$25-55k local; +$50-90k remote",
      payback_yrs: "<6mo cert; ~1yr AAS",
      local_slots: "Thin — SIU IT shop is the dominant local employer (two civil-service classes 5031+5032); private-sector Information supersector is ~1-3% of Carbondale-Marion MSA employment (BLS OEWS 2023); LinkedIn shows ~36 information-security postings in the MSA",
      saturation: "PHANTOM",
      verdict: "PHANTOM for a livable LOCAL job. SIU (the dominant local IT employer) staffs IT through two active civil-service classes — IT Support Associate (Class 5032, SUCSS hourly range $16.77-$30.00 → $32,702-$58,500/yr) and IT Technical Associate (Class 5031, $17.31-$45.36/hr → $33,755-$88,452/yr; effective 10/2025). Top step of the higher class is $8,800/yr BELOW the 1A+2C bar ($97,260). Promotion above the civil-service ladder requires conversion to A/P track (master's + nationally-competitive hiring) — not a step-up the JALC credential pipeline feeds. Private-sector local IT employment is thin: the Carbondale-Marion MSA Information supersector is small + LinkedIn shows ~36 area information-security postings vs JALC's annual Cyber-Security AAS + Computer Networking Certificate completers competing for them. Reframe the cohort outcome: this credential lands a livable wage via REMOTE work or relocation, not a local-employer ladder. JALC's program is strong; the local employer market is the constraint. Sources: SIU HR Civil Service Salary Schedule + SUCSS Salary Range Reports + JALC Center for Information Assurance program page + BLS OEWS Carbondale-Marion 2023." },
    // === CEJA clean-energy ===
    { pathway: "CEJA solar installer (NABCEP)",
      train_cost: "$0-1,000 (CEJA Climate Works subsidized)", train_duration: "8-16 weeks",
      journey_wage: "$26/hr (~$54k/yr)",
      annual_premium: "+$22k/yr if placed",
      payback_yrs: "<6mo IF placed",
      local_slots: "Modest — verified local installers exist (StraightUp Solar Marion, Tick Tock Energy)", saturation: "MED-HIGH",
      verdict: "Local NABCEP-employer base is modest but real: StraightUp Solar (Marion office) + Tick Tock Energy + other installers listed on EnergySage Carbondale/Marion/Murphysboro. Capacity-vs-cohort question stands (annual CEJA grad count vs annual hiring at small installers). Distinct from Big Muddy Solar (utility-scale, goes to IBEW/IUOE/LIUNA — NOT NABCEP installers); the residential/commercial installer market IS where NABCEP graduates land." },
    { pathway: "CEJA wind technician (GWO)",
      train_cost: "$0-2,000 subsidized", train_duration: "12-20 weeks",
      journey_wage: "$31/hr base + per-diem traveling (~$80-100k all-in)",
      annual_premium: "+$48-68k IF travel-tolerant",
      payback_yrs: "<6mo IF travel-circuit accepted",
      local_slots: "~0/yr local; IA/TX wind belt circuit (low-saturation if travel-tolerant)", saturation: "PHANTOM",
      verdict: "PHANTOM as local-employment credential; reasonable ROI as travel-pay credential. Reframe cohort outcome from 'local job' to 'regional travel-pay job with predictable home time.'" },
    // === Viticulture (Shawnee Hills AVA ~12-winery footprint) ===
    { pathway: "Viticulture vineyard manager",
      train_cost: "$5,000-10,000 (VESTA/Highland Community College AAS)", train_duration: "1-2 years",
      journey_wage: "$50-80k/yr",                annual_premium: "+$28k/yr",       payback_yrs: "~3mo to 1yr",
      local_slots: "~12-24 total positions region-wide (1-2 per winery × 12 wineries)", saturation: "EXTREME",
      verdict: "Pay is real but total positions across the Shawnee Hills AVA region cap at 12-24. New entrants displace incumbents only on retirement / expansion. Don't oversell as reliable destination." },
    { pathway: "Viticulture winemaker",
      train_cost: "$20,000-60,000 (UC Davis / Cornell / VESTA AAS bridge)", train_duration: "2-4 years",
      journey_wage: "$55-90k small ops; $90-150k+ large", annual_premium: "+$58k mid-range", payback_yrs: "~1-2yr",
      local_slots: "~12 total positions region-wide (1 per winery)", saturation: "EXTREME",
      verdict: "Same scarcity. Total ~12 positions in the AVA. Most workers train and relocate to larger wine regions (CA, OR, WA) for opportunity." },
    // === Cannabis (handful of IL-licensed facilities, low-throughput employment) ===
    { pathway: "Cannabis budtender / cultivation tech",
      train_cost: "Free OJT or JALC Horticulture AA ($5-10k)", train_duration: "0-2 years",
      journey_wage: "$16-25/hr (~$33-52k/yr)",  annual_premium: "+$1-20k/yr",      payback_yrs: "<6mo",
      local_slots: "~30-100 region-wide (handful of facilities currently)", saturation: "MED-HIGH",
      verdict: "Easy entry, low-mid wage. Single-adult LW barely cleared at top of range. Below 1A+2C." },
    { pathway: "Cannabis cultivation manager",
      train_cost: "3-5yr OJT + AAS ($5-10k)", train_duration: "5+ years",
      journey_wage: "Up to $120k/yr",            annual_premium: "+$88k/yr",       payback_yrs: "<6mo",
      local_slots: "~5-10 total region-wide (1-2 per facility)", saturation: "EXTREME",
      verdict: "Pay is real but slots are scarce + filled internally or by experienced outside hires. Realistic local pathway tops out at assistant grower for most workers." },
    { pathway: "Cannabis master grower",
      train_cost: "5-10yr OJT + degree", train_duration: "10+ years",
      journey_wage: "$80-150k/yr",               annual_premium: "+$68k/yr",       payback_yrs: "N/A (career-ladder)",
      local_slots: "~5-10 total region-wide", saturation: "EXTREME",
      verdict: "Ceiling that exists, not reliable destination. Don't oversell." },
    // === Childcare (per gateway-constraint analysis) ===
    { pathway: "Childcare worker / CDA → director ladder",
      train_cost: "$500-2,000 CDA; $5,000-15,000 AAS ECE; $20,000-40,000 BA",
      train_duration: "Months to 4 years",
      journey_wage: "CDA $13-17/hr; AAS $17-22/hr; BA director $40-60k",
      annual_premium: "BELOW baseline at CDA/AAS; +$8-28k at director",
      payback_yrs: "Long; Smart Start Workforce Grants offset",
      local_slots: "Dozens/yr (chronic shortage)", saturation: "LOW",
      verdict: "Below livable for entry positions; director-level barely family-supporting. Smart Start $90M Workforce Grant pool partially raises floor. Strategic on-ramp, not destination." },
  ];

  const satTone = (s: string) =>
    s === "LOW" ? { bg: "oklch(96% 0.04 142)", fg: "oklch(35% 0.18 142)" } :
    s === "LOW-MED" ? { bg: "oklch(96% 0.04 142)", fg: "oklch(35% 0.18 142)" } :
    s === "MED" ? { bg: "oklch(97% 0.04 60)", fg: "oklch(40% 0.15 60)" } :
    s === "MED-HIGH" ? { bg: "oklch(97% 0.04 60)", fg: "oklch(40% 0.15 60)" } :
    s === "HIGH" ? { bg: "oklch(97% 0.04 60)", fg: "oklch(40% 0.15 60)" } :
    s === "EXTREME" ? { bg: "oklch(96% 0.05 22)", fg: "oklch(40% 0.20 22)" } :
    /* PHANTOM */ { bg: "oklch(96% 0.05 22)", fg: "oklch(40% 0.20 22)" };

  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Training ROI · cost-of-training vs available-jobs vs wage-payback per pathway
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        <strong>The honest ROI question:</strong> for each named training pathway on
        this page, how many jobs actually exist regionally to absorb credential
        holders, and how does that compare to training cost + payback at <em>local
        cost-of-living</em>? The family-supporting wage threshold is necessary but not
        sufficient — a $100k pathway with only 12 total positions region-wide is
        fundamentally different from a $50k pathway with hundreds of slots.
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(97% 0.04 60)", border: "1px solid oklch(58% 0.15 60)33", borderLeft: "6px solid oklch(58% 0.15 60)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(40% 0.15 60)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Cost-of-living context for the wage comparisons
        </div>
        <p style={{ margin: "0 0 6px 0" }}>
          Wages in LWA-25 are nominally lower than national averages (BLS Carbondale-Marion MSA May 2023: $26.21/hr mean vs $31.48 national = 17% nominal gap). But cost-of-living in Jackson + Williamson counties is also materially lower than national average. The two largest deltas: housing (~30-40% cheaper than national median) and consumer services. Per <a href="https://www.bea.gov/data/prices-inflation/regional-price-parities-state-and-metro-area" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BEA Regional Price Parities</a>, the Carbondale-Marion MSA RPP is roughly 85-87% of the national average — meaning <strong>$1 here buys what ~$1.15 buys nationally</strong>.
        </p>
        <p style={{ margin: "0 0 6px 0" }}>
          <strong>What that means for the table below:</strong> the MIT Living Wage thresholds used as the "1A+2C $46.76/hr" benchmark are <em>already</em> Jackson-County-specific and account for local COL. Wages that clear MIT 1A+2C in Jackson Co. are genuinely family-supporting AT JACKSON COUNTY PRICES. A "single-adult LW cleared" verdict in this region means actual local-COL livability, not just a nominal-wage hit.
        </p>
        <p style={{ margin: 0 }}>
          <strong>What the 17% wage gap still means:</strong> regional COL is ~13-15% lower than national, but wages are ~17% lower. <strong>So even after COL adjustment, there's a residual 2-4% real-wage gap</strong> — workers in LWA-25 are slightly worse off in real terms than national averages, not vastly worse off. This residual gap is what the State Employer Wage Benchmark section + the RN wage-gap context above describe structurally.
        </p>
      </div>

      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 720 }}>
          <thead>
            <tr style={{ background: "#f0ece1", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d" }}>
              <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Pathway</th>
              <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Train cost / time</th>
              <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Journey wage</th>
              <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Payback</th>
              <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Local slots / year</th>
              <th style={{ textAlign: "center", padding: "8px 10px", fontWeight: 600 }}>Saturation</th>
              <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const s = satTone(r.saturation);
              return (
                <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                  <td style={{ padding: "10px", fontWeight: 600, color: "#1f1d18" }}>{r.pathway}</td>
                  <td style={{ padding: "10px", color: "#3d3a33" }}>{r.train_cost}<div style={{ color: "#7a756b", fontSize: 11 }}>{r.train_duration}</div></td>
                  <td style={{ padding: "10px", color: "#3d3a33" }}>{r.journey_wage}<div style={{ color: "#7a756b", fontSize: 11 }}>premium {r.annual_premium}</div></td>
                  <td style={{ padding: "10px", textAlign: "right", fontWeight: 600 }}>{r.payback_yrs}</td>
                  <td style={{ padding: "10px", color: "#3d3a33" }}>{r.local_slots}</td>
                  <td style={{ padding: "10px", textAlign: "center" }}>
                    <span style={{ background: s.bg, color: s.fg, padding: "3px 8px", borderRadius: 3, fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em" }}>{r.saturation}</span>
                  </td>
                  <td style={{ padding: "10px", color: "#3d3a33", fontSize: 11, maxWidth: 280 }}>{r.verdict}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What the table tells the workforce board
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Union apprenticeships dominate ROI</strong> (paid training, $0 cost, family-supporting journey wages) BUT their intake is capacity-constrained. Lineworker / Electrician / Pipefitter total ~30-50 apprenticeship slots/yr region-wide. workforce-board pre-apprenticeship investment is highest-leverage where it positions candidates to WIN those slots.</li>
          <li><strong>RN-ADN at JALC + 1yr local → travel-RN is the highest-dollar 2-year credential</strong> with abundant slots. The system already runs but is under-promoted as a deliberate ladder.</li>
          <li><strong>Welder + Industrial Maintenance + CDL OTR + IT-remote</strong> form the second tier — reasonable ROI, hundreds-of-slots local + travel/remote expansion.</li>
          <li><strong>EXTREME-saturation pathways are NOT primary investments</strong>: viticulture top-rung (12-24 total slots region-wide), cannabis top-rung (5-10 slots). Train for these only as second-credential or hobby-to-employment moves, never as primary workforce-board cohort focus.</li>
          <li><strong>CEJA clean-energy pathways split:</strong> CEJA solar installer (NABCEP) has a modest but real local employer base — StraightUp Solar (Marion office, 65MW installed since 2006, NABCEP-certified team), Tick Tock Energy, plus other EnergySage-listed installers in Carbondale/Marion/Murphysboro. Verdict: MED-HIGH saturation, cohort-vs-capacity analysis still needed (annual CEJA grad count vs annual hiring at small installers). CEJA wind technician is PHANTOM locally — Illinois wind farms are 5+ hours north in Livingston / McLean / Lee / LaSalle counties; see the travel-work row for the wind-belt rotational pathway.</li>
        </ul>
      </div>

      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Slot estimates are advisory ranges; verify against the workforce board&apos;s
        own PIRL outcome data (see the &quot;Workforce-board program outcomes (the accountability question)&quot;
        section near the bottom of this page) + employer hiring plans. Wage figures from prior
        sections of this page (training-demand alignment, travel jobs, viticulture, cannabis).
        Baseline for &quot;annual premium&quot; calculation is $32,000/yr (~$15.40/hr) — roughly
        the US median single-earner. MIT 1A+2C livable wage for Jackson County is $97,260/yr
        ($46.76/hr).
      </div>
    </section>
  );
}

function ChildcareGatewaySection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Childcare cost in LWA-25 · $14-22k per child per year (MIT LWC)
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The 1-adult + 2-children Jackson Co. living wage is <strong>$46.76/hr</strong> not
        because food + rent require that much — the MIT Living Wage Calculator allocates{" "}
        <strong>$14,000-$22,000 per child per year</strong> for childcare in that household.{" "}
        <strong>Childcare cost is what makes most training ladders fail the 1A+2C test by
        design.</strong> Until single-parent or two-earner-with-children households can
        secure affordable, quality childcare, the family-supporting wage bar is structurally
        hard to clear for anyone except journey-level union trades — which are themselves
        gated by multi-year apprenticeships and limited annual intake. This is the gateway
        constraint — not the training credentials.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>What helps Illinois families afford childcare</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Child Care Assistance Program (CCAP)</strong> — IL DHS subsidy for working-parent households below specific income thresholds. The eligibility cliff is sharp — small income gains can lose all subsidy. <a href="https://www.dhs.state.il.us/page.aspx?item=149603" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL DHS CCAP</a>.</li>
            <li><strong>Smart Start Illinois</strong> — multi-year initiative to expand childcare access + raise provider-staff wages. $90M in Smart Start Workforce Grants in 2026 ($6,750/classroom/quarter to raise classroom-staff wages by $2-3/hr). <a href="https://www.ilgateways.com/smart-start" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Smart Start IL</a> · <a href="https://www.dhs.state.il.us/page.aspx?item=31667" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDHS Smart Start</a>.</li>
            <li><strong>IL Employer Child Care Tax Credit (2026)</strong> — 20% employer credit for childcare costs paid + 50% start-up credit. Direct lever for employers attracting workers with kids.</li>
            <li><strong>Federal Child Tax Credit + IL EITC</strong> stack with CCAP. Combined refundable credits move ~10-15% of low-income families above the family-supporting bar post-tax.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Human-coordination levers (dashboard cannot automate)</div>
          <div style={{ fontSize: 11, color: "#5a564d", marginBottom: 8, lineHeight: 1.5 }}>The dashboard surfaces childcare cost as the gateway constraint with MIT-LWC + Smart Start sources above. The four levers below are the residual human-coordination work:</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Co-locate childcare with training sites</strong> at JALC / Rend Lake / regional offices. Drop-in childcare materially lowers the barrier for parents enrolling in 12-24mo credentials.</li>
            <li><strong>Negotiate employer-paired childcare benefits</strong> in CBA / community-engagement framing with major federal-contracting employers (GD-OTS, Continental Tire, Aisin). On-site or stipend-based childcare costs the employer $200-400/wk and gains ~$3-5/hr in retained-worker effective wage.</li>
            <li><strong>Steer local in-home providers into Smart Start grant applications.</strong> The $90M IL DHS Workforce Grant pool is materially under-applied-for by LWA-25 providers; the technical-assistance gap is the constraint.</li>
            <li><strong>Frame childcare-worker positions as a career on-ramp</strong> in regional credential outreach. The CDA → Bachelor&apos;s ECE → director ladder reaches family-supporting at the upper rungs (same structure as CNA → LPN → RN).</li>
          </ul>
        </div>
      </div>
      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Childcare-cost figures from <a href="https://livingwage.mit.edu/counties/17077" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>MIT Living Wage Calculator — Jackson County 17077</a>. Smart Start $90M figure from <a href="https://aftonpartners.com/case-studies/smart-start-workforce-grants/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Afton Partners Smart Start case study</a>.
      </div>
    </section>
  );
}

function HealthcareWorkforceSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Healthcare workforce shortage · the federal-dollar lever the page nearly missed
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Most of LWA-25 carries federal <strong>Health Professional Shortage Area
        (HPSA)</strong> designations. HPSA designations unlock specific federal-funded
        workforce-recruitment incentives that bring physicians, NPs, PAs, dentists,
        psychiatrists, certified nurse midwives, behavioral-health clinicians — AND
        registered nurses (via a separate Nurse Corps program) — into the region at
        competitive loan-repayment rates.
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(97% 0.04 60)", border: "1px solid oklch(58% 0.15 60)33", borderLeft: "6px solid oklch(58% 0.15 60)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(40% 0.15 60)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Does the region pay less than other regions for nursing?
        </div>
        <p style={{ margin: "0 0 8px 0" }}>
          Yes — verifiably so, and the gap is structural. Per the most recent <a href="https://www.bls.gov/regions/midwest/news-release/occupationalemploymentandwages_carbondale.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS Carbondale-Marion MSA occupational wage release (May 2023)</a>, workers in the Carbondale-Marion MSA had an <strong>average hourly wage of $26.21 vs the national average of $31.48 — a 17% wage gap across ALL occupations</strong>. For registered nurses specifically: per the <a href="https://www.bls.gov/regions/midwest/news-release/nursesoccupationalemploymentandwages_illinois.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS Midwest Office Illinois nursing-occupations release</a>, <strong>10 of 13 Illinois metropolitan areas (Carbondale-Marion among them) had RN annual mean wages significantly below the national average</strong>. Pull the current Carbondale-Marion RN-specific figure from the <a href="https://www.bls.gov/oes/2023/may/oes_16060.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS OES May 2023 Carbondale-Marion table</a> (SOC 29-1141) and compare against the national RN median of $93,600 (May 2024).
        </p>
        <p style={{ margin: 0 }}>
          <strong>Implication for the workforce board:</strong> credential pipelines for RN ladder
          (CNA → LPN → ADN-RN → BSN at JALC) produce graduates who land into a regional
          wage structure ~17% below national norms. Loan repayment programs partially
          offset this — but the structural wage compression matters when private healthcare
          employers benchmark offers against the broader regional wage market. This is the
          same dynamic the State Employer Wage Benchmark section describes, applied to
          healthcare specifically.
        </p>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>HPSA designation + NHSC loan repayment</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Look up LWA-25 HPSA designations</strong> at <a href="https://data.hrsa.gov/topics/health-workforce/shortage-areas" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>HRSA Shortage Area tool</a>. Counties with Primary Care HPSAs, Mental Health HPSAs, and Dental HPSAs each unlock separate federal programs.</li>
            <li><strong>NHSC Loan Repayment</strong> — up to <strong>$75,000 over 2 years</strong> for primary-care clinicians serving full-time at an NHSC-approved site in a HPSA ($50k for non-primary-care). Half-time options at half-pay. Renewable. <a href="https://nhsc.hrsa.gov/loan-repayment/nhsc-loan-repayment-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NHSC LRP</a>.</li>
            <li><strong>NHSC Rural Community LRP</strong> — separate stream for SUD treatment in rural HPSAs. <a href="https://nhsc.hrsa.gov/loan-repayment/nhsc-rural-community-loan-repayment-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NHSC Rural LRP</a>.</li>
            <li><strong>NHSC Substance Use Disorder Workforce LRP</strong> — direct overlay on regional opioid crisis. <a href="https://nhsc.hrsa.gov/loan-repayment/nhsc-sud-workforce-loan-repayment-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NHSC SUD LRP</a>.</li>
            <li><strong>IL State Loan Repayment Program (SLRP)</strong> — stackable with NHSC; IDPH-administered. Currently in funding gap (<a href="https://dph.illinois.gov/topics-services/life-stages-populations/rural-underserved-populations/slrp.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDPH SLRP</a>); track for re-opening.</li>
            <li><strong>Behavioral Health Workforce Center</strong> — IL-specific BH practitioner loan repayment. <a href="https://illinoisbhwc.org/about/loan-repayment-programs/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BHWC</a>.</li>
            <li><strong>NHSC Nurse Corps Loan Repayment Program (RN-specific — separate from main NHSC LRP).</strong> The NHSC LRP referenced above is for physicians + NPs + PAs + CNMs + dentists + psychiatrists. <strong>Registered nurses, advanced practice nurses, and nursing-school faculty have their own separate program</strong> through HRSA: the <a href="https://bhw.hrsa.gov/funding/apply-loan-repayment/nurse-corps" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Nurse Corps LRP</a>. Pays up to 85% of outstanding nursing-school loan balance for 3 years of service at a Critical Shortage Facility in a HPSA. Marion VA, SIH, Memorial Carbondale, and Shawnee Health Service are candidate qualifying employers. Direct, specific lever for the RN wage-gap problem above.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Federal-grant programs anchored on HPSA designation</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>HRSA Rural Residency Planning and Development (RRPD)</strong> — up to $750k over 36mo to plan a new rural residency program. SIU School of Medicine + SIH/Memorial could partner.</li>
            <li><strong>J-1 visa waiver Conrad 30 program</strong> — each state has 30 slots/year for foreign-trained physicians completing US residency to waive 2-year home-country requirement in exchange for 3yr serving a HPSA. <strong>DRA&apos;s Delta Doctors program is the J-1 waiver overlay for DRA-eligible counties</strong> — direct lever (see DRA section below).</li>
            <li><strong>HRSA FQHC New Access Point grants</strong> — start-up funding for new community health centers in HPSAs. Existing LWA-25 FQHC: Shawnee Health Service.</li>
            <li><strong>HRSA Teaching Health Center GME</strong> — funds primary-care residency slots at community-based teaching sites (vs traditional AMCs). SIH or Memorial could host.</li>
            <li><strong>USDA Rural Health Care Services Outreach Grant</strong> — operational support for rural healthcare delivery.</li>
          </ul>
        </div>
      </div>
      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Sources: <a href="https://nhsc.hrsa.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NHSC / HRSA</a>, <a href="https://www.ruralhealthinfo.org/funding/3492" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Rural Health Information Hub</a>, <a href="https://illinoisbhwc.org/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL Behavioral Health Workforce Center</a>.
      </div>
    </section>
  );
}

function HousingAffordabilitySection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Housing affordability for relocators · what every people-attraction strategy needs
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Every people-attraction strategy assumes housing exists at price points relocators
        can absorb. The good news: Carbondale-Marion MSA housing is materially cheaper than
        nearly every metro relocators would be leaving. The bad news: cheap relative to
        coastal metros doesn&apos;t mean adequate — the local rental + sale stock may not
        absorb 50-200+ relocators per year without price escalation that hurts incumbent
        renters.
      </div>
      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Current housing indicators (full detail in /carbondale + /murphysboro pages)</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong>Carbondale</strong>: median home ~$124,800 · median gross rent ~$750/mo · 73% renter-occupied (college-town pattern).</li>
          <li><strong>Murphysboro</strong>: median home ~$79,600 · median gross rent ~$655/mo · 51% renter-occupied — more owner-occupied than Carbondale.</li>
          <li><strong>Carbondale-Marion MSA median days on market: ~89 days</strong> — buyer-leverage market, not seller-leverage. Buyer demand can absorb at current price levels.</li>
        </ul>
      </div>

      {/* Inventory-quality bifurcation: the median-price math hides which inventory is actually desirable */}
      <div style={{ marginBottom: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          The median-price math hides the inventory-quality problem
        </div>
        <div style={{ marginBottom: 10 }}>
          The price/wage affordability ratio shows Southern IL as cheap relative to coastal metros — true. But the relocator BD pitch needs to acknowledge that <strong>the affordable inventory is mostly old, mostly rental-degraded, and mostly outside the growth corridor.</strong> Census-verified town-by-town pattern (ACS 5-year tables; full source list below):
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 10 }}>
          <div style={{ background: "white", border: "1px solid #ebe5d6", borderRadius: 4, padding: 12 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>✓ Newer construction corridor (Williamson Co.)</div>
            <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
              <li><strong>Marion</strong> — Morningside Phase 11 + Tower Square Art District + Prairie Meadows subdivisions. 90-acre S. Market St tract + 36.6-acre Longstreet Rd tract adjacent to Marion Star Bond District in active development. 4BR/2.5BA contemporary-style new construction.</li>
              <li><strong>Carterville</strong> — Cedar Creek + Rolling Hills Estates + Spring Garden Estates subdivisions; newer properties $400-600k.</li>
              <li><strong>Herrin</strong> — newer single-family + duplex developments on the north + east sides; Williamson County Housing Authority active.</li>
              <li><strong>Anchor:</strong> Walker&apos;s Bluff Casino &amp; Resort + IL-13 6-lane widening Marion→Carbondale + Aisin + GD-OTS = economic-growth driver.</li>
              <li><strong>Williamson Co. is the standout growth county in the Southern Illinois (LWA-25) footprint</strong> per Marion Chamber + regional sources; growth is modest relative to Chicago collar counties (Kendall, McHenry) + Metro-East but is the active corridor downstate.</li>
            </ul>
          </div>
          <div style={{ background: "white", border: "1px solid #ebe5d6", borderRadius: 4, padding: 12 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(45% 0.20 22)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>⚑ Old-stock / rental-degraded clusters</div>
            <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
              <li><strong>Carbondale</strong> — median build year <strong>1976</strong>, <strong>73% renter-occupied</strong> (college-town pattern), <strong>19.79% vacancy</strong> (~2× healthy market), <strong>0.1% of stock added 2020+</strong> (essentially zero new construction in 5 years). Price spread $97k–$150k same window = bifurcated inventory tiers. A Mandatory Rental Housing Inspection Program is in place; buyers should pull the inspection record before pricing condition.</li>
              <li><strong>Murphysboro</strong> — median build year <strong>1962</strong>; <strong>25.1% built before 1940; 9.2% by 1949 → 34% pre-WWII stock</strong>. <strong>5.5% of housing lacks complete plumbing; 7.1% lacks complete kitchen</strong> (Census ACS condition red flags). The City Code Enforcement Division is the public-records source for parcel-level condition checks.</li>
              <li><strong>Desoto</strong> (Jackson Co. village, ~5 mi north of Carbondale) — small village, predominantly pre-1970 stock (ACS B25034 for Census Place 16000US1719993).</li>
              <li><strong>Ziegler + Royalton</strong> (Franklin Co.) — legacy coal-town housing; ACS B25034 shows pre-1940 share well above county average for both villages.</li>
              <li><strong>Benton</strong> (Franklin Co. seat) + <strong>Mt. Vernon</strong> (Jefferson Co. seat, Continental Tire town) — both carry pre-1970-dominant ACS B25034 profiles despite Mt. Vernon having Continental Tire (3,667 jobs). The economic anchor didn&apos;t pull new residential construction.</li>
            </ul>
          </div>
        </div>
        <div style={{ marginBottom: 6 }}>
          <strong>What this means for the relocator BD pitch:</strong>
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Direct relocators (federal retirees, data-center execs, climate-migration prospects) to the Marion–Herrin–Carterville corridor</strong>, not to Carbondale or Murphysboro. The Williamson Co. triangle has the desirable inventory; the Jackson Co. cities have the old stock.</li>
          <li><strong>Don&apos;t hide the bifurcation.</strong> Carbondale&apos;s $97-150k median-price spread looks affordable but most of the cheap inventory is rental-degraded; the desirable inventory is priced at premium relative to condition. A long-tenured rental craftsman two-bedroom isn&apos;t a good deal at the upper end of that range — buyers should pull the Mandatory Rental Housing Inspection record before pricing condition.</li>
          <li><strong>SIU graduate-retention housing</strong> needs to be in walking/biking distance of campus — that&apos;s Carbondale&apos;s old stock. Pair retention incentives with rental-quality enforcement, not pure affordability.</li>
          <li><strong>The economic-anchor → housing-growth link is not automatic.</strong> Mt. Vernon has 3,667 Continental Tire jobs and still has old residential stock; without an active municipal posture toward new construction (TIF, sewer extension, zoning incentives), anchor employers alone don&apos;t pull desirable inventory. Marion has gotten this right (Star Bond District + Longstreet expansion); other anchor towns can copy the playbook.</li>
        </ul>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
          Sources: <a href="https://www.census.gov/quickfacts/fact/table/carbondalecityillinois/PST045221" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census QuickFacts Carbondale</a> + <a href="http://censusreporter.org/profiles/16000US1711163-carbondale-il/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census Reporter Carbondale</a> + <a href="https://www.city-data.com/housing/houses-Murphysboro-Illinois.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>City-Data Murphysboro housing</a> + <a href="https://www.explorecarbondale.com/189/Mandatory-Rental-Housing-Inspection-Prog" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Carbondale Mandatory Rental Housing Inspection Program</a> + <a href="https://murphysboro.com/government/city-departments/public-works/code-enforcement/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>City of Murphysboro Code Enforcement</a> + <a href="https://marionillinois.com/relocation/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Marion Chamber of Commerce — Relocating + Investing in Marion</a> . Census housing-stock figures are sourced from ACS 5-year tables: B25034 (Year Structure Built), B25003 (Tenure / Owner vs Renter), B25002 (Occupancy Status incl. vacancy), B25047 (Complete Plumbing Facilities), B25048 (Complete Kitchen Facilities), accessible via Census Reporter + data.census.gov for the Carbondale + Murphysboro Census Place codes (16000US1711163 + 16000US1751453).
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Supply work needed before scaling relocation</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Rental-stock vacancy audit</strong> via ACS B25004; if &lt;5% in target neighborhoods, incentive program drives rent inflation.</li>
            <li><strong>Single-family inventory tracking.</strong> 89 days on market looks healthy now; below 30 days = supply-constrained. Track quarterly.</li>
            <li><strong>Carbondale Amtrak TOD overlay</strong> should add 200-400 mixed-use units within 1/4 mi of the new station. Murphysboro could add 100-150.</li>
            <li><strong>Modular + manufactured housing</strong> is the under-leveraged affordable-supply category. Most LWA-25 zoning permits it; quality + financing-access are the constraints (FHA Title I + USDA Section 502 manufactured-home loans).</li>
            <li><strong>Senior/retiree housing.</strong> Federal-retiree strategy needs accessible one-story stock; currently under-supplied. Addressable via Section 202 + LIHTC senior allocations.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Federal + state housing-supply funding levers</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>USDA Rural Housing Service (Sections 502, 504, 515)</strong> — single-family rural housing loans + multifamily rural housing development. LWA-25 is rural-eligible across most census tracts. <a href="https://www.rd.usda.gov/programs-services/single-family-housing-programs" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USDA RHS</a>.</li>
            <li><strong>IL Housing Development Authority (IHDA)</strong> — LIHTC + tax credits + low-interest loans. <a href="https://www.ihda.org/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IHDA</a>.</li>
            <li><strong>HUD Section 202 (senior) + Section 811 (disability)</strong> — capital advance + project-based rental assistance. Direct lever for retiree-targeted housing supply.</li>
            <li><strong>HUD HOME Investment Partnerships</strong> — block-grant flexible affordable-housing funding.</li>
            <li><strong>CDFI Capital Magnet Fund + New Markets Tax Credits</strong> — both stackable in LWA-25 (also under IL programs stack below).</li>
            <li><strong>FHLB Chicago Affordable Housing Program (AHP)</strong> — competitive grants for affordable housing development.</li>
          </ul>
        </div>
      </div>
      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <strong>The strategic sequence:</strong> housing-supply work should run 12-18
        months AHEAD of any major people-attraction program scaling. Standing up a
        200-unit TOD overlay near the new Amtrak station is a 24-36 month build; the
        relocation incentive program should launch only when supply can absorb demand
        without driving local-renter rent burden up. <strong>The Boulder / Bozeman / Bend
        cautionary tale:</strong> desirable-place economic-development success creates
        housing-affordability crisis for incumbent residents if supply lags demand.
      </div>
    </section>
  );
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
        Training-to-demand alignment · the single-earner 1A+2C test · 22 pathways scored against $46.76/hr MIT-LWC bar
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Workforce-development theater: grant comes in, training cohort starts, graduates
        hit the labor market — but does the credential they earned have local employers
        to hire them, at wages that clear the MIT Living Wage 1A+2C bar (one working
        adult supporting two children)? This cross-references every major regional
        training ladder against (a) actual local sector employment from BLS QCEW and
        (b) the MIT Living Wage benchmark for Jackson County. PHANTOM PIPELINE means
        the credential has nowhere to land locally — graduates relocate, commute, or
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
                {l.entry_gates && l.entry_gates.length > 0 && (
                  <div style={{ display: "flex", gap: 5, marginTop: 8, flexWrap: "wrap" }}>
                    {l.entry_gates.map(g => (
                      <span key={g} title="Entry-gate filter — washes out portions of the trainable cohort independent of training success" style={{
                        fontSize: 10, fontWeight: 600, color: "#5a564d", background: "#f0ece1",
                        padding: "2px 7px", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.04em",
                        border: "1px solid #d8d2c4",
                      }}>
                        ⚑ {g.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                )}
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
            <div style={{ marginTop: 10, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
              {l.verdict.startsWith("TRAVEL-WORK") ? (
                <>
                  <strong>TRAVEL-WORK pathway · this row is detailed in the Travel Jobs section below.</strong>{" "}
                  See <a href="#sec-travel-jobs" style={{ color: "#1f5f8f", fontWeight: 600 }}>Travel Jobs (§09)</a> for the rotation pattern, per-diem math, and the 4-way household-configuration matrix (dual-earner-w-kids ✓ / single-no-kids ✓ / single-parent-w-kids ✗ / dual-on-rotation ⚑) that determines whether the wage&apos;s family-supporting clearance translates into family-supporting reality.
                </>
              ) : (
                l.notes
              )}
            </div>
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
      name: "Pipefitter / Steamfitter (UA Local 553)",
      cred: "5yr apprenticeship → journey",
      trainSource: "UA Local 553 (East Alton IL) pre-apprenticeship — chartered Aug 1933, 7-county jurisdiction in southern IL",
      wage_hrly: "$50-65/hr",
      per_diem: "$80-130/day",
      annual_est: "$110-160k+",
      travel_pattern: "Refinery/petrochem/power-plant outages; 4-12wk projects; predictable home weekends",
      family_compat: "OK",
      note: "Outage season concentrates work in spring/fall. UA Local 553 jurisdiction covers southern IL including the Carbondale-Marion area (Illinois Pipe Trades Association locals directory). Top-paying construction trade in the region. Verify current scale + apprenticeship intake at ualocal553.org.",
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
      name: "Ironworker (verify exact local for Carbondale-Marion)",
      cred: "3-4yr apprenticeship → journey",
      trainSource: "Verify correct IW local for LWA-25 via ironworkers.org directory — IW Local 393 is Aurora IL (not Marion); Local 392 is East St. Louis IL (closer fit for downstate work); members may also work via the IW traveling card",
      wage_hrly: "$40-50/hr",
      per_diem: "$80-110/day",
      annual_est: "$90-130k",
      travel_pattern: "Bridge + industrial steel; mix of local + 2-4hr radius projects",
      family_compat: "GOOD",
      note: "Two ironworkers locals split LWA-25 jurisdiction: <strong>Iron Workers Local 392 (East St. Louis)</strong> covers Perry + Jefferson + parts of Franklin + Jackson (per Local 392's published territorial map); <strong>Iron Workers Local 782 (Paducah KY, &quot;mixed local&quot; covering KY + IL + MO + TN)</strong> covers Williamson + Alexander + Hardin + Johnson + Massac + Pope + Pulaski + Union + parts of Franklin + Jackson + Saline + Gallatin. Williamson County (Marion + GD-OTS + Aisin + Marion VA) is Local 782 territory. Local 393 (Aurora, ~4hr north) does NOT serve Southern IL.",
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
      note: "Local 318 staffed Big Muddy Solar construction (124 MW, Jackson Co. — south of Vergennes; $200M Arevon investment, ~$12.6M property tax flowing to Elverado School District + Jackson Co. over project life). Same union has wind-farm cranes in IA/TX wind belt — multi-week projects with per-diem.",
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
      note: "The CEJA wind tech credential lives here, NOT as a local job. Illinois wind farms are concentrated in Livingston / McLean / Lee / LaSalle Cos. — 4-8hr drive from LWA-25. Many techs do rotational shifts that keep half the month at home.",
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
      <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 16 }}>
        <strong>Honest framing — wage clears, home-time depends on household configuration.</strong> Each row below carries a TRAVEL-WORK verdict in the 1A+2C single-earner taxonomy. The wage column shows the all-in number that clears the MIT Living Wage Jackson Co. 1A+2C bar ($46.76/hr / ~$97k/yr) — sometimes by 2× or more. <strong>The viability of travel work depends entirely on who&apos;s at home covering the kids.</strong>{" "}<em>The same home-time test extends to the LOCAL · FAMILY-SUPPORTING rows further down — the §14 Structural Constraints section documents the mandatory-overtime pattern (Aisin, Continental, USG, etc.) where wage clears 1A+2C but a 6-day / 50-60 hr schedule destroys home-life. Read both together.</em>
        <div style={{ marginTop: 10, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
          <div style={{ background: "white", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "4px solid oklch(45% 0.16 142)", borderRadius: 4, padding: 10 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(35% 0.18 142)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>✓ Dual-earner with children</div>
            <div style={{ fontSize: 12, lineHeight: 1.5 }}>One spouse travels, the other stays home (or stays at a local job) and covers the kids. Wage clears 1A+2C math AND home-time reality. <strong>Family-supporting.</strong></div>
          </div>
          <div style={{ background: "white", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "4px solid oklch(45% 0.16 142)", borderRadius: 4, padding: 10 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(35% 0.18 142)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>✓ Single, no children</div>
            <div style={{ fontSize: 12, lineHeight: 1.5 }}>No dependents to cover during away-time. Wage clears 1A+2C math AND is far above single-adult LW. <strong>Family-supporting (no family to support yet, but the math works for future).</strong></div>
          </div>
          <div style={{ background: "white", border: "1px solid oklch(45% 0.20 22)33", borderLeft: "4px solid oklch(45% 0.20 22)", borderRadius: 4, padding: 10 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(40% 0.20 22)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>✗ Single parent with children</div>
            <div style={{ fontSize: 12, lineHeight: 1.5 }}>No surrogate caregiver during away-time. Wage clears the math but the kids have no parent at school pickup, dinner table, back-to-school night. <strong>Fails the family-supporting reality unless grandparents or paid live-in help cover.</strong></div>
          </div>
          <div style={{ background: "white", border: "1px solid oklch(45% 0.20 22)33", borderLeft: "4px solid oklch(45% 0.20 22)", borderRadius: 4, padding: 10 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(40% 0.20 22)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>⚑ Dual-earner, neither home, with children</div>
            <div style={{ fontSize: 12, lineHeight: 1.5 }}>Both spouses on travel rotations. Wage stacks well but requires full-time surrogate caregiver (live-in nanny, grandparents, boarding school). Rare; high cost.</div>
          </div>
        </div>
        <div style={{ marginTop: 10 }}>
          Some rows carry the LOCAL · WAGE-SUPPRESSED flag where BASE-only wage (no per-diem, no rotation premium) falls below 1A+2C; those clear the family bar only on the all-in number that ASSUMES the away-from-home schedule. The TRAVEL-WORK lifestyle cost is the structural trade-off salary tables don&apos;t show: high divorce risk among long-rotation crews + missed milestones + surrogate childcare expense (grandparents, the home-side spouse&apos;s parents, paid live-in help). Workforce planning must match credential pathway to household configuration, not pitch the all-in wage and ignore the configuration question.
        </div>
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
                <div style={{ fontSize: 11, marginTop: 6, lineHeight: 1.45 }}>
                  <strong style={{ color: "#1f1d18" }}>Household-config fit:</strong>
                  <span style={{ color: "oklch(35% 0.18 142)", marginLeft: 6, fontWeight: 600 }}>✓ dual-earner-with-kids</span>
                  <span style={{ color: "oklch(35% 0.18 142)", marginLeft: 6, fontWeight: 600 }}>✓ single-no-kids</span>
                  <span style={{ color: "oklch(45% 0.20 22)", marginLeft: 6, fontWeight: 600 }}>✗ single-parent-with-kids</span>
                  <span style={{ color: "#5a564d", marginLeft: 6 }}>(worker away during {r.family_compat === "TOUGH" ? "13-week-on / 8-week-home" : r.family_compat === "OK" ? "rotation hitch" : "shift block"} — childcare must be covered by spouse / grandparents / paid arrangement)</span>
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-end" }}>
                <div style={{
                  fontSize: 11, fontWeight: 700, color: "white", background: compatTone(r.family_compat),
                  padding: "5px 10px", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.06em",
                  whiteSpace: "nowrap",
                }}>
                  {r.family_compat === "GOOD" ? "FAMILY-FRIENDLY TRAVEL" : r.family_compat === "OK" ? "MANAGEABLE TRAVEL" : "TRAVEL-HEAVY"}
                </div>
                <div style={{
                  fontSize: 10, fontWeight: 700, color: "oklch(40% 0.18 60)", background: "oklch(97% 0.04 60)",
                  padding: "3px 8px", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.05em",
                  whiteSpace: "nowrap", border: "1px solid oklch(45% 0.18 60)33",
                }}>
                  TRAVEL-WORK · wage clears w/ per-diem · home-time fails
                </div>
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
        <strong>The regional workforce-development strategic gap this fills:</strong> the
        existing CEJA wind technician pipeline suffers from local-employer
        scarcity (Illinois wind farms are 5+ hours north). CEJA solar installer
        has a modest local employer base (StraightUp Solar Marion + Tick Tock Energy
        + others) — capacity-vs-cohort sizing is the question, not credential-validity.
        The wind-tech credential is real and valuable on travel-supported work — reframing
        the wind cohort outcome from &quot;land a local job&quot; to &quot;land a regional
        travel-pay job with predictable home time&quot; changes what success looks like.
        Pair with Big Muddy Solar (which IS hiring local IBEW/IUOE/LIUNA for utility-scale
        construction) for that line of work + the broader regional travel circuit for
        ongoing income.
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
        Anchor-employer attraction · Tier-2 data centers, federal satellite labs, university-anchored programs
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
          { factor: "Stranded coal-plant interconnect", grade: "✓ STRONG", note: "Baldwin retirement (Randolph Co., adjacent) = ~1,200 MW of substation capacity in MISO-South. PLUS: Grand Tower Energy Center (Jackson Co., on the Mississippi River) = 478-523 MW natural-gas combined-cycle, currently MOTHBALLED. Owned by Rockland Capital / Main Line Generation since January 2014 (acquired from Ameren). The original CIPS Grand Tower plant was coal; the combined-cycle gas facility replaced it and is now sitting idle. Two stranded interconnects within ~50 mi — both Ameren IL service territory, both grid-scale ready. Hyperscalers + AI-training operators value stranded sites for fast interconnect timelines. Source: Global Energy Monitor Grand Tower Energy Center page.", color: "oklch(45% 0.16 142)" },
          { factor: "Power utility — Egyptian Electric as Ameren alternative", grade: "✓ STRONG", note: "Egyptian Electric Cooperative Association (EECA, Murphysboro HQ) serves four of five LWA-25 counties (Jackson, Williamson, Perry, Franklin) plus six adjacent (Randolph, St. Clair, Johnson, Union, Monroe, Washington). Note: Jefferson County is NOT in EECA territory (same gap pattern as DRA eligibility). Member-owned coops typically structure more flexible industrial rates than IOUs. For 100MW+ data-center loads, the wholesale supply comes from EECA's G&T parent (Southern Illinois Power Cooperative, generation physically located in Williamson + Washington Cos.) + the MISO market — but EECA is the negotiation counterparty for retail-scale arrangements. The TVA + local-distribution-coop model served Google's Chattanooga DC.", color: "oklch(45% 0.16 142)" },
          { factor: "Local renewable supply pipeline", grade: "✓ EMERGING", note: "Arevon Energy's 124 MW Big Muddy Solar Project (Jackson County, commercial operation end of 2026, $200M private investment) is utility-scale solar feeding the local grid. For data-center recruitment, this is a concrete answer to the 'green PPA?' question — both Ameren-served and EECA-served sites can structure direct or virtual PPAs against Big Muddy generation.", color: "oklch(45% 0.16 142)" },
          { factor: "IL Data Center Investments Act", grade: "✓ STRONG", note: "Public Act 101-0031 — 20-year sales-tax exemption on equipment + property-tax abatement eligible. Eligibility floor per IL DCEO program page (dceo.illinois.gov/expandrelocate/incentives/datacenters.html): $250M minimum capital investment over 60 months, minimum 20 FTE at 120% of COUNTY MEDIAN WAGE, carbon-neutral OR green-building certification required. The 120%-of-county-median-wage requirement is a workforce-board WIN — any DC operator must pay above median to qualify. Underserved-area projects unlock an additional 20% construction-wage tax credit. File DCEO certification before any RFP arrives.", color: "oklch(45% 0.16 142)" },
          { factor: "Water (cooling)", grade: "✓ STRONG", note: "Crab Orchard NWR, Kinkaid Lake, Mississippi River access. Sufficient for all but the largest installations.", color: "oklch(45% 0.16 142)" },
          { factor: "Land cost", grade: "✓ STRONG", note: "Undervalued vs Northern Virginia, Phoenix, Columbus.", color: "oklch(45% 0.16 142)" },
          { factor: "Power cost — Ameren vs Egyptian Electric Cooperative (EECA) head-to-head", grade: "~ MODERATE", note: "Ameren IL industrial rate ~$0.08-0.09/kWh. EECA (member co-op) negotiates large-power deals bespoke; expect ~$0.06-0.08/kWh range. EECA wholesales from SIPC's Marion Generating Station at Lake of Egypt (Williamson Co.) — 260 MW total (120 MW coal + 140 MW gas/oil). SIPC also owns 125 MW of Prairie State (Lively Grove), 28 MW SEPA hydro, 10 MW Pioneer Trail Wind, 100 MW Big River Solar. Local generation for local load. Neither beats NoVa $0.06 on paper; the bespoke-deal latitude + IL Data Center Act sales-tax exemption change the all-in math. Sources: sipower.org, icl.coop, thesouthern.com.", color: "oklch(48% 0.15 60)" },
          { factor: "Federal IRA Energy Communities adder", grade: "✓ STRONG", note: "Franklin and Perry counties are coal-closure tracts. Solar/wind/storage projects sited here get IRA §48 +10pp ITC bonus on top of 30% base. Use for behind-the-meter generation co-located with DC.", color: "oklch(45% 0.16 142)" },
          { factor: "Fiber diversity — the grant-but-no-coverage paradox", grade: "✗ WEAK", note: "Public broadband investment in Southern IL is large and verifiable. NTIA's BTOP program funded a 23-county middle-mile network connecting 232 community anchor institutions (NTIA grant filing, ntia.doc.gov). Recent IL state Connect Illinois rounds have added WK&T's $9.8M (Jackson + Union Cos.) and ProTek Communications' $51M (Franklin/Jackson/Johnson/Massac/Williamson/Union Cos.). BEAD adds another $1B+ in IL allocation. Coverage on paper has improved. But data-center-grade fiber diversity is a different problem these grants don't fully solve: hyperscale needs 3+ INDEPENDENT carriers with physically diverse routes; most LWA-25 enterprise-class footprint has 1-2 carriers, not 3+ with route diversity. Carriers present include AT&T, Frontier, Mediacom, Clearwave, WK&T, ProTek. Constructive fix-up paths the region's recruiters can pursue: (a) IL Century Network (ICN — state-owned middle-mile) as alternative wholesale source, (b) municipal / coop broadband authority creation, (c) IIJA middle-mile grants directed to public or cooperative entities. This remains the single weakest scorecard line for hyperscale recruitment.", color: "oklch(45% 0.20 22)" },
          { factor: "Operations talent (200-person ops staff)", grade: "✗ WEAK", note: "SIU produces some IT capacity but no existing data-center workforce concentration. the workforce board + JALC + Rend Lake would need to stand up a DC-ops training program in parallel to any recruitment.", color: "oklch(45% 0.20 22)" },
          { factor: "On-campus utility anchor — SIU Carbondale Power Plant", grade: "✓ EMERGING", note: "SIU operates a 3.14 MW coal-fired turbine + district steam loop (4 boilers: 1 primary coal CFB + 1 gas + 2 mothballed). Supplies 10-15% of campus electrical load. 2022 sustainability proposals ($105-120M) target the coal→gas conversion. Existing generation + steam + electrical-bus + cooling-tower water are an anchor asset for university-adjacent data-center siting. NSF NCSA Petascale at UIUC is the documented comparable. Sources: facilities.siu.edu/utilities/ + Global Energy Monitor.", color: "oklch(48% 0.15 60)" },
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
              fit_strong: "Shawnee NF is the largest forest reservation in IL — 280k acres. The USFS Northern Research Station (NRS) HAS HAD historical Carbondale-area presence via the Kaskaskia Experimental Forest (researchers Minckler + Lane in published NRS literature) — verify current staffing structure post-NRS consolidation before claiming an active office. The University of Illinois Natural History Survey operates a separate Kaskaskia Biological Station near Lake Shelbyville (not USFS).",
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
        University research-anchored federal programs · &quot;Eds and Meds&quot; · SIU as the bid vehicle
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        SIU Carbondale is a <strong>Carnegie R1 research university</strong> (top tier of US
        research institutions) — the credential most federal research programs require to
        even compete. This puts LWA-25 squarely in the <strong>&quot;Eds and Meds&quot;</strong>
        category — the playbook that anchored post-industrial-transition Pittsburgh
        (Carnegie Mellon + UPMC), Cleveland (Case Western + Cleveland Clinic — birthplace of
        the Evergreen Cooperatives model already cited), Indianapolis (IUPUI + IU Health),
        and Buffalo (UB + Roswell Park) — to <a href="https://anchors.org/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Anchor Institutions Task Force / anchors.org</a> for the framework.
        LWA-25&apos;s Eds-and-Meds substrate: SIU + SIU School of Medicine (Springfield) +
        SIH + Memorial Carbondale + Marion VA + JALC + Rend Lake. That&apos;s a real
        institutional stack to anchor regional strategy on. SIU is the bid vehicle through
        which the region can capture multi-decade, multi-million-dollar federal research
        investment that <em>creates $80-130k research-staff positions and graduate-student-
        to-permanent-staff pipelines</em>. SIU already wins individual NSF/NIH/USDA grants
        — the strategic move is to win the BIG center-scale programs <em>using the
        Eds-and-Meds anchor frame</em>.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
        {[
          {
            program: "NSF Regional Innovation Engines",
            funding: "Up to $160M over 10 years (Type-2) with initial $15M committed; remainder subject to annual NSF progress review · ~$1M / 2yr Type-1 prep grant",
            what: "NSF&apos;s flagship 'transform a region around a technology specialty' program. 29 semifinalists in the 2025 round. Each Engine builds a research-to-commercialization ecosystem around one key technology area.",
            fit: "SIU&apos;s coal-mine rare-earth extraction work + the broader 'critical minerals from legacy coal infrastructure' theme is exactly the kind of differentiated regional bet NSF wants. Other candidate themes: rural broadband + AI agriculture (with UIUC partnership); Mississippi River corridor environmental sensing.",
            process: "Need multi-sector regional coalition: SIU + UIUC + JALC + Rend Lake + the workforce board + IL DCEO + at least 3-5 industry partners. Start with the $1M Type-1 prep grant — apply for Type-2 after 24mo coalition-building.",
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
            fit: "Underleveraged. The local feed could be much stronger if the workforce board promoted the pathway.",
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
          <li><strong>Forestry / forest health</strong> — Shawnee NF adjacent (280k acres); Kaskaskia Experimental Forest legacy through USFS NRS literature. Confirm current staffing structure with the NRS directorate before claiming an active station.</li>
          <li><strong>Aviation</strong> — SIU Aviation Flight + FAA AT-CTI partnership — underleveraged.</li>
          <li><strong>Agriculture</strong> — College of Agricultural, Life &amp; Physical Sciences — natural USDA partner.</li>
          <li><strong>Medical / rural health</strong> — SIU School of Medicine (Springfield) is the NIH bid vehicle.</li>
          <li><strong>Workforce development research</strong> — partnership with JALC + Rend Lake creates a community-college-research consortium opportunity for DOL grants.</li>
        </ul>
      </div>

      {/* === Industrial real-estate inventory: success + candidate === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Industrial real-estate inventory · success precedent + active candidate
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Vacant industrial buildings are a BD lever — they signal the labor shed already exists, the utility infrastructure is built, and the cost basis for a new tenant is far below greenfield. LWA-25 has one successful adaptive-reuse precedent and one active municipally-controlled redevelopment candidate.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 24 }}>
        <div style={{ background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, padding: 16 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>Success precedent — Maytag → Aisin</div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 6 }}>Former Maytag Plant · Herrin · 800,000+ sq ft</div>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            Maytag operated this Williamson Co. facility for 40+ years; Whirlpool acquired Maytag March 2006 and closed Herrin by end of 2006 (~1,000 jobs lost; ~$35M/yr regional payroll). <strong>Phoenix Investors LLC (Milwaukee) acquired the property for $1 million in 2015</strong>; the plant is now repurposed with <strong>Aisin Manufacturing</strong> + Ortho Tech + Southern Illinois Hospital as anchor tenants. That is HOW the Aisin Marion footprint (2,000+ jobs across Aisin Mfg + Electronics + Light Metals) partially rebuilt regional manufacturing presence — adaptive reuse of stranded industrial space. The playbook works.
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8 }}>
            Sources: <a href="https://www.kfvs12.com/story/28988141/milwaukee-based-company-buys-former-maytag-facility-in-herrin-il/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>KFVS12 — Milwaukee company buys former Maytag</a>, <a href="https://phoenixinvestors.com/articles/attracting-new-business-to-the-old-maytag-factory/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Phoenix Investors — Attracting new business to the old Maytag factory</a>, <a href="https://thesouthern.com/news/data/look-back-herrin-maytag-plant-closing-had-big-impact/collection_9d30eff4-f404-11e4-9ed0-3b77acf63687.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>thesouthern.com — Look back: Herrin Maytag plant closing</a>.
          </div>
        </div>
        <div style={{ background: "oklch(97% 0.04 60)", border: "1px solid oklch(45% 0.18 60)33", borderLeft: "6px solid oklch(45% 0.18 60)", borderRadius: 6, padding: 16 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "oklch(40% 0.18 60)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>Active candidate — Curwood property</div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 6 }}>Former Curwood Plant · Murphysboro · CITY-OWNED</div>
          <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
            Bemis-subsidiary Curwood (food-packaging films, meat casings) operated in Murphysboro for decades; the plant closed in 2004. <strong>The City of Murphysboro now owns the property</strong> (per thesouthern.com archive). Municipal ownership is a meaningful BD lever — the city can structure free or below-market land transfer for a qualified buyer, tie redevelopment to a TIF (Tax Increment Financing) district, pre-zone for the target use, and move faster than a private owner would. Apply the Maytag→Aisin playbook here: identify a Phoenix-Investors-style acquirer + anchor-tenant package. Contact: Mayor&apos;s Office, City of Murphysboro (murphysboro.com).
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8 }}>
            Source: <a href="https://thesouthern.com/news/local/communities/murphysboro/its-official-former-curwood-property-in-murphysboro-has-a-new-owner/article_081c9c28-cde6-5c94-b35e-b152d6a36ae2.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>thesouthern.com — Former Curwood property in Murphysboro has a new owner</a>.
          </div>
        </div>
      </div>

      {/* === SIU's indigenous entrepreneurship infrastructure (paired with the federal-attraction story above) === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Indigenous entrepreneurship infrastructure · SIU Dunn-Richmond + SBDC + Research Park
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Southern Illinois doesn&apos;t only need to attract outside employers — it has an indigenous entrepreneurial pipeline that&apos;s currently capacity-constrained. The federal-relocation + university-research lanes above pair with this in-house lane.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16, marginBottom: 24 }}>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong>SIU Research Park</strong> (1740 Innovation Drive, Carbondale) — non-profit affiliated with SIU; the primary innovation + technology space in the southern third of Illinois.</li>
          <li><strong>Dunn-Richmond Economic Development Center</strong> — 55,000 sq ft mixed-use facility inside the Research Park, built 1990. Houses the region&apos;s LARGEST business incubator. <strong>Currently AT CAPACITY with a growing waitlist for the first time in its 35-year history</strong> — a real signal that regional entrepreneurial demand exceeds incubator supply.</li>
          <li><strong>Illinois SBDC at SIU</strong> — named the <strong>2024 Illinois SBDC of the Year</strong> by the US Small Business Administration. 40 years of operation; no-cost confidential business services (one-on-one consulting, training/workshops, capital-access support, technology adoption, market expansion). The credential the region has but doesn&apos;t cite enough.</li>
          <li><strong>$150K recent grant funding</strong> to Southern Illinois Research Park to support entrepreneurs (per thesouthern.com, 2024).</li>
        </ul>
        <div style={{ fontSize: 12, color: "#5a564d", marginTop: 12, lineHeight: 1.5 }}>
          <strong>BD lever:</strong> when pitching anchor-attraction targets (federal-agency staff relocation, data-center execs, university-anchored programs), the Dunn-Richmond + SBDC stack is the credential that says &quot;this region knows how to start + grow businesses, not just collect WIOA grants.&quot; The Dunn-Richmond capacity constraint is itself a federal-funding ask — expansion to a second incubator building or satellite location would directly relieve a documented bottleneck.
        </div>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8 }}>
          Sources: <a href="https://researchpark.siu.edu/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SIU Research Park</a>, <a href="https://researchpark.siu.edu/our-tenants/dunn-richmond-development-center.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Dunn-Richmond Economic Development Center</a>, <a href="https://news.siu.edu/2024/04/043024-sius-small-business-development-center-named-illinois-sbdc-of-the-year.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SIU SBDC named 2024 Illinois SBDC of the Year</a>, <a href="https://thesouthern.com/news/local/siu/sius-southern-illinois-research-park-receives-150k-to-help-entrepreneurs/article_e286ae2f-c79c-55e3-a9f9-7c2978fe5d07.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>thesouthern.com — SIU Research Park $150K</a>.
        </div>
      </div>

      {/* === Lifestyle pitch additions: Solar eclipse + Giant City Lodge (used by visiting executives) === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Lifestyle pitch · destination-grade assets for visiting execs + relocator open houses
      </h3>
      <div style={{ background: "#fff", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16, marginBottom: 24, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Solar eclipse crossroads</strong> — Carbondale sat on the path of TOTALITY for both the <strong>2017 + 2024 solar eclipses</strong> (the &quot;crossroads of the eclipse&quot; — only US location with two totalities within 7 years). 2017 drew an estimated 100,000+ visitors; 2024 was larger. Operationally significant for BD: the region has proven event-hosting capacity at scale + national-press credentials in the science-tourism lane.</li>
          <li><strong>Giant City Lodge</strong> (Giant City State Park, Jackson Co., ~12 mi south of Carbondale) — historic CCC-built (1930s) IL DNR-operated state-park lodge with stone-and-timber main building, cabin rentals, restaurant, and meeting space. THIS is the destination-grade lodging asset for hosting visiting executives, federal-retiree open houses, climate-migration tours, and anchor-attraction site visits. The I-57/I-64 chain hotels (Hampton + Holiday Inn Express + Drury + Best Western) provide commercial-traveler capacity; Giant City Lodge provides the &quot;authentic outdoor-rec experience&quot; that competes with Asheville NC / Sedona AZ when pitching to relocators.</li>
          <li><strong>Hospitality is NOT a primary jobs anchor</strong> — see the Training-to-Demand section&apos;s &quot;Hotel / hospitality management&quot; row, marked LOCAL · WAGE-SUPPRESSED · SATURATED. Lodging is a quality-of-life amenity for attracting OTHER industries, not a workforce-development end in itself.</li>
        </ul>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8 }}>
          Sources: NASA Eclipse maps (2017 + 2024 total solar eclipse paths); <a href="https://giantcitylodge.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Giant City Lodge</a> + IL DNR Giant City State Park.
        </div>
      </div>

      {/* === Supplementary Sectors parent heading — groups Viticulture, Cannabis, Outdoor Industry === */}
      <h2 style={{ fontSize: 22, fontWeight: 600, color: "#1f1d18", margin: "40px 0 4px 0", paddingTop: 16, borderTop: "2px solid #d8d2c4" }}>
        Supplementary sectors · allowed, real, not primary anchor candidates
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Three sectors deserve allow-and-support treatment without being primary
        jobs anchors: viticulture (Shawnee Hills AVA), cannabis (legal in IL since
        2020), and outdoor recreation tourism (Shawnee NF + Crab Orchard + Cache
        River). Each contributes real economic value but each shares the same
        structural pattern — hospitality-heavy job mix that doesn&apos;t clear the
        1A+2C family-supporting wage bar at entry positions, with scarce top-rung
        positions that pay well but don&apos;t exist in volume. Worth allowing,
        supporting, and amenity-leveraging for relocator recruitment. NOT worth
        building primary training-cohort strategy around. (Outdoor recreation
        industry HQ attraction is covered inside the data-center attraction
        scorecard above.)
      </div>

      {/* === Viticulture / agri-tourism === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "20px 0 8px 0" }}>
        Viticulture &amp; agri-tourism · regional asset, selective opportunity
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The Shawnee Hills American Viticultural Area (AVA, designated December 2006 — the
        FIRST AVA in Illinois) spans Jackson + Union counties along a 40-mile wine trail
        with 12 active wineries (down from 15 at AVA designation). The industry contributes
        an estimated <strong>$126M/year to the regional economy with 150,000 annual visitors</strong> (figure attributed to Carol Hoffman, Southernmost Illinois Tourism Bureau, via Illinois Farm Bureau Partners reporting — IGGVA's commissioned 2019 study showed Illinois wineries supported ~5,700 FTE statewide with ~$1.09B visitor spend, suggesting the Shawnee Hills slice is methodologically reasonable but not source-of-record),
        and Shawnee Hills wineries took <strong>7 of the top 11 awards</strong> at the
        2024 Illinois Wine Competition — quality is real, not just a tourism gimmick. But
        the honest job-economics analysis matters: tourism revenue is real, but most
        winery employment is hospitality (tasting rooms, restaurants, B&amp;Bs) at
        \$14-22/hr — well below the family-supporting wage threshold.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>What viticulture IS doing for the region</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>\$126M/yr economic injection</strong> — real money flowing in from out-of-region visitors</li>
            <li><strong>Amenity for BD pitches</strong> — Carbondale&apos;s lifestyle pitch to relocators (data-center execs, federal-agency staff, remote workers) is genuinely strengthened by a quality wine region 20 min away. Pair with Shawnee NF, Crab Orchard, Giant City.</li>
            <li><strong>Land use that resists strip-mall sprawl</strong> — vineyards preserve rural character + agricultural use that supports the broader ag economy</li>
            <li><strong>Brand differentiation</strong> — Southern IL's first-AVA status is a regional marketing asset; the Shawnee Hills name carries</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>What viticulture is NOT doing (honest framing)</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Not creating family-supporting jobs at scale</strong> — most jobs are tasting-room / hospitality / restaurant at \$14-22/hr. Doesn&apos;t clear the 1A+2C livable-wage bar.</li>
            <li><strong>Wineries themselves are small businesses</strong>, mostly owner-operated. Limited employee headcount per winery (5-25 typical).</li>
            <li><strong>Industry contraction</strong> — count dropped from 15 wineries (2006) to 12 (current). Underlying business pressure is real.</li>
            <li><strong>Tourism is seasonal</strong> — peak Apr-Oct; winter staff retention is hard.</li>
          </ul>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Where the higher-wage opportunities actually are
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
          {[
            { role: "Vineyard manager", wage: "SCARCE — not a realistic entry path", note: "Only ~12-24 total positions across the entire Shawnee Hills AVA region (1-2 per winery × 12 wineries). New entrants displace incumbents only on retirement / expansion. Wage data omitted to avoid implying this is a reliable destination for someone breaking in cold.", training: "If a slot opens: hands-on apprenticeship + viticulture cert (VESTA / Highland CC) + 3-5yr in field" },
            { role: "Winemaker / cellar master", wage: "SCARCE — not a realistic entry path", note: "~12 total positions in the entire AVA (1 per winery). Most aspiring winemakers train locally then RELOCATE to CA / OR / WA for opportunity — that's the typical outcome, not local employment. Wage data omitted.", training: "Enology training (VESTA AAS pathway + UC Davis / Cornell bridge) — primarily for export-of-labor, not local placement" },
            { role: "Value-add processing (bottling / packaging / case-goods)", wage: "$20-30/hr ($40-60k)", note: "The most realistically-accessible higher-wage viticulture-adjacent role IF a multi-winery shared facility gets stood up. Currently does not exist; needs to be built. Real workforce-board project opportunity.", training: "JALC packaging / food-processing program (would need to be created)" },
            { role: "Tasting-room / hospitality / events", wage: "$14-25/hr (typical hospitality wage)", note: "The realistic-entry positions in viticulture. BELOW family-supporting wage for anyone except single adults. Tier-up via sommelier credentials raises wage ceiling but slots stay limited.", training: "Hospitality background + WSET wine credentials for tier-up" },
          ].map((r, i) => (
            <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18" }}>{r.role}</div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "oklch(35% 0.18 142)", marginTop: 2 }}>{r.wage}</div>
              <div style={{ fontSize: 12, color: "#3d3a33", marginTop: 4, lineHeight: 1.5 }}>{r.note}</div>
              <div style={{ fontSize: 11, color: "#5a564d", marginTop: 6 }}><strong>Training:</strong> {r.training}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Strategic moves that could expand viticulture into a more substantive jobs anchor
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Shared value-add processing facility</strong> — pool multiple wineries to build / use a mid-scale bottling, packaging, label-printing, and warehousing facility. Could create 15-40 stable \$40-60k production jobs (vs current pattern where each winery does small-batch bottling separately).</li>
          <li><strong>SIU viticulture &amp; enology research center</strong> — UC Davis &amp; Cornell anchor major wine programs that drive both R&amp;D and a steady winemaker talent pipeline. SIU could bid for a USDA Specialty Crop Block Grant ($1-3M) to seed a small program. Would also attract grad-student research labor + faculty.</li>
          <li><strong>USDA SARE + SCBG grants</strong> — Sustainable Agriculture Research and Education + Specialty Crop Block Grant. Both fund small-vineyard improvements, pest research, climate-adaptation work. Apply through IL Dept of Agriculture.</li>
          <li><strong>Wine industry as recruitment lever, not direct anchor</strong> — when pitching data-center execs, federal-agency relocators, or remote workers, the Shawnee Hills experience is a genuine quality-of-life differentiator. Pair the wine trail with Shawnee NF, Crab Orchard NWR, Giant City SP, and the new Amtrak station for the &quot;outdoor-recreation + wine country + Chicago-by-rail&quot; lifestyle pitch.</li>
          <li><strong>Hospitality-tier training that respects the wage floor</strong> — CNA-equivalent low-wage training for the wine-tourism industry doesn&apos;t clear the MIT Living Wage 1A+2C bar that anchors this page&apos;s family-supporting threshold. Better workforce-board play: tier-up training (sommelier WSET 2/3, restaurant management, winery operations) that has a higher wage ceiling.</li>
        </ul>
      </div>

      <div style={{ marginBottom: 24, fontSize: 12, color: "#7a756b", lineHeight: 1.5 }}>
        Sources: <a href="https://shawneewinetrail.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>shawneewinetrail.com</a>, <a href="https://illinoiswine.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>illinoiswine.com</a> (IL Grape Growers &amp; Vintners Association), <a href="https://en.wikipedia.org/wiki/Shawnee_Hills_AVA" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Shawnee Hills AVA</a>, IL Wine Competition 2024 results, BD-expert advisory. Refresh annually.
      </div>

      {/* === Cannabis / craft grow === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Cannabis industry · how an individual enters the market to earn a living
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Illinois legalized recreational cannabis under the <a href="https://cannabis.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Cannabis Regulation and Tax Act</a> (effective Jan 1, 2020). Carbondale City Council has affirmatively permitted cannabis businesses within city limits (<a href="https://www.explorecarbondale.com/646/Recreational-Cannabis-Information" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>explorecarbondale.com</a>). The IL Department of Agriculture regulates craft growers, cultivation centers, infusers, and transporters; the IL Dept of Financial &amp; Professional Regulation (IDFPR) regulates dispensaries. There are two practical entry paths for an individual seeking to earn a living from this industry: as a worker, or as a license-holding business owner.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Path 1 · Enter as a worker (no license required)</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Entry-level retail (budtender / dispensary associate)</strong> — \$17-22/hr to start; tips supplement. Hiring posted on standard job boards.</li>
            <li><strong>Cultivation technician / trimmer</strong> — production-floor work at craft-grow + cultivation-center facilities. \$16-25/hr.</li>
            <li><strong>Credential ladder</strong> — JALC offers a <a href="https://www.jalc.edu/agriculture-horticulture-aa-degree/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>2-year Agriculture-Horticulture AA Degree (63 credit hours)</a> that directly transfers to cannabis cultivation work + traditional horticulture. The IL Dept of Ag also licenses <a href="https://cannabis.illinois.gov/agencies/cannabis-idoa.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Community College Cannabis Vocational Pilot Programs</a> specifically for cannabis-credential community-college offerings.</li>
            <li><strong>Worker progression — with honest caveat on top-rung scarcity.</strong> Budtender / cultivation tech → Assistant grower (up to ~\$55k) → Cultivation manager (~\$120k) → Master grower (\$80-150k). The wage ceiling at upper-rung positions is genuinely family-supporting BUT those positions are scarce: typically 1-2 master growers + 1-2 cultivation managers per facility. With only a handful of cannabis facilities currently operating in LWA-25, the upper-rung slots are few — and existing workers + outside experienced hires fill most of them. Realistic local pathway tops out for most workers at assistant-grower or below. Frame as &quot;ceiling that exists&quot; not as &quot;reliable destination.&quot;</li>
            <li><strong>Adjacent technical roles</strong> — extraction technician, compliance officer, lab QA, packaging — \$45-80k range. JALC chemistry / biology credits transfer.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Path 2 · Enter as a business owner (license required)</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Craft grower license</strong> — issued by IL Dept of Ag. 5,000-14,000 sq ft canopy. Statewide cap of 150 licenses. Sell wholesale to dispensaries.</li>
            <li><strong>Dispensary license</strong> — IDFPR-issued retail license, allocated via state lottery rounds.</li>
            <li><strong>Infuser license</strong> — for cannabis-infused products (edibles, topicals); lower capital threshold.</li>
            <li><strong>Transporter license</strong> — B2B logistics between licensed facilities.</li>
            <li><strong>Social-Equity Applicant track</strong> — lower fees, technical assistance, and access to the <a href="https://cannabis.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Cannabis Business Development Fund (CBDF)</a> for state-backed loans + grants (federal SBA loans are not available for cannabis because cannabis remains federally Schedule I; cannabis-specific state funding is the only public-capital path). Eligibility is based on residence in a Disproportionately Impacted Area, prior cannabis-conviction history, or family member with same.</li>
            <li><strong>Most-current license-round info</strong> always lives at <a href="https://cannabis.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>cannabis.illinois.gov</a>. Application windows and lotteries operate on cycles; check there for current openings.</li>
          </ul>
        </div>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "#fff", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.16 220)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Why this matters for the workforce board
        </div>
        Cannabis is a real, growing employer in Illinois — the broader hemp-derived cannabinoid industry employs ~13,500 workers statewide and pays ~\$545M annually in wages (<a href="https://themarijuanaherald.com/2025/12/illinois-hemp-industry-supports-nearly-13500-jobs-and-2-7-billion-in-revenue-analysis-finds/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Marijuana Herald, Dec 2025</a>). The local share is small but real. The credential ladder from JALC Horticulture AA → cultivation work → grower management is one of the few <em>2-year-degree</em> paths that ends in a family-supporting wage. The action items: (1) confirm whether JALC could add cannabis-specific elective modules under the IL Community College Cannabis Vocational Pilot framework, (2) when a new local facility is approved (e.g., the 2023 SuiteGreens LLC craft-grow in Carbondale, per <a href="https://thesouthern.com/news/local/company-hopes-to-bring-cannabis-craft-grow-facility-dispensary-to-carbondale/article_7e4b5fd2-3c60-526e-8c62-5a42ca995135.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Southern Illinoisan</a>), the workforce board coordinates pre-hire training pipelines.
      </div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Wage analysis — most positions are NOT family-supporting; some are
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
          {[
            { role: "Budtender / dispensary associate", wage: "$17-22/hr (~$31-40k/yr)", note: "Most numerous position; doesn't clear single-adult living wage. Tips supplement.", verdict: "BELOW LIVABLE" },
            { role: "Cultivation technician / trimmer", wage: "$16-25/hr (~$33-52k/yr)", note: "Production floor work. Borderline single-adult; below family.", verdict: "BELOW LIVABLE → SINGLE ADULT" },
            { role: "Assistant grower", wage: "Up to $55k/yr", note: "1-2yr experience; some autonomy.", verdict: "SINGLE ADULT ONLY" },
            { role: "Cultivation manager", wage: "SCARCE — not a realistic entry path", note: "Only 1-2 per facility × handful of LWA-25 facilities = ~5-10 slots region-wide. Filled by existing workers + outside experienced hires. Wage data omitted to avoid implying this is a reliable destination.", verdict: "EXTREME SATURATION" },
            { role: "Master grower", wage: "SCARCE — not a realistic entry path", note: "1-2 per facility × handful of facilities = ~5-10 slots region-wide. 5-10yr experience required + positions are not local-promotion-from-budtender in practice. Wage data omitted.", verdict: "EXTREME SATURATION" },
            { role: "Compliance / extraction tech", wage: "$45-80k/yr", note: "Realistically more accessible than top-rung grower positions, but still limited slots (1-3 per facility). Technical credential roles.", verdict: "SINGLE → FAMILY · MED-HIGH saturation" },
          ].map((r, i) => (
            <div key={i} style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18" }}>{r.role}</div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "oklch(35% 0.18 142)", marginTop: 2 }}>{r.wage}</div>
              <div style={{ fontSize: 12, color: "#3d3a33", marginTop: 4, lineHeight: 1.5 }}>{r.note}</div>
              <div style={{ fontSize: 11, color: "#5a564d", marginTop: 6 }}><strong>Verdict:</strong> {r.verdict}</div>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b" }}>
          Wage sources: <a href="https://www.indeed.com/career/marijuana-budtender/salaries/IL" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Indeed</a>, <a href="https://www.ziprecruiter.com/Jobs/Cannabis/--in-Illinois" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>ZipRecruiter</a>, <a href="https://www.highbluffgroup.com/cannabis-industry-salary-guides-for-2024/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>High Bluff Group 2024 Cannabis Salary Guide</a>, <a href="https://cannabizteam.com/wp-content/uploads/2024/03/2024-CannabizTeam-Salary-Guide_1.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>CannabizTeam 2024</a>.
        </div>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Strategic moves that could capture real value from the cannabis economy
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>What the dashboard already shows:</strong> per-role wage analysis above with verdict against MIT LWC Jackson Co. 1A+2C $46.76/hr, sourced from BLS-adjacent industry salary tables; the credential ladder (JALC Horticulture AA → cultivation → grower management) cross-referenced against local-facility scarcity (~5-10 top-rung slots region-wide); honest size-up (~13,500 IL hemp-cannabinoid jobs statewide, LWA-25 share small).
        </div>
        <div style={{ marginBottom: 4 }}>
          <strong>Your residual moves (the dashboard cannot self-execute these):</strong>
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Apply for a Community-College Cannabis Vocational Pilot Program license</strong> — IL Dept of Ag licenses these (<a href="https://cannabis.illinois.gov/agencies/cannabis-idoa.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>cannabis.illinois.gov</a>). JALC or Rend Lake applies; the dashboard surfaced the credential gap and the wage-ceiling reality — the application is human work.</li>
          <li><strong>Sponsor local social-equity applicants through the next IL Cannabis Business Development Fund (CBDF) license round</strong> — the dashboard names the eligibility criteria + the fund; the application support + capital-access introduction is human work. <a href="https://illinoisanswers.org/2023/10/19/illinois-cannabis-business-development-fund-craft-growers/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois Answers Project on CBDF barriers</a> documents the typical obstacles.</li>
          <li><strong>Negotiate local-hiring + livable-wage zoning conditions when the next cannabis facility seeks approval</strong> in Carbondale or Marion. Use the next SuiteGreens-style approval as precedent. The dashboard surfaces the WAGE-vs-MIT-LWC gap that justifies the condition; the negotiation itself happens in the council chamber.</li>
          <li><strong>Steer cohort planning toward adjacent industries</strong> — cannabis processing equipment, packaging, lab testing, security, compliance consulting carry higher-wage ceilings than retail/cultivation. The training-to-demand section above already maps these; cohort enrollment decisions are the human residual.</li>
          <li><strong>Frame cannabis honestly in regional pitches</strong> — supplementary economic activity, not a primary jobs anchor. The dashboard provides the numbers (LWA-25 share is small; ~13,500 IL hemp-cannabinoid jobs statewide per <a href="https://themarijuanaherald.com/2025/12/illinois-hemp-industry-supports-nearly-13500-jobs-and-2-7-billion-in-revenue-analysis-finds/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Marijuana Herald Dec 2025</a>); how you frame this for a chamber-of-commerce audience is human work.</li>
        </ul>
      </div>

      <div style={{ marginBottom: 24, fontSize: 12, color: "#7a756b", lineHeight: 1.5 }}>
        All licensing process &amp; wage figures are public record from state agencies and the named industry-salary sources above. Verify current local license status + open application windows at <a href="https://cannabis.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>cannabis.illinois.gov</a> before acting on any specific claim.
      </div>

      {/* === Outside-the-box people-attraction strategies === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Outside-the-box people-attraction strategies · creative pathways to a living-wage population
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Recruiting new anchor employers is one strategy. <strong>Recruiting new
        residents directly — people who already earn living wages, or will earn them
        once they arrive — is a complementary strategy</strong> with documented ROI
        in peer regions. Each option below carries a named precedent + sources.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
        {[
          {
            name: "Remote-worker relocation incentive — 'Choose Carbondale' / 'Move to Shawnee'",
            fit: "STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "Pay remote workers a cash incentive (typically $10k) to relocate, with a 12-month residency requirement. They bring their out-of-state salary into the local economy.",
            why_here: "Tulsa Remote documented impact (2025 EIG evaluation): 3,972 Remoters at close of 2025 (page rounds to 4,000+), $878M cumulative direct employment income, 80% 2-year retention (2025 survey — higher than the 70% earlier estimate). EIG headline benefit-cost ratio is $13.77 in new local earnings per $1 invested for the initial 2021 cohort; the broader whole-program metric is the often-cited 4:1. Cost-per-job ~$36k vs $218k typical business incentive (6× more efficient). LWA-25's amenity profile (Shawnee NF, wine trail, Amtrak via the new station, cheap housing, SIU community) is competitive with Tulsa / Topeka / Bentonville.",
            action: "Stand up 'Choose Carbondale' or regional equivalent. $5K-10K relocation grant + curated welcome program. Funding: hotel-tax allocation + EDA seed grant + IL DCEO match. Target: 30-50 relocators/year initial.",
            sources: [
              { url: "https://www.brookings.edu/articles/work-from-anywhere-as-a-public-policy-three-findings-from-the-tulsa-remote-program/", label: "Brookings — Tulsa Remote findings" },
              { url: "https://www.upjohn.org/research-highlights/each-dollar-spent-drawing-remote-workers-tulsa-delivers-4-benefit-current-residents", label: "Upjohn Institute — 4:1 benefit-cost ratio" },
              { url: "https://www.tulsaremote.com/", label: "Tulsa Remote program" },
            ],
          },
          {
            name: "University graduate retention — 'Stay Carbondale' for SIU grads",
            fit: "STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "Match SIU graduates with regional employers + first-year housing assistance + employer-funded student-loan-payment match. Address rural brain drain at the source.",
            why_here: "SIU graduates ~3,000+ students/year. Per the Demographics section, Carbondale's population dropped 15.6% in 5 years driven largely by SIU enrollment + graduate-retention failure. Retaining even 10% of annual graduates at family-supporting wages materially offsets the population trend.",
            action: "Partnership between SIU Career Services + the workforce board + Carbondale + Marion Chambers. Build employer-graduate matching platform + offer relocation-style $5K stipend conditional on 2-year regional commitment. Apply for EDA Recompete grant.",
            sources: [
              { url: "https://www.eda.gov/funding/programs/recompete", label: "EDA Recompete Pilot (rural workforce program)" },
              { url: "https://siu.edu/", label: "Southern Illinois University Carbondale" },
            ],
          },
          {
            name: "Federal retiree / military veteran (especially disabled veteran) relocation pitch",
            fit: "STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "Target federal civilian retirees + veteran retirees + ESPECIALLY 70%+ service-connected disabled veterans seeking low cost-of-living retirement with healthcare access. They bring pension income (typically $40-100k+) and Medicare / VA healthcare demand that supports the regional health-sector workforce. Illinois' combined disabled-veteran + retiree tax stack is one of the strongest in the US — verified specifics below.",
            why_here: "Marion VA Medical Center is the existing healthcare anchor. SIH + Memorial Carbondale add capacity. LWA-25 cost-of-living is far below federal-retiree concentration cities. Veteran population already loves the region (per the Federal Money Concentration section — VA-driven economic flows dominate). \n\nIL STATE TAX STACK FOR THIS COHORT (verified IL Dept of Revenue Pub-102 + Pub-120 + 35 ILCS 200/15-169): \n\n(a) PROPERTY TAX — Standard Homestead Exemption for Veterans with Disabilities (SHEVD), 35 ILCS 200/15-169: 30-49% SC disability = $2,500 EAV exemption; 50-69% SC disability = $5,000 EAV exemption; 70%+ SC disability = exemption on the first $250,000 of EAV (Equalized Assessed Value), which translates to roughly $750,000 market value (IL EAV ≈ ⅓ of market value). Homes above that cap pay tax only on the portion above the $750k market threshold. Statute was amended in 2023 to add the $250k EAV ceiling — pre-2023 was unlimited. Unmarried surviving spouse qualifies if vet held exemption pre-death OR if service member KIA. Annual filing required at the county assessor (PTAX-342). \n\n(b) IL INCOME TAX — Illinois does not tax military active pay. It does not tax military retirement, including disability pay. It does not tax federal civilian pensions (FERS or CSRS), state pensions, Social Security, IRA withdrawals, 401(k) withdrawals, or railroad retirement. The IL income tax rate is a flat 4.95%. For federal and military retirees, almost no retirement income is taxed. File IL-1040 with the Line-5 subtraction. \n\n(c) COMBINED MATH for a 70%+ SC disabled vet in LWA-25: $0 property tax + $0 IL income tax on disability/military-retired/federal-pension/Social-Security/IRA-401k + on-site VA healthcare at Marion VAMC + low housing cost in the Marion-Herrin-Carterville newer-construction corridor (Williamson Co. growth area) = a stack few US regions can match.",
            action: "Targeted marketing through Federal News Network, Military Times, VFW + American Legion networks, DAV chapters, Vet Tix, MOAA. Carbondale + Marion Chambers partner with Marion VA to host quarterly retirement-relocation open houses (at Giant City Lodge — the destination-grade venue, not the I-57 chain hotels). Each open house leads with the IL combined tax-stack math + property-tax SHEVD calculator for the prospect's specific disability rating.",
            sources: [
              { url: "https://www.marion.va.gov/", label: "Marion VA Medical Center" },
              { url: "https://tax.illinois.gov/localgovernments/property/disabledveteraninfo.html", label: "IL Dept of Revenue — Property Tax Relief for Veterans with Disabilities" },
              { url: "https://www.ilga.gov/legislation/ilcs/fulltext.asp?DocName=003502000K15-169", label: "35 ILCS 200/15-169 (SHEVD statute)" },
              { url: "https://tax.illinois.gov/research/publications/pubs/illinois-filing-requirements-for-military-personnel.html", label: "IL DoR Pub-102 (Military filing requirements)" },
              { url: "https://tax.illinois.gov/content/dam/soi/en/web/tax/research/publications/pubs/documents/pub-120.pdf", label: "IL DoR Pub-120 (Retirement Income)" },
              { url: "https://tax.illinois.gov/questionsandanswers/answer.99.html", label: "IL DoR Q&A — does Illinois tax pension / SS / retirement income" },
              { url: "https://www.opm.gov/policy-data-oversight/data-analysis-documentation/federal-employment-reports/", label: "OPM federal workforce statistics" },
            ],
          },
          {
            name: "Mid-career career-change relocation — coding bootcamp / trades retraining + lifestyle pitch",
            fit: "MODERATE-STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "35-50yo professionals leaving expensive metros seeking lower-COL location + career pivot. They self-fund a credential (coding bootcamp, IBEW pre-apprenticeship, RN program at JALC) while consuming local services and bringing remaining savings into the local economy.",
            why_here: "JALC offers the credential infrastructure (Agriculture-Horticulture AA, RN ADN, electrical, welding programs). IBEW Local 702 takes pre-apprentices. Living-cost gap vs SF/NYC/Seattle covers 12-24 months of credential training with no income.",
            action: "Marketing partnership between JALC + the workforce board + Chamber: 'Reset your career in Carbondale.' Target 30-50 enrollees/year. Bundle with the remote-worker incentive when graduates take remote jobs post-credential.",
            sources: [
              { url: "https://www.jalc.edu/", label: "John A. Logan College programs" },
              { url: "https://ibew702.org/", label: "IBEW Local 702 (West Frankfort)" },
            ],
          },
          {
            name: "Climate-migration positioning — Mississippi River valley as water-rich refuge",
            fit: "MODERATE FIT",
            fit_color: "oklch(48% 0.15 60)",
            what: "Position LWA-25 as climate-stable: ample fresh water (Mississippi River + Kinkaid + Crab Orchard), no hurricane risk, lower wildfire risk than the West, lower flood risk than coastal regions, lower extreme-heat risk than Southwest.",
            why_here: "Academic literature documents climate migration to the Upper Midwest as a real and accelerating phenomenon. LWA-25 is south of the typical 'Great Lakes climate haven' framing but shares the water-rich + disaster-resistant profile, with materially lower COL than Buffalo or Duluth (the named climate-haven cities).",
            action: "Marketing campaign positioning the region for SW drought refugees + FL/coastal flood refugees. Track climate-driven home-insurance unavailability in source regions (the active leading indicator).",
            sources: [
              { url: "https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2022EF002942", label: "AGU 2022 — Climate Migration to Great Lakes Cities" },
              { url: "https://www.planetizen.com/features/135561-great-lakes-cities-are-touted-climate-refuge-reality-much-more-complex", label: "Planetizen — climate refuge realities" },
              { url: "https://www.crainsdetroit.com/crains-forum/climate-change-extreme-weather-spur-migration-great-lakes", label: "Crain's Detroit — climate migration data" },
            ],
          },
          {
            name: "Outdoor recreation industry HQ + tourism magnet attraction",
            fit: "MODERATE FIT",
            fit_color: "oklch(48% 0.15 60)",
            what: "Attract outdoor-industry companies + adventure-tourism operators to base regional HQs near Shawnee NF. Industries: outdoor gear retail, guide services, outdoor education, eco-lodge operators.",
            why_here: "Shawnee NF is the ONLY national forest in IL — 280k acres. Climbing at Jackson Falls + Cedar Falls; MTB at Rim Rock + Lake Glendale; paddling on Cache River + Mississippi backwaters; backpacking the River-to-River Trail. BEA Outdoor Recreation Satellite Account shows outdoor rec contributes ~$1.1T to US GDP annually; the industry hasn't placed an HQ in Illinois.",
            action: "Partnership with Shawnee NF Forest Service + IL Office of Tourism. Pitch outdoor gear brands + regional outfitters + adventure-education orgs (Outward Bound, NOLS).",
            sources: [
              { url: "https://www.fs.usda.gov/main/shawnee/home", label: "Shawnee National Forest" },
              { url: "https://www.bea.gov/data/special-topics/outdoor-recreation", label: "BEA Outdoor Recreation Satellite Account" },
            ],
          },
          {
            name: "Worker-owned cooperative seeding — capture more value locally",
            fit: "LONG SHOT BUT INTERESTING",
            fit_color: "oklch(48% 0.15 60)",
            what: "Seed worker-owned cooperative businesses in sectors with stable local demand (childcare, eldercare, food production, construction). Cooperative ownership means workers capture more of the business surplus → higher individual income than the same role at a traditional employer.",
            why_here: "Evergreen Cooperatives Cleveland is the US showcase (10+ co-ops, 250+ worker-owners). Sectors with cooperative-friendly fit in LWA-25: childcare (chronic shortage), home healthcare (aging population), specialty food production (wine, dairy, produce), retrofit construction (federal weatherization money flowing).",
            action: "Partner with Cooperative Development Foundation + Democracy at Work Institute. Pilot one cooperative in childcare or home healthcare. Apply for USDA Rural Cooperative Development Grant.",
            sources: [
              { url: "https://institute.coop/", label: "Democracy at Work Institute" },
              { url: "https://www.evgoh.com/", label: "Evergreen Cooperatives — Cleveland" },
              { url: "https://www.rd.usda.gov/programs-services/business-programs/rural-cooperative-development-grant-program", label: "USDA Rural Cooperative Development Grant" },
            ],
          },
          {
            name: "Returning-expat / native-return program — 'Come home to Southern Illinois'",
            fit: "STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "Target SIU alumni + Southern Illinois natives who left for college/work in expensive metros. Mid-career relocators with established earning power return for lower COL + family proximity + lifestyle. Brings outside income into the local economy without competing with existing residents for jobs.",
            why_here: "SIU has ~95k alumni network. Southern Illinois natives who left for college/work face the same SF/NYC/Seattle cost-burden as everyone else; midcareer they're prime relocation targets. Layers cleanly with remote-worker incentive (#1) — native returners are remote-worker incentive's best-fit candidates.",
            action: "Build alumni-targeted campaign via SIU Alumni Association + LinkedIn export. Estimated cost ~\$15k for the database work + targeted outreach. Pair with the 'Choose Carbondale' $5-10k relocation grant. West Virginia's Ascend WV program (\$12k incentive with native-return preference) and Maine's 'Live &amp; Work in Maine' are the closest precedents.",
            sources: [
              { url: "https://ascendwv.com/", label: "Ascend WV — Remote-worker incentive program" },
              { url: "https://liveandworkinmaine.com/", label: "Live &amp; Work in Maine" },
              { url: "https://alumni.siu.edu/", label: "SIU Alumni Association" },
            ],
          },
        ].map((s, i) => (
          <div key={i} style={{
            background: "white",
            border: `1px solid ${s.fit_color}33`,
            borderLeft: `6px solid ${s.fit_color}`,
            borderRadius: 6, padding: 16,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 8 }}>
              <div style={{ fontSize: 16, fontWeight: 600, color: "#1f1d18", flex: 1 }}>{s.name}</div>
              <div style={{
                fontSize: 11, fontWeight: 700, color: "white", background: s.fit_color,
                padding: "5px 10px", borderRadius: 3, textTransform: "uppercase", letterSpacing: "0.06em",
                whiteSpace: "nowrap",
              }}>{s.fit}</div>
            </div>
            <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 6 }}><strong>What it is:</strong> {s.what}</div>
            <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 6 }}><strong>Why it fits LWA-25:</strong> {s.why_here}</div>
            <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 8 }}><strong>Action items:</strong> {s.action}</div>
            <div style={{ fontSize: 11, color: "#5a564d" }}>
              <strong>Sources:</strong>{" "}
              {s.sources.map((src, j) => (
                <span key={j}>
                  {j > 0 && " · "}
                  <a href={src.url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>{src.label}</a>
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 16, marginBottom: 24, padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <strong>Strategic sequencing:</strong> remote-worker incentive + graduate
        retention are highest ROI, fastest to deploy, lowest political risk —
        start there with EDA Recompete seed funding. Federal-retiree pitch is
        relationship-driven and 18-36 months. Climate-migration positioning is
        essentially marketing — low cost, optional upside. Outdoor industry HQ
        is a multi-year courtship. Cooperative seeding is the longest-cycle but
        has the strongest local-value-capture once it works. None of these
        substitute for the anchor employer recruitment in the scorecard above —
        they complement it.
      </div>

      {/* Delta Regional Authority — federal regional commission covering LWA-25 */}
      <div style={{ marginTop: 20, padding: 16, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Delta Regional Authority — federal regional commission covering 4 of 5 LWA-25 counties
        </div>
        <div style={{ marginBottom: 10 }}>
          The Delta Regional Authority (<a href="https://dra.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>dra.gov</a>) is a federal-state partnership covering the eight-state Mississippi River Delta region. <strong>Franklin, Jackson, Perry, and Williamson counties are DRA-eligible</strong> (Jefferson County is NOT in the DRA territory — verify county-by-county on the <a href="https://dra.gov/states/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>DRA states page</a>). Note: Illinois is NOT in ARC (Appalachian Regional Commission), so don&apos;t pursue ARC POWER — DRA is the analogue.
        </div>
        <div style={{ marginBottom: 6 }}><strong>Active DRA programs to stack:</strong></div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0 }}>
          <li><strong>SEDAP (States&apos; Economic Development Assistance Program)</strong> — workforce + infrastructure + small-business. Annual NOFA; typically $1-2M per state allocation cycle.</li>
          <li><strong>Delta Workforce</strong> — workforce-training capacity for DRA-eligible communities.</li>
          <li><strong>Delta Doctors / J-1 visa waiver program</strong> — recruits foreign-trained physicians to underserved DRA counties. Direct lever for Marion VA + SIH + Memorial primary-care shortage.</li>
          <li><strong>Healthy Delta Communities</strong> — community-health investment.</li>
          <li><strong>Delta Workforce Innovation</strong> — competitive grants for regional training partnerships.</li>
        </ul>
        <div style={{ fontSize: 12, color: "#5a564d", marginTop: 6 }}>
          DRA money is materially under-applied-for by IL applicants — the political and grant-writing weight historically goes to MS/AR/LA counties. the workforce board partnering with DRA staff (delta.gov contact directory) to coordinate an annual IL-counties SEDAP cohort is the play.
        </div>
      </div>

      {/* === Federal infrastructure + reshoring + climate adaptation + foundation capital === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Federal infrastructure + reshoring + foundation capital · additional federal &amp; philanthropic levers
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Beyond the data-center / federal-agency / university-research plays, three more
        federal funding streams + one philanthropic stream are under-leveraged in LWA-25:
        CHIPS Act + IRA Energy-Communities reshoring; climate-adaptation infrastructure
        (Mississippi River + Cache River + flood resilience); and place-based foundation
        capital. Each creates either family-supporting union-construction jobs or
        federal-grant capacity that doesn&apos;t require federal-program eligibility tests.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 4 }}>CHIPS Act + IRA Energy Communities manufacturing reshoring</div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}>
            <strong>What it is:</strong> CHIPS &amp; Science Act ($52B for US semiconductor manufacturing) + IRA §45X Advanced Manufacturing Production Tax Credit
            + IRA §48 ITC bonus adders for Energy Communities (10pp on top of base 30%).
          </div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}>
            <strong>Why LWA-25 fits:</strong> Franklin and Perry counties are designated
            IRA Energy Communities tracts (coal-closure status). That's an automatic
            10pp ITC bonus on top of the base credit for any solar / wind / storage /
            advanced-manufacturing project sited there. Stranded Baldwin coal-plant
            interconnect adds the grid-capacity angle. Realistic targets: semiconductor
            packaging (Wolfspeed Marcy NY precedent — $1.5B CHIPS-supported expansion);
            polysilicon (Hemlock Semiconductor Saginaw MI — $375M CHIPS award); battery
            cell / module assembly; EV charging-infrastructure components.
          </div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 6 }}>
            <strong>Action:</strong> File site nominations with US Commerce CHIPS Program
            Office for advanced-packaging + ATP (Advanced Technology Packaging) consortia.
            Apply for DOE Industrial Demonstrations Program funding on adjacent clean-energy
            manufacturing. SIU's existing critical-minerals seed grant is a credibility
            anchor.
          </div>
          <div style={{ fontSize: 11, color: "#5a564d" }}>
            <strong>Sources:</strong>{" "}
            <a href="https://www.commerce.gov/issues/chips-and-science-act" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>US Commerce CHIPS Program</a> · {" "}
            <a href="https://www.energy.gov/manufacturing-energy-supply-chains/articles/inflation-reduction-act-energy-community-tax-credit-bonus" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>DOE IRA Energy Community Tax Credit Bonus</a> · {" "}
            <a href="https://www.irs.gov/credits-deductions/businesses/section-45x-advanced-manufacturing-production-credit" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IRS §45X Advanced Manufacturing PTC</a>
          </div>
        </div>

        <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 4 }}>Climate-adaptation infrastructure · USACE + FEMA + EPA flood-resilience work</div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}>
            <strong>What it is:</strong> Federal climate-adaptation appropriations are at
            record levels post-IIJA. USACE St. Louis District is responsible for the
            Mississippi River reach along LWA-25's western boundary. FEMA BRIC (Building
            Resilient Infrastructure and Communities) funds pre-disaster mitigation. EPA
            Section 319 nonpoint-source funds fund watershed-scale work on Big Muddy +
            Cache River.
          </div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}>
            <strong>Why LWA-25 fits:</strong> Mississippi River runs along Jackson + Union
            counties' west edge. Big Muddy + Cache River are major tributaries with
            documented flood + sediment + habitat issues. Federal climate work in this
            corridor creates union-construction jobs (IBEW + LIUNA + IUOE) at scale and
            multi-decade duration. Louisiana&apos;s Coastal Master Plan precedent: $50B+
            over 50 years funding sustained construction-trades employment.
          </div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 6 }}>
            <strong>Action:</strong> Position the city/county as co-applicants on
            USACE Section 219 (Environmental Infrastructure) projects + FEMA BRIC
            grants. Partner with The Nature Conservancy IL on Mississippi River
            initiatives. State leadership through IL Office of Resource Conservation.
          </div>
          <div style={{ fontSize: 11, color: "#5a564d" }}>
            <strong>Sources:</strong>{" "}
            <a href="https://www.fema.gov/grants/mitigation/building-resilient-infrastructure-communities" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>FEMA BRIC</a> · {" "}
            <a href="https://www.mvs.usace.army.mil/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USACE St. Louis District</a> · {" "}
            <a href="https://www.epa.gov/nps/319-program-grants" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>EPA §319 Nonpoint Source grants</a>
          </div>
        </div>

        <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 4 }}>Foundation / philanthropic capital · the non-federal funding lane</div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}>
            <strong>What it is:</strong> Major US foundations directly fund regional
            economic-development planning, capacity-building, and pilot programs.
            Foundation capital doesn&apos;t require federal-program eligibility tests, has
            longer time horizons, and is more flexible than government grants.
          </div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 5 }}>
            <strong>Why LWA-25 fits:</strong> Walton Family Foundation invests ~$30M/yr in
            whole-of-river Mississippi work — LWA-25 sits on the river. RWJF Culture of
            Health Prizes recognize rural communities. Kresge Strong Cities (community
            development capital + TA). Knight Foundation has rural pilots. Ford Foundation
            BUILD program provides general-operating support to community-anchor orgs.
            None of these require a federal-eligibility match.
          </div>
          <div style={{ fontSize: 12, color: "#3d3a33", marginBottom: 6 }}>
            <strong>Action:</strong> the workforce-development organizations + Carbondale Chamber partner with Carbondale Chamber + SIU
            Foundation to develop a regional-strategy planning grant proposal — Walton
            Mississippi work is the most geographically aligned. Targets: $200k-2M planning
            grants leading to multi-year program funding.
          </div>
          <div style={{ fontSize: 11, color: "#5a564d" }}>
            <strong>Sources:</strong>{" "}
            <a href="https://www.waltonfamilyfoundation.org/our-work/environment/mississippi-river" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Walton Family Foundation — Mississippi River</a> · {" "}
            <a href="https://www.rwjf.org/en/grants/funding-opportunities.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>RWJF funding opportunities</a> · {" "}
            <a href="https://kresge.org/our-work/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Kresge Foundation</a> · {" "}
            <a href="https://knightfoundation.org/communities/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Knight Foundation Communities</a> · {" "}
            <a href="https://www.fordfoundation.org/work/our-grants/build/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Ford Foundation BUILD</a>
          </div>
        </div>
      </div>

      {/* IL programs to file under — converted to scannable table per UX audit */}
      <div style={{ marginTop: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 12, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Stack these IL state programs in any pitch
        </div>
        <div style={{ background: "white", border: "1px solid #f0d98a", borderRadius: 4, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 600 }}>
            <thead>
              <tr style={{ background: "rgba(240,217,138,0.4)", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d" }}>
                <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Program</th>
                <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>What it provides</th>
                <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>How to apply</th>
              </tr>
            </thead>
            <tbody>
              {[
                { p: "EDGE Tax Credit",                        v: "Income-tax credit against new jobs created", h: "IL DCEO (dceo.illinois.gov/expandrelocate/incentives.html)" },
                { p: "REV Illinois",                           v: "EV / clean-energy capital-investment + income-tax credit", h: "IL DCEO Office of Business Development" },
                { p: "High Impact Business designation",       v: "Sales-tax exemption on building materials + machinery", h: "IL DCEO; confirm sector + minimum-investment thresholds" },
                { p: "Enterprise Zone designation",            v: "Local property-tax abatement + sales-tax exemption", h: "Confirm current LWA-25 EZ status with IL DCEO" },
                { p: "IL Data Center Investments Act",         v: "20-year sales-tax exemption + property-tax abatement", h: "IL DCEO; $250M minimum capex / 20 FTE at 120% county median wage / carbon-neutral cert (see scorecard)" },
                { p: "SBA HUBZone",                            v: "Federal-contracting set-aside preference", h: "SBA HUBZone certification (sba.gov/federal-contracting); most LWA-25 census tracts qualify" },
                { p: "CDFI Capital Magnet Fund",               v: "Affordable-housing development capital", h: "Local CDFI partnerships; competitive annual NOFA" },
                { p: "New Markets Tax Credits",                v: "39% federal tax credit for investment in low-income census tracts", h: "Carbondale + Murphysboro NMTC-eligible; partner with a CDE allocatee" },
                { p: "Delta Regional Authority SEDAP",         v: "Workforce + infrastructure + small-business grants", h: "DRA annual NOFA; 4 of 5 LWA-25 counties eligible (Jefferson NOT)" },
                { p: "DRA Delta Doctors (J-1 waiver)",         v: "Foreign-trained physician waiver for 3yr HPSA service", h: "DRA + IL Secretary of State + Marion VA / SIH / Memorial" },
                { p: "IRA §48 Energy Communities ITC bonus",   v: "+10pp investment tax credit on solar / wind / storage / advanced mfg", h: "Automatic for projects sited in coal-closure tracts (Franklin + Perry)" },
                { p: "IRA §45X Advanced Mfg PTC",              v: "Per-unit production tax credit for clean-energy components", h: "IRS — applies at component-mfg level for solar / wind / battery / EV" },
                { p: "USDA Rural Housing Service",             v: "Sections 502/504/515 single-family + multifamily rural housing", h: "USDA Rural Development (rd.usda.gov); LWA-25 mostly rural-eligible" },
                { p: "IHDA LIHTC + loans",                     v: "Low-Income Housing Tax Credit allocations + low-interest loans", h: "IHDA annual NOFA (ihda.org)" },
                { p: "Smart Start IL Workforce Grants",        v: "$90M/yr childcare-staff wage floor support", h: "IL DHS + Gateways to Opportunity (ilgateways.com/smart-start)" },
                { p: "IL CCAP",                                v: "Childcare subsidy for working-parent households", h: "IL DHS (dhs.state.il.us); eligibility cliff at ~200% FPL family of 3" },
                { p: "NHSC Loan Repayment (LRP)",              v: "$50-75k over 2yr for primary-care MDs/NPs/PAs/CNMs in HPSAs", h: "HRSA NHSC (nhsc.hrsa.gov); 2-yr commitment minimum" },
                { p: "NHSC Nurse Corps LRP",                   v: "Up to 85% of outstanding RN/APRN loans over 3yr at Critical Shortage Facility", h: "HRSA BHW (bhw.hrsa.gov/funding/apply-loan-repayment/nurse-corps)" },
                { p: "FEMA BRIC",                              v: "Pre-disaster flood + climate resilience infrastructure", h: "FEMA annual NOFA; partner with USACE St. Louis District" },
                { p: "EDA Recompete Pilot",                    v: "Rural workforce capacity + planning grants", h: "EDA (eda.gov); LWA-25 likely qualifies on persistent-distress thresholds" },
              ].map((r, i) => (
                <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #f0d98a" }}>
                  <td style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", verticalAlign: "top" }}>{r.p}</td>
                  <td style={{ padding: "8px 10px", color: "#3d3a33", verticalAlign: "top" }}>{r.v}</td>
                  <td style={{ padding: "8px 10px", color: "#5a564d", verticalAlign: "top" }}>{r.h}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div style={{ marginTop: 12, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Source: synthesized from local-BD expert advisory + IL DCEO program documentation. Refresh annually.
      </div>
    </section>
  );
}

function StructuralWorkforceConstraintsSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Structural workforce constraints · crime + drug-class reality + framing
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 780, lineHeight: 1.55 }}>
        Workforce-development planning that ignores the actual security + substance-use reality of the region will mis-design programs. These constraints are not the workforce board&apos;s fault and not its to solve alone — but ignoring them produces brochures that read true on paper while trainees wash out in practice. Sourced data, plain framing.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 24 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>LWA-25 crime · 9-city safety ranking (FBI UCR 2024)</div>
          <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 10, lineHeight: 1.5 }}>
            The relocator BD pitch + workforce-board safety planning both need city-level granularity, not regional aggregate. The 9 sizable LWA-25 cities span a 50× crime-rate range — Benton (1 per 1,000) is among the safest in America; Carbondale (50 per 1,000) is among the highest.
          </div>
          <div style={{ overflowX: "auto", marginBottom: 8 }}>
            <table style={{ width: "100%", fontSize: 11.5, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: "#ebe5d6", textAlign: "left" }}>
                  <th style={{ padding: "5px 6px", borderBottom: "1px solid #d8d2c4" }}>#</th>
                  <th style={{ padding: "5px 6px", borderBottom: "1px solid #d8d2c4" }}>City · County</th>
                  <th style={{ padding: "5px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>Crime / 1,000</th>
                  <th style={{ padding: "5px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>Violent (1 in N)</th>
                  <th style={{ padding: "5px 6px", borderBottom: "1px solid #d8d2c4", textAlign: "right" }}>Property (1 in N)</th>
                </tr>
              </thead>
              <tbody>
                {[
                  {rank:"🟢 1", city:"Benton · Franklin", rate:"1", violent:"very low", property:"1 in 940", tone:"safe", note:"Elite-safest in America"},
                  {rank:"🟢 2", city:"Du Quoin · Perry", rate:"5", violent:"1 in 802", property:"1 in 255", tone:"safe", note:"Safer than 62% of IL"},
                  {rank:"🟢 3", city:"Carterville · Williamson", rate:"10", violent:"—", property:"property 8/1k", tone:"safe", note:"Newer-construction corridor"},
                  {rank:"🟡 4", city:"Mt. Vernon · Jefferson", rate:"13", violent:"1 in 405", property:"1 in 92", tone:"moderate", note:"Continental Tire town"},
                  {rank:"🟠 5", city:"Herrin · Williamson", rate:"29", violent:"1 in 261", property:"1 in 40", tone:"high", note:"Williamson Co. growth corridor"},
                  {rank:"🔴 6", city:"West Frankfort · Franklin", rate:"31", violent:"1 in 3,573 (very low)", property:"1 in 33 · MV theft 1 in 159", tone:"high", note:"IBEW 702 HQ; nearly-zero violent + high MV theft / property"},
                  {rank:"🔴 7", city:"Marion · Williamson", rate:"34", violent:"1 in 215", property:"1 in 34", tone:"high", note:"Federal-contracting hub"},
                  {rank:"🔴 8", city:"Murphysboro · Jackson", rate:"38", violent:"1 in 170", property:"1 in 31", tone:"high", note:"Old housing stock"},
                  {rank:"🔴 9", city:"Carbondale · Jackson", rate:"50", violent:"1 in 101", property:"1 in 25", tone:"high", note:"SIU town; MV theft among highest in US"},
                ].map((r, i) => (
                  <tr key={r.city} style={{ borderBottom: i < 8 ? "1px solid #ebe5d6" : "none", background: r.tone === "safe" ? "oklch(98% 0.02 142)" : r.tone === "moderate" ? "oklch(98% 0.02 60)" : "oklch(98% 0.02 22)" }}>
                    <td style={{ padding: "4px 6px", whiteSpace: "nowrap" }}>{r.rank}</td>
                    <td style={{ padding: "4px 6px" }}><strong>{r.city}</strong><br /><span style={{ fontSize: 10.5, color: "#7a756b" }}>{r.note}</span></td>
                    <td style={{ padding: "4px 6px", textAlign: "right", fontWeight: 600, color: r.tone === "safe" ? "oklch(40% 0.18 142)" : r.tone === "high" ? "oklch(45% 0.20 22)" : "oklch(45% 0.18 60)" }}>{r.rate}</td>
                    <td style={{ padding: "4px 6px", textAlign: "right", color: "#5a564d" }}>{r.violent}</td>
                    <td style={{ padding: "4px 6px", textAlign: "right", color: "#5a564d" }}>{r.property}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 12, lineHeight: 1.55, color: "#3d3a33", marginBottom: 8 }}>
            <strong>BD takeaways:</strong>
            <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
              <li><strong>Direct safety-prioritized relocators to Benton, Du Quoin, or Carterville</strong> — these are the safest LWA-25 cities. Carterville pairs safety with newer-construction inventory + Walker&apos;s Bluff anchor + I-13 corridor.</li>
              <li><strong>Mt. Vernon is the &quot;safer larger town&quot; pick</strong> for relocators wanting more amenity density than Benton + Continental Tire job adjacency. Old housing stock is the trade-off.</li>
              <li><strong>Marion has elevated crime (34/1,000) despite the newer-construction + federal-contracting story.</strong> Pair the BD pitch with honest acknowledgment + the response (Marion PD + Williamson County Sheriff + IL State Police District 13).</li>
              <li><strong>West Frankfort is a profile outlier</strong> — total crime 31/1,000 but VIOLENT crime is nearly zero (1 in 3,573); essentially all crime is property-side, with MV theft 1 in 159 (among the highest in the nation). Quality-of-life for residents is closer to Mt. Vernon than to Marion / Murphysboro / Carbondale, but car theft is a real exposure.</li>
              <li><strong>Carbondale (50/1,000) + Murphysboro (38/1,000) + Marion (34) + West Frankfort property-only (31) + Herrin (29) are the higher-crime cities.</strong> SIU recruitment / graduate-retention housing strategy has to address security + visibility-of-response, not just price-to-wage math. Motor vehicle theft is the signature local crime in Carbondale + West Frankfort.</li>
              <li><strong>SIU campus (Clery Act 2024) three-year totals:</strong> zero murder, robbery, and arson across all three years. Burglary: 10 → 4 → 10. Aggravated assault: 3 → 6 → 2. Motor vehicle theft on-campus: 5 → 0 → 2. Full Clery breakdown including sex-offense categories is on the <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale page</a>.</li>
              <li><strong>Cross-county network activity</strong> across the LWA-25 footprint is not visible at the offense-aggregate level: FBI UCR + IL State Police annual reports track individual offenses by jurisdiction but don&apos;t aggregate cross-county network association. Treat the per-city rates above as the answerable metric and direct security-concerned relocators to county sheriff + ISP District 13 for site-specific advisory.</li>
            </ul>
          </div>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://www.neighborhoodscout.com/il/carbondale/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NeighborhoodScout Carbondale</a> · <a href="https://www.neighborhoodscout.com/il/marion/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Marion</a> · <a href="https://www.neighborhoodscout.com/il/mount-vernon/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Mt. Vernon</a> · <a href="https://www.neighborhoodscout.com/il/herrin/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Herrin</a> · <a href="https://www.neighborhoodscout.com/il/murphysboro/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Murphysboro</a> · <a href="https://www.neighborhoodscout.com/il/du-quoin/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Du Quoin</a> · <a href="https://www.neighborhoodscout.com/il/benton/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Benton</a> · <a href="https://www.neighborhoodscout.com/il/west-frankfort/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>West Frankfort</a> · <a href="https://www.neighborhoodscout.com/il/carterville/crime" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Carterville</a> · <a href="https://isp.illinois.gov/CrimeReporting/CrimeInIllinoisReports" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL State Police Crime in Illinois reports</a>. All FBI UCR 2024 calendar year, released October 2025. (West Frankfort 2023 data; NeighborhoodScout 2024 release pending.)
          </div>
        </div>

        <div style={{ background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>Drug-use reality (not "opioid epidemic")</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, lineHeight: 1.6, color: "#3d3a33" }}>
            <li>IL 2023 OD deaths: <strong>3,502</strong>; opioid-involved <strong>2,855</strong> (81%) — but most current opioid deaths are <strong>fentanyl cut into street drugs</strong>, not prescription pills.</li>
            <li>IL&apos;s <strong>16 southernmost counties are explicitly named as the region hardest-hit by IL&apos;s overdose deaths</strong>, including 4 of 5 LWA-25 counties (Franklin, Jackson, Perry, Williamson per IDPH/newspaper investigation; Jefferson included in the broader IDPH Marion Region).</li>
            <li>Dominant local drugs are <strong>meth + heroin (now fentanyl-contaminated) + emerging xylazine + cocaine</strong> — NOT pain pills. The &quot;opioid epidemic&quot; national label is misleading for Southern IL; the early-2010s prescription-pill wave is largely historical.</li>
            <li>Statewide OD declined 8.3% in 2023 (first drop since 2018) — but baseline rate in Southern IL remains substantially elevated.</li>
            <li>Active meth-enforcement indictments across Franklin/Jackson/Jefferson/Perry/Williamson per thesouthern.com archive.</li>
          </ul>
          <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
            Sources: <a href="https://dph.illinois.gov/topics-services/opioids/idph-data-dashboard/overdoses.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDPH Overdose Data Dashboard</a> + <a href="https://thesouthern.com/news/local/state-and-regional/newspaper-investigation-shows-that-illinois-16-southernmost-counties-are-hardest-hit-by-states-opioid-epidemic/article_3806ccec-495e-5f60-880f-3f27552a3994.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>thesouthern.com — 16 southernmost counties hardest hit</a> + <a href="https://dph.illinois.gov/resource-center/news/2025/march/release-20250306.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Pritzker administration OD-decline announcement 2025-03-06</a>.
          </div>
        </div>
      </div>

      <div style={{ padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>Program-design implication — match the local drug-supply reality</div>
        <div style={{ marginBottom: 6 }}>
          The national &quot;opioid epidemic&quot; label is anchored on the prescription-pill pattern of the 2000s-2012 wave. For rural Southern IL workforce planning, that label is a poor fit for current conditions:
        </div>
        <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
          <li>The dominant drugs here are <strong>meth + street heroin (fentanyl-contaminated) + xylazine</strong> — not Rx pills (IDPH overdose dashboard + thesouthern.com regional reporting).</li>
          <li>Workforce-board program designs that assume Rx-pill recovery pathways (pain-clinic referral, prescriber education, etc.) will mis-fit a population whose constraint is street-supply fentanyl + xylazine + meth. MAT (medication-assisted treatment) clinics + recovery-housing referral remain the right local infrastructure.</li>
        </ul>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
          Source: <a href="https://dph.illinois.gov/topics-services/opioids/idph-data-dashboard/overdoses.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IDPH Overdose Data Dashboard</a> (drug-class breakdown by region + year).
        </div>
      </div>

      {/* Mandatory-OT cross-credential meta-finding (social-media pull 2026-05-27) */}
      <div style={{ padding: 14, background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Mandatory overtime is the dominant attrition driver across LWA-25 family-supporting employers
        </div>
        <div style={{ marginBottom: 8 }}>
          Cross-credential lived-experience signal (social-media + Indeed + Glassdoor pull 2026-05-27): the family-supporting employers on this page&apos;s roster share a single dominant attrition driver — <strong>mandatory overtime</strong>, not low pay. Workers stay for the pension or benefits or wage; they leave because the schedule destroys home-life. Verbatim employee signal across multiple employers:
        </div>
        <ul style={{ margin: "0 0 8px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li><strong>IL DOC officer:</strong> &quot;40-hour shift PLUS at least two shifts of Mandatory overtime every week — does not make up for lost time with family&quot; (Indeed)</li>
          <li><strong>Continental Tire Mt. Vernon:</strong> &quot;They will mandate you for overtime every week&quot; · &quot;constant turnover&quot; (Glassdoor + Indeed; 4.0/5 overall, 77% recommend locally)</li>
          <li><strong>Aisin Marion:</strong> &quot;6 days a week mandatory overtime&quot; · &quot;If you want a life forget it&quot; (Glassdoor; 3.2/5; 79% recommend in Marion vs 68% company-wide)</li>
          <li><strong>Foresight Energy (Sugar Camp / Pond Creek):</strong> &quot;Make as much money as you like but do not plan on having a home life&quot; · vacation forced during mine shutdowns (Indeed + Glassdoor)</li>
          <li><strong>SIH (Southern Illinois Healthcare):</strong> staffing pressures + advancement &quot;preferential treatment related to who you know&quot; (Glassdoor; 3.4/5)</li>
        </ul>
        <div>
          <strong>Implication for the 1A+2C single-earner framing:</strong> the wage column shows whether the credential clears the 1A+2C math. The home-time column the dashboard cannot show is whether the worker can BE the parent the wage assumes they can support. Mandatory OT is the structural cost the wage doesn&apos;t reflect — same lens we applied to the TRAVEL-WORK rows, now extended to LOCAL · FAMILY-SUPPORTING employers where the schedule structurally destroys home-time. Workforce planning that ignores the OT pattern produces cohort attrition at the &quot;retention&quot; step, not at training. Sources: Indeed.com + Glassdoor employer reviews, pulled 2026-05-27.
        </div>
      </div>

      <div style={{ padding: 14, background: "#f0ece1", border: "1px solid #d8d2c4", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>What this means for workforce planning</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Drug-screen failure rate is a real cohort-selection issue</strong> — most family-supporting credentials in the page&apos;s roster (IL DOC officer, IDOT, IBEW 702 apprenticeship, GD-OTS production, coal-mine MSHA, IL State Police) require passing a pre-employment drug screen. A workforce board that recruits trainees without honest drug-screen pre-vetting produces cohort attrition at the placement step, not at training.</li>
          <li><strong>The dashboard shows what credentials clear 1A+2C; the trainee&apos;s ability to actually keep a slot depends on substance-use status the credential doesn&apos;t measure.</strong> Address this with recovery-program partnership (Centerstone, IDHS-funded MAT clinics) rather than ignoring it.</li>
          <li><strong>Crime rate affects relocator BD pitch</strong> — visiting executives + federal-retiree open houses see Carbondale&apos;s crime rate before they see the wine trail. Be honest with prospects about the security profile + show the response (Carbondale PD, SIU Department of Public Safety, IL State Police District 13 in Du Quoin); don&apos;t hide it.</li>
          <li><strong>Gang activity across the 5-county footprint</strong> affects worker mobility — workers in West Frankfort or Du Quoin may avoid Carbondale corridors after dark; that&apos;s a real transit-and-safety constraint on the &quot;match worker to job&quot; mapping the dashboard implies.</li>
        </ul>
      </div>

      {/* Agricultural labor + immigration enforcement constraint */}
      <div style={{ padding: 16, background: "white", border: "1px solid #d8d2c4", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Agricultural labor + immigration-enforcement squeeze
        </div>
        <div style={{ marginBottom: 10 }}>
          The regional ag workforce is heavily H-2A-dependent. The local labor pool to backfill that workforce is small and skill-specific — planting + harvest require operators capable of running large-acre tractors and grain trucks during narrow peak windows. <strong>Documented in H-2A program data, IL Farm Bureau, and farmdoc daily:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.6 }}>
          <li><strong>H-2A program is the agricultural-labor backbone</strong> — 398,258 positions certified nationally in 2025 (300% growth from 2010&apos;s 94,000). Illinois&apos; farm-labor reliance reflects this national pattern.</li>
          <li><strong>Domestic applicants fill less than 0.04% of positions</strong> — only 182 farming positions out of 415,000+ certified in 2025 were filled by domestic applicants. The formal local labor market essentially does NOT supply farm workers.</li>
          <li><strong>90% of H-2A workers are from Mexico</strong>; South Africa + Jamaica are secondary source countries.</li>
          <li><strong>Illinois AEWR (Adverse Effect Wage Rate) up 6% in 2024-2025</strong> — labor-cost pressure increasing. Some states up 15%.</li>
          <li><strong>Southern IL farm economic squeeze:</strong> per farmdoc daily 2025 grain-farm earnings analysis, southern Illinois grain farm operator labor + management income ranged DOWN TO NEGATIVE $276,707 in the most-southern parts of the state in 2024. The squeeze compounds the labor problem — farmers can&apos;t afford rising H-2A costs AND can&apos;t source domestic alternatives.</li>
          <li><strong>Enforcement-related processing delays:</strong> DHS partial-shutdown + broader enforcement climate has extended H-2A wait times. Local employers feeling the squeeze in real time.</li>
        </ul>
        <div style={{ marginBottom: 6 }}>
          <strong>What the credential market actually needs (planting/harvest big-equipment operator):</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5 }}>
          <li>CDL Class A (grain trucks haul to elevator)</li>
          <li>Tractor + combine operator certification (precision-ag GPS systems, large-acre tillage + planting + harvest equipment)</li>
          <li>Ag-mechanic credential (John Deere / Case IH dealer-certified) for maintenance during peak windows</li>
          <li>Spanish-language competency for crew supervisors (the H-2A workforce is overwhelmingly Mexican)</li>
          <li>Seasonal-work tolerance — planting (March-May) + harvest (Sept-Nov) are concentrated peak windows; off-season pivot to elevator / fertilizer-plant / equipment-shop work</li>
        </ul>
        <div style={{ marginBottom: 6 }}>
          <strong>Workforce-board implications:</strong>
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li>If H-2A access tightens further, IL farms either pay materially more or leave acres unharvested. The local labor pool to absorb the gap is tiny (the 182-of-415,000 nationwide pattern is the headwind) and big-equipment-credential-trained.</li>
          <li>JALC + Rend Lake + SIC <strong>precision-agriculture + ag-mechanic credentials</strong> are the right pipeline, but enrollment is small + the work is seasonal — pairs naturally with the CDL Class A row above for a year-round combined-pathway income.</li>
          <li>The agricultural-labor constraint is NOT a workforce-board problem to solve alone — it&apos;s a federal immigration-policy + ag-labor-economics problem that workforce planning sits downstream of. Be honest with regional ag employers about what the local credential pipeline CAN supply (a small specialized cohort) vs. what it cannot (the H-2A scale).</li>
        </ul>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
          Sources: <a href="https://farmdocdaily.illinois.edu/2025/07/the-growing-role-of-h-2a-workers-in-us-agriculture.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>farmdoc daily · The Growing Role of H-2A Workers in U.S. Agriculture</a> + <a href="https://www.ilfb.org/resources/farmer-rural-resources/h-2a-program/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois Farm Bureau · H-2A Program</a> + <a href="https://www.wsiu.org/state-of-illinois/2026-04-08/illinois-farmers-ease-critical-labor-shortages-through-this-agricultural-visa-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>WSIU · IL farmers ease critical labor shortages</a> + <a href="https://farmdocdaily.illinois.edu/2025/08/lower-grain-prices-lead-to-lower-earnings-for-grain-farms-in-2024-livestock-sector-sees-gains.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>farmdoc daily · Lower Grain Prices + 2024 IL Grain Farm Earnings (Southern IL grain-farm operator labor income negative $276,707)</a> + <a href="https://www.migrationpolicy.org/sites/default/files/publications/Martin-ImmigrationAgricultureH2AWorkers-FINAL.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Migration Policy Institute · Immigration and Farm Labor (Martin)</a>.
        </div>
      </div>
    </section>
  );
}

function FundingDrivenProgrammingSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        WIOA funding incentives vs LWA-25 demand mix · where the formula and the region diverge
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The pattern shows up in several sections: CEJA solar installer training
        with a small local installer base; CEJA wind tech training when the
        wind farms are 5+ hours north; PIRL targets that annualize below the
        local single-adult living wage. Each looks like a local choice. It is
        not. It follows from how WIOA and state workforce dollars flow. Boards
        are funded against metrics the federal and state programs measure
        (enrollment, completion, credential attainment, Q2 employment rate).
        They are not funded against whether trainees land in family-supporting
        local jobs. When a new funding stream opens, boards deploy it.
        Operating budgets depend on deployment, whether or not the local
        economy can absorb the credentials.
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>The mechanism · what the WIOA reform literature names this</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong>WIOA performance metrics measure rapid placement at any employer + credential attainment</strong> — not wage levels, not family-supporting outcomes, not local-economic-development fit.</li>
          <li><strong>Local workforce boards are funded against those metrics.</strong> Operating budgets, staffing, contract renewals all depend on hitting enrollment + completion + Q2 employment + credential targets.</li>
          <li><strong>When new categorical funding streams open</strong> (CEJA, sector partnerships, dislocated-worker rapid-response grants), boards deploy them because: (a) the money exists, (b) deployment generates metric-counted activity, (c) declining the funding signals reduced capacity to the state and the next funding cycle.</li>
          <li><strong>Result, per published WIOA reform literature</strong>: &quot;A good portion of WIOA funding effectively serves as a publicly subsidized recruitment and training mechanism for firms that rely on a high-churn, low-wage labor model with no clear pathway to professional advancement or upward mobility&quot; — <a href="https://tcf.org/content/report/beyond-job-placement-reimagining-wioa-for-economic-mobility-and-workforce-resilience/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Century Foundation, &quot;Beyond Job Placement&quot;</a>.</li>
          <li><strong>And</strong>: workforce boards are &quot;incentivized to prioritize rapid job placement and cost-efficiency, often focusing on industries that can absorb large numbers of workers quickly with minimal training investment. High-churn sectors—such as health care and transportation—fit this model well, offering fast placement outcomes and low-cost credentialing programs that help boards meet federal targets&quot; (TCF, same report).</li>
          <li><strong>Recommended reform direction</strong>: add wage-based outcome metrics + hourly-wage outcomes to WIOA performance requirements. <a href="https://www.americanprogress.org/article/recommendations-for-reauthorizing-the-workforce-innovation-and-opportunity-act/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Center for American Progress — WIOA reauthorization recommendations</a> explicitly call for &quot;performance measures that measure program success based on participants&apos; hourly wage outcomes in addition to their quarterly earnings.&quot;</li>
        </ul>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.05 22)", border: "1px solid oklch(45% 0.20 22)33", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.22 22)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          The Southern IL evidence · concrete examples of the pattern
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>CEJA wind technician training in LWA-25.</strong> Illinois wind farms are in Central + Northern IL (Livingston, McLean, Lee, LaSalle, Bureau, DeKalb, Vermilion counties). Zero operating utility-scale wind farms in Southern IL. Training Southern IL residents for wind-tech credentials when the work is 5+ hours north violates the regional-tailoring principle WIOA Section 108 + local-plan requirements exist to enforce.</li>
          <li><strong>CEJA solar installer training.</strong> Local NABCEP-installer employer base is modest but real — StraightUp Solar (Marion office, NABCEP-certified team), Tick Tock Energy, and other EnergySage-listed installers operate in the LWA-25 area. The capacity-vs-cohort question stands: how many CEJA graduates per year vs annual hiring capacity at the small residential / commercial installers. Big Muddy Solar (124 MW, Jackson Co.) is the largest local solar project but is being built by IBEW Local 702 lineworkers + IUOE Local 318 + LIUNA Local 773 under Signal Energy — so utility-scale solar goes to union trades, while NABCEP graduates land at the smaller installers.</li>
          <li><strong>Negotiated PY24 median-earnings <em>targets</em> are set below the single-adult living wage.</strong> These are the agreed-upon performance <em>targets</em> in the IL DCEO PY24-25 Model Summary (negotiated between the state and USDOL), not the realized outcomes. Targets: Adult $9,500/quarter (~$18.27/hr), Dislocated Worker $9,400/quarter (~$18.08/hr), Youth $5,000/quarter (~$9.62/hr). MIT Jackson Co. single-adult LW is $18.95/hr; 1A+2C is $46.76/hr. Even if every grantee delivers exactly to target, the median grad clears the single-adult bar by a few cents and falls roughly $28/hr short of the family-supporting bar. Actuals for LWA-25 are not surfaced at LWIA scale in the publicly released statewide narrative — that detail sits in IPATS, which is authorized-users-only. <a href="https://www.dol.gov/sites/dolgov/files/ETA/Performance/pdfs/Negotiations/state-model-summaries/PY24-25/IL_PY2024-2025_Model_Summary.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USDOL IL PY24-25 Model Summary</a>.</li>
        </ul>
        <p style={{ margin: "12px 0 0 0", fontWeight: 600 }}>
          This isn&apos;t a failure of local-board execution — it&apos;s exactly what the
          incentive structure rewards. Reform requires changing federal + state metrics,
          not asking the local board to optimize against metrics they aren&apos;t funded for.
        </p>
      </div>
    </section>
  );
}

function HarmCascadeSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Cascade cost · what the gap means for the 75,950 not-in-labor-force adults
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The funding-driven programming pattern above isn&apos;t harmless. Here&apos;s what
        actually happens to a worker who enrolls in a phantom-pipeline training program —
        and why the outcome shows up nowhere in the official metrics.
      </div>

      <div style={{ marginBottom: 16 }}>
        {[
          {
            n: "1",
            title: "Program gets funded; board + community college both have economic incentive to run it.",
            body: "State or federal categorical grant (CEJA, sector partnership, dislocated-worker rapid-response) opens. Workforce board applies because operating-budget renewal depends on deployment. Community college (JALC, Rend Lake) gets training-delivery contract or curriculum-development funding. Both organizations now have financial stake in enrolling participants.",
          },
          {
            n: "2",
            title: "Worker enrolls with reasonable belief that completing training leads to a job.",
            body: "Marketing materials describe the credential + wage potential. Recruitment events emphasize placement opportunities. Trainee is not told that local employer demand for the specific credential is essentially zero. The trainee invests 8 weeks (CEJA solar) to 5 years (apprenticeship-style) of their working life into the program.",
          },
          {
            n: "3",
            title: "Trainee completes program. Credential earned. Metrics look good.",
            body: "PIRL outcome measures register: Measurable Skill Gains ✓ · Credential Attainment ✓. The workforce board and the community college both record a successful outcome on the metrics they're funded against. The state-level Annual Statewide Performance Report counts the credential. From a federal accountability standpoint, the program 'worked.'",
          },
          {
            n: "4",
            title: "Trainee can't find local work in the credential — because no local employers exist.",
            body: "CEJA wind tech with GWO cert → nearest operating wind farms are 5+ hours north in Livingston / McLean / Lee / LaSalle counties (PHANTOM locally). CDL Class A → local trucking pays $22-28/hr (below 1A+2C livable), OTR available but breaks family time. CEJA solar installer is a MIXED case (verified): local installers DO exist (StraightUp Solar Marion, Tick Tock Energy) but cohort-throughput-vs-hiring capacity isn't yet measured — could be over-saturation if cohort size exceeds annual installer hiring. Outcomes can diverge from expectations.",
          },
          {
            n: "5",
            title: "Trainee accepts the local low-wage job they could have gotten without the training, OR drops out of the labor force.",
            body: "If they land a $14-22/hr hospitality / retail / CNA job, the PIRL Q2-employment metric still registers as success (they're employed) even though the credential is irrelevant to the role. If they don't, they join the 'not in labor force' population documented in the True Labor Picture section — adding to the regional LFPR gap to IL state (-8pp aggregate, worse in some counties). The 'invisible population' grows by one.",
          },
          {
            n: "6",
            title: "Trainee can't easily get re-trained — the WIOA Individual Training Account chance is largely used up.",
            body: "Local workforce boards set lifetime ITA caps (typically $7,000-$10,000 per participant per 20 CFR 680 + local board policy). One round of CEJA / vocational training often consumes most of that allotment. Subsequent ITAs are issued only if (a) under the lifetime maximum, OR (b) participant qualifies for new Dislocated Worker eligibility via a qualifying layoff. The phantom-pipeline failure consumed the participant's main shot at federally-funded retraining. Source: 20 CFR Part 680 Subpart C — Individual Training Accounts.",
          },
          {
            n: "7",
            title: "The official record shows program success. The trainee's actual outcome is invisible.",
            body: "The board's PY24 performance report counts the credential. The community college counts the enrollment. The state aggregates these into 'Illinois met all negotiated levels of performance for Title I.' The trainee — discouraged, out of retraining eligibility, possibly out of the labor force — does not appear in the success metrics. The gap between metric-success and outcome-reality is the harm.",
          },
        ].map((step, i) => (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "44px 1fr", gap: 12, padding: "12px 0", borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
            <div style={{ fontSize: 28, fontWeight: 700, color: "oklch(45% 0.20 22)", textAlign: "center", lineHeight: 1 }}>{step.n}</div>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#1f1d18", marginBottom: 4 }}>{step.title}</div>
              <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>{step.body}</div>
            </div>
          </div>
        ))}
      </div>

      <div style={{ padding: 14, background: "oklch(96% 0.05 22)", border: "1px solid oklch(45% 0.20 22)33", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.22 22)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          The policy implication
        </div>
        <p style={{ margin: 0 }}>
          The cascade is reproducible. Every misaligned training cohort generates: (a) a
          workforce-board operating-budget renewal, (b) a community-college contract,
          (c) trainee opportunity-cost loss, (d) a depleted ITA entitlement, and
          (e) a metric-success record. The only party harmed is the trainee. Reform
          requires aligning metrics to actual local employer demand + wage outcomes (per
          the TCF + CAP recommendations cited above) AND protecting individual retraining
          eligibility when a board-driven training program fails to land participants in
          the promised credential&apos;s actual labor market.
        </p>
      </div>
    </section>
  );
}

function PirlOutcomesSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Workforce-board program outcomes · where the WIOA performance data already lives
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        This page critiques training-program effectiveness against employer demand and
        against the family-supporting wage threshold. The same accountability standard
        applies to workforce-board program outcomes. Under WIOA, workforce boards file
        Title I program data quarterly with USDOL Employment &amp; Training Administration
        via the <a href="https://www.dol.gov/agencies/eta/performance/wips" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>WIPS portal</a> in the
        Participant Individual Record Layout (PIRL) format. The data IS published
        publicly — here&apos;s where to find it.
      </div>
      <div style={{ fontSize: 13, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.55, padding: 12, background: "#f0ece1", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <strong>LWIA-25 identifier reference:</strong> DCEO Local Area ID <strong>17125</strong> · fiscal agent + operator <strong>Man-Tra-Con Corp</strong>, 3117 Civic Circle Blvd Suite B, Marion IL 62959 · 5-county footprint (Franklin, Jackson, Jefferson, Perry, Williamson) · Southern Illinois Workforce Development Board (SIWDB) governance. Sister workforce-area page: <a href="/east-central-illinois" style={{ color: "#1f5f8f", fontWeight: 600 }}>East Central Illinois (LWA-23, DCEO ID 17115, Lake Land College fiscal agent + CEFS operator) →</a>.
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Where WIOA performance outcomes are published (verified):</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong>Illinois workNet WIOA Performance &amp; Transparency dashboard</strong> — <a href="https://www.illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx</a>. Snapshot + Timeline graphs of all WIOA key performance indicators reported to USDOL + USDOE by the four WIOA core partners (Adult / Dislocated Worker / Youth / Wagner-Peyser).</li>
          <li><strong>Illinois WIOA Annual Statewide Performance Report Narratives</strong> — IL DCEO publishes these annually. <a href="https://dceo.illinois.gov/content/dam/soi/en/web/dceo/aboutdceo/reportsrequiredbystatute/illinois-wioa-annual-narrative-report-py24-usdol.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>PY2024 (latest)</a> · <a href="https://dceo.illinois.gov/content/dam/soi/en/web/dceo/aboutdceo/reportsrequiredbystatute/wioa-2024.11.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>PY2023</a>. ETA 9169 form data + qualitative narrative on key initiatives.</li>
          <li><strong>USDOL ETA Performance Data</strong> — <a href="https://www.dol.gov/agencies/eta/performance/results" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>dol.gov/agencies/eta/performance/results</a> — federal aggregator with state-level + national-level PIRL data tables, the WIPS Data Book, and quarterly performance summaries.</li>
        </ul>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(97% 0.04 60)", border: "1px solid oklch(58% 0.15 60)33", borderLeft: "6px solid oklch(58% 0.15 60)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(40% 0.15 60)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What&apos;s NOT typically published — the local-area breakout
        </div>
        <p style={{ margin: 0 }}>
          The above sources publish data at the STATE-AGGREGATE level, with some
          program-by-program breakouts. What is NOT usually surfaced in a dedicated
          public dashboard is <strong>local-workforce-area-specific outcomes</strong> —
          PY-by-PY enrollment, completion, Q2 + Q4 employment rates, median earnings,
          credential attainment, and Measurable Skill Gains broken out for LWA-25
          (or any individual Local Workforce Investment Area). That data exists in
          the state submissions but isn&apos;t typically extracted to a single board-
          accessible page. The local accountability ask is to surface those LWA-level
          breakouts alongside the statewide aggregates, so board members and the
          public can compare local performance against statewide and national
          benchmarks.
        </p>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>The 5 federally-mandated WIOA Title I outcome measures (LWIA-level)</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong>Employment Rate 2nd Quarter after Exit</strong> — % of participants employed in 2nd quarter after exiting program.</li>
          <li><strong>Employment Rate 4th Quarter after Exit</strong> — same, 4th quarter (durability of placement).</li>
          <li><strong>Median Earnings 2nd Quarter after Exit</strong> — dollar level (compare against MIT Living Wage thresholds).</li>
          <li><strong>Credential Attainment within 4 Quarters after Exit</strong> — % of program participants earning a recognized credential within 1 year of exit.</li>
          <li><strong>Measurable Skill Gains</strong> — % of participants meeting interim skill-gain benchmarks during program.</li>
        </ul>
        <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b" }}>
          A 6th statewide-level measure — <strong>Effectiveness in Serving Employers</strong> (repeat-business + employer-penetration) — is tracked at state level but not in LWIA-level breakouts.
        </div>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.05 22)", border: "1px solid oklch(45% 0.20 22)33", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.22 22)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What the LWIA-25 PY24 targets actually say about wage outcomes
        </div>
        <p style={{ margin: "0 0 8px 0" }}>
          The PY24 IL Annual Statewide Performance Report Narrative publishes <strong>LWIA-25
          specific negotiated targets</strong> for each Title I program. Cross-referencing
          against the MIT Living Wage Jackson Co. thresholds elsewhere on this page reveals
          the structural truth:
        </p>
        <div style={{ background: "white", border: "1px solid oklch(45% 0.20 22)33", borderRadius: 4, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 600 }}>
            <thead>
              <tr style={{ background: "oklch(96% 0.05 22)", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", color: "oklch(35% 0.22 22)" }}>
                <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Indicator</th>
                <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Adult</th>
                <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Dislocated Worker</th>
                <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Youth</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderTop: "1px solid oklch(45% 0.20 22)33" }}>
                <td style={{ padding: "8px 10px", fontWeight: 600 }}>Employment Rate Q2</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>75.0%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>82.5%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>67.0%</td>
              </tr>
              <tr style={{ borderTop: "1px solid oklch(45% 0.20 22)33" }}>
                <td style={{ padding: "8px 10px", fontWeight: 600 }}>Employment Rate Q4</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>76.0%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>82.0%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>70.0%</td>
              </tr>
              <tr style={{ borderTop: "1px solid oklch(45% 0.20 22)33", background: "oklch(94% 0.06 22)" }}>
                <td style={{ padding: "8px 10px", fontWeight: 700, color: "oklch(35% 0.22 22)" }}>Median Earnings Q2 (per-quarter)</td>
                <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 700, color: "oklch(35% 0.22 22)" }}>$9,500</td>
                <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 700, color: "oklch(35% 0.22 22)" }}>$9,400</td>
                <td style={{ padding: "8px 10px", textAlign: "right", fontWeight: 700, color: "oklch(35% 0.22 22)" }}>$5,000</td>
              </tr>
              <tr style={{ borderTop: "1px solid oklch(45% 0.20 22)33", background: "oklch(94% 0.06 22)" }}>
                <td style={{ padding: "8px 10px", color: "oklch(40% 0.20 22)" }}>↳ Annualized</td>
                <td style={{ padding: "8px 10px", textAlign: "right", color: "oklch(40% 0.20 22)" }}>~$38,000 / ~$18.27/hr</td>
                <td style={{ padding: "8px 10px", textAlign: "right", color: "oklch(40% 0.20 22)" }}>~$37,600 / ~$18.08/hr</td>
                <td style={{ padding: "8px 10px", textAlign: "right", color: "oklch(40% 0.20 22)" }}>~$20,000 / ~$9.62/hr</td>
              </tr>
              <tr style={{ borderTop: "1px solid oklch(45% 0.20 22)33" }}>
                <td style={{ padding: "8px 10px", fontWeight: 600 }}>Credential Attainment (4Q)</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>74.5%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>73.0%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>65.0%</td>
              </tr>
              <tr style={{ borderTop: "1px solid oklch(45% 0.20 22)33" }}>
                <td style={{ padding: "8px 10px", fontWeight: 600 }}>Measurable Skill Gains</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>72.5%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>68.0%</td>
                <td style={{ padding: "8px 10px", textAlign: "right" }}>75.0%</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p style={{ margin: "12px 0 0 0", fontWeight: 600 }}>
          The negotiated TARGET for median Q2 earnings — what the local board AGREED TO
          DELIVER, not what they exceeded — annualizes to ~$18/hr for Adult + Dislocated
          Worker exiters and ~$9.62/hr for Youth.
        </p>
        <p style={{ margin: "8px 0 0 0" }}>
          MIT Living Wage Jackson County (2026): single adult = $18.95/hr · 1 adult + 2
          children family-supporting = $46.76/hr. <strong>The LWIA-25 negotiated targets
          place exiters at-or-just-below single-adult living wage; the Youth target is
          less than half of single-adult LW.</strong> This isn&apos;t a critique of
          execution — it&apos;s a statement about what the system was designed to
          produce. Raising those negotiated targets is a state-level conversation
          (DCEO + the State Workforce Innovation Board) about what &quot;successful&quot;
          workforce-program completion should actually mean in terms of livable wages.
        </p>
        <p style={{ margin: "8px 0 0 0", fontSize: 11, color: "#7a756b" }}>
          Source: <a href="https://dceo.illinois.gov/content/dam/soi/en/web/dceo/aboutdceo/reportsrequiredbystatute/illinois-wioa-annual-narrative-report-py24-usdol.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL DCEO PY24 WIOA Annual Statewide Performance Report Narrative</a>, p. 14-18 (LWIA-25 / Local Area 17125 Adult / Dislocated Worker / Youth negotiated-target rows).
        </p>
      </div>

      {/* PY2025 negotiated targets */}
      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          LWIA-25 PY2025 negotiated targets · Local Area 17125
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 4, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 600 }}>
            <thead>
              <tr style={{ background: "#f0ece1", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", color: "#5a564d" }}>
                <th style={{ textAlign: "left", padding: "8px 10px", fontWeight: 600 }}>Indicator</th>
                <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Adult</th>
                <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Dislocated Worker</th>
                <th style={{ textAlign: "right", padding: "8px 10px", fontWeight: 600 }}>Youth</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["Employment Rate Q2", "76.5%", "83.0%", "70.0%"],
                ["Employment Rate Q4", "77.0%", "82.0%", "72.0%"],
                ["Median Earnings Q2 (per-quarter)", "$9,500", "$9,500", "$5,000"],
                ["Credential Attainment (4Q)", "74.5%", "74.0%", "73.0%"],
                ["Measurable Skill Gains", "73.0%", "68.5%", "75.0%"],
              ].map((r, i) => (
                <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid #ebe5d6" }}>
                  <td style={{ padding: "8px 10px", fontWeight: 600 }}>{r[0]}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{r[1]}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{r[2]}</td>
                  <td style={{ padding: "8px 10px", textAlign: "right" }}>{r[3]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p style={{ margin: "10px 0 0 0", fontSize: 12, color: "#5a564d" }}>
          PY2025 targets hold the Adult + Youth median-earnings benchmark flat at $9,500 / $5,000; Dislocated Worker median earnings step up $100 (~$0.19/hr). The same single-adult living wage gap as PY2024 remains structurally intact.
        </p>
        <p style={{ margin: "8px 0 0 0", fontSize: 11, color: "#7a756b" }}>
          Source: same IL DCEO PY24 narrative, LWIA-25 / 17125 PY25 rows. Indicator codes per ETA Performance Accountability Reporting (AER2/AER4/AMER/ACAR/AMSG for Adult; DER2/DER4/DMER/DCAR/DMSG for DW; YER2/YER4/YMER/YCAR/YMSG for Youth).
        </p>
      </div>

      {/* Actuals pending disclosure */}
      <div style={{ marginBottom: 16, padding: 14, background: "oklch(97% 0.04 60)", border: "1px solid oklch(58% 0.15 60)33", borderLeft: "6px solid oklch(58% 0.15 60)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(40% 0.15 60)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          LWIA-25 actual outcomes · status as of 2026-05-28
        </div>
        <p style={{ margin: 0 }}>
          The tables above are <strong>negotiated targets</strong> — the floor the local board agreed to deliver against. Actual realized outcomes for LWIA-25 PY2022, PY2023, and PY2024 are <strong>not yet published in any public source located</strong>. The IL DCEO PY24 narrative, p. 23, explicitly states: <em>&quot;Final adjusted levels of performance will not be made available until early 2026.&quot;</em> Sources checked: IL DCEO Annual Statewide Performance Narratives (statewide aggregates only, no LWIA-level actuals), SIWDB meeting-minutes index (titles enumerated but PDFs not exposed in rendered HTML), Man-Tra-Con program brochures (no annual report), USDOL ETA Performance Results (state-level dashboards, 403 to programmatic per-LWIA extraction), WIPS (authenticated grantee-only). When IL DCEO publishes the PY24 adjusted assessments in early 2026, this section will be updated with the actual vs target delta — that is the publication-ready accountability comparison.
        </p>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(97% 0.04 60)", border: "1px solid oklch(58% 0.15 60)33", borderLeft: "6px solid oklch(58% 0.15 60)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(40% 0.15 60)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Where to view PIRL outcomes by program year + LWIA
        </div>
        <p style={{ margin: 0 }}>
          The PY24 report (p. 14) confirms: <strong>a new WIOA Title I Participant
          Dashboard was launched as part of the WIOA Performance &amp; Transparency
          dashboard.</strong> Per the report&apos;s own language: &quot;LWIBs, Title I
          Director, Performance Managers and other stakeholders will have the ability
          to view participant data, enrollment information and outcomes to better
          assess the effectiveness of their programs. Data has been extracted from the
          Participant Individual Record Layout (PIRL) from Program Years 2017 through
          2023.&quot; Public-facing entry point:{" "}
          <a href="https://www.illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>
            illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx
          </a>.
        </p>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What a useful local-area dashboard would surface
        </div>
        <p style={{ margin: "0 0 8px 0" }}>
          Drawing from the state-aggregate sources above + IWDS local-area extracts
          (the Illinois Workforce Development System is the state&apos;s record-of-truth
          for PIRL submissions), the next-tier accountability view would publish
          LWA-25-specific outcomes by program (WIOA Adult, Dislocated Worker, Youth,
          regional CEJA Climate Works cohorts, every named training ladder):
        </p>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li>Enrollment count + completion rate (last 3 program years)</li>
          <li>Median Q2 post-exit earnings — cross-checked against MIT Living Wage 1A+2C (\$46.76/hr or \$97,260/yr) bar</li>
          <li>% of completers earning above single-adult living wage</li>
          <li>% of completers earning above family-supporting wage</li>
          <li>Credential attainment rate</li>
          <li>Employer-side: which employers hired completers, in which roles</li>
        </ul>
        <p style={{ margin: "8px 0 0 0" }}>
          The standard the page applies to credential-vs-demand alignment (the CEJA wind
          PHANTOM verdict + the CEJA solar capacity-question + the CNA BELOW LIVABLE
          WAGE verdict) is the same standard worth applying to
          local-area workforce-board outcomes. Honest measurement, including the
          inconvenient outcomes, is what makes a workforce board credible to fund.
        </p>
      </div>

      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Sources: <a href="https://www.dol.gov/agencies/eta/performance/wips" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USDOL ETA WIPS (Workforce Integrated Performance System)</a> · <a href="https://www.dol.gov/sites/dolgov/files/ETA/wioa/pdfs/WIOA-Joint-Performance-Standards-FAQs.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>WIOA Joint Performance Standards FAQ</a> · <a href="https://www.illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois workNet WIOA Performance &amp; Transparency dashboard</a> · <a href="https://dceo.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>IL DCEO Annual Statewide Performance Reports</a> · <a href="https://www.dol.gov/agencies/eta/performance/results" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USDOL ETA Performance Results</a>.
      </div>
    </section>
  );
}

function fmtUsdShort(n: number): string {
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}

function GdotsSubawardLanesTable({ lanes }: { lanes: GdotsSubawardLanes }) {
  const fetchedDate = lanes.fetched_at ? new Date(lanes.fetched_at) : null;
  const fetchedLabel = fetchedDate
    ? fetchedDate.toISOString().slice(0, 10)
    : "—";
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>
        GD-OTS Marion sub-award lanes · top 15 by NAICS-6 (24-month lookback)
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, color: "#3d3a33" }}>
          <thead>
            <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>NAICS-6 · industry</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", textAlign: "right" }}>24-mo sub-$</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", textAlign: "right" }}>Sub-awards</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Top-3 sub-recipients</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Out-of-region candidate</th>
            </tr>
          </thead>
          <tbody>
            {lanes.rows.map((r) => {
              const topNames = r.top_sub_recipients.map((s) => s.name).join(", ");
              return (
                <tr
                  key={r.naics_code}
                  style={{
                    borderTop: "1px solid #ece7d8",
                    borderLeft: r.out_of_region_candidate ? "3px solid #b8851f" : "3px solid transparent",
                  }}
                >
                  <td style={{ padding: "8px 10px", verticalAlign: "top" }}>
                    <div style={{ fontFamily: "ui-monospace, monospace", color: "#1f1d18", fontWeight: 600 }}>{r.naics_code}</div>
                    <div style={{ color: "#5a564d", fontSize: 11 }}>{r.naics_name || "—"}</div>
                  </td>
                  <td style={{ padding: "8px 10px", textAlign: "right", verticalAlign: "top", fontFamily: "ui-monospace, monospace" }}>
                    {fmtUsdShort(r.subaward_total_usd)}
                    <div style={{ color: "#7a756b", fontSize: 11 }}>{r.prime_award_count} prime{r.prime_award_count === 1 ? "" : "s"}</div>
                  </td>
                  <td style={{ padding: "8px 10px", textAlign: "right", verticalAlign: "top", fontFamily: "ui-monospace, monospace" }}>
                    {r.subaward_count}
                  </td>
                  <td style={{ padding: "8px 10px", verticalAlign: "top", maxWidth: 360 }}>
                    {topNames || <span style={{ color: "#7a756b" }}>—</span>}
                  </td>
                  <td style={{ padding: "8px 10px", verticalAlign: "top" }}>
                    {r.out_of_region_candidate ? (
                      <span style={{ fontSize: 11, color: "#7a5e15", fontWeight: 600 }}>Yes</span>
                    ) : (
                      <span style={{ fontSize: 11, color: "#7a756b" }}>—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Source: <a href={lanes.source_url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USAspending.gov</a>,
        {" "}{lanes.lookback_months}-mo lookback through {fetchedLabel}.
        {" "}{fmtUsdShort(lanes.total_subaward_amount_usd)} flowed through sub-awards on Marion prime contracts.
        The &quot;out-of-region candidate&quot; flag is a name-only heuristic — sub-recipient names lacking MARION / CARBONDALE / ILLINOIS / IL tokens are highlighted as likely-out-of-region. Treat as a BD-triage hint; verify each candidate&apos;s actual place-of-performance at SAM.gov before stakeholder outreach.
      </div>
    </div>
  );
}

function GdotsSubawardLanesBulkTable({ lanes }: { lanes: GdotsSubawardLanesBulk }) {
  const fetchedDate = lanes.fetched_at ? new Date(lanes.fetched_at) : null;
  const fetchedLabel = fetchedDate
    ? fetchedDate.toISOString().slice(0, 10)
    : "—";
  return (
    <div style={{ marginTop: 24, marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 4 }}>
        Sub-recipient detail (USAspending bulk-download, weekly refresh — verified state)
      </div>
      <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 8, lineHeight: 1.5 }}>
        Per-NAICS rollup of <em>all</em> sub-recipients (not just the top-3 per prime), grouped on the <strong>sub-award NAICS</strong> rather than the prime-award NAICS (332993 Ammunition Mfg). This is the view that exposes the services lanes hidden under the manufacturing rollup. Services lanes (the local-firm BD-action set) are accented.
      </div>
      <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, color: "#3d3a33" }}>
          <thead>
            <tr style={{ background: "#f0ece1", textAlign: "left", borderBottom: "1px solid #d8d2c4" }}>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>NAICS-6 · industry</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", textAlign: "right" }}>24-mo sub-$</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18", textAlign: "right" }}>Sub-award count</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Top-3 sub-recipients (name · state)</th>
              <th style={{ padding: "8px 10px", fontWeight: 600, color: "#1f1d18" }}>Out-of-region (verified state)</th>
            </tr>
          </thead>
          <tbody>
            {lanes.rows.map((r) => {
              const accent = r.is_services_lane;
              return (
                <tr
                  key={r.naics_code}
                  style={{
                    borderTop: "1px solid #ece7d8",
                    background: accent ? "#fbf6e8" : undefined,
                    borderLeft: accent ? "3px solid #b8851f" : "3px solid transparent",
                  }}
                >
                  <td style={{ padding: "8px 10px", verticalAlign: "top" }}>
                    <div style={{ fontFamily: "ui-monospace, monospace", color: "#1f1d18", fontWeight: 600 }}>
                      {r.naics_code}
                      {accent && (
                        <span style={{ marginLeft: 6, fontSize: 10, fontWeight: 600, color: "#7a5e15", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                          services
                        </span>
                      )}
                    </div>
                    <div style={{ color: "#5a564d", fontSize: 11 }}>{r.naics_name || "—"}</div>
                  </td>
                  <td style={{ padding: "8px 10px", textAlign: "right", verticalAlign: "top", fontFamily: "ui-monospace, monospace" }}>
                    {fmtUsdShort(r.subaward_total_usd)}
                  </td>
                  <td style={{ padding: "8px 10px", textAlign: "right", verticalAlign: "top", fontFamily: "ui-monospace, monospace" }}>
                    {r.subaward_count}
                  </td>
                  <td style={{ padding: "8px 10px", verticalAlign: "top", maxWidth: 360 }}>
                    {r.top_sub_recipients.length > 0 ? (
                      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                        {r.top_sub_recipients.map((s, i) => (
                          <div key={`${s.uei || s.name}-${i}`} style={{ fontSize: 11.5 }}>
                            <span>{s.name}</span>
                            {s.state && (
                              <span style={{ color: "#7a756b", marginLeft: 6 }}>
                                · {s.state}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <span style={{ color: "#7a756b" }}>—</span>
                    )}
                  </td>
                  <td style={{ padding: "8px 10px", verticalAlign: "top" }}>
                    {r.out_of_region_total_count > 0 ? (
                      <span style={{ fontSize: 11, color: r.out_of_region_count > 0 ? "#7a5e15" : "#5a564d", fontWeight: r.out_of_region_count > 0 ? 600 : 400 }}>
                        {r.out_of_region_count} of {r.out_of_region_total_count} subs in non-IL states
                      </span>
                    ) : (
                      <span style={{ fontSize: 11, color: "#7a756b" }}>—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Source: <a href={lanes.source_url} target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USAspending.gov</a> bulk-download CSV, weekly refresh through {fetchedLabel}. Verified sub-recipient state (not name-heuristic). The realtime view above shows top-3 recipients per prime; this view shows the per-NAICS rollup of ALL sub-recipients. {fmtUsdShort(lanes.total_subaward_amount_usd)} represented across the top 25 sub-award NAICS lanes.
      </div>
    </div>
  );
}

function SupplyChainSubawardSection({
  lanes,
  bulkLanes,
}: {
  lanes?: GdotsSubawardLanes | null;
  bulkLanes?: GdotsSubawardLanesBulk | null;
}) {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Local supply-chain mapping · where the federal money flows after the prime
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The federal-money concentration section above shows GD-OTS Marion receiving
        the lion&apos;s share of LWA-25 federal CONTRACTING obligations. The
        community-engagement leverage hinges on a question that the dashboard can&apos;t
        fully answer yet: <strong>what does GD-OTS (and other primes) buy from local
        subcontractors, and what are they buying from out-of-region subs that LOCAL
        firms could supply?</strong> This is the actionable BD lead the
        concentration section promises but doesn&apos;t yet deliver. The data exists
        — it&apos;s in USAspending&apos;s subaward records — but querying it requires
        per-prime filtering that&apos;s not yet wired into this page.
      </div>

      {lanes && lanes.rows.length > 0 && <GdotsSubawardLanesTable lanes={lanes} />}

      {bulkLanes && bulkLanes.rows.length > 0 && <GdotsSubawardLanesBulkTable lanes={bulkLanes} />}

      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>How to query subaward data for community-engagement leverage</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong>USAspending recipient profile + subaward tab.</strong> Each prime contractor has a recipient page at <a href="https://www.usaspending.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>usaspending.gov</a> with a Sub-Awards tab listing every subaward of $30k+. For GD-OTS Marion, this is the operational view of who the prime actually pays.</li>
          <li><strong>Filter subawards by NAICS code.</strong> Common GD-OTS munitions-manufacturing subaward NAICS: 332710 (Machine Shops), 332618 (Wire Products Manufacturing), 332999 (Misc Fabricated Metal Products), 488510 (Freight Transportation Arrangement), 561621 (Security Systems Services), 423840 (Industrial Supplies Wholesale).</li>
          <li><strong>Filter subaward recipients by place-of-performance.</strong> Subawardees in OTHER states for work performed at GD-OTS Marion are the candidates for local-firm replacement.</li>
          <li><strong>IL DCEO Industrial Supply Directory</strong> at <a href="https://dceo.illinois.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>dceo.illinois.gov</a> — cross-reference local IL firms with capability to fill those NAICS gaps.</li>
          <li><strong>SBA HUBZone + 8(a) directories</strong> — local certified-status firms get federal-contracting set-aside preference. The Marion-headquartered SDVOSB recipient profiled in the Federal Money Concentration section is the local precedent.</li>
        </ul>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <strong>What this dashboard already does for you:</strong> the GD-OTS subaward table above is auto-refreshed every 24 hours from USAspending.gov, aggregated by NAICS-6 lane, with top sub-recipient names + out-of-region flags pre-computed. The federal-money concentration section above marks each top recipient with its SBA certification status (SDVOSB / HUBZone / 8(a) / WOSB / Large biz / Verify @SAM.gov) via the maintained KNOWN_SBA_STATUS lookup.
        <br /><br />
        <strong>Your one residual step:</strong> for any NAICS lane flagged out-of-region above, identify whether a local firm in the same NAICS code could pick up that work, and broker the introduction to the prime&apos;s procurement team. That introduction is the human-only part — the data join is done. This is the practical CBA-precedent move — and it&apos;s how the local SDVOSB profiled in the Federal Money Concentration section grew to $11.9M / 24 months on the same Marion-area federal pipeline.
      </div>
      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Sources: <a href="https://www.usaspending.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USAspending.gov</a> subaward data; <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/hubzone-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA HUBZone Program</a>; <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/8a-business-development-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA 8(a) Business Development Program</a>; IL DCEO Industrial Supply Directory.
      </div>

      {/* Services-lane BD intelligence — hidden under the 332993 prime-NAICS rollup */}
      <div style={{ marginTop: 20, padding: 16, background: "#f7f5ef", border: "1px solid #d8d2c4", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Services-lane BD intelligence — the easier lanes hidden under the 332993 rollup
        </div>
        <div style={{ marginBottom: 10 }}>
          The realtime USAspending API rolls every GD-OTS Marion sub-award up under the prime-award NAICS (332993 Ammunition Mfg). The actual sub-recipient work spans many NAICS codes — and the SERVICES lanes (grounds, janitorial, HVAC, freight, pest, waste, food, equipment repair) are typically lower-clearance + lower-precision-machining-barrier than the manufacturing lanes. These are the most replaceable lanes by local SDVOSB / HUBZone / 8(a) firms.
        </div>
        <div style={{ overflowX: "auto", marginBottom: 10 }}>
          <table style={{ width: "100%", fontSize: 11.5, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: "#ebe5d6", textAlign: "left" }}>
                <th style={{ padding: "6px 8px", borderBottom: "1px solid #d8d2c4" }}>NAICS</th>
                <th style={{ padding: "6px 8px", borderBottom: "1px solid #d8d2c4" }}>Category</th>
                <th style={{ padding: "6px 8px", borderBottom: "1px solid #d8d2c4" }}>Clearance tier</th>
                <th style={{ padding: "6px 8px", borderBottom: "1px solid #d8d2c4" }}>Local pickup</th>
              </tr>
            </thead>
            <tbody>
              {[
                {n: "561730", c: "Grounds / landscape maintenance", t: "Tier 1 (escorted, exterior)", l: "EASY"},
                {n: "561720", c: "Janitorial — admin spaces", t: "Tier 1-2 (background + escorted)", l: "EASY — common SDVOSB set-aside"},
                {n: "561210", c: "Facilities support (umbrella)", t: "Tier 1-2", l: "MEDIUM"},
                {n: "561612", c: "Security guard & patrol", t: "Tier 3 (DoD Secret)", l: "HARD — clearance barrier"},
                {n: "561621", c: "Security systems services", t: "Tier 2-3", l: "MEDIUM (page already flags this lane)"},
                {n: "238220", c: "HVAC maintenance", t: "Tier 1-2 (exterior) / Tier 3 (production zones)", l: "MEDIUM — SMART Local 268 union shops fit"},
                {n: "238210", c: "Electrical contractors", t: "Tier 1-2 (exterior) / Tier 3 (production zones)", l: "MEDIUM — IBEW Local 702 fit"},
                {n: "484110", c: "Local freight trucking", t: "DOT + drug screen (no clearance)", l: "EASY — Knight Hawk-area CDL operators already exist"},
                {n: "488510", c: "Freight transp. arrangement", t: "Tier 1 (background)", l: "EASY (page already flags this lane)"},
                {n: "722310", c: "Food service contractor", t: "Tier 1-2", l: "EASY-MEDIUM — SIH already runs the Marion VA cafeteria"},
                {n: "562111", c: "Solid waste collection", t: "Tier 1 (background)", l: "EASY"},
                {n: "562211", c: "Hazmat waste treatment", t: "Tier 2-3 (cleared technicians)", l: "HARDER (regulatory)"},
                {n: "811310", c: "Industrial equipment repair", t: "Tier 1-2 (escorted)", l: "MEDIUM"},
                {n: "561710", c: "Pest control", t: "Tier 1 (background)", l: "EASY"},
              ].map((r, i) => (
                <tr key={r.n} style={{ borderBottom: i < 13 ? "1px solid #ebe5d6" : "none" }}>
                  <td style={{ padding: "5px 8px", fontFamily: "monospace", color: "#1f5f8f" }}>{r.n}</td>
                  <td style={{ padding: "5px 8px" }}><strong>{r.c}</strong></td>
                  <td style={{ padding: "5px 8px", color: "#5a564d" }}>{r.t}</td>
                  <td style={{ padding: "5px 8px", color: r.l.startsWith("EASY") ? "oklch(40% 0.18 142)" : r.l.startsWith("HARD") ? "oklch(45% 0.20 22)" : "#5a564d", fontWeight: r.l.startsWith("EASY") ? 600 : 400 }}>{r.l}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Clearance reality (per FAR §1252.204-70 + DoD personnel security policy)</strong> — there are <em>three</em> tiers, not a single Secret-clearance barrier:
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12, lineHeight: 1.5 }}>
          <li><strong>Tier 1 — NACI / HSPD-12 facility access:</strong> exterior maintenance / landscaping / gates / supply delivery / food service. Background investigation + drug screen + DoD facility-access card (RAPIDGate-style). NO security clearance. Most replaceable services lanes live here. Time-to-clear: 1-4 weeks. Sponsor cost: ~$200-500/employee.</li>
          <li><strong>Tier 2 — Public Trust (Moderate Risk):</strong> interior unclassified work (admin janitorial, food service in plant cafeteria, IT support, vehicle maintenance). OPM NACI / Tier 1 BI + drug screen + identity proofing. Still NOT a clearance — it's a suitability determination. Time: 4-12 weeks. Cost: ~$500-1500/employee.</li>
          <li><strong>Tier 3 — DoD Secret:</strong> classified production zones, controlled materials handling, security posts at GD-OTS Marion (M119A2 propellant areas, classified records, certain HVAC in production zones). Full DoD Secret clearance. Time: 6-12+ months. Cost: $5-10k+/employee to sponsor.</li>
        </ul>
        <div style={{ padding: 12, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderRadius: 4, marginBottom: 8 }}>
          <strong>Local-BD principle:</strong> the supply-chain replacement strategy targets sub-recipients <em>outside the broader Midwest economic shed</em> — outside ~200 miles, outside the St. Louis / Evansville / Paducah / Indianapolis labor markets. <strong>Don&apos;t take jobs from St. Louis-area neighbors</strong> (John J. Steuby Co. = St. Louis MO; Spartan Light Metals = Mexico MO + Sparta IL — these are shared labor market, leave them alone). Target out-of-economic-shed primes like AMTEC (Janesville WI, ~350 mi) for any precision-manufacturing replacement; concentrate services-lane replacement on out-of-region service contractors first.
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>SDVOSB local-entry playbook (reproducible for local services-lane pickup):</strong>
        </div>
        <ol style={{ margin: "0 0 0 18px", padding: 0, fontSize: 12, lineHeight: 1.55 }}>
          <li>Local small business (S-corp or LLC) with primary place of business in LWA-25 — Marion / Carbondale / Murphysboro qualify; Franklin / Perry / parts-of-Jackson qualify for HUBZone</li>
          <li>SDVOSB / HUBZone / 8(a) / WOSB certification through SBA — Veterans Business Outreach Center (VBOC) for SDVOSB; SBA District Office for HUBZone + 8(a) + WOSB</li>
          <li>SAM.gov registration + UEI assignment + qualifying NAICS codes</li>
          <li>Background-investigated workforce (Tier 1 / Tier 2 — not Secret clearance for most services lanes)</li>
          <li>Start with a single small contract under FAR §19 (small-business set-aside) — even a $50-200k starter is enough to establish past-performance</li>
          <li>Optional: SBA Mentor-Protégé Program pairing with an established firm (the local SDVOSB precedent firm is the obvious mentor candidate)</li>
        </ol>
        <div style={{ marginTop: 8, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
          Sources: <a href="https://www.naics.com/naics-code-description/?code=561210" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NAICS 561210 Facilities Support Services</a>; <a href="https://www.acquisition.gov/tar/1252.204-70-contractor-personnel-security-and-agency-access." target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>FAR §1252.204-70 (Contractor Personnel Security)</a>; <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/sba-mentor-protege-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA Mentor-Protégé Program</a>; OPM e-QIP investigation guidance.
        </div>
      </div>

      {/* ─── Drop-ship federal-product reseller model (with expert corrections) ─── */}
      <div style={{ marginTop: 20, padding: 16, background: "#f7f5ef", border: "1px solid #d8d2c4", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          The drop-ship federal-product reseller path · honest BD analysis
        </div>
        <div style={{ marginBottom: 10 }}>
          A complementary pathway to services replacement: bid federal supply contracts for commodity products (PPE, hardware, office supplies, specialty equipment, ag supplies, sport / wildlife / debris nets, etc.), source from US small-business manufacturers, drop-ship directly from manufacturer to the requesting government agency. Minimal warehousing, low fixed cost, geographic location nearly irrelevant. The local SDVOSB set-aside pattern at scale, applied to commodity supplies rather than services.
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Expert-reviewed feasibility verdict (independent 2026-05-27 second-opinion):</strong> <span style={{ color: "oklch(40% 0.18 60)", fontWeight: 600 }}>FEASIBLE-WITH-CAVEATS · 6/10 confidence.</span> Real path but a small-percentage path. Below are the corrections the promotional GSA/SBA materials don&apos;t surface.
        </div>

        <div style={{ marginBottom: 8 }}>
          <strong>Critical regulatory constraint — HUBZone has NO Nonmanufacturer Rule (NMR) waivers:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>SBA NMR waivers (Class + Individual) exist for <strong>8(a) / SDVOSB / WOSB</strong> set-asides — but <strong>NOT HUBZone</strong>.</li>
          <li>A HUBZone reseller MUST source from a <strong>US small-business manufacturer</strong> on every HUBZone set-aside above $250k. There is no waiver path.</li>
          <li><strong>As of 2025</strong>, NMR applies to ALL socio-economic set-asides above <strong>$10,000</strong> (down from earlier thresholds) — the lane is tighter than the marketing implies.</li>
          <li>Competitors will size-protest you on NMR compliance the moment you win; the SBA digs in, GAO sustains where the proposal shows facial non-compliance. Document the manufacturer chain immaculately.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>HUBZone "advantage" is half what the marketing implies:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>Government-wide 3% HUBZone goal <strong>missed every year</strong> — actual spend is <strong>~2.05-2.5%</strong> (USFCR + GovScout data).</li>
          <li>The <strong>10% price preference triggers only in full-and-open competition</strong>, not in set-asides themselves.</li>
          <li>The widely-quoted "HUBZone resellers sell 350% more" figure is from Winvale (vendor marketing), <strong>not a peer-reviewed number</strong>. Discount accordingly.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Actual success rate — sobering reality:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li><strong>~50% of GSA Schedule holders reported $0 in sales in FY24</strong> — and those are the holders who already cleared the Schedule-application bar. SAM-only attrition is materially worse (practitioner consensus &lt;25% of SAM registrants ever win anything).</li>
          <li><strong>Median time to first contract: ~12 months</strong> (6-18 month range). Practitioner norm: bid 30-60 qualified opportunities to land win #1.</li>
          <li>Encouraging counterweight: <strong>67% of first-time winners win another contract within 12 months</strong>. The cliff is getting to win #1, not scaling past it.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Working-capital cost is higher than the casual framing:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>Federal Prompt Payment Act says Net-30; <strong>reality is Net-60 to Net-90 typical</strong>.</li>
          <li>Government-contract factoring runs <strong>~1-2% per 30 days</strong> — so on a 90-day federal pay cycle you give up <strong>3-6% of gross</strong>, not the 1-3% the headline factoring rate implies.</li>
          <li>SBA Contract CAPLine (specifically designed for this) + SBA Working CAPLine + Live Oak Bank 7(a) + USDA B&amp;I rural loan are the bank-side options.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Realistic margin structure (thinner than commodity-reseller intuition):</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>Gross margins on commodity supply contracts: 8-15% typical</li>
          <li>Subtract: NMR-domestic premium (5-15% cost over offshore alternatives), factoring on 90-day federal cycle (3-6%), bid/proposal cost amortized (~2-4%), wrong-SKU / return risk (~1-3%)</li>
          <li><strong>Net margin lands at 3-8% of revenue</strong>, not the 15-25% retail-reseller intuition.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Realistic income trajectory:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li><strong>Year 1:</strong> gross $0-$150k, net often <em>negative</em> after $30-$233k of cash + sweat</li>
          <li><strong>Year 3:</strong> median surviving operator $300k-$1.5M gross; net 8-15% of revenue (after Years 1-2 absorbed the learning curve)</li>
          <li><strong>Year 5 real-business threshold:</strong> $1M+ gross with 2+ recurring IDIQ / BPA vehicles</li>
          <li><strong>Without a Mentor-Protégé JV OR a recurring vehicle, this stays a hobby past year 3.</strong></li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Mentor-Protégé JV is NOT optional flavoring:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>Protégés in <strong>active MPP joint ventures had a 34% win rate</strong> on 16,651 offers in FY22 (NCMA published analysis).</li>
          <li>Unmentored small-business win rates are dramatically lower (single-digit percentages typical).</li>
          <li>The MPP mentor relationship isn&apos;t optional — it&apos;s the variable that moves you from &lt;10% to 30%+ win rates on bids.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Profile of the typical winning operator (calibrate yourself against this):</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>Prior corporate procurement, supply-chain, or military-logistics background</li>
          <li>Brings <strong>$50k-$150k in working capital</strong> + a tolerance for 12-18 months without revenue</li>
          <li>Treats it as <strong>40+ hrs/week from month 1</strong> — not a side hustle</li>
          <li>Has <strong>2-3 specific manufacturer relationships locked before SAM registration</strong></li>
          <li>Picks <strong>ONE NAICS lane</strong> and stays in it</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>Sourcing + tariff strategy for Southern IL HUBZone reseller:</strong>
        </div>
        <ol style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li><strong>Source domestic first</strong> — avoids tariff issues entirely + qualifies for Buy American Act preference + meets HUBZone NMR (no waiver alternative).</li>
          <li><strong>TAA-designated countries</strong> (~125 countries: USMCA partners + WTO GPA + KORUS + FTA partners) when domestic unavailable — zero tariff for supply contracts above ~$183k threshold. Note: this works for SDVOSB/8(a)/WOSB but is more constrained for HUBZone due to NMR.</li>
          <li><strong>HTSUS Chapter 98 Subchapters VIII + X</strong> — narrow government-contract duty-exemption mechanism for specific supplies imported under contract. Consult a customs broker before bidding. FAR Subpart 25.9 covers the procedure.</li>
          <li><strong>Foreign Trade Zone (FTZ) #271 (Metro East — Madison + St. Clair Co.)</strong> is nearest to LWA-25 if operation scales enough to warrant FTZ benefits — duty deferral / cash-flow timing.</li>
          <li><strong>AVOID China-origin</strong> — Section 301 tariffs (25-100% depending on category) + broader 2025 tariffs make Chinese goods uneconomic for federal supply.</li>
          <li><strong>Don&apos;t take jobs from St. Louis neighbors</strong> — same principle as the services-lane BD intel above. Target out-of-economic-shed (~200mi+) competitors for displacement, not regional Midwest manufacturers.</li>
        </ol>

        <div style={{ marginBottom: 8 }}>
          <strong>Common &quot;tariff break&quot; myths to drop:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li><strong>"Not-for-profit gets tariff break"</strong> — MOSTLY WRONG. 501(c)(3) = income-tax exemption, NOT customs-duty exemption. Narrow exception (HTSUS 9810.00.60) for nonprofit imports of scientific instruments/apparatus for educational/scientific purposes — doesn&apos;t fit a commodity-reseller drop-ship model.</li>
          <li><strong>"Government contract = automatic duty exemption"</strong> — PARTIALLY RIGHT. HTSUS Chapter 98 Subchapters VIII + X do provide narrow duty exemptions for supplies imported under specific government contracts. Requires customs-broker filing + contract-specific certification + government-end-use documentation. Not a blanket exemption.</li>
        </ul>

        <div style={{ marginBottom: 8 }}>
          <strong>What the dashboard already does for you:</strong> The federal-money concentration section above + the supply-chain subaward integration + the services-lane NAICS table + the clearance-tier mapping all surface the data a prospective Southern IL HUBZone reseller needs to identify lanes, sub-recipients, and entry barriers. The local SDVOSB proof-of-concept (Marion IL · $11.9M / 24 months) is documented in the federal-money section.
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Your residual moves (the dashboard cannot self-execute these):</strong>
        </div>
        <ol style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Apply for SBA HUBZone certification</strong> — Franklin / Perry / parts of Jackson qualify; the SBA District Office (Chicago) processes applications. SIU SBDC (2024 IL SBDC of the Year) provides free application support.</li>
          <li><strong>Lock 2-3 US small-business manufacturer relationships BEFORE SAM registration.</strong> Pick one NAICS commodity lane. Without these locked, the bid process is hypothetical.</li>
          <li><strong>Apply to SBA Mentor-Protégé Program with Smith Hafeli</strong> as the mentor candidate. The 34% MPP-JV win rate vs single-digit unmentored is the leverage that makes this viable.</li>
          <li><strong>Bring $50-150k working capital</strong> + 12-18 month dry-spell tolerance. SBA Contract CAPLine + USDA B&amp;I (rural-eligible) + Live Oak Bank 7(a) cover the gap once contracts start flowing.</li>
          <li><strong>Treat it as 40+ hrs/week from month 1.</strong> Side-hustle commitment produces side-hustle outcomes (the $0-sales 50% of GSA Schedule holders).</li>
        </ol>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
          Sources (expert-verified second opinion): <a href="https://www.sba.gov/partners/contracting-officials/small-business-procurement/nonmanufacturer-rule" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA Nonmanufacturer Rule</a>; <a href="https://smallgovcon.com/sba-size-protests/back-to-basics-the-nonmanufacturer-rule/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SmallGovCon · NMR Basics &amp; Size Protests</a>; <a href="https://www.ecfr.gov/current/title-13/chapter-I/part-121/subpart-A/subject-group-ECFR0fca5207262de47/section-121.406" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>13 CFR 121.406 Nonmanufacturer Rule</a>; <a href="https://www.acquisition.gov/far/subpart-19.13" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>FAR Subpart 19.13 HUBZone</a>; <a href="https://blogs.usfcr.com/the-hubzone-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>USFCR · HUBZone 3% goal missed every year</a>; <a href="https://growfedbiz.com/the-ultimate-guide-to-win-federal-contracts-2/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Summit Insight / Judy Bradt · Ultimate Guide (50% of Schedule holders $0 sales)</a>; <a href="https://ncmahq.org/Web/Shared_Content/CM-Magazine/CM-Magazine-October-2024/The-SBA-s-All-Small-Mentor-Prot-g--Program--A-Bane-for-Most-Small-Businesses.aspx" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>NCMA · All Small Mentor-Protégé analysis (34% MPP-JV win rate)</a>; <a href="https://altline.sobanco.com/invoice-factoring/invoice-factoring-rates-explained/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>altLINE · Factoring Rates</a>; <a href="https://www.acquisition.gov/far/subpart-25.9" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>FAR Subpart 25.9 · Customs &amp; Duties (Chapter 98 government-contract exemption)</a>.
        </div>
      </div>
    </section>
  );
}

function MobilityJobAccessSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Mobility &amp; job access · transit reality vs the family-supporting jobs map
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Most of the family-supporting jobs identified above (Continental Tire 2nd-shift industrial maintenance, GD-OTS Marion shifts, healthcare facility shifts at Memorial / SIH / Marion VA, IBEW project work at remote sites) require transportation. Workers in Murphysboro / Du Quoin / Benton / West Frankfort who don&apos;t own a vehicle face a structural access problem if local transit doesn&apos;t reach their employer or doesn&apos;t run during their shift. This is the &quot;spatial mismatch&quot; constraint on training-program outcomes — a regional credential pipeline can&apos;t solve a transportation gap.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>Current transit operators serving LWA-25</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>JAX Mass Transit</strong> (formerly Jackson County Mass Transit District; rebranded Oct 2024) — operates Saluki Express (5 fixed routes) + SOAR (seasonal recreation), Saluki Night Shuttle, paratransit. <a href="https://ridejax.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>ridejax.com</a></li>
            <li><strong>Saluki Express fixed routes</strong>: Saluki (campus loop), Pyramid (campus + west Carbondale + airport + Murdale Shopping), Sahara (campus + east Carbondale + CCHS + Kroger/Walmart), Nile (south Carbondale + campus), and the <strong>Big Muddy Route (added recently)</strong> connecting University Mall + Amtrak station + Murphysboro Courthouse. <a href="https://www.ridesmtd.com/saluki-express/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Saluki Express route detail</a> · confirm current schedule with JAX Mass Transit before public stakeholder use.</li>
            <li><strong>RIDES Mass Transit District (RMTD)</strong> — serves Harrisburg, Marion, Robinson, Paris, Mount Carmel, Olney with fixed-route + 17-county demand-response. Transferred Saluki Express to JAX in 2024 due to funding cuts. <a href="https://www.ridesmtd.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>ridesmtd.com</a></li>
            <li><strong>Service hours</strong>: Mon-Fri + weekend 7:00am-7:30pm depending on route.</li>
            <li><strong>Federal funding</strong>: FTA Section 5311 (Rural Areas Formula) is the primary federal source. <a href="https://www.transit.dot.gov/rural-formula-grants-5311" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>FTA §5311</a>. Additional possible: Section 5339(b) Bus + Bus Facilities Competitive, 5339(c) Low-No Emissions.</li>
          </ul>
        </div>
        <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>The job-access gap — what current service covers vs doesn&apos;t</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>2nd-shift &amp; 3rd-shift work is not transit-accessible.</strong> Service closes 7:30pm. Continental Tire (Mt. Vernon), GD-OTS (Marion), and most regional manufacturing run 2nd shifts ending 10pm-midnight. Healthcare 3rd-shift starts at 11pm. Workers without vehicles can&apos;t take these shifts.</li>
            <li><strong>Cross-county work commutes are mostly demand-response.</strong> Murphysboro → Marion (~30min by car), Du Quoin → Carbondale (~25min), West Frankfort → Marion (~20min) work commutes rely on RMTD demand-response, not fixed-route. Same-day demand-response slots are limited.</li>
            <li><strong>Big Muddy Route is a real improvement</strong> — connects Amtrak station + University Mall + Murphysboro Courthouse. First fixed-route service genuinely tied to the train station.</li>
            <li><strong>Rural connectivity outside fixed-route corridors</strong> (Pomona, Makanda, Anna, Goreville, Vienna) is paratransit + demand-response only.</li>
            <li><strong>The fixed routes DO serve retail + employer destinations</strong> (Walmart, Kroger, airport, SIU campus, Memorial Hospital) — characterization of local transit as &quot;social-services only&quot; is incomplete; structural gaps are around shift timing + geographic edge + same-day demand-response capacity, not destination mix.</li>
          </ul>
        </div>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "oklch(96% 0.04 142)", border: "1px solid oklch(45% 0.16 142)33", borderLeft: "6px solid oklch(45% 0.16 142)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "oklch(35% 0.18 142)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What would fix the job-access gap
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Extend service hours to cover 2nd/3rd shift.</strong> The single highest-leverage transit fix. Requires FTA §5311 + state matching funds. Coordinate with major employers on shift-end timing.</li>
          <li><strong>Microtransit overlay for rural + cross-county trips.</strong> On-demand small-vehicle service via apps (TripShot, Via, RideCo) is the modern solution for low-density coverage. Multiple state RTAs have piloted this with FTA §5310 + §5311 funding.</li>
          <li><strong>Vanpool / employer-sponsored commute programs</strong> for major worksites (GD-OTS Marion, Continental Tire Mt. Vernon, Marion VA). Federal Vanpool Tax Benefit pre-tax, employer-sponsored. Reduces 1-vehicle-per-worker requirement.</li>
          <li><strong>Integrated Amtrak station + transit hub planning</strong> — Big Muddy Route is a start. Connect to Carbondale park-and-ride for rural commuters reaching the train.</li>
          <li><strong>Coordinate with employers + healthcare on shift transit</strong> — Marion VA and Memorial Carbondale could co-fund shift-specific transit between their facilities and worker neighborhoods.</li>
        </ul>
      </div>
      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Transit service info from <a href="https://ridejax.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>ridejax.com</a> + <a href="https://www.ridesmtd.com/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>ridesmtd.com</a> + <a href="https://en.wikipedia.org/wiki/Saluki_Express" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Saluki Express wiki</a> + <a href="https://news.siu.edu/2024/08/081224-saluki-express-bus-service-has-new-provider-routes.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SIU News 2024-08 service transition</a>. FTA program detail at <a href="https://www.transit.dot.gov/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>transit.dot.gov</a>.
      </div>
    </section>
  );
}

function StateEmployerWageBenchmarkSection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Public-sector wage benchmark · SIU + state agencies as a regional wage floor or ceiling?
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Public-sector employers — SIU (the largest single employer in LWA-25), the IL state
        agencies, IDOC, the federal/state prison system, and the Marion VA — set a
        meaningful share of the regional wage benchmark. Whether those public-employer
        wages function as a regional FLOOR (rates other employers must match to compete
        for talent) or a regional CEILING (rates that keep professional-class compensation
        from rising even as cost-of-living does) depends on role-specific compensation
        data that the workforce board should know but most board members don&apos;t.
        Every claim in this area must be backed by named data sources, not anecdote.
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "white", border: "1px solid #d8d2c4", borderRadius: 6 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>How to verify role-specific public-sector pay (without making accusations)</div>
        <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
          <li><strong><a href="https://salaries.bettergov.org/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BetterGov Illinois Public Salaries Database</a></strong> — search by employer + role + year. Returns individual + median compensation for SIU, IL DOA, IL DOC, IL DHS, IL DCEO, etc. This is public-record FOIA-disclosed data, not third-party hearsay.</li>
          <li><strong><a href="https://www.bls.gov/oes/current/oes_16060.htm" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>BLS OES Carbondale-Marion MSA wage tables</a></strong> — private + public combined median wage by detailed occupation (SOC code). Cross-reference SIU classifications against private-sector comparators in the same MSA.</li>
          <li><strong>SIU Civil Service Council bargaining-unit contracts</strong> + SIU&apos;s annual budget filings (public) — give the SIU side of the wage story for non-faculty positions.</li>
          <li><strong>Federal Pay Schedule (GS / WG) for Marion VA + federal prisons</strong> — published at <a href="https://www.opm.gov/policy-data-oversight/pay-leave/salaries-wages/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>opm.gov</a>. GS-1 through GS-15 rates with locality-pay adjustment for the Carbondale Rest of US locality area.</li>
        </ul>
      </div>

      <div style={{ marginBottom: 16, padding: 14, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <strong>Why the benchmark matters strategically:</strong> when a region&apos;s
        largest employers cluster at the public-sector compensation curve, the
        market-wage curve for similar roles in private employers tends to anchor to that
        public level — both up and down. If the workforce board recruits private
        family-supporting employers (data center operators, manufacturing reshoring,
        federal-contractor primes), those employers will benchmark THEIR offers against
        what SIU + the state pays for analogous roles. If public-sector compensation has
        been compressed below regional cost-of-living growth over a decade-plus window,
        the entire regional private-sector market for those occupations is anchored too
        low — and individual employers struggle to compete with coastal-metro counterparts
        for talent even when their local labor budget is rationally generous.
        <strong> The strategic ask isn&apos;t to attack SIU or state agencies — it&apos;s
        to make the wage-benchmark dynamic visible and to factor it into private-employer
        recruitment math.</strong>
      </div>

      <div style={{ marginBottom: 16, fontSize: 11, color: "#7a756b", lineHeight: 1.5 }}>
        Sources: BetterGov Illinois Public Salaries Database is BGA Foundation&apos;s aggregated FOIA-disclosed dataset; BLS OES MSA wage tables are US Bureau of Labor Statistics; OPM GS / WG schedules are the federal pay system. Verify any specific role-level comparison against these sources directly before using a public-sector wage figure in a board presentation.
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
        Federal contract dollars in LWA-25 · $812.8M, 95.6% to one prime
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        Federal contract dollars to the 5-county LWA over the last {tr.lookback_months} months:
        <strong> {formatM(tr.total_dollars)}</strong>. One recipient holds 95.6% of the dollars.
        That is normal for this kind of data. Ammunition contracts are large per job, and
        one Marion plant does most of that work. The local economy does not depend on
        that one company — QCEW counts roughly 77,000 covered jobs across 11 NAICS
        supersectors. But the federal-contracting channel does run mostly through one
        operator. That gives the workforce board one concentrated point of contact for
        CBA / apprenticeship / supplier-development talks.
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
            <strong>What the dashboard already did:</strong> the table above identifies every SDVOSB recipient by name, dollar amount, certification source, and {`{LOCAL · Marion IL}`} vs {`{OUT-OF-REGION · state}`} tag. Smith Hafeli (Marion IL) is the only LOCAL SDVOSB; the rest are FL / KY / NC.
          </div>
          <div style={{ marginBottom: 4 }}>
            <strong>Your residual steps</strong> (the dashboard cannot self-execute these):
          </div>
          <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
            <li>Stand up an &quot;SDVOSB certification on-ramp&quot; with the regional{" "}
              <a href="https://www.sba.gov/local-assistance/find/?type=Veterans%20Business%20Outreach%20Center" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Veterans Business Outreach Center (VBOC)</a>{" "}
              — help local veterans apply for SBA SDVOSB certification + bid for Marion VA work
            </li>
            <li>Broker{" "}
              <a href="https://www.sba.gov/federal-contracting/contracting-assistance-programs/sba-mentor-protege-program" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SBA Mentor-Protégé</a>{" "}
              relationships pairing the out-of-region SDVOSBs in the table above (Above Group FL, Jett&apos;s KY, SDV Office NC) with local protégés so the work stays here
            </li>
            <li>The Marion-headquartered SDVOSB above is the proof-of-concept: $11.9M won in 24 months. The data shows there&apos;s no reason 5-10 more local SDVOSBs couldn&apos;t exist with the right certification support.</li>
          </ul>
        </div>
      )}

      {/* Community leverage callout */}
      <div style={{ marginTop: 20, padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          What the dashboard already did + your residual leverage
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Dashboard contributions (above):</strong> total federal contract obligations into LWA-25 ($812.8M / 24mo), top-1 + top-3 concentration percentages, recipient-by-recipient table with SBA certification status + LOCAL vs OUT-OF-REGION tags, NAICS-lane breakout via the supply-chain mapping below, sub-recipient names for the dominant lane. The diagnosis is done.
        </div>
        <div style={{ marginBottom: 4 }}>
          <strong>Your residual leverage (the human-only steps the dashboard cannot execute):</strong>
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Negotiate a Community Benefit Agreement (CBA)</strong> when a new federal-funded project lands — the concentration data above gives you the standing. Precedents: Intel Ohio, Amazon HQ2, Foxconn Wisconsin (revised). The CBA negotiation itself is human work; the dashboard surfaces the concentration evidence that justifies it.</li>
          <li><strong>Broker the dominant-recipient apprenticeship partnership.</strong> Federal contractors with prevailing-wage requirements are natural apprenticeship anchors. The skill ladders they consume (machinist, electrician, industrial maintenance, quality tech) are already mapped in the Training-to-Demand section above. Your step: schedule the meeting with GD-OTS HR + the regional training partners.</li>
          <li><strong>Broker Tier-2 supplier introductions</strong> for the out-of-region sub-recipients flagged in the supply-chain table below. The dashboard identified the lanes + the candidates; the human step is the procurement-team intro.</li>
          <li><strong>Sponsor local firms through SBA HUBZone / 8(a) / WOSB certification.</strong> Franklin / Perry / parts-of-Jackson qualify for HUBZone (per the SBA HUBZone map). The dashboard surfaced the gap (only 1 LOCAL SDVOSB vs 3 out-of-region); the human step is the certification on-ramp with VBOC + SBA District Office.</li>
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
        Labor force participation + not-in-labor-force · 75,950 working-age adults outside the count
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The headline unemployment rate only counts people <em>actively looking for work</em>.
        It misses every working-age person who has stopped looking, gone on disability, dropped
        into the cash/informal economy, or is otherwise &quot;not in the labor force.&quot;
        These three metrics carry the real story.
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

      {/* Not-in-labor-force decomposition — the population isn't homogeneous */}
      <div style={{ padding: 16, background: "#fef9eb", border: "1px solid #f0d98a", borderLeft: "6px solid oklch(45% 0.20 22)", borderRadius: 6, fontSize: 13, color: "#3d3a33", lineHeight: 1.55, marginBottom: 24 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1f1d18", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          The not-in-labor-force population is not homogeneous · "lost" vs. "self-employed hustler" are different demographics
        </div>
        <div style={{ marginBottom: 10 }}>
          The 75,950 working-age adults outside the count includes multiple distinct demographics. The harm-cascade framing further down captures one slice — workers churned through training pipelines who exit discouraged. <strong>It does NOT capture another meaningful slice: self-employed informal-economy workers who run their own income off the formal grid</strong> (construction contractors operating sole-proprietor, cash-paid side work, gray-market trades, real earners that don&apos;t show up in W-2 / QCEW data).
        </div>
        <div style={{ marginBottom: 10 }}>
          <strong>Census Bureau Nonemployer Statistics (NES) — the authoritative source on this slice:</strong>
        </div>
        <ul style={{ margin: "0 0 10px 18px", padding: 0, fontSize: 12.5, lineHeight: 1.55 }}>
          <li>US nonemployer businesses (sole proprietors with NO paid employees) grew <strong>72% from 2000–2021</strong> — from 16.5M to 28.5M. Employer businesses grew only 15% in the same period.</li>
          <li><strong>Rural states show HIGHER percentages of self-employed workers</strong> than urban states.</li>
          <li><strong>In rural areas the trend reverses the formal-economy trajectory:</strong> nonemployer businesses INCREASED while employer businesses DECREASED.</li>
          <li>Construction (NAICS 23) is one of the most common nonemployer-business sectors — direct match for the local pattern of independent tradesmen running construction subcontracting on their own.</li>
          <li>Per ACS class-of-worker breakdown: self-employed unincorporated + 1099-paid contractors are separately enumerated from W-2 wage workers; the IRS Schedule C filer count is the matched tax-data view.</li>
        </ul>
        <div style={{ marginBottom: 6 }}>
          <strong>Honest decomposition of the 75,950 not-in-LF population:</strong>
        </div>
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Retired</strong> — expected demographic; no harm-cascade interpretation.</li>
          <li><strong>Disabled / on SSDI</strong> — real Census category; no harm-cascade interpretation.</li>
          <li><strong>Enrolled students (SIU + JALC + Rend Lake)</strong> — SIU alone enrolls ~11,000 students who are working-age and may report "not in LF" in ACS.</li>
          <li><strong>Unpaid caregivers</strong> — mostly women raising young children (childcare gateway constraint — see Childcare section).</li>
          <li><strong>Discouraged formal-economy workers</strong> — the harm-cascade demographic; trained, washed out of placement, stopped looking.</li>
          <li><strong>Informal-economy participants / self-employed independent tradespeople</strong> — Census NES + Boston Fed informal-work-activity empirical pattern. Running construction subcontracting on their own, cash-paid trades, gray-market work. NOT &quot;lost&quot; — economically active, just outside the W-2/QCEW reporting grid.</li>
        </ul>
        <div style={{ marginTop: 10 }}>
          <strong>What this means for workforce-development planning:</strong> programs that assume the entire 75,950 want W-2 employment misallocate. A meaningful subset would benefit from MICROENTERPRISE SUPPORT (small-contractor licensing, business-formation help via the <a href="https://news.siu.edu/2024/04/043024-sius-small-business-development-center-named-illinois-sbdc-of-the-year.php" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>SIU SBDC (2024 Illinois SBDC of the Year)</a>, 1099-to-W2 bridge programs for those who&apos;d prefer formalization, capital-access through IL Treasurer&apos;s Microbusiness program) — not from the same training pipelines designed for displaced manufacturing workers seeking W-2 reentry. The harm-cascade framing applies to the discouraged-formal-economy slice; the informal-economy slice needs a different policy lever.
        </div>
        <div style={{ fontSize: 11, color: "#7a756b", marginTop: 8, lineHeight: 1.5 }}>
          Sources: <a href="https://www.census.gov/programs-surveys/nonemployer-statistics.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census Bureau Nonemployer Statistics (NES)</a> + <a href="https://www.census.gov/newsroom/press-releases/2025/2023-nonemployer-statistics.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Census 2023 NES press release</a> + <a href="https://farmdocdaily.illinois.edu/2025/06/nonemployer-businesses-and-the-geography-of-self-employment.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>farmdoc daily · Nonemployer Businesses and the Geography of Self-Employment</a> + <a href="https://www.choicesmagazine.org/choices-magazine/submitted-articles/nonemployer-businesses-are-increasing-in-number-in-rural-america" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Choices Magazine · Nonemployer Businesses Are Increasing in Number in Rural America</a> + <a href="https://www.bostonfed.org/-/media/Documents/Workingpapers/PDF/economic/cpp1413.pdf" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Boston Fed · Informal Work Activity in the United States (working paper)</a>.
        </div>
      </div>

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

async function fetchData(): Promise<PageData | null> {
  try {
    return (await getMantraconData()) as unknown as PageData;
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

function CountyTable({ d }: { d: PageData }) {
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
        {(() => {
          const regionTotal = mix.by_county.reduce((acc, c) => acc + c.total_employment, 0);
          return (
            <div style={{ background: "#f3efe4", border: "1px dashed #d8d2c4", borderRadius: 6, padding: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
                <h3 style={{ fontSize: 16, fontWeight: 600, color: "#5a564d", margin: 0 }}>LWA-25 · region</h3>
                <div style={{ fontSize: 12, color: "#7a756b" }}>5 counties combined</div>
              </div>
              <div style={{ fontSize: 12, color: "#5a564d", marginBottom: 12 }}>
                Total covered employment: <strong>{regionTotal.toLocaleString()}</strong>
              </div>
              <div style={{ fontSize: 13, color: "#3d3a33", lineHeight: 1.5 }}>
                Healthcare anchors every county (SIH 4,000+ jobs leads Jackson;
                Heartland + Marion VA + Good Samaritan + Marshall Browning + Pinckneyville
                Community + Franklin Hospital across the rest). Manufacturing
                concentrates in Jefferson (Continental Tire 3,667) and Williamson
                (Aisin Marion 2,000+ across Mfg/Electronics/Light Metals, plus GD-OTS).
                Construction strength in Jackson at $1,545/wk is driven by
                <strong> Big Muddy Solar </strong>(124 MW Arevon/Signal Energy; 250+
                IBEW 702 + IUOE 318 + LIUNA 773 workers, CoD end of 2026) layered on
                a steady IDOT-contractor baseline anchored by <strong>E.T. Simonds </strong>
                (Carbondale, 1946, IDOT Prequal #5550 — highways, bridges, dams,
                runways) plus outage-pay at Continental + Aisin + GD-OTS. Perry runs
                on Trade/Transport (warehouse, rail, IL DOC).
              </div>
            </div>
          );
        })()}
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

export default async function SouthernIllinoisPage() {
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
  // Hero stats only — no editorial verdict. Reader interprets the numbers.
  const lfprGap = data.labor_truth?.aggregate?.gap_lfpr_vs_state ?? null;
  const aggLfpr = data.labor_truth?.aggregate?.lfpr ?? null;
  const aggNotLF = data.labor_truth?.aggregate?.not_in_labor_force ?? null;
  const aggNotLFPct = data.labor_truth?.aggregate?.not_lf_pct ?? null;
  const renderedAt = data.ts.slice(0, 16).replace("T", " ") + " UTC";

  return (
    <html lang="en">
      <head>
        <DashboardHead title="Southern Illinois Region · Workforce + Economic Development Dashboard" />
      </head>
      <body>
        <div className="shell">
          <Topbar brand="Southern Illinois Region · Workforce + Economic Development" region="LWA-25" renderedAt={renderedAt} />

          {/* Hero — data-first; numbers and identifiers only, no verdict adjectives. */}
          <header className="hero">
            <div>
              <div className="eyebrow">LWA-25 · Five-county service area · Franklin · Jackson · Jefferson · Perry · Williamson</div>
              <h1 className="serif" style={{ fontFamily: '"IBM Plex Serif", Georgia, serif', fontSize: 56, fontWeight: 500, lineHeight: 1.04, margin: "18px 0 18px", letterSpacing: "-0.02em", color: "var(--ink)", textWrap: "balance" }}>
                Workforce + economic-development profile
              </h1>
              <p className="lead" style={{ fontSize: 17, lineHeight: 1.5, color: "var(--ink-2)", maxWidth: "58ch", margin: 0 }}>
                {ag.unemployment_rate_weighted != null && lfprGap != null ? (
                  <>
                    Weighted unemployment rate <b>{ag.unemployment_rate_weighted.toFixed(1)}%</b>. Labor-force participation <b>{aggLfpr?.toFixed(1) ?? "—"}%</b> ({lfprGap >= 0 ? "+" : ""}{lfprGap.toFixed(1)}pp vs Illinois). Sources cited inline; every section names its API endpoint and as-of date.
                  </>
                ) : (
                  <>Five-county Southern Illinois Workforce Development service area. Sources cited inline.</>
                )}
              </p>
            </div>
            <aside className="hero-side">
              <div className="hero-stat">
                <div className="n">
                  {ag.unemployment_rate_weighted != null ? ag.unemployment_rate_weighted.toFixed(1) : "—"}
                  <span style={{ fontSize: 18, color: "var(--ink-3)" }}>%</span>
                </div>
                <div className="label">Headline UE rate<br />weighted, 5 counties</div>
              </div>
              <div className="hero-stat">
                <div className={`n ${lfprGap != null && lfprGap <= -6 ? "neg" : lfprGap != null && lfprGap <= -3 ? "warn" : ""}`}>
                  {aggLfpr != null ? aggLfpr.toFixed(1) : "—"}
                  <span style={{ fontSize: 18, color: "var(--ink-3)" }}>%</span>
                </div>
                <div className="label">
                  Labor-force participation<br />
                  {lfprGap != null && (
                    <span className={`diff ${lfprGap < 0 ? "neg" : "pos"}`}>{lfprGap >= 0 ? "+" : ""}{lfprGap.toFixed(1)}pp vs Illinois</span>
                  )}
                </div>
              </div>
              <div className="hero-stat">
                <div className="n">{aggNotLF != null ? aggNotLF.toLocaleString() : "—"}</div>
                <div className="label">
                  Working-age, not in labor force<br />
                  {aggNotLFPct != null && (
                    <span className="diff">{aggNotLFPct.toFixed(1)}% of pop 16+</span>
                  )}
                </div>
              </div>
            </aside>
          </header>

          {/* Freshness strip — matches scaffold */}
          <div className="freshness">
            <div className="fresh-cell">
              <div className="k">BLS LAUS · labor market</div>
              <div className="v">Through {data.indicators?.crb_jackson_unemployment_rate?.date ?? "—"}</div>
              <div className="sub">refreshes monthly</div>
            </div>
            <div className="fresh-cell">
              <div className="k">BLS QCEW · industry mix</div>
              <div className="v">{data.industry_mix?.as_of_quarter ?? "—"}</div>
              <div className="sub">each quarter published ~7mo after it ends</div>
            </div>
            <div className="fresh-cell">
              <div className="k">Census ACS · labor utilization</div>
              <div className="v">{data.labor_truth?.year ?? "2023"} 5-year</div>
              <div className="sub">refreshes annually · Dec</div>
            </div>
            <div className="fresh-cell">
              <div className="k">USAspending · federal $</div>
              <div className="v">{data.business_opportunities?.totals?.lookback_months ?? 24}-month rolling</div>
              <div className="sub">refreshes continuously</div>
            </div>
          </div>

          {/* Sticky nav */}
          <nav className="nav">
            <span className="nav-label">Jump §</span>
            <a href="#sec-labor"><span className="num">01</span>Labor Market</a>
            <a href="#sec-labor-truth"><span className="num">02</span>True Picture</a>
            <a href="#sec-industry"><span className="num">03</span>Industry Mix</a>
            <a href="#sec-wage-benchmark"><span className="num">04</span>Wages</a>
            <a href="#sec-federal-money"><span className="num">05</span>Federal $</a>
            <a href="#sec-anchor"><span className="num">06</span>Anchor</a>
            <a href="#sec-roi"><span className="num">07</span>Training ROI</a>
            <a href="#sec-training"><span className="num">08</span>1A+2C Wage Test</a>
            <a href="#sec-travel-jobs"><span className="num">09</span>Travel Jobs</a>
            <a href="#sec-healthcare"><span className="num">10</span>Healthcare</a>
            <a href="#sec-childcare"><span className="num">11</span>Childcare</a>
            <a href="#sec-mobility"><span className="num">12</span>Mobility</a>
            <a href="#sec-housing"><span className="num">13</span>Housing</a>
            <a href="#sec-structural-constraints"><span className="num">14</span>Constraints</a>
            <a href="#sec-pirl"><span className="num">15</span>PIRL</a>
          </nav>

          {/* ═══════════════════════════════════════════════════════════════
              LWA-25 standardized report flow · §01-§19 sequential sections,
              modelled on the LWA-23 page architecture but carrying LWA-25-
              specific facts, strategy, and theory of change.
              ═══════════════════════════════════════════════════════════════ */}

          {/* §01 · Executive verdict · regional diagnosis */}
          <SectionHeader num="01" title="Executive verdict · regional diagnosis" />
          <section id="sec-labor" style={{ scrollMarginTop: 60 }}>
            <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 820, lineHeight: 1.6 }}>
              LWA-25 is the 5-county Southern Illinois workforce footprint (Franklin, Jackson, Jefferson, Perry, Williamson). The county-by-county labor market below is the first-pass diagnosis — UR, labor force, and the largest single employer per county. Subsequent sections (§02-§07) deepen the diagnosis, §03 declares the theory of change, and §04 segments the 5 counties into 5 distinct subregional archetypes with different best-fit interventions.
            </div>
            <CountyTable d={data} />
          </section>

          {/* §02 · Root causes · true labor picture */}
          <SectionHeader num="02" title="Root causes · true labor picture" />
          <div id="sec-labor-truth" style={{ scrollMarginTop: 60 }}>
            {data.labor_truth && <LaborTruthSection lt={data.labor_truth} />}
          </div>

          {/* §03 · Theory of change */}
          <SectionHeader num="03" title="Theory of change · anchor-concentration-at-risk + structural gateways" />
          <div id="sec-theory-of-change" style={{ scrollMarginTop: 60 }}>
            <Lwa25TheoryOfChangeSection />
          </div>

          {/* §04 · County / subregional strategy matrix */}
          <SectionHeader num="04" title="County / subregional strategy matrix · 5 archetypes, 5 interventions" />
          <div id="sec-county-strategy" style={{ scrollMarginTop: 60 }}>
            <Lwa25CountyStrategyMatrixSection />
          </div>

          {/* §05 · Labor-force + unemployment evidence */}
          <SectionHeader num="05" title="Labor-force + unemployment evidence · weighted LWA-25 trend" />
          {data.lwa_unemployment_series.length > 0 && (
            <section id="sec-lwa-ur" style={{ marginTop: 8, scrollMarginTop: 60 }}>
              <div style={{ fontSize: 13, color: "#5a564d", marginBottom: 12, maxWidth: 760 }}>
                Labor-force-weighted average across the 5 counties. Calculated from BLS LAUS monthly data — the same series each county council uses.
              </div>
              <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
                <URTrendChart series={data.lwa_unemployment_series} />
              </div>
            </section>
          )}

          {/* §06 · Industry mix · economic base */}
          <SectionHeader num="06" title="Industry mix · economic base by sector and county" />
          <div id="sec-industry" style={{ scrollMarginTop: 60 }}>
            {data.industry_mix && <IndustryMixSection mix={data.industry_mix} scope="the LWA-25 (5-county region)" />}
            {data.industry_mix && <IndustryMixByCountySection mix={data.industry_mix} />}
          </div>

          {/* §07 · Federal awards · GD-OTS concentration exposure */}
          <SectionHeader num="07" title="Federal awards · GD-OTS Marion concentration exposure" />
          <div id="sec-federal-money" style={{ scrollMarginTop: 60 }}>
            {data.top_federal_recipients && <FederalConcentrationSection tr={data.top_federal_recipients} />}
            <SupplyChainSubawardSection lanes={data.gdots_subaward_lanes} bulkLanes={data.gdots_subaward_lanes_bulk} />
            <BusinessLeadsSection b={data.business_opportunities} />
          </div>

          {/* §08 · City/town safety + LWA-25 town context score */}
          <SectionHeader num="08" title="City/town safety + LWA-25 town context score · 10-town composite" />
          <div id="sec-town-context" style={{ scrollMarginTop: 60 }}>
            <Lwa25TownContextScoreSection />
          </div>

          {/* §09 · Mobility / commute / transit */}
          <SectionHeader num="09" title="Mobility · commute + transit access" />
          <div id="sec-mobility" style={{ scrollMarginTop: 60 }}>
            <MobilityJobAccessSection />
          </div>

          {/* §10 · Childcare · gateway barrier */}
          <SectionHeader num="10" title="Childcare · the gateway barrier that makes ladders fail" />
          <div id="sec-childcare" style={{ scrollMarginTop: 60 }}>
            <ChildcareGatewaySection />
          </div>

          {/* §11 · Housing affordability */}
          <SectionHeader num="11" title="Housing affordability · stock, cost, corridor" />
          <div id="sec-housing" style={{ scrollMarginTop: 60 }}>
            <HousingAffordabilitySection />
          </div>

          {/* §12 · Healthcare anchors / workforce ladder */}
          <SectionHeader num="12" title="Healthcare anchors · workforce ladder + wage-suppression pattern" />
          <div id="sec-healthcare" style={{ scrollMarginTop: 60 }}>
            <HealthcareWorkforceSection />
          </div>

          {/* §13 · Wage benchmark · household reality check */}
          <SectionHeader num="13" title="Wage benchmark · supersector pay vs IL statewide" />
          <div id="sec-wage-benchmark" style={{ scrollMarginTop: 60 }}>
            <StateEmployerWageBenchmarkSection />
          </div>

          {/* §14 · Training ROI · credential ladders */}
          <SectionHeader num="14" title="Training ROI · credential ladders against the Jackson-Co 1A+2C bar" />
          <div id="sec-roi" style={{ scrollMarginTop: 60 }}>
            <TrainingROISection />
          </div>
          <div id="sec-training" style={{ scrollMarginTop: 60 }}>
            {data.training_alignment && (
              <TrainingAlignmentSection
                ta={data.training_alignment}
                industryMixAvailable={!!data.industry_mix?.top_supersectors?.length}
              />
            )}
          </div>
          <div id="sec-travel-jobs" style={{ scrollMarginTop: 60 }}>
            <TravelJobsSection />
          </div>

          {/* §15 · IL DCEO In-Demand Occupations + 1A/2C clearance */}
          <SectionHeader num="15" title="IL DCEO In-Demand Occupations · EDR 8 + 1A/2C wage clearance" />
          <div id="sec-dceo-demand" style={{ scrollMarginTop: 60 }}>
            <Lwa25DceoOccupationsSection />
          </div>

          {/* §16 · WIOA/PIRL accountability */}
          <SectionHeader num="16" title="WIOA/PIRL accountability · LWIA-25 targets + funding-driven critique + harm cascade" />
          <div id="sec-pirl" style={{ scrollMarginTop: 60 }}>
            <PirlOutcomesSection />
          </div>
          <div id="sec-funding-driven" style={{ scrollMarginTop: 60 }}>
            <FundingDrivenProgrammingSection />
          </div>
          <div id="sec-harm-cascade" style={{ scrollMarginTop: 60 }}>
            <HarmCascadeSection />
          </div>
          <div id="sec-structural-constraints" style={{ scrollMarginTop: 60 }}>
            <StructuralWorkforceConstraintsSection />
          </div>

          {/* §17 · Anchor attraction · supply-chain diversification */}
          <SectionHeader num="17" title="Anchor attraction · supply-chain diversification + data-center recruitment" />
          <div id="sec-anchor" style={{ scrollMarginTop: 60 }}>
            <AttractionPipelineSection />
          </div>

          {/* §18 · Action ladder · implementation priorities */}
          <SectionHeader num="18" title="Action ladder · implementation priorities the dashboard cannot self-execute" />
          <section style={{ marginTop: 8 }}>
            <h3 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
              Your residual moves — the human-only steps the dashboard cannot self-execute
            </h3>
            <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
              Everything above was auto-pulled, cross-referenced, scored, and surfaced by the dashboard.
              The four boxes below name the remaining steps that require a human in the room — a phone call,
              a negotiation, a vote, a policy decision. The data is already in your hand; these are the moves
              that turn the data into outcomes.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
              {[
                {
                  title: "Make the WIOA cohort-planning call",
                  body: "The Industry Mix + Training-to-Demand sections above already cross-reference QCEW supersector employment against credential pipelines and assign each ladder a verdict (PHANTOM / TRAVEL-WORK / WAGE-SUPPRESSED / FAMILY-SUPPORTING / SATURATED / OWNER-OP). Your residual: bias the next annual WIOA cohort plan toward the FAMILY-SUPPORTING + TRAVEL-WORK rows, away from the WAGE-SUPPRESSED + PHANTOM rows. That's a policy decision the dashboard surfaces but doesn't make.",
                },
                {
                  title: "Place the procurement intro call",
                  body: "The Supply-Chain Mapping section above already shows GD-OTS Marion's $406M sub-award pool by NAICS lane + out-of-region candidates + top sub-recipient names. Your residual: for each out-of-region lane with a local-firm candidate (per the local precision-shop inventory we're queuing), call GD-OTS Procurement and broker the Tier-2 introduction. The local SDVOSB profiled in the Federal Money Concentration section is the proof-of-concept.",
                },
                {
                  title: "Negotiate the next CBA",
                  body: "The Federal Money Concentration section above already documents the 95.6% top-1 share + the SDVOSB local-vs-out-of-region gap. Your residual: when the next federal-funded project lands (data-center prospect, new GD-OTS expansion, anchor-attraction win), use that data to negotiate a Community Benefit Agreement on local-hire + apprenticeship + supplier-development. Standing → leverage → CBA is human work; the standing is already in your hand.",
                },
                {
                  title: "Decide where the next relocation cohort lands",
                  body: (
                    <>
                      The page shows the bifurcation — Marion-Herrin-Carterville (Williamson Co.) is the newer-construction corridor with safer crime rates; Carbondale + Murphysboro (Jackson Co.) carry SIU and the federal-money concentration but pair with older housing stock and elevated crime. <strong>Your residual decision:</strong> whether the next federal-retiree / data-center / climate-migration cohort gets directed to the Marion corridor for inventory + safety, or paired with SIU graduate-retention housing in Carbondale&apos;s older stock. Companion pages:{" "}
                      <a href="/carbondale" style={{ color: "#1f5f8f", fontWeight: 600 }}>Carbondale →</a>{" "}
                      and{" "}
                      <a href="/murphysboro" style={{ color: "#1f5f8f", fontWeight: 600 }}>Murphysboro →</a>{" "}
                      share the Jackson County substrate;{" "}
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

          {/* §19 · Methodology + Known Limits */}
          <SectionHeader num="19" title="Methodology + Known Limits · source integrity + open caveats" />
          <div id="sec-known-limits" style={{ scrollMarginTop: 60 }}>
            <KnownLimitsSection />
          </div>

          <div className="sources" style={{ marginTop: 40, lineHeight: 1.6 }}>
            <b>Coverage:</b> LWA-25 = Franklin, Jackson, Jefferson, Perry, Williamson —
            the regional workforce-development board service area.{" "}
            <b>Sources:</b> County labor-market data — BLS LAUS via FRED. Federal contract
            awards — USAspending.gov (Treasury / OMB). SAM.gov for active solicitations.
            SBA HUBZone &amp; 8(a) program info from sba.gov.{" "}
            <b>Caveats:</b> BLS LAUS series are 1–2 months lagged. USAspending data reflects
            what agencies have reported — there is reporting lag, and prime-award
            place-of-performance does not capture subcontract flow.
          </div>

          <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
        </div>
      </body>
    </html>
  );
}
