/**
 * Public market-health page — written for non-experts. Indicators are
 * grouped into themed sections (Stock-market mood / Recession watch /
 * Credit & borrowing / Investor mood / Consumer mood). Each card has
 * a plain-English question, a traffic-light tone, and a "What is this?"
 * disclosure for the curious.
 *
 * Same data source: GET /api/public/market-health. No auth.
 */
import { DashboardHead, Topbar, DashboardFooter, DEFAULT_FOOTER_COLUMNS } from "@/components/dashboard-chrome";
import { getMarketHealth, type MarketHealth, type Dir } from "@/lib/market-data";

// Self-fetching: data comes straight from FRED / FMP / AAII / Shiller (no DB,
// no Railway console-api). Daily ISR; a Vercel cron revalidates at 00:00 ET.
export const revalidate = 86400;

async function fetchMarketHealth(): Promise<MarketHealth | null> {
  try {
    return await getMarketHealth();
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
  asOf?: string;   // indicator's as-of date (how old the data is)
  sub?: string;    // trend line (SHOW-set only), neutral, decoupled from tone
}

type Section = { id: string; title: string; subtitle: string; cards: Card[] };

// card.key → indicators[] key, for the as-of date + trend lookup.
const CARD_IND: Record<string, string> = {
  vix: "vix", fg: "score", aaii: "bullish_pct", sahm: "sahm_rule",
  cfnai: "cfnai_ma3", ic: "initial_claims", unrate: "unemployment_rate",
  yc: "yield_curve", hy: "hy_spread", cs: "credit_spread", nfci: "nfci",
  ffr: "fed_funds_rate", epu: "epu_index", umich: "michigan_sentiment",
};
// SHOW-set (markets-expert verdict): where rate-of-change is itself signal.
// Native units only (never % on spreads/zero-crossers); per-unit noise floor.
type Unit = "bps" | "pp" | "count" | "points";
const SHOW_TREND: Record<string, Unit> = {
  yc: "bps", hy: "bps", cs: "bps", unrate: "pp", ic: "count", umich: "points", aaii: "points",
};
const FLOOR: Record<Unit, number> = { bps: 15, pp: 0.1, count: 3000, points: 1 };

function fmtTrend(delta: number, dir: Dir, unit: Unit, windowLabel: string): string {
  const mag = unit === "bps" ? delta * 100 : delta; // spreads/curve stored in %
  if (Math.abs(mag) < FLOOR[unit]) return `little changed vs ${windowLabel}`;
  const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "▬";
  const sign = mag >= 0 ? "+" : "";
  const num =
    unit === "count" ? `${sign}${Math.round(mag / 1000)}k`
    : unit === "bps" ? `${sign}${Math.round(mag)} bps`
    : unit === "pp" ? `${sign}${mag.toFixed(1)} pp`
    : `${sign}${mag.toFixed(1)} pts`;
  return `${arrow} ${num} vs ${windowLabel}`;
}

function attachMeta(sections: Section[], d: MarketHealth): void {
  for (const s of sections) for (const c of s.cards) {
    if (c.key === "cape") { c.asOf = d.valuation?.cape?.date; continue; }
    if (c.key === "buffett") { c.asOf = d.valuation?.buffett?.date; continue; }
    if (c.key === "breadth" || c.key === "bear") { c.asOf = d.ts.slice(0, 10); continue; }
    const it = d.indicators[CARD_IND[c.key]];
    if (!it) continue;
    c.asOf = it.date;
    const unit = SHOW_TREND[c.key];
    if (unit && it.trend) {
      const t = fmtTrend(it.trend.delta, it.trend.dir, unit, it.trend.window);
      // AAII shows both bull & bear; the trend is the BULLISH %, so label it.
      c.sub = c.key === "aaii" ? `bulls ${t}` : t;
    }
  }
}

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
    detail: "VIX — the stock market's 'fear gauge'. Higher = bigger expected price swings over the next month. Source: CBOE Volatility Index methodology (Whaley 2009, 'Understanding VIX'); FRED series VIXCLS.",
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
      detail: "Composite of 7 inputs (momentum, breadth, options put/call, junk-bond demand, safe-haven demand, market volatility, stock-price strength). 0 = extreme fear, 100 = extreme greed. Source: CNN Business Fear & Greed Index methodology (published since 2012).",
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
    detail: "Sahm rule — when unemployment's 3-month moving average rises 0.5 percentage points above its 12-month low, a recession is usually starting. Currently below that line. Source: Sahm, Claudia (2019), 'Direct Stimulus Payments to Individuals', Federal Reserve; FRED series SAHMREALTIME.",
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
    detail: "Chicago Fed National Activity Index, 3-month moving average. Combines 85 monthly economic indicators into one number. Above 0 = above-average growth, below -0.7 = NBER-defined recession territory. Source: Federal Reserve Bank of Chicago methodology (Stock & Watson 1989 coincident-index lineage); FRED series CFNAIMA3.",
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
      detail: "Weekly first-time unemployment claims. Below 275k is healthy; sustained climbs past 350k often signal recessions. Source: US Department of Labor / Employment & Training Administration weekly release; FRED series ICSA (we use the 4-week MA, IC4WSA).",
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
    detail: "Headline unemployment rate (U-3). Historically: 3.5-5% is full employment, sustained > 5% signals a slowdown. Source: US Bureau of Labor Statistics, Current Population Survey, monthly; FRED series UNRATE.",
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
    detail: "10-year minus 2-year Treasury yields. When negative ('inverted'), it's historically been a reliable recession predictor 12-18 months later. Source: Estrella, A. & Mishkin, F. (1996), 'The Yield Curve as a Predictor of US Recessions', NY Fed Current Issues 2(7); FRED series T10Y2Y.",
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
    detail: "High-yield (junk) bond option-adjusted spread over Treasuries. How much extra interest junk-rated companies must pay vs the US government. Higher = more default fears. Source: ICE BofA US High Yield Master II OAS; FRED series BAMLH0A0HYM2.",
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
    detail: "BAA-rated (investment-grade) corporate bond yield minus 10-year Treasury. Wider = more credit stress in the safer end of the corporate market. Source: Moody's Seasoned BAA Corporate Bond Yield; FRED series BAA10Y. Theoretical basis: Gilchrist, S. & Zakrajšek, E. (2012), 'Credit Spreads and Business Cycle Fluctuations', AER 102(4).",
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
    detail: "Chicago Fed National Financial Conditions Index. Combines 105 financial-conditions indicators (money markets, debt and equity markets, traditional and shadow banking). Below 0 = easier-than-average; above 0 = tighter. Source: Brave, S. & Butters, R. A. (2011), Federal Reserve Bank of Chicago; FRED series NFCI. Adrian, Boyarchenko & Giannone (2019, AER) showed NFCI is the single best predictor of left-tail GDP growth.",
  };

  const ffr = get("fed_funds_rate");
  const ffrCard: Card | null = ffr === undefined ? null : {
    key: "ffr",
    question: "How high are interest rates?",
    value: `${ffr.toFixed(2)}%`,
    tone: "ok",
    explain: `The Federal Reserve's policy rate right now. Affects every other interest rate in the economy.`,
    detail: "Federal funds effective rate. The rate banks charge each other overnight; the Fed targets this to set monetary policy. Source: Federal Reserve Board H.15 Selected Interest Rates; FRED series DFF.",
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
      detail: "AAII Investor Sentiment Survey of individual investors: percent expecting stocks UP / DOWN in the next 6 months. Extreme readings (one side > 50%) sometimes mark turning points. Source: American Association of Individual Investors weekly survey, published since 1987.",
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
    detail: "Economic Policy Uncertainty Index. Counts newspaper articles mentioning economic policy uncertainty across 10 major US dailies. Above 200 is historically high. Source: Baker, S., Bloom, N. & Davis, S. (2016), 'Measuring Economic Policy Uncertainty', Quarterly Journal of Economics 131(4); FRED series USEPUINDXD. Caveat: news-attention-driven; can spike on political theater without translating to real economic damage.",
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
    detail: "University of Michigan Consumer Sentiment Index. Indexed to 100 in 1966-Q1. Tracks how consumers feel about the economy + their own finances; historically turned down 6-12 months before recessions (1973, 1980, 1990, 2001, 2008). Source: University of Michigan, Surveys of Consumers (monthly since 1952). Caveat: Curtin (2007, J. Economic Perspectives) shows sentiment is coincident-to-slightly-lagging, not a robust standalone recession predictor.",
  };

  // Valuation — how EXPENSIVE (not when). CAPE + Buffett confirm each other.
  const cape = d.valuation?.cape?.value;
  const capeCard: Card | null = cape == null ? null : {
    key: "cape",
    question: "How expensive are stocks vs their own history?",
    value: cape.toFixed(1),
    tone: cape >= 35 ? "stress" : cape >= 28 ? "watch" : cape >= 22 ? "ok" : "calm",
    explain:
      cape >= 35 ? "Extremely stretched — near or above the 2000/2021 peaks. Says nothing about timing." :
      cape >= 28 ? "Expensive vs history. A stretched-valuation caution, not a timing signal." :
                   "Valuations are within a more normal historical range.",
    detail: "CAPE (Shiller cyclically-adjusted P/E, a.k.a. P/E10): price ÷ the 10-year average of inflation-adjusted earnings, which smooths the business cycle. Historical mean ~17; readings above 30 are rare and have preceded weak forward 10-year returns. Source: Robert Shiller's data, re-priced on the live S&P 500. It tells you stocks are expensive — NOT when anything happens.",
  };
  const buffett = d.valuation?.buffett?.value;
  const buffettCard: Card | null = buffett == null ? null : {
    key: "buffett",
    question: "How big is the stock market vs the whole economy?",
    value: `${buffett.toFixed(0)}%`,
    tone: buffett >= 180 ? "stress" : buffett >= 140 ? "watch" : buffett >= 100 ? "ok" : "calm",
    explain:
      buffett >= 180 ? "Total market value is far above GDP — stretched even vs 2000/2021. Valuation, not timing." :
      buffett >= 140 ? "Market value is well above the size of the economy — on the expensive side." :
                       "Market value is in a more normal range vs the economy.",
    detail: "Buffett Indicator: total US stock-market value ÷ GDP. Warren Buffett (2001) called it “the best single measure of where valuations stand at any given moment.” Above ~100% is rich; current readings are historically extreme. It confirms CAPE — when BOTH are stretched, that is a stronger signal than either alone. Source: Wilshire-5000 total-market index ÷ FRED GDP, re-priced on the live index.",
  };

  // Breadth — PARTICIPATION / timing. Narrowing = a few names carry everyone.
  const br = d.breadth;
  const breadthCard: Card | null = !br ? null : {
    key: "breadth",
    question: "Is the whole market rising, or just a few giant stocks?",
    value: br.state === "narrow" ? "Narrow" : br.state === "broad" ? "Broad" : "Mixed",
    tone: br.state === "narrow" ? "watch" : "calm",
    explain: br.note,
    detail: `Breadth measures participation, not price. Equal-weight S&P 500 (RSP) vs cap-weight S&P 500 (^GSPC): over the past year ${br.conc_1y >= 0 ? "+" : ""}${br.conc_1y}pp, last 20 days ${br.trend_20d >= 0 ? "+" : ""}${br.trend_20d}pp. A negative number means cap-weight (the mega-cap / AI names) is beating the average stock — a shrinking handful is carrying the index (narrow). The 1-year figure captures the structural concentration; the 20-day figure is the recent direction. This is the participation/timing read valuation cannot give. Companion gauges: % of stocks above their 200-day average, new highs minus new lows, the advance-decline line.`,
  };

  return [
    {
      id: "market-mood",
      title: "Stock-market mood",
      subtitle: "How nervous or greedy is the stock market right now?",
      cards: cards(vixCard, fgCard, aaiiCard),
    },
    {
      id: "valuation",
      title: "Valuation — how expensive",
      subtitle: "How stretched are stocks vs their own history (CAPE) and vs the economy (Buffett Indicator)? These say the market is expensive — not when anything happens. Both stretched at once is a stronger signal than either alone.",
      cards: cards(capeCard, buffettCard),
    },
    {
      id: "breadth",
      title: "Breadth — who's participating",
      subtitle: "Is the whole market rising together, or is a shrinking handful of giant stocks carrying everyone else? This is the timing/participation read that valuation cannot give.",
      cards: cards(breadthCard),
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

// Section tiering — only hard recession + financial-stress data drive the
// top "weather" headline (see topHeadline). Soft / sentiment / policy data,
// valuation, and breadth each get their own treatment because they are
// news-driven or backdrop/timing signals, not standalone recession predictors
// (Curtin 2007; Baker-Bloom-Davis 2016).

interface TopHeadline {
  headline: string;
  subhead: string;
  tone: Tone;
  weatherWord: string;
  t1Stress: number;
  t1Watch: number;
  t1Total: number;
  t2Stress: number;
  t2Watch: number;
}

function topHeadline(sections: Section[]): TopHeadline {
  const cardsIn = (ids: string[]) => sections.filter(s => ids.includes(s.id)).flatMap(s => s.cards);
  // Hard recession + credit data drives the "weather". Valuation = backdrop
  // (how expensive, not when). Breadth = timing/participation caution.
  // Sentiment/policy = soft note. Each gets its OWN treatment so the new
  // valuation/breadth cards aren't mislabeled as "sentiment".
  const tier1 = cardsIn(["recession", "credit"]);
  const valuation = cardsIn(["valuation"]);
  const breadthCards = cardsIn(["breadth"]);
  const soft = cardsIn(["consumer"]);

  const t1Stress = tier1.filter(c => c.tone === "stress").length;
  const t1Watch  = tier1.filter(c => c.tone === "watch").length;
  const t1Total  = tier1.length;
  const t2Stress = soft.filter(c => c.tone === "stress").length;
  const t2Watch  = soft.filter(c => c.tone === "watch").length;

  // Valuation backdrop — stronger when BOTH CAPE and Buffett are stretched.
  const valStretched = valuation.filter(c => c.tone === "stress" || c.tone === "watch").length;
  const valNote = valStretched >= 2
    ? " Valuations are historically stretched on both CAPE and the Buffett Indicator — a backdrop that raises the stakes, though it says nothing about timing."
    : valStretched === 1
      ? " Valuations are on the expensive side — a backdrop, not a timing signal."
      : "";
  // Breadth caution — participation/timing the valuation tools can't give.
  const breadthNote = breadthCards.some(c => c.tone === "watch")
    ? " Market breadth is narrowing — a shrinking handful of stocks is carrying the gains, the kind of participation slip that can precede trouble."
    : "";
  const extra = valNote + breadthNote;

  // Soft-data note — sentiment/policy only (news-driven, weak predictor alone).
  const softNote = t2Stress > 0
    ? ` (${t2Stress} sentiment/policy flag${t2Stress > 1 ? "s" : ""} up, but soft data isn't a recession predictor on its own.)`
    : t2Watch > 0
      ? ` (Sentiment is mixed but hard data is clean.)`
      : "";

  const headline = "How is the US market today?";
  const base = { headline, t1Stress, t1Watch, t1Total, t2Stress, t2Watch };

  if (t1Stress >= 2) {
    return { ...base, weatherWord: "Stormy", subhead: `${t1Stress} of ${t1Total} recession or credit indicators are flashing red.${softNote}${extra}`, tone: "stress" };
  }
  if (t1Stress >= 1) {
    return { ...base, weatherWord: "Mixed", subhead: `${t1Stress} of ${t1Total} hard-data flags is red; the rest are calm.${softNote}${extra}`, tone: "watch" };
  }
  if (t1Watch >= 3) {
    return { ...base, weatherWord: "Cloudy", subhead: `${t1Watch} of ${t1Total} recession/credit indicators are yellow.${softNote}${extra}`, tone: "watch" };
  }
  if (t1Watch >= 1) {
    return { ...base, weatherWord: "Mostly clear", subhead: `${t1Watch} of ${t1Total} recession/credit flags is yellow; the rest are calm.${softNote}${extra}`, tone: "ok" };
  }
  return { ...base, weatherWord: "Clear", subhead: `All ${t1Total} recession/credit indicators are calm.${softNote}${extra}`, tone: "calm" };
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

  // Sparse date ticks — 4 evenly-spaced points INCLUDING the last index, so
  // the rightmost label is the latest date (the chart must visibly end "now",
  // not ~6 weeks short). Edge labels are anchored start/end to stay in-bounds.
  const TICK_COUNT = 4;
  const tickIdxs = Array.from({ length: TICK_COUNT }, (_, i) =>
    Math.round((i / (TICK_COUNT - 1)) * (series.length - 1))
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
      {tickIdxs.map((idx, i) => {
        const p = series[idx];
        if (!p) return null;
        const x = (idx / Math.max(1, series.length - 1)) * 780 + 10;
        const anchor = i === 0 ? "start" : i === tickIdxs.length - 1 ? "end" : "middle";
        return (
          <g key={idx}>
            <line x1={x} y1="220" x2={x} y2="226" stroke="#8a857c" strokeWidth="0.5" />
            <text x={x} y="245" fill="#5a564d" fontSize="11" fontFamily="ui-sans-serif" textAnchor={anchor}>
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
  if (data) attachMeta(sections, data);
  const top = data && sections.length ? topHeadline(sections) : null;

  const renderedAt = data ? data.ts.slice(0, 16).replace("T", " ") + " UTC" : "—";

  return (
    <html lang="en">
      <head>
        <DashboardHead title="US Market Health · Recession & credit gauges" />
      </head>
      <body>
        <div className="shell">
          <Topbar brand="US Market Health · Recession & credit gauges" region="US · National" renderedAt={renderedAt} />

          {!data && (
            <div style={{ padding: 40, textAlign: "center", color: "var(--ink-3)" }}>
              Sorry — the market-health data feed isn&apos;t responding right now. Try again in a minute.
            </div>
          )}

          {data && top && (
            <>
              <header className="hero">
                <div>
                  <div className="eyebrow">US · National · macro & market gauges · automatic, no opinions</div>
                  <h1 className="serif" style={{ fontFamily: '"IBM Plex Serif", Georgia, serif', fontSize: 56, fontWeight: 500, lineHeight: 1.04, margin: "18px 0 18px", letterSpacing: "-0.02em", color: "var(--ink)", textWrap: "balance" }}>
                    {top.headline}
                  </h1>
                  <p className="lead" style={{ fontSize: 17, lineHeight: 1.5, color: "var(--ink-2)", maxWidth: "58ch", margin: 0 }}>{top.subhead}</p>
                  <p style={{ fontSize: 13, color: "var(--ink-3)", margin: "12px 0 0" }}>
                    Updated {renderedAt} · refreshes daily at midnight Eastern · each gauge below shows its own &ldquo;as of&rdquo; date
                  </p>
                </div>
                <aside className="hero-side">
                  <div className="hero-stat">
                    <div className={`n ${top.tone === "stress" ? "neg" : top.tone === "watch" ? "warn" : top.tone === "calm" ? "pos" : ""}`}>
                      {top.t1Stress}
                      <span style={{ fontSize: 18, color: "var(--ink-3)" }}> / {top.t1Total}</span>
                    </div>
                    <div className="label">Hard-data flags red<br />recession + credit tier</div>
                  </div>
                  <div className="hero-stat">
                    <div className={`n ${top.t1Watch >= 3 ? "warn" : ""}`}>
                      {top.t1Watch}
                      <span style={{ fontSize: 18, color: "var(--ink-3)" }}> / {top.t1Total}</span>
                    </div>
                    <div className="label">Hard-data flags yellow<br />watch zone</div>
                  </div>
                  <div className="hero-stat">
                    <div className="n" style={{ fontSize: 22 }}>{top.weatherWord}</div>
                    <div className="label">Composite read<br />derived from flag counts</div>
                  </div>
                </aside>
              </header>

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
                        <div style={{ fontSize: 24, fontWeight: 500, color: TONE_COLOR[c.tone], lineHeight: 1.1, marginBottom: 6 }}>
                          {c.value}
                        </div>
                        {(c.sub || c.asOf) && (
                          <div style={{ fontSize: 12, color: "#7a756b", marginBottom: 10, display: "flex", gap: 12, flexWrap: "wrap", alignItems: "baseline" }}>
                            {c.sub && <span style={{ color: "#5a564e", fontVariantNumeric: "tabular-nums" }}>{c.sub}</span>}
                            {c.asOf && <span>as of {c.asOf}</span>}
                          </div>
                        )}
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
                <strong>Where the data comes from:</strong> every number is fetched live, with no
                database in between. Fast-moving market gauges (VIX, S&amp;P 500) come from a live
                quote feed so they are not a day behind; slow macro series (jobless claims,
                unemployment, yield curve, credit spreads, financial conditions, sentiment) from
                FRED (Federal Reserve Economic Data) and the Chicago Fed; CAPE from Shiller&apos;s
                data via multpl.com; the Buffett Indicator from the Federal Reserve Z.1
                flow-of-funds; AAII sentiment from the AAII weekly survey. The page refreshes
                once a day at midnight US Eastern, and each card shows its own &ldquo;as of&rdquo;
                date so you can see exactly how current each number is.
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
                <p style={{ margin: "0 0 6px 0", fontWeight: 600, color: "#3d3a33" }}>Valuation &amp; breadth:</p>
                <ul style={{ margin: "0 0 10px 18px", padding: 0 }}>
                  <li><strong>CAPE (Shiller P/E10)</strong> — Campbell, J. &amp; Shiller, R. (1988). <em>Stock Prices, Earnings, and Expected Dividends</em>. Journal of Finance 43(3); Shiller, R. (2000). <em>Irrational Exuberance</em>. Price ÷ the 10-year average of inflation-adjusted earnings. Current value via multpl.com, which re-prices Shiller&apos;s monthly data ~daily off the live S&amp;P 500. A valuation level, not a timing signal.</li>
                  <li><strong>Buffett Indicator</strong> — Buffett, W. &amp; Loomis, C. (2001). <em>Warren Buffett on the Stock Market</em>. Fortune. Total US stock-market value ÷ GDP. Computed from the Federal Reserve Z.1 Financial Accounts (corporate equities, <span style={{ fontFamily: "monospace" }}>NCBEILQ027S</span>) ÷ GDP. Confirms CAPE — both stretched at once is a stronger signal than either alone.</li>
                  <li><strong>Breadth (participation)</strong> — equal-weight S&amp;P 500 (<span style={{ fontFamily: "monospace" }}>RSP</span>) vs cap-weight S&amp;P 500, 20-day return. When cap-weight pulls ahead, a shrinking handful of mega-caps is carrying the index — the real-time timing/participation signal valuation cannot give. Companion gauges: % of stocks above their 200-day average, new highs−lows, advance-decline line. Cf. Zweig, M. (1986). <em>Winning on Wall Street</em> (breadth thrust).</li>
                  <li><strong>Fear &amp; Greed</strong> — a computed composite (market momentum vs its 125-day average, volatility vs its 50-day average, junk-bond demand via the high-yield spread, safe-haven demand). Mirrors CNN Business&apos;s Fear &amp; Greed methodology, computed directly rather than scraped; CNN&apos;s put/call and NYSE-breadth sub-indices are approximated.</li>
                  <li><strong>AAII sentiment</strong> — American Association of Individual Investors weekly Sentiment Survey (% bullish / bearish over the next 6 months), running since 1987. A contrarian indicator at extremes.</li>
                </ul>
                <p style={{ margin: "0 0 6px 0", fontWeight: 600, color: "#3d3a33" }}>Related composite recession / financial-stress indices:</p>
                <ul style={{ margin: "0 0 10px 18px", padding: 0 }}>
                  <li><strong>GS Bear Market Risk Indicator</strong> — Mueller-Glissmann, C., Wright, I., Kraïdy, A., Maguire, A. (2017). <em>The Bear Necessities</em>. Goldman Sachs Portfolio Strategy Research. The closest architectural ancestor.</li>
                  <li><strong>Chicago Fed NFCI</strong> — Brave, S. &amp; Butters, R. A. (2011). <em>Monitoring Financial Stability: A Financial Conditions Index Considering Real and Financial Indicators</em>. Federal Reserve Bank of Chicago.</li>
                  <li><strong>NBER Recession Probability</strong> — Chauvet, M. &amp; Piger, J. (2008). <em>A Comparison of the Real-Time Performance of Business Cycle Dating Methods</em>. JBES 26(1). Smoothed series on FRED as <span style={{ fontFamily: "monospace" }}>USRECP</span>.</li>
                  <li><strong>Vulnerable Growth</strong> — Adrian, T., Boyarchenko, N. &amp; Giannone, D. (2019). <em>Vulnerable Growth</em>. American Economic Review 109(4). Formalized financial-conditions → GDP-at-Risk.</li>
                  <li><strong>Conference Board LEI</strong> — The Conference Board, <em>Leading Economic Index</em>. 10-component composite, monthly.</li>
                </ul>
                <p style={{ margin: 0 }}>
                  This is a public snapshot of widely-watched market and economic gauges.
                  It is <strong>not</strong> investment advice, and reasonable people can disagree
                  about what the gauges mean.
                </p>
              </div>

              <DashboardFooter columns={DEFAULT_FOOTER_COLUMNS} />
            </>
          )}
        </div>
      </body>
    </html>
  );
}
