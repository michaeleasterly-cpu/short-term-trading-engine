/**
 * Public market-health page — no auth required.
 * Renders macro indicators (VIX, yield curve, Sahm rule, CFNAI,
 * unemployment, inflation, etc.) from platform.macro_indicators with
 * a heuristic regime classification.
 *
 * Excluded from NextAuth middleware via the matcher in
 * src/middleware.ts.
 */
export const dynamic = "force-dynamic";
export const revalidate = 0;

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "https://console-api-production-4576.up.railway.app";

interface MarketHealth {
  ts: string;
  indicators: Record<string, { value: number; date: string }>;
  vix_series: Array<{ date: string; value: number }>;
  spy_series: Array<{ date: string; close: number }>;
  summary: { vol_regime: string; macro_regime: string; headline: string };
}

async function fetchMarketHealth(): Promise<MarketHealth | null> {
  try {
    const res = await fetch(`${API_BASE}/api/public/market-health`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as MarketHealth;
  } catch {
    return null;
  }
}

const INDICATOR_LABELS: Record<string, { label: string; unit: string; tone: (v: number) => "pos" | "neg" | "warn" | "neutral" }> = {
  vix:            { label: "VIX",                  unit: "",   tone: v => v < 15 ? "pos" : v < 20 ? "neutral" : v < 30 ? "warn" : "neg" },
  yield_curve:    { label: "Yield curve 10y-3mo",  unit: "%",  tone: v => v < -0.5 ? "neg" : v < 0 ? "warn" : "pos" },
  sahm_rule:      { label: "Sahm rule",            unit: "",   tone: v => v >= 0.5 ? "neg" : v >= 0.3 ? "warn" : "pos" },
  cfnai_ma3:      { label: "CFNAI 3-mo MA",        unit: "",   tone: v => v <= -0.7 ? "neg" : v <= -0.35 ? "warn" : "pos" },
  hy_spread:      { label: "HY OAS spread",        unit: "%",  tone: v => v > 7 ? "neg" : v > 5 ? "warn" : "pos" },
  credit_spread:  { label: "Credit spread",        unit: "%",  tone: v => v > 3 ? "neg" : v > 2 ? "warn" : "pos" },
  nfci:           { label: "Chicago Fed NFCI",     unit: "",   tone: v => v > 0.5 ? "neg" : v > 0 ? "warn" : "pos" },
  epu_index:      { label: "Policy uncertainty",   unit: "",   tone: v => v > 250 ? "neg" : v > 150 ? "warn" : "pos" },
  initial_claims: { label: "Initial claims",       unit: "k",  tone: v => v > 350 ? "neg" : v > 275 ? "warn" : "pos" },
  bullish_pct:    { label: "AAII bullish",         unit: "%",  tone: () => "neutral" },
  bearish_pct:    { label: "AAII bearish",         unit: "%",  tone: () => "neutral" },
  neutral_pct:    { label: "AAII neutral",         unit: "%",  tone: () => "neutral" },
  score:          { label: "Fear & Greed",         unit: "",   tone: v => v < 25 ? "neg" : v < 45 ? "warn" : v > 75 ? "warn" : "pos" },
};

function VixChart({ series }: { series: Array<{ date: string; value: number }> }) {
  if (!series.length) return null;
  const values = series.map(p => p.value);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 40);
  const range = max - min || 1;
  const pts = series.map((p, i) => {
    const x = (i / Math.max(1, series.length - 1)) * 780 + 10;
    const y = 240 - ((p.value - min) / range) * 220;
    return `${x},${y}`;
  }).join(" ");
  // Threshold lines at 15 / 20 / 30
  const lineY = (v: number) => 240 - ((v - min) / range) * 220;
  return (
    <svg viewBox="0 0 800 250" preserveAspectRatio="none" className="w-full h-[240px]">
      {[15, 20, 30].map(t => (
        <g key={t}>
          <line x1="0" y1={lineY(t)} x2="800" y2={lineY(t)} stroke="var(--line)" strokeWidth="0.5" strokeDasharray="4 4" />
          <text x="6" y={lineY(t) - 3} fill="var(--ink-3)" fontSize="10" fontFamily="monospace">{t}</text>
        </g>
      ))}
      <polyline fill="none" stroke="var(--accent)" strokeWidth="1.5" points={pts} />
    </svg>
  );
}

export default async function MarketHealthPage() {
  const data = await fetchMarketHealth();
  return (
    <html lang="en">
      <head>
        <title>Market Health · STE</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
        <style>{`
          :root {
            --bg: oklch(13% 0.005 60); --bg-1: oklch(15% 0.006 60); --bg-2: oklch(18% 0.006 60); --bg-3: oklch(22% 0.006 60);
            --panel: oklch(16% 0.005 60); --panel-hd: oklch(19% 0.006 60);
            --line: oklch(28% 0.004 60); --ink: oklch(92% 0.005 75); --ink-2: oklch(78% 0.005 75); --ink-3: oklch(58% 0.005 75); --ink-4: oklch(40% 0.005 75);
            --pos: oklch(72% 0.16 142); --neg: oklch(67% 0.20 22); --warn: oklch(78% 0.15 78); --accent: oklch(74% 0.16 60);
          }
          html, body { background: var(--bg); color: var(--ink); font-family: "IBM Plex Sans", sans-serif; font-size: 12.5px; margin: 0; }
          .mono { font-family: "JetBrains Mono", monospace; font-variant-numeric: tabular-nums; }
          .hairline { border: 1px solid var(--line); }
          .hairline-b { border-bottom: 1px solid var(--line); }
          .eyebrow { font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); }
        `}</style>
      </head>
      <body style={{ minHeight: "100vh", background: "var(--bg)" }}>
        <div style={{ maxWidth: 1200, margin: "0 auto", padding: "32px 24px" }}>
          <div className="hairline-b" style={{ paddingBottom: 18, marginBottom: 24 }}>
            <div className="eyebrow">PUBLIC SNAPSHOT</div>
            <h1 style={{ fontSize: 28, fontWeight: 500, letterSpacing: "-0.01em", margin: "4px 0 0 0", color: "var(--ink)" }}>Market Health</h1>
            <div style={{ marginTop: 6, fontSize: 12.5, color: "var(--ink-3)" }}>
              {data ? (
                <span>{data.summary.headline} · macro snapshot as of <span className="mono">{data.ts.slice(0, 16).replace("T", " ")} UTC</span></span>
              ) : (
                <span style={{ color: "var(--neg)" }}>(market-health endpoint unreachable)</span>
              )}
            </div>
          </div>

          {data && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 8, marginBottom: 24 }}>
                {Object.entries(data.indicators).map(([key, v]) => {
                  const spec = INDICATOR_LABELS[key];
                  if (!spec) return null;
                  const tone = spec.tone(v.value);
                  const color = tone === "pos" ? "var(--pos)" : tone === "neg" ? "var(--neg)" : tone === "warn" ? "var(--warn)" : "var(--ink)";
                  return (
                    <div key={key} className="hairline" style={{ background: "var(--panel)", padding: "10px 12px" }}>
                      <div className="eyebrow" style={{ marginBottom: 4 }}>{spec.label}</div>
                      <div className="mono" style={{ fontSize: 20, color, lineHeight: 1.1 }}>
                        {v.value.toFixed(2)}{spec.unit}
                      </div>
                      <div className="mono" style={{ fontSize: 10, color: "var(--ink-3)", marginTop: 4 }}>{v.date}</div>
                    </div>
                  );
                })}
              </div>

              <div className="hairline" style={{ background: "var(--panel)", marginBottom: 24 }}>
                <div className="hairline-b" style={{ padding: "10px 14px", background: "var(--panel-hd)", display: "flex", alignItems: "center" }}>
                  <div style={{ color: "var(--ink-2)", fontSize: 12.5 }}>VIX — 180 day</div>
                  <div className="eyebrow" style={{ marginLeft: 12 }}>thresholds at 15 / 20 / 30</div>
                </div>
                <div style={{ padding: 16 }}>
                  <VixChart series={data.vix_series} />
                </div>
              </div>

              <div className="hairline" style={{ background: "var(--panel)" }}>
                <div className="hairline-b" style={{ padding: "10px 14px", background: "var(--panel-hd)" }}>
                  <div style={{ color: "var(--ink-2)", fontSize: 12.5 }}>Regime classification (heuristic)</div>
                </div>
                <table style={{ width: "100%", fontSize: 12.5 }}>
                  <tbody>
                    <tr><td className="eyebrow" style={{ padding: "10px 14px" }}>Volatility regime</td>
                        <td className="mono" style={{ padding: "10px 14px", color: data.summary.vol_regime === "crisis" ? "var(--neg)" : data.summary.vol_regime === "stress" ? "var(--warn)" : "var(--pos)" }}>{data.summary.vol_regime}</td></tr>
                    <tr><td className="eyebrow" style={{ padding: "10px 14px" }}>Yield-curve regime</td>
                        <td className="mono" style={{ padding: "10px 14px", color: data.summary.macro_regime === "inverted" ? "var(--warn)" : "var(--pos)" }}>{data.summary.macro_regime}</td></tr>
                    <tr><td className="eyebrow" style={{ padding: "10px 14px" }}>SPY (90d trail)</td>
                        <td className="mono" style={{ padding: "10px 14px", color: "var(--ink-2)" }}>
                          {data.spy_series.length ? `${data.spy_series[0].close.toFixed(2)} → ${data.spy_series[data.spy_series.length - 1].close.toFixed(2)}` : "—"}
                        </td></tr>
                  </tbody>
                </table>
              </div>

              <div style={{ marginTop: 24, fontSize: 10.5, color: "var(--ink-4)" }}>
                Data: <span className="mono">platform.macro_indicators</span> · <span className="mono">platform.prices_daily</span>.
                Indicators sourced from FRED. Regime thresholds match the reversion engine&apos;s internal classifier.
                This is a public snapshot of market structure; it is <em>not</em> investment advice.
              </div>
            </>
          )}
        </div>
      </body>
    </html>
  );
}
