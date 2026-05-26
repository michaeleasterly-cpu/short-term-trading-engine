/**
 * Mock data for the operator console — placeholder until the FastAPI
 * backend ships. Shapes mirror what the real /api endpoints will return.
 * Pulled from design_handoff_trading_console/trading-console/data.jsx.
 */

export type EngineTone = "mom" | "rev" | "vec" | "sen" | "can";
export type EngineId = "momentum" | "reversion" | "vector" | "sentinel" | "canary";

export interface KpiTile {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg" | "warn" | "neutral";
}

export interface EngineCard {
  id: EngineId;
  name: string;
  tone: EngineTone;
  status: "GRADUATED" | "GATED" | "HEARTBEAT";
  kind: string;
  credibility: number;
  oosSharpe?: number;
  dsr?: number;
  positions?: number;
  capital?: string;
  alloc?: string;
  note?: string;
}

export const KPI_TILES: KpiTile[] = [
  { label: "Equity",        value: "$103,442", sub: "+1.51% today",  tone: "pos" },
  { label: "Day P&L",       value: "+$1,538",  sub: "+1.51%",         tone: "pos" },
  { label: "Unrealized",    value: "+$842",    sub: "open positions", tone: "pos" },
  { label: "YTD P&L",       value: "+$8,212",  sub: "+8.62%",         tone: "pos" },
  { label: "Cash",          value: "$24,118",  sub: "23.3% of NAV",   tone: "neutral" },
  { label: "Buying Power",  value: "$48,236",  sub: "2x margin avail",tone: "neutral" },
  { label: "Open Positions",value: "12",       sub: "across 4 eng",   tone: "neutral" },
  { label: "Trades Today",  value: "7",        sub: "1 blocked",      tone: "warn" },
];

export const ENGINE_CARDS: EngineCard[] = [
  { id: "momentum",  name: "Momentum",  tone: "mom", status: "GRADUATED", kind: "monthly cross-sectional", credibility: 78, oosSharpe: 1.24, dsr: 0.971, positions: 5, capital: "$24,100", alloc: "23.3%" },
  { id: "reversion", name: "Reversion", tone: "rev", status: "GRADUATED", kind: "intraday mean-reversion", credibility: 71, oosSharpe: 1.08, dsr: 0.961, positions: 3, capital: "$16,800", alloc: "16.2%" },
  { id: "vector",    name: "Vector",    tone: "vec", status: "GATED",     kind: "catalyst-driven momentum", credibility: 54, oosSharpe: 0.82, dsr: 0.918, positions: 2, capital: "$8,400",  alloc: "8.1%" },
  { id: "sentinel",  name: "Sentinel",  tone: "sen", status: "GRADUATED", kind: "defensive macro tilt",     credibility: 68, oosSharpe: 0.91, dsr: 0.952, positions: 2, capital: "$12,200", alloc: "11.8%" },
  { id: "canary",    name: "Canary",    tone: "can", status: "HEARTBEAT", kind: "end-to-end heartbeat",     credibility: 0,  note: "Non-graduating — platform liveness probe only." },
];

export const NAV_BADGES = {
  forensics: 2,
  lab: 1,
  ecr: 3,
  health: 4,
  digest: 1,
};

export const HOLDINGS = [
  { engine: "MOM", ticker: "AAPL", qty: 100, entry: 184.10, last: 186.42, pnl: "+$232", pnlPct: "+1.26%", wgt: "18.1%", held: "12d" },
  { engine: "MOM", ticker: "MSFT", qty: 25,  entry: 412.30, last: 418.55, pnl: "+$156", pnlPct: "+1.52%", wgt: "10.2%", held: "12d" },
  { engine: "REV", ticker: "NVDA", qty: 15,  entry: 880.10, last: 891.20, pnl: "+$167", pnlPct: "+1.26%", wgt: "13.0%", held: "2d" },
  { engine: "VEC", ticker: "TSLA", qty: 10,  entry: 218.40, last: 215.80, pnl: "-$26",  pnlPct: "-1.19%", wgt: "2.1%",  held: "5d" },
  { engine: "SEN", ticker: "TLT",  qty: 50,  entry: 93.20,  last: 94.40,  pnl: "+$60",  pnlPct: "+1.29%", wgt: "4.6%",  held: "31d" },
];

export const SIGNALS = [
  { engine: "MOM", ticker: "GOOGL", side: "LONG",  note: "monthly rebalance", strength: 0.82, time: "14:32 UTC" },
  { engine: "REV", ticker: "AMD",   side: "SHORT", note: "5d z-score = +2.1", strength: 0.71, time: "14:18 UTC" },
  { engine: "VEC", ticker: "PLTR",  side: "LONG",  note: "earnings catalyst — BLOCKED (credibility)", strength: 0.65, time: "13:55 UTC" },
];

export const AARS = [
  { engine: "MOM", ticker: "SPY",  side: "LONG",  exitReason: "take_profit",   dates: "Apr 12 → May 22", hold: "40d", qty: 25, prices: "$520 → $548", pnlPct: "+5.4%" },
  { engine: "REV", ticker: "META", side: "SHORT", exitReason: "tier2_target",  dates: "May 18 → May 22", hold: "4d",  qty: 8,  prices: "$478 → $466", pnlPct: "+2.5%" },
  { engine: "SEN", ticker: "GLD",  side: "LONG",  exitReason: "regime_change", dates: "Mar 02 → May 19", hold: "78d", qty: 30, prices: "$208 → $216", pnlPct: "+3.8%" },
];
