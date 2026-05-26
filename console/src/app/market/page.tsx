/**
 * Public market-health page — written for non-experts. Indicators are
 * grouped into themed sections (Stock-market mood / Recession watch /
 * Credit & borrowing / Investor mood / Consumer mood). Each card has
 * a plain-English question, a traffic-light tone, and a "What is this?"
 * disclosure for the curious.
 *
 * Same data source: GET /api/public/market-health. No auth.
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
  bear_score?: {
    score: number;
    raw: number;
    max_raw: number;
    breakdown: {
      sahm_rule: number;
      industrial_production: number;
      initial_claims: number;
      yield_curve: number;
      credit_spread: number;
      vix: number;
    };
  };
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

type Tone = "calm" | "ok" | "watch" | "stress";
const TONE_COLOR: Record<Tone, string> = {
  calm:   "oklch(55% 0.16 142)",
  ok:     "oklch(55% 0.16 142)",
  watch:  "oklch(58% 0.15 60)",
  stress: "oklch(55% 0.20 22)",
};
const TONE_WORD: Record<Tone, string> = {
  calm:   "Calm",
  ok:     "OK",
  watch:  "Watch",
  stress: "Stressed",
};

interface Card {
  key: string;
  question: string;
  value: string;
  tone: Tone;
  explain: string;
  detail: string;
}

type Section = { id: string; title: string; subtitle: string; cards: Card[] };

function buildSections(d: MarketHealth): Section[] {
  const ind = d.indicators;
  const get = (k: string) => ind[k]?.value;

  const cards = (...arr: Array<Card | null>): Card[] => arr.filter(Boolean) as Card[];

  // Bear Market Risk Score — composite recession-regime signal.
  // Architectural twin of the Goldman Sachs Bear Market Risk Indicator
  // (Mueller-Glissmann et al., 2017). 6 macro sub-scorers, raw 0-85
  // scaled to 0-100; ≥60 triggers the platform's defensive engine.
  const bearCard: Card | null = d.bear_score === undefined ? null : (() => {
    const bs = d.bear_score!;
    const t: Tone = bs.score >= 80 ? "stress" : bs.score >= 60 ? "watch" : bs.score >= 40 ? "ok" : "calm";
    const breakdownPills = Object.entries(bs.breakdown)
      .filter(([, v]) => v > 0)
      .map(([k]) => k.replace(/_/g, " "))
      .join(", ") || "none";
    return {
      key: "bear",
      question: "What's the composite bear-market risk?",
      value: `${bs.score} / 100`,
      tone: t,
      explain:
        t === "stress" ? "Deep recession territory — defensive posture warranted." :
        t === "watch"  ? "At or above the 60 activation threshold. Bear-market regime risk elevated." :
        t === "ok"     ? "Some flags up, but below the 60 activation threshold." :
                         "Bear-market risk is low — recession indicators are quiet.",
      detail: `Bear Market Risk Score — composite of 6 macro sub-scorers (Sahm rule, industrial production, initial claims, yield curve, credit spread, VIX). Sums to ${bs.raw}/${bs.max_raw} raw, scaled to ${bs.score}/100. Active sub-scorers: ${breakdownPills}. Architecture follows Goldman Sachs' Bear Market Risk Indicator framework (Mueller-Glissmann et al., 2017). See "Methodology & references" at the bottom of the page.`,
    };
  })();

  // Stock-market mood
  const vix = get("vix");
  const vixCard: Card | null = vix === undefined ? null : {
    key: "vix",
    question: "How nervous are stock investors?",
    value: vix.toFixed(1),
    tone: vix < 15 ? "calm" : vix < 20 ? "ok" : vix < 30 ? "watch" : "stress",
    explain:
      vix < 15 ? "Investors are pretty relaxed about the next month." :
      vix < 20 ? "Investors are calm-ish. No alarm bells." :
      vix < 30 ? "Investors are getting jumpy. Bigger price swings expected." :
                 "Investors are scared. Expect big up-and-down days.",
    detail: "VIX — the stock market's 'fear gauge'. Higher = bigger expected price swings over the next month.",
  };

  const fg = get("score");
  const fgCard: Card | null = fg === undefined ? null : (() => {
    const label = fg < 25 ? "Extreme Fear" : fg < 45 ? "Fear" : fg < 55 ? "Neutral" : fg < 75 ? "Greed" : "Extreme Greed";
    const t: Tone = fg < 25 ? "watch" : fg < 45 ? "ok" : fg > 75 ? "watch" : "calm";
    return {
      key: "fg",
      question: "How greedy or fearful is the market overall?",
      value: `${fg.toFixed(0)} — ${label}`,
      tone: t,
      explain:
        fg < 25 ? "Extreme fear in the market. Historically a buy signal more often than not." :
        fg > 75 ? "Extreme greed in the market. Historically a caution signal." :
                  "Mood is mixed — neither panic nor euphoria.",
      detail: "Composite of 7 inputs (momentum, breadth, options, junk bonds, safe-haven demand, volatility, put/call). 0 = extreme fear, 100 = extreme greed.",
    };
  })();

  // Recession watch
  const sahm = get("sahm_rule");
  const sahmCard: Card | null = sahm === undefined ? null : {
    key: "sahm",
    question: "Is a recession starting?",
    value: sahm.toFixed(2),
    tone: sahm >= 0.5 ? "stress" : sahm >= 0.3 ? "watch" : "calm",
    explain:
      sahm >= 0.5 ? "The recession-warning light just turned red." :
      sahm >= 0.3 ? "Some early signs of a slowdown. Worth watching." :
                    "Jobs market looks healthy — no recession signal.",
    detail: "Sahm rule — when unemployment rises 0.5 above its 12-month low, a recession is usually starting. Currently below that line.",
  };

  const cfnai = get("cfnai_ma3");
  const cfnaiCard: Card | null = cfnai === undefined ? null : {
    key: "cfnai",
    question: "How is the overall economy doing?",
    value: cfnai.toFixed(2),
    tone: cfnai <= -0.7 ? "stress" : cfnai <= -0.35 ? "watch" : "calm",
    explain:
      cfnai <= -0.7 ? "Economy looks weak — possibly shrinking." :
      cfnai <= -0.35 ? "Economy is slowing down a bit." :
                       "Economy is growing at a normal pace.",
    detail: "CFNAI 3-month average. Combines 85 monthly economic indicators into one number. Above 0 = above-average growth, below -0.7 = recession territory.",
  };

  const ic = get("initial_claims");
  const icCard: Card | null = ic === undefined ? null : (() => {
    const k = ic / 1000;
    const t: Tone = k > 350 ? "stress" : k > 275 ? "watch" : "calm";
    return {
      key: "ic",
      question: "Are people losing their jobs?",
      value: `${k.toFixed(0)}k / week`,
      tone: t,
      explain:
        t === "calm"  ? "Layoffs are low. Jobs market is healthy." :
        t === "watch" ? "Layoffs are picking up. Worth watching." :
                        "Layoffs are high — recession-level filings.",
      detail: "Weekly first-time unemployment claims. Below 275k is healthy; sustained climbs past 350k often signal recessions.",
    };
  })();

  const unrate = get("unemployment_rate");
  const unrateCard: Card | null = unrate === undefined ? null : {
    key: "unrate",
    question: "What's the unemployment rate?",
    value: `${unrate.toFixed(1)}%`,
    tone: unrate > 6 ? "stress" : unrate > 4.5 ? "watch" : "calm",
    explain:
      unrate > 6   ? "Unemployment is high — recession-level." :
      unrate > 4.5 ? "Unemployment is climbing — watch this." :
                     "Unemployment is low — labor market is healthy.",
    detail: "BLS headline unemployment rate. Historically: 3.5-5% is full employment, sustained > 5% signals a slowdown.",
  };

  // Yield curve goes here for plain-readers (rate context)
  const yc = get("yield_curve");
  const ycCard: Card | null = yc === undefined ? null : {
    key: "yc",
    question: "Are bond markets predicting a recession?",
    value: `${yc.toFixed(2)}%`,
    tone: yc < -0.5 ? "stress" : yc < 0 ? "watch" : "calm",
    explain:
      yc < -0.5 ? "Bonds are deeply upside-down — a strong recession warning." :
      yc < 0    ? "Bonds are slightly upside-down — a recession warning." :
                  "Bond markets see normal growth ahead.",
    detail: "10-year minus 2-year Treasury yields. When negative ('inverted'), it's historically been a reliable recession predictor 12-18 months later.",
  };

  // Credit & borrowing
  const hy = get("hy_spread");
  const hyCard: Card | null = hy === undefined ? null : {
    key: "hy",
    question: "Are risky companies in trouble?",
    value: `${hy.toFixed(1)}%`,
    tone: hy > 7 ? "stress" : hy > 5 ? "watch" : "calm",
    explain:
      hy > 7 ? "Risky borrowers under heavy stress. Default fears rising." :
      hy > 5 ? "Risky borrowers paying more to borrow. Some stress brewing." :
               "Risky borrowers are paying low extra rates — markets aren't worried.",
    detail: "Junk-bond spread over Treasuries. How much extra interest junk-rated companies must pay vs the US government. Higher = more default fears.",
  };

  const cs = get("credit_spread");
  const csCard: Card | null = cs === undefined ? null : {
    key: "cs",
    question: "Are investment-grade companies in trouble?",
    value: `${cs.toFixed(2)}%`,
    tone: cs > 3 ? "stress" : cs > 2 ? "watch" : "calm",
    explain:
      cs > 3 ? "Even safer corporates paying high premiums. Stress widespread." :
      cs > 2 ? "Corporate borrowing costs rising. Mild concern." :
               "Investment-grade borrowing is normal.",
    detail: "BAA-rated corporate vs 10-year Treasury yield. Wider = more credit stress in the safer end of the corporate market.",
  };

  const nfci = get("nfci");
  const nfciCard: Card | null = nfci === undefined ? null : {
    key: "nfci",
    question: "How easy is it to borrow money?",
    value: nfci.toFixed(2),
    tone: nfci > 0.5 ? "stress" : nfci > 0 ? "watch" : "calm",
    explain:
      nfci > 0.5 ? "Lending has seized up — financial stress." :
      nfci > 0   ? "Lending is getting tighter than normal." :
                   "Banks and markets are lending freely — easy money.",
    detail: "Chicago Fed National Financial Conditions Index. Combines 100+ borrowing-cost measures. Below 0 = easier-than-average; above 0 = tighter.",
  };

  const ffr = get("fed_funds_rate");
  const ffrCard: Card | null = ffr === undefined ? null : {
    key: "ffr",
    question: "How high are interest rates?",
    value: `${ffr.toFixed(2)}%`,
    tone: "ok",
    explain: `The Federal Reserve's policy rate right now. Affects every other interest rate in the economy.`,
    detail: "Federal funds effective rate (DFF). The rate banks charge each other overnight; the Fed targets this to set monetary policy.",
  };

  // Investor mood
  const bull = get("bullish_pct");
  const bear = get("bearish_pct");
  const aaiiCard: Card | null = (bull === undefined || bear === undefined) ? null : (() => {
    const net = bull - bear;
    const t: Tone = Math.abs(net) > 30 ? "watch" : "ok";
    return {
      key: "aaii",
      question: "Are everyday investors bullish or bearish?",
      value: `${bull.toFixed(0)}% bull · ${bear.toFixed(0)}% bear`,
      tone: t,
      explain:
        net > 20  ? "Way more bulls than bears — crowds are sometimes wrong at extremes." :
        net < -20 ? "Way more bears than bulls — pessimism often marks a turning point." :
                    "Bulls and bears about balanced — no extreme to fade.",
      detail: "AAII survey of individual investors: percent expecting stocks UP / DOWN in the next 6 months. Extreme readings (one side > 50%) sometimes mark turning points.",
    };
  })();

  const epu = get("epu_index");
  const epuCard: Card | null = epu === undefined ? null : {
    key: "epu",
    question: "How uncertain is policy news?",
    value: epu.toFixed(0),
    tone: epu > 250 ? "stress" : epu > 150 ? "watch" : "calm",
    explain:
      epu > 250 ? "Very high policy uncertainty in the news — markets often shaky." :
      epu > 150 ? "Elevated policy uncertainty. Markets watching headlines closely." :
                  "Calm news cycle — policy uncertainty is low.",
    detail: "Baker-Bloom-Davis Economic Policy Uncertainty index. Counts newspaper articles mentioning economic policy uncertainty. Above 200 is historically high.",
  };

  // Consumer mood (new with Michigan ingest)
  const umich = get("michigan_sentiment");
  const umichCard: Card | null = umich === undefined ? null : {
    key: "umich",
    question: "How do consumers feel about the economy?",
    value: umich.toFixed(1),
    tone: umich < 65 ? "stress" : umich < 80 ? "watch" : "calm",
    explain:
      umich < 65 ? "Consumers are gloomy — historically near-recession levels." :
      umich < 80 ? "Consumers are cautious. Worth watching." :
                   "Consumers feel pretty good about the economy.",
    detail: "University of Michigan Consumer Sentiment Index. Indexed to 100 in 1966. Historically turns down 6-12 months before recessions.",
  };

  return [
    {
      id: "market-mood",
      title: "Stock-market mood",
      subtitle: "How nervous or greedy is the stock market right now?",
      cards: cards(vixCard, fgCard, aaiiCard),
    },
    {
      id: "recession",
      title: "Recession watch",
      subtitle: "Are the warning lights flashing yet? The Bear Market Risk Score below combines all of these into one number our defensive engine watches.",
      cards: cards(bearCard, sahmCard, cfnaiCard, icCard, unrateCard, ycCard),
    },
    {
      id: "credit",
      title: "Credit & borrowing",
      subtitle: "How healthy is the plumbing that lets companies borrow money?",
      cards: cards(hyCard, csCard, nfciCard, ffrCard),
    },
    {
      id: "consumer",
      title: "Consumer & policy",
      subtitle: "How do people on Main Street and policy-watchers feel?",
      cards: cards(umichCard, epuCard),
    },
  ].filter(s => s.cards.length > 0);
}

// Section tiering — only hard recession + financial-stress data drive
// the top "weather" headline. Soft / sentiment / policy-uncertainty data
// gets its own subhead because it's news-driven and historically a poor
// recession predictor on its own (Curtin 2007; Baker-Bloom-Davis 2016).
const TIER1_SECTION_IDS = new Set(["recession", "credit"]);

function topHeadline(sections: Section[]): { headline: string; subhead: string; tone: Tone } {
  const tier1 = sections.filter(s => TIER1_SECTION_IDS.has(s.id)).flatMap(s => s.cards);
  const tier2 = sections.filter(s => !TIER1_SECTION_IDS.has(s.id)).flatMap(s => s.cards);

  const t1Stress = tier1.filter(c => c.tone === "stress").length;
  const t1Watch  = tier1.filter(c => c.tone === "watch").length;
  const t2Stress = tier2.filter(c => c.tone === "stress").length;
  const t2Watch  = tier2.filter(c => c.tone === "watch").length;

  // Soft-data note appended when sentiment/policy is elevated but hard
  // data is clean — calls it out without overweighting it.
  const softNote = t2Stress > 0
    ? ` (${t2Stress} sentiment/policy flag${t2Stress > 1 ? "s" : ""} up, but soft data isn't a recession predictor on its own.)`
    : t2Watch > 0
      ? ` (Sentiment is mixed but hard data is clean.)`
      : "";

  if (t1Stress >= 2) {
    return {
      headline: "Stormy",
      subhead: `${t1Stress} recession or credit indicators flashing red. Time to be careful.`,
      tone: "stress",
    };
  }
  if (t1Stress >= 1) {
    return {
      headline: "Mixed weather",
      subhead: `One recession/credit flag is red, but the rest of the hard data is OK.${softNote}`,
      tone: "watch",
    };
  }
  if (t1Watch >= 3) {
    return {
      headline: "Cloudy",
      subhead: `Several recession/credit indicators are yellow — worth watching.${softNote}`,
      tone: "watch",
    };
  }
  if (t1Watch >= 1) {
    return {
      headline: "Mostly sunny",
      subhead: `A few small worries on the recession/credit side, but the hard data is broadly healthy.${softNote}`,
      tone: "ok",
    };
  }
  // Tier-1 all clean — sunny regardless of soft-data noise.
  return {
    headline: "Sunny",
    subhead: `Recession and credit indicators are all clean.${softNote || " Markets and the economy look healthy."}`,
    tone: "calm",
  };
}

function VixChart({ series }: { series: Array<{ date: string; value: number }> }) {
  if (!series.length) return null;
  const values = series.map(p => p.value);
  const min = Math.max(0, Math.min(...values) - 2);
  const max = Math.max(...values, 40) + 2;
  const range = max - min || 1;
  const pts = series.map((p, i) => {
    const x = (i / Math.max(1, series.length - 1)) * 780 + 10;
    const y = 220 - ((p.value - min) / range) * 200;
    return `${x},${y}`;
  }).join(" ");
  const lineY = (v: number) => 220 - ((v - min) / range) * 200;

  // Sparse date ticks — 4 evenly-spaced points along the x-axis, formatted
  // as "Mon YYYY". Picked so the chart shows roughly a tick every ~6 weeks
  // for a 6-month window. Skipping the first index to avoid label collision
  // with the y-axis tone-line labels on the left.
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
      <line x1="0" y1={lineY(20)} x2="800" y2={lineY(20)} stroke="oklch(60% 0.15 60)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(20) - 5} fill="oklch(50% 0.15 60)" fontSize="11" fontFamily="ui-sans-serif">Watch line · 20</text>
      <line x1="0" y1={lineY(30)} x2="800" y2={lineY(30)} stroke="oklch(55% 0.20 22)" strokeWidth="1" strokeDasharray="4 4" />
      <text x="8" y={lineY(30) - 5} fill="oklch(50% 0.20 22)" fontSize="11" fontFamily="ui-sans-serif">Scared line · 30</text>
      <polyline fill="none" stroke="oklch(45% 0.16 220)" strokeWidth="2" points={pts} />
      {/* Sparse x-axis date labels — month + year only, 4 ticks across */}
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

export default async function MarketHealthPage() {
  const data = await fetchMarketHealth();
  const sections = data ? buildSections(data) : [];
  const top = data && sections.length ? topHeadline(sections) : null;

  return (
    <html lang="en">
      <head>
        <title>How is the market today? · STE</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
        <style>{`
          :root { color-scheme: light; }
          * { box-sizing: border-box; }
          html, body { margin: 0; padding: 0; background: #f7f5f1; color: #1f1d18; font-family: "IBM Plex Sans", system-ui, sans-serif; line-height: 1.5; }
          a { color: #1f5f8f; }
          .container { max-width: 1000px; margin: 0 auto; padding: 32px 20px 64px; }
        `}</style>
      </head>
      <body>
        <div className="container">
          {!data && (
            <div style={{ padding: 40, textAlign: "center", color: "#8a857c" }}>
              Sorry — the market-health data feed isn&apos;t responding right now. Try again in a minute.
            </div>
          )}

          {data && top && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src="/logo-icon.svg" alt="Packet Void Labs" width={28} height={28} />
                <div style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.08em", color: "#8a857c" }}>
                  How is the market today?
                </div>
              </div>
              <h1 style={{ fontSize: 56, fontWeight: 600, lineHeight: 1.05, margin: "8px 0 8px 0", color: TONE_COLOR[top.tone] }}>
                {top.headline}
              </h1>
              <div style={{ fontSize: 19, color: "#3d3a33", maxWidth: 720 }}>{top.subhead}</div>
              <div style={{ fontSize: 12, color: "#8a857c", marginTop: 8 }}>
                Updated {data.ts.slice(0, 16).replace("T", " ")} UTC — automatic, no opinions.
              </div>

              {sections.map(section => (
                <section key={section.id} style={{ marginTop: 40 }}>
                  <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", marginBottom: 16 }} />
                  <h2 style={{ fontSize: 22, fontWeight: 600, margin: "0 0 4px 0", color: "#1f1d18" }}>
                    {section.title}
                  </h2>
                  <div style={{ fontSize: 14, color: "#5a564d", marginBottom: 16 }}>{section.subtitle}</div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 16 }}>
                    {section.cards.map(c => (
                      <div key={c.key} style={{
                        background: "white",
                        border: "1px solid #d8d2c4",
                        borderLeft: `6px solid ${TONE_COLOR[c.tone]}`,
                        borderRadius: 6,
                        padding: 16,
                      }}>
                        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: TONE_COLOR[c.tone], marginBottom: 8 }}>
                          {TONE_WORD[c.tone]}
                        </div>
                        <div style={{ fontSize: 15, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>
                          {c.question}
                        </div>
                        <div style={{ fontSize: 24, fontWeight: 500, color: TONE_COLOR[c.tone], lineHeight: 1.1, marginBottom: 10 }}>
                          {c.value}
                        </div>
                        <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 8 }}>
                          {c.explain}
                        </div>
                        <details style={{ fontSize: 12, color: "#7a756b" }}>
                          <summary style={{ cursor: "pointer", userSelect: "none" }}>What is this?</summary>
                          <div style={{ marginTop: 6 }}>{c.detail}</div>
                        </details>
                      </div>
                    ))}
                  </div>
                </section>
              ))}

              <hr style={{ border: 0, borderTop: "1px solid #d8d2c4", margin: "40px 0 24px" }} />

              <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8, color: "#1f1d18" }}>
                Stock-market nerves over the last 6 months
              </h2>
              <div style={{ fontSize: 14, color: "#3d3a33", marginBottom: 16 }}>
                When the line crosses the yellow dotted line, investors are getting jumpy. Above red, they&apos;re scared.
              </div>
              <div style={{ background: "white", border: "1px solid #d8d2c4", borderRadius: 6, padding: 16 }}>
                <VixChart series={data.vix_series} />
              </div>

              <div style={{ marginTop: 40, fontSize: 12, color: "#8a857c", lineHeight: 1.6 }}>
                <strong>Where the data comes from:</strong> macro indicators are pulled from FRED
                (Federal Reserve Economic Data) and the Chicago Fed. Consumer-sentiment numbers
                from the University of Michigan via FRED. Stock prices from the daily-close data
                feed. Updated every weekday after the US market closes.
              </div>

              <div style={{ marginTop: 24, fontSize: 12, color: "#5a564d", lineHeight: 1.7 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#1f1d18", marginBottom: 8 }}>
                  Methodology &amp; references
                </div>
                <p style={{ margin: "0 0 10px 0" }}>
                  <strong>Bear Market Risk Score</strong> is a 6-component composite (Sahm rule, industrial production, initial claims,
                  yield curve, credit spread, VIX) summed to a raw 0-85 then scaled to 0-100. Activation threshold is 60.
                  Architecture follows the <em>Goldman Sachs Bear Market Risk Indicator</em> framework with adapted inputs.
                </p>
                <p style={{ margin: "0 0 6px 0", fontWeight: 600, color: "#3d3a33" }}>Component sources &amp; prior art:</p>
                <ul style={{ margin: "0 0 10px 18px", padding: 0 }}>
                  <li><strong>Sahm Rule</strong> — Sahm, Claudia (2019). <em>Direct Stimulus Payments to Individuals</em>. Federal Reserve. Triggers at unemployment 0.5pp above its 12m low.</li>
                  <li><strong>Yield curve</strong> — Estrella, A. &amp; Mishkin, F. (1996). <em>The Yield Curve as a Predictor of US Recessions</em>. NY Fed Current Issues 2(7).</li>
                  <li><strong>Credit spread (BAA-10Y)</strong> — Gilchrist, S. &amp; Zakrajšek, E. (2012). <em>Credit Spreads and Business Cycle Fluctuations</em>. American Economic Review 102(4).</li>
                  <li><strong>Industrial production (INDPRO)</strong> — Federal Reserve Statistical Release G.17. Standard ISM-PMI-equivalent indicator.</li>
                  <li><strong>Initial claims</strong> — US Department of Labor / Employment &amp; Training Administration weekly release. FRED series <span style={{ fontFamily: "monospace" }}>ICSA</span>.</li>
                  <li><strong>VIX</strong> — CBOE Volatility Index. Standard volatility-stress threshold ≥25.</li>
                </ul>
                <p style={{ margin: "0 0 6px 0", fontWeight: 600, color: "#3d3a33" }}>Related composite recession / financial-stress indices:</p>
                <ul style={{ margin: "0 0 10px 18px", padding: 0 }}>
                  <li><strong>GS Bear Market Risk Indicator</strong> — Mueller-Glissmann, C., Wright, I., Kraïdy, A., Maguire, A. (2017). <em>The Bear Necessities</em>. Goldman Sachs Portfolio Strategy Research. The closest architectural ancestor.</li>
                  <li><strong>Chicago Fed NFCI</strong> — Brave, S. &amp; Butters, R. A. (2011). <em>Monitoring Financial Stability: A Financial Conditions Index Considering Real and Financial Indicators</em>. Federal Reserve Bank of Chicago.</li>
                  <li><strong>NBER Recession Probability</strong> — Chauvet, M. &amp; Piger, J. (2008). <em>A Comparison of the Real-Time Performance of Business Cycle Dating Methods</em>. JBES 26(1). Smoothed series on FRED as <span style={{ fontFamily: "monospace" }}>USRECP</span>.</li>
                  <li><strong>Vulnerable Growth</strong> — Adrian, T., Boyarchenko, N. &amp; Giannone, D. (2019). <em>Vulnerable Growth</em>. American Economic Review 109(4). Formalized financial-conditions → GDP-at-Risk.</li>
                  <li><strong>Conference Board LEI</strong> — The Conference Board, <em>Leading Economic Index</em>. 10-component composite, monthly.</li>
                </ul>
                <p style={{ margin: "0 0 10px 0" }}>
                  <strong>Implementation</strong>: <span style={{ fontFamily: "monospace" }}>sentinel/plugs/setup_detection.py</span> (engine path, with full bear-steepener detector for the yield-curve component);
                  this page&apos;s reading uses a simplified inline computation in <span style={{ fontFamily: "monospace" }}>console-api/main.py</span> (binary inverted=15 pts on yield curve).
                </p>
                <p style={{ margin: 0 }}>
                  This is a public snapshot of widely-watched market and economic gauges.
                  It is <strong>not</strong> investment advice, and reasonable people can disagree
                  about what the gauges mean.
                </p>
              </div>
            </>
          )}
        </div>
      </body>
    </html>
  );
}
