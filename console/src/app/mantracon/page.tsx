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

function ChildcareGatewaySection() {
  return (
    <section style={{ marginTop: 40 }}>
      <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
      <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
        Childcare · the gateway constraint that determines what training outcomes mean
      </h2>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The 1-adult + 2-children Jackson Co. living wage is <strong>$46.76/hr</strong> not
        because food + rent require that much — the MIT Living Wage Calculator allocates{" "}
        <strong>$14,000-$22,000 per child per year</strong> for childcare in that household.{" "}
        <strong>Childcare cost is what makes most training ladders fail the 1A+2C test by
        design.</strong> Until single-parent or two-earner-with-children households can
        secure affordable, quality childcare, the family-supporting wage bar is structurally
        hard to clear for anyone except journey-level union trades and master-grower /
        cultivation-manager roles. This is the gateway constraint — not the training credentials.
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
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>What the workforce board can do</div>
          <ul style={{ margin: "0 0 0 18px", padding: 0, fontSize: 13, color: "#3d3a33", lineHeight: 1.65 }}>
            <li><strong>Co-locate childcare with training programs.</strong> Drop-in childcare at JALC / Rend Lake / Mantracon training sites materially lowers the barrier for parents enrolling in 12-24mo credentials.</li>
            <li><strong>Push employer-paired childcare benefits</strong> in CBA / community-engagement framing with major federal-contracting employers. On-site or stipend-based childcare costs the employer $200-400/wk and gains ~$3-5/hr in retained-worker effective wage.</li>
            <li><strong>Help local childcare providers become Smart Start grantees.</strong> Many small in-home providers in LWA-25 are eligible for the $90M Workforce Grant pool but don&apos;t apply. Technical-assistance pipeline through Mantracon + IDHS.</li>
            <li><strong>Frame childcare-worker positions as a career on-ramp.</strong> The credential ladder (CDA → Bachelor&apos;s in ECE → director) reaches family-supporting at the upper rungs. Same playbook as CNA → LPN → RN.</li>
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
        psychiatrists, certified nurse midwives, and behavioral-health clinicians into
        the region at competitive loan-repayment rates.
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
          { factor: "IL Data Center Investments Act", grade: "✓ STRONG", note: "Public Act 101-0031 — 20-year sales-tax exemption on equipment + property-tax abatement eligible. Eligibility floor per IL DCEO program page (dceo.illinois.gov/expandrelocate/incentives/datacenters.html): $250M minimum capital investment over 60 months, minimum 20 FTE at 120% of COUNTY MEDIAN WAGE, carbon-neutral OR green-building certification required. The 120%-of-county-median-wage requirement is a workforce-board WIN — any DC operator must pay above median to qualify. Underserved-area projects unlock an additional 20% construction-wage tax credit. File DCEO certification before any RFP arrives.", color: "oklch(45% 0.16 142)" },
          { factor: "Water (cooling)", grade: "✓ STRONG", note: "Crab Orchard NWR, Kinkaid Lake, Mississippi River access. Sufficient for all but the largest installations.", color: "oklch(45% 0.16 142)" },
          { factor: "Land cost", grade: "✓ STRONG", note: "Undervalued vs Northern Virginia, Phoenix, Columbus.", color: "oklch(45% 0.16 142)" },
          { factor: "Power cost — Ameren vs Egyptian Electric Cooperative (EECA) head-to-head", grade: "~ MODERATE", note: "Ameren IL published industrial rate ~$0.08-0.09/kWh. EECA does not publish a comparable industrial-class per-kWh tariff in the same machine-readable way (member-coops negotiate large-power deals bespoke; see eeca.coop/member-services/rate-schedules/). Typical rural-coop industrial rates run 1-2¢/kWh below IOU — call it ~$0.06-0.08/kWh expected range, subject to negotiation. EECA's wholesale supplier Southern Illinois Power Cooperative (SIPC) owns coal + natural-gas generation PHYSICALLY LOCATED in Williamson and Washington counties (inside the LWA-25 footprint), plus long-term contracts for IL solar (White County) + IL wind (Paxton). That's a 'local generation for local load' pitch with minimal transmission distance — Northern VA can't claim that. Neither can compete with NoVa $0.06 on a paper-rate basis, but the bespoke-deal latitude + local-generation story plus the IL Data Center Act sales-tax exemption changes the all-in math.", color: "oklch(48% 0.15 60)" },
          { factor: "Federal IRA Energy Communities adder", grade: "✓ STRONG", note: "Franklin and Perry counties are coal-closure tracts. Solar/wind/storage projects sited here get IRA §48 +10pp ITC bonus on top of 30% base. Use for behind-the-meter generation co-located with DC.", color: "oklch(45% 0.16 142)" },
          { factor: "Fiber diversity — the grant-but-no-coverage paradox", grade: "✗ WEAK", note: "Public broadband investment in Southern IL is large and verifiable. Delta Communications dba Clearwave Communications received $31.5M from NTIA's BTOP program + $11M IL state match ($42.5M total) for a 23-county middle-mile network connecting 232 community anchor institutions (NTIA grant filing, ntia.doc.gov). Recent IL state Connect Illinois rounds have added WK&T's $9.8M (Jackson + Union Cos.) and ProTek Communications' $51M (Franklin/Jackson/Johnson/Massac/Williamson/Union Cos.). BEAD adds another $1B+ in IL allocation. Coverage on paper has improved. But data-center-grade fiber diversity is a different problem these grants don't fully solve: hyperscale needs 3+ INDEPENDENT carriers with physically diverse routes; most LWA-25 enterprise-class footprint has 1-2 carriers, not 3+ with route diversity. Carriers present include AT&T, Frontier, Mediacom, Clearwave, WK&T, ProTek. NTIA's original Clearwave grant terms included an open-access interconnection requirement for smaller last-mile providers — small ISP operators who believe these conditions are not being honored should file complaints with the IL Office of Broadband (DCEO) and NTIA. The fix-up paths: (a) audit grant compliance (open-access conditions), (b) IL Century Network (ICN — state-owned middle-mile) as alternative wholesale source, (c) municipal / coop broadband authority creation, (d) IIJA middle-mile grants directed to public or cooperative entities rather than incumbents. This remains the single weakest scorecard line for hyperscale recruitment.", color: "oklch(45% 0.20 22)" },
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

      {/* === Viticulture / agri-tourism === */}
      <h3 style={{ fontSize: 18, fontWeight: 600, color: "#1f1d18", margin: "32px 0 8px 0" }}>
        Viticulture &amp; agri-tourism · regional asset, selective opportunity
      </h3>
      <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16, maxWidth: 760, lineHeight: 1.55 }}>
        The Shawnee Hills American Viticultural Area (AVA, designated December 2006 — the
        FIRST AVA in Illinois) spans Jackson + Union counties along a 40-mile wine trail
        with 12 active wineries (down from 15 at AVA designation). The industry contributes
        an estimated <strong>$126M/year to the regional economy with 150,000 annual visitors</strong>,
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
            { role: "Vineyard manager", wage: "$50-80k", note: "Larger established vineyards (Owl Creek, Blue Sky, Von Jakob, Pomona). Year-round. Family-supporting at the upper end.", training: "Hands-on apprenticeship + viticulture cert (VESTA / Highland CC) + 3-5yr in field" },
            { role: "Winemaker / cellar master", wage: "$55-90k (small ops); $90-150k+ (large)", note: "Limited positions — 1-2 per winery. Quality matters more than volume here.", training: "Enology degree (UC Davis, Cornell, MSU, or VESTA AAS pathway) + 5-10yr cellar experience" },
            { role: "Tasting-room / hospitality manager", wage: "$30-50k", note: "Not family-supporting at the upper end. Year-round at larger operations.", training: "Hospitality background + WSET wine credentials" },
            { role: "Value-add processing (case-good production, bottling, packaging)", wage: "$20-30/hr ($40-60k)", note: "Borderline family-supporting. Multi-winery shared facility would amortize. This is a real Mantracon project opportunity.", training: "JALC packaging / food-processing program (would need to be created)" },
            { role: "Agritourism / events ops", wage: "$30-55k", note: "Wedding venues, harvest festivals, multi-winery tour ops. Often owner-operated.", training: "Hospitality + small-business management" },
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
          <li><strong>Hospitality-tier training that respects the wage floor</strong> — if Mantracon does CNA-equivalent low-wage training for the wine-tourism industry, the operator&apos;s family-supporting mandate disqualifies it. Better Mantracon play: tier-up training (sommelier WSET 2/3, restaurant management, winery operations) that has a higher wage ceiling.</li>
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
            <li><strong>Worker progression</strong> — Budtender / cultivation tech → Assistant grower (up to ~\$55k) → Cultivation manager (up to ~\$120k) → Master grower (\$80-150k). The wage ceiling at journey-level positions is genuinely family-supporting.</li>
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
        Cannabis is a real, growing employer in Illinois — the broader hemp-derived cannabinoid industry employs ~13,500 workers statewide and pays ~\$545M annually in wages (<a href="https://themarijuanaherald.com/2025/12/illinois-hemp-industry-supports-nearly-13500-jobs-and-2-7-billion-in-revenue-analysis-finds/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Marijuana Herald, Dec 2025</a>). The local share is small but real. The credential ladder from JALC Horticulture AA → cultivation work → grower management is one of the few <em>2-year-degree</em> paths that ends in a family-supporting wage. The action items: (1) confirm whether JALC could add cannabis-specific elective modules under the IL Community College Cannabis Vocational Pilot framework, (2) when a new local facility is approved (e.g., the 2023 SuiteGreens LLC craft-grow in Carbondale, per <a href="https://thesouthern.com/news/local/company-hopes-to-bring-cannabis-craft-grow-facility-dispensary-to-carbondale/article_7e4b5fd2-3c60-526e-8c62-5a42ca995135.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Southern Illinoisan</a>), Mantracon coordinates pre-hire training pipelines.
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
            { role: "Cultivation manager", wage: "Up to $120k/yr", note: "Larger operations only. Strong family-supporting wage.", verdict: "FAMILY-SUPPORTING" },
            { role: "Master grower", wage: "$80-150k/yr", note: "1-2 per facility. 5-10 yr experience required.", verdict: "FAMILY-SUPPORTING" },
            { role: "Compliance / extraction tech", wage: "$45-80k/yr", note: "Technical credential roles. Family-supporting at upper end.", verdict: "SINGLE → FAMILY" },
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
        <ul style={{ margin: "0 0 0 18px", padding: 0 }}>
          <li><strong>Community-college cannabis vocational program</strong> — IL Dept of Ag licenses Community College Cannabis Vocational Pilot Programs (<a href="https://cannabis.illinois.gov/agencies/cannabis-idoa.html" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>cannabis.illinois.gov</a>). JALC or Rend Lake could apply. Cannabis cultivation + horticulture credentials + business operations.</li>
          <li><strong>Help local applicants navigate the next license rounds</strong> — Mantracon partnership with the IL Cannabis Business Development Fund (<a href="https://illinoisanswers.org/2023/10/19/illinois-cannabis-business-development-fund-craft-growers/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>Illinois Answers Project</a> reporting on barriers). The Cannabis Equity Program offers loans + technical assistance to social-equity applicants.</li>
          <li><strong>Local employment requirements in zoning approvals</strong> — when Carbondale or Marion approves a cannabis facility, the approval can include local-hiring + livable-wage commitments. Use the next SuiteGreens-style approval as precedent.</li>
          <li><strong>Adjacent industries</strong> — cannabis processing equipment, packaging, lab testing, security, compliance consulting all have higher-wage opportunity ceilings than retail/cultivation labor. Mantracon could front-load training for these niches.</li>
          <li><strong>Honest size-up</strong> — cannabis is a real industry but a small one for jobs at scale. IL hemp-derived cannabinoid industry employs ~13,500 statewide (<a href="https://themarijuanaherald.com/2025/12/illinois-hemp-industry-supports-nearly-13500-jobs-and-2-7-billion-in-revenue-analysis-finds/" target="_blank" rel="noopener noreferrer" style={{ color: "#1f5f8f" }}>The Marijuana Herald, Dec 2025</a>); LWA-25 share is small. Don&apos;t pitch cannabis as primary jobs anchor; pitch it as supplementary economic activity that should be allowed and supported on its own terms.</li>
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
            why_here: "Tulsa Remote documented impact: 4,000+ relocated, $878M economic impact, $36k cost-per-job vs $218k typical business incentive (6× more efficient, 4:1 benefit-cost ratio for existing residents). 70% of relocators stay past their initial obligation. LWA-25's amenity profile (Shawnee NF, wine trail, Amtrak via the new station, cheap housing, SIU community) is competitive with Tulsa / Topeka / Bentonville.",
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
            action: "Partnership between SIU Career Services + Mantracon + Carbondale + Marion Chambers. Build employer-graduate matching platform + offer relocation-style $5K stipend conditional on 2-year regional commitment. Apply for EDA Recompete grant.",
            sources: [
              { url: "https://www.eda.gov/funding/programs/recompete", label: "EDA Recompete Pilot (rural workforce program)" },
              { url: "https://siu.edu/", label: "Southern Illinois University Carbondale" },
            ],
          },
          {
            name: "Federal retiree / military veteran relocation pitch",
            fit: "STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "Target federal civilian retirees + veteran retirees seeking low cost-of-living retirement with healthcare access. They bring pension income (typically $40-100k+) and Medicare/VA healthcare demand that supports the regional health-sector workforce.",
            why_here: "Marion VA Medical Center is the existing healthcare anchor. SIH + Memorial Carbondale add capacity. LWA-25 cost-of-living is far below federal-retiree concentration cities. Veteran population already loves the region (per the Federal Money Concentration section — VA-driven economic flows dominate).",
            action: "Targeted marketing through Federal News Network, Military Times, VFW + American Legion networks. Carbondale + Marion Chambers partner with Marion VA to host quarterly retirement-relocation open houses.",
            sources: [
              { url: "https://www.marion.va.gov/", label: "Marion VA Medical Center" },
              { url: "https://www.opm.gov/policy-data-oversight/data-analysis-documentation/federal-employment-reports/", label: "OPM federal workforce statistics" },
            ],
          },
          {
            name: "Mid-career career-change relocation — coding bootcamp / trades retraining + lifestyle pitch",
            fit: "MODERATE-STRONG FIT",
            fit_color: "oklch(45% 0.16 142)",
            what: "35-50yo professionals leaving expensive metros seeking lower-COL location + career pivot. They self-fund a credential (coding bootcamp, IBEW pre-apprenticeship, RN program at JALC) while consuming local services and bringing remaining savings into the local economy.",
            why_here: "JALC offers the credential infrastructure (Agriculture-Horticulture AA, RN ADN, electrical, welding programs). IBEW Local 702 takes pre-apprentices. Living-cost gap vs SF/NYC/Seattle covers 12-24 months of credential training with no income.",
            action: "Marketing partnership between JALC + Mantracon + Chamber: 'Reset your career in Carbondale.' Target 30-50 enrollees/year. Bundle with the remote-worker incentive when graduates take remote jobs post-credential.",
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
          DRA money is materially under-applied-for by IL applicants — the political and grant-writing weight historically goes to MS/AR/LA counties. Mantracon partnering with DRA staff (delta.gov contact directory) to coordinate an annual IL-counties SEDAP cohort is the play.
        </div>
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
        Total federal contract obligations with place-of-performance in the 5-county
        LWA over the last {tr.lookback_months} months: <strong>{formatM(tr.total_dollars)}</strong>.
        Concentration on a single recipient is a natural consequence of how the data
        flows: ammunition manufacturing contracts are large dollar-per-job by industry
        nature, and one Marion-based facility happens to be the work locale for most
        of that spend. This is <em>not</em> a statement that the local economy depends on
        one company — QCEW shows roughly 77,000 covered jobs distributed across 11
        NAICS supersectors. It IS a statement that the federal-contracting channel
        most active in the region runs primarily through one operator, which gives
        the workforce board a concentrated point of engagement for CBA / apprenticeship
        / supplier-development conversations.
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

          <ChildcareGatewaySection />

          {data.training_alignment && (
            <TrainingAlignmentSection
              ta={data.training_alignment}
              industryMixAvailable={!!data.industry_mix?.top_supersectors?.length}
            />
          )}

          <TravelJobsSection />

          <HealthcareWorkforceSection />

          <AttractionPipelineSection />

          <HousingAffordabilitySection />

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
