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
  industry_mix?: IndustryMix;
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
  const tone = urTone(ag.unemployment_rate_weighted);
  const headline =
    ag.unemployment_rate_weighted == null ? "LWA-25 Workforce Snapshot" :
    ag.unemployment_rate_weighted < 4 ? "Strong regional labor market" :
    ag.unemployment_rate_weighted < 6 ? "Healthy regional labor market" :
    ag.unemployment_rate_weighted < 8 ? "Softening regional labor market" :
    "Stressed regional labor market";

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
            {ag.unemployment_rate_weighted != null && ag.labor_force != null ? (
              <>
                LWA-25 weighted-average unemployment <strong>{ag.unemployment_rate_weighted.toFixed(1)}%</strong> across a regional labor force of <strong>{fmtNum(ag.labor_force)}</strong>. Five counties: Franklin, Jackson, Jefferson, Perry, Williamson.
              </>
            ) : (
              "Five-county Southern Illinois Workforce Development Board service area (Franklin, Jackson, Jefferson, Perry, Williamson)."
            )}
          </div>
          <div style={{ fontSize: 12, color: "#8a857c", marginTop: 8 }}>
            Updated {data.ts.slice(0, 16).replace("T", " ")} UTC. Workforce metrics from BLS LAUS via FRED, monthly (1-2 month lag). Federal awards from USAspending.gov.
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

          {data.industry_mix && <IndustryMixSection mix={data.industry_mix} scope="the LWA-25 (5-county region)" />}

          <BusinessLeadsSection b={data.business_opportunities} />

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
