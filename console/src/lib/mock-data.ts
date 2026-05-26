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

export const NAV_BADGES = { forensics: 2, lab: 1, ecr: 3, health: 4, digest: 1 };

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

export const FORENSICS = [
  { id: "F-22-014", severity: "high", trigger: "drawdown_pct", engine: "vector", note: "rolling 30d DD -4.8%, 2σ over baseline", when: "2026-05-22 14:02 UTC" },
  { id: "F-22-009", severity: "med",  trigger: "loss_cluster", engine: "reversion", note: "4 consecutive losing AARs (avg hold 2d)", when: "2026-05-21 22:18 UTC" },
  { id: "F-20-001", severity: "low",  trigger: "outlier_loss", engine: "momentum", note: "single -3.1% on AAPL — within tail", when: "2026-05-20 16:35 UTC" },
];

export const SOURCE_HOLDS = [
  { source: "fmp_fundamentals", held: "2026-05-25 03:14", cycles: 3, reason: "rate-limited 429 > 3 retries", esc: "L2" },
  { source: "finnhub_insider",  held: "2026-05-25 06:02", cycles: 1, reason: "schema drift — 'sentiment' renamed", esc: "L1" },
];

export const RECENT_ESCALATIONS = [
  { when: "2026-05-25 03:18 UTC", type: "DATA", refid: "esc-1142", cls: "rate_limit",       open: true,  msg: "fmp_fundamentals 429-storm" },
  { when: "2026-05-24 19:55 UTC", type: "ENG",  refid: "esc-1141", cls: "credibility_drop", open: false, msg: "vector credibility 54 (was 62)" },
  { when: "2026-05-23 21:30 UTC", type: "DATA", refid: "esc-1140", cls: "schema_drift",     open: true,  msg: "finnhub_insider field rename" },
];

export const DAEMONS = [
  { daemon: "data-operations", lane: "data",     pid: "—",     uptime: "—",    last: "21:30 UTC daily", status: "READY",   role: "scheduled 15-stage data refresh" },
  { daemon: "engine-service",  lane: "engine",   pid: "12451", uptime: "13h",  last: "00:00:31 UTC",    status: "RUNNING", role: "poll DATA_OPERATIONS_COMPLETE; dispatch engine sweep + allocator (WEEKLY_FIRST_TRADING_DAY)" },
  { daemon: "lane-service",    lane: "data",     pid: "12452", uptime: "13h",  last: "12:55:58 UTC",    status: "RUNNING", role: "data-repair listener" },
  { daemon: "trade-monitor",   lane: "engine",   pid: "12453", uptime: "13h",  last: "13:02:38 UTC",    status: "RUNNING", role: "Alpaca trade_updates websocket" },
];

export const LAB_RUNS = [
  { id: "L-22-014", engine: "momentum",  candidate: "lab.mom_lookback_24mo", date: "2026-05-22", seed: 7421, duration: "8m22s", verdict: "SURVIVED", dsr: 0.971, sharpe: 1.31, credibility: 79, trials: 64, isolationViolations: 0, promotion_pending: true,  note: "12-stop walk-forward survives gate" },
  { id: "L-21-009", engine: "reversion", candidate: "lab.rev_zscore_5d",      date: "2026-05-21", seed: 9117, duration: "5m04s", verdict: "FAILED",   dsr: 0.918, sharpe: 0.71, credibility: 48, trials: 96, isolationViolations: 0, promotion_pending: false, note: "credibility < 60 in last 2 windows" },
];

export const ECR_QUEUE = [
  { id: "ECR-217", kind: "MODIFY", engine: "vector",    action: "raise credibility floor",   submitted_by: "operator", submitted_when: "2026-05-25 03:30 UTC", summary: "Bump capital_gate min_credibility from 50 → 60 on vector to align with reversion/momentum.", diff: "-min_credibility=50\n+min_credibility=60", lab_dossier: "L-21-007" },
  { id: "ECR-216", kind: "ADD",    engine: "momentum",  action: "lab.mom_lookback_24mo",     submitted_by: "lab",      submitted_when: "2026-05-22 14:12 UTC", summary: "Promote 24mo lookback variant from Lab to PAPER. DSR 0.971 / credibility 79 / 64 trials.", diff: "+ENGINE_LOOKBACK_DAYS=504\n+CANDIDATE='lab.mom_lookback_24mo'", lab_dossier: "L-22-014" },
  { id: "ECR-215", kind: "RETIRE", engine: "vector",    action: "retire pre-2026-04 ledger", submitted_by: "operator", submitted_when: "2026-05-20 12:00 UTC", summary: "Archive vector AARs older than 2026-04-01; ledger compaction.", diff: "+archive_before_date='2026-04-01'", lab_dossier: null },
];

export const ECR_DECIDED = [
  { decided: "2026-05-24 19:50 UTC", kind: "MODIFY", engine: "reversion", action: "tighten signal_threshold", verdict: "APPROVED", diff: "-thr=2.0/+thr=2.25" },
  { decided: "2026-05-23 14:20 UTC", kind: "ADD",    engine: "sentinel",  action: "add TLT to defensive basket", verdict: "APPROVED", diff: "+basket+=['TLT']" },
  { decided: "2026-05-21 16:00 UTC", kind: "RETIRE", engine: "sigma",     action: "RETIRE sigma engine",     verdict: "APPROVED", diff: "+lifecycle_state='RETIRED'" },
];

export const ENGINE_LIFECYCLE = {
  LAB:    [{ id: "carver", name: "Carver" }],
  PAPER:  [{ id: "momentum", name: "Momentum" }, { id: "reversion", name: "Reversion" }, { id: "vector", name: "Vector" }, { id: "sentinel", name: "Sentinel" }, { id: "canary", name: "Canary" }, { id: "catalyst", name: "Catalyst" }],
  LIVE:   [],
  RETIRED:[{ id: "sigma", name: "Sigma" }],
};

export const WEEKLY_DIGEST = {
  week_of: "2026-05-19",
  generated_ts: "2026-05-23 21:30 UTC",
  acked: false,
  weeks_unacked: 1,
  threshold: 2,
  live_clearance: "PAPER",
  sections: [
    { id: "undispositioned", label: "Undispositioned escalations",       open: true,  tone: "warn",    items: ["esc-1142 fmp_fundamentals 429-storm", "esc-1140 finnhub_insider schema drift"] },
    { id: "adversarial",     label: "Adversarial drift",                  open: true,  tone: "warn",    items: ["vector credibility 54 (was 62) — 8pt slide in 14d"] },
    { id: "wins",            label: "Wins (last 7d)",                     open: false, tone: "neutral", items: ["MOM SPY +5.4% / 40d hold", "SEN GLD +3.8% / 78d hold"] },
    { id: "losses",          label: "Losses (last 7d)",                   open: false, tone: "neutral", items: ["VEC TSLA -1.2% / 5d hold"] },
    { id: "data_validation", label: "Data-validation reds (this week)",   open: false, tone: "neutral", items: ["none — 13/13 checks green every day"] },
  ],
  ack_history: [
    { week: "2026-05-19", acked_at: "—",                        unacked: true },
    { week: "2026-05-12", acked_at: "2026-05-13 08:42 UTC",      unacked: false },
    { week: "2026-05-05", acked_at: "2026-05-06 10:14 UTC",      unacked: false },
  ],
};

export const LLM_TRIAGE_PROPOSALS = [
  { id: "T-1142", lane: "data",   ref: "esc-1142", cls: "rate_limit",       disposition: "increase_backoff_to_15s", confidence: 0.74, model: "claude-opus-4-7", persona: "v2.2", rationale: "fmp_fundamentals returns 429 only when concurrent requests exceed 5/s. Recommend doubling Retry-After backoff floor from 8s to 15s.", fence: "ratelimit-class-A" },
  { id: "T-1140", lane: "data",   ref: "esc-1140", cls: "schema_drift",     disposition: "rename_field_sentiment_to_score", confidence: 0.61, model: "claude-opus-4-7", persona: "v2.2", rationale: "finnhub renamed 'sentiment' to 'score' in their 2026-Q2 release notes. Adapter needs same rename + alias map.", fence: "schema-class-A" },
];

export const DATA_VALIDATION = [
  { check: "prices_daily_completeness",   status: "PASS", rows: 1_840_122, age: "2h", notes: "all liquid tickers covered" },
  { check: "prices_daily_freshness",      status: "PASS", rows: 7_643,     age: "2h", notes: "CRITICAL_TICKERS up to date" },
  { check: "fundamentals_cache",          status: "PASS", rows: 320_410,   age: "3d", notes: "weekly refresh" },
  { check: "corporate_actions_lookback",  status: "PASS", rows: 18_240,    age: "1d", notes: "" },
  { check: "macro_indicators_freshness",  status: "PASS", rows: 9_440,     age: "1d", notes: "all 14 FRED series" },
  { check: "insider_mspr_daily",          status: "PASS", rows: 130_043,   age: "1d", notes: "SEC Form-4 derived" },
  { check: "ticker_history_continuity",   status: "PASS", rows: 78_540,    age: "2d", notes: "rename-aware" },
  { check: "ingest_manifest_loaded",      status: "PASS", rows: 1_822,     age: "0d", notes: "archive-first invariant" },
  { check: "ingest_quarantine_review",    status: "PASS", rows: 0,         age: "0d", notes: "0 rejected rows" },
  { check: "alpaca_corporate_actions",    status: "PASS", rows: 4_217,     age: "1d", notes: "" },
  { check: "tradier_options_chain",       status: "PASS", rows: 12_440,    age: "1d", notes: "" },
  { check: "aaii_sentiment",              status: "PASS", rows: 1_440,     age: "5d", notes: "weekly cadence" },
  { check: "finra_short_interest",        status: "PASS", rows: 88_240,    age: "6d", notes: "biweekly cadence" },
];

export const SELF_HEAL_LOG = [
  { time: "2026-05-25 22:14 UTC", stage: "fmp_fundamentals",   result: "HEALED",     duration: "1m02s", notes: "backfill window 2026-05-22..2026-05-25" },
  { time: "2026-05-25 21:48 UTC", stage: "prices_daily",       result: "HEALED",     duration: "32s",   notes: "5 missing bars filled from Tradier" },
  { time: "2026-05-25 21:31 UTC", stage: "data_operations",    result: "ESCALATED",  duration: "—",     notes: "schema_drift on finnhub_insider — handed off to operator review" },
];

export const ALLOCATIONS = [
  { engine: "momentum",  pct: 23.3, color: "var(--mom)" },
  { engine: "reversion", pct: 16.2, color: "var(--rev)" },
  { engine: "sentinel",  pct: 11.8, color: "var(--sen)" },
  { engine: "vector",    pct:  8.1, color: "var(--vec)" },
  { engine: "catalyst",  pct:  6.0, color: "var(--mom)" },
  { engine: "cash",      pct: 34.6, color: "var(--bg-3)" },
];

export const PROVIDERS = [
  { feed: "prices_daily",         provider: "fmp",     status: "ACTIVE",     adapter: "tpcore.data.ingest_fmp_bars", note: "primary daily-bars feed since 2026-05-22 (CTA consolidated)" },
  { feed: "prices_daily",         provider: "tradier", status: "FALLBACK",   adapter: "tpcore.data.ingest_tradier_bars", note: "secondary fallback (acceptable)" },
  { feed: "prices_daily",         provider: "alpaca",  status: "DEPRECATED", adapter: "tpcore.data.ingest_alpaca_bars", note: "demoted 2026-05-25 (close-date skew vs FMP/Tradier)" },
  { feed: "fundamentals_cache",   provider: "fmp",     status: "ACTIVE",     adapter: "tpcore.data.ingest_fmp_fundamentals", note: "" },
  { feed: "corporate_actions",    provider: "fmp",     status: "ACTIVE",     adapter: "tpcore.data.ingest_fmp_corp_actions", note: "" },
  { feed: "macro_indicators",     provider: "fred",    status: "ACTIVE",     adapter: "tpcore.data.ingest_fred_macro", note: "14 series" },
  { feed: "sec_insider",          provider: "sec",     status: "ACTIVE",     adapter: "tpcore.data.ingest_sec_insider", note: "SEC EDGAR bulk Form-4" },
  { feed: "aaii_sentiment",       provider: "aaii",    status: "ACTIVE",     adapter: "tpcore.data.ingest_aaii_sentiment", note: "weekly" },
  { feed: "finra_short_interest", provider: "finra",   status: "ACTIVE",     adapter: "tpcore.data.ingest_finra_short_interest", note: "biweekly" },
  { feed: "tradier_options",      provider: "tradier", status: "ACTIVE",     adapter: "tpcore.data.ingest_tradier_options", note: "max-pain" },
];

export const CREDIBILITY_GATES: Record<EngineId, { gates: Array<{ k: string; v: number; thr: number; passed: boolean }>; best_params: Array<[string, string]> }> = {
  momentum:  { gates: [
    { k: "DSR",                  v: 0.971, thr: 0.95, passed: true },
    { k: "credibility",          v: 78,    thr: 60,   passed: true },
    { k: "OOS Sharpe (HAC-NW)",  v: 1.24,  thr: 0.80, passed: true },
    { k: "trades / quarter",     v: 31,    thr: 20,   passed: true },
    { k: "n_trials (cum)",       v: 192,   thr: 500,  passed: true },
    { k: "max DD ratio",         v: 0.18,  thr: 0.25, passed: true },
  ], best_params: [["lookback_days","252"], ["hold_days","21"], ["min_dollar_vol","5M"], ["top_n","8"]] },
  reversion: { gates: [
    { k: "DSR",                  v: 0.961, thr: 0.95, passed: true },
    { k: "credibility",          v: 71,    thr: 60,   passed: true },
    { k: "OOS Sharpe (HAC-NW)",  v: 1.08,  thr: 0.80, passed: true },
    { k: "trades / quarter",     v: 84,    thr: 50,   passed: true },
    { k: "n_trials (cum)",       v: 312,   thr: 500,  passed: true },
    { k: "max DD ratio",         v: 0.22,  thr: 0.25, passed: true },
  ], best_params: [["window_days","5"], ["z_threshold","2.0"], ["hold_max","3"], ["regime_filter_v1","off"]] },
  vector:    { gates: [
    { k: "DSR",                  v: 0.918, thr: 0.95, passed: false },
    { k: "credibility",          v: 54,    thr: 60,   passed: false },
    { k: "OOS Sharpe (HAC-NW)",  v: 0.82,  thr: 0.80, passed: true },
    { k: "trades / quarter",     v: 12,    thr: 20,   passed: false },
    { k: "n_trials (cum)",       v: 88,    thr: 500,  passed: true },
    { k: "max DD ratio",         v: 0.31,  thr: 0.25, passed: false },
  ], best_params: [["catalyst_window","5d"], ["min_surprise","0.05"], ["max_concurrent","3"]] },
  sentinel:  { gates: [
    { k: "DSR",                  v: 0.952, thr: 0.95, passed: true },
    { k: "credibility",          v: 68,    thr: 60,   passed: true },
    { k: "OOS Sharpe (HAC-NW)",  v: 0.91,  thr: 0.80, passed: true },
    { k: "trades / quarter",     v: 4,     thr: 4,    passed: true },
    { k: "n_trials (cum)",       v: 42,    thr: 500,  passed: true },
    { k: "max DD ratio",         v: 0.12,  thr: 0.25, passed: true },
  ], best_params: [["bear_threshold","60"], ["basket","['TLT','GLD','SHV']"]] },
  canary:    { gates: [], best_params: [["heartbeat_basket","['SPY']"], ["non_graduating","true"]] },
};
