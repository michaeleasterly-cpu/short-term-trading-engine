// data.jsx — mock data inspired by the repo's actual structure
// (engines, credibility gates, AAR exit reasons, sentinel phases, etc.)

// ─── Engines ────────────────────────────────────────────────────────────────
const ENGINES = [
  {
    id: 'momentum',
    name: 'Momentum',
    kind: '12-1 cross-sectional',
    rebalance: 'monthly',
    state: 'PAPER_TRADING',
    lifecycle: 'PAPER',
    credibility: 52,
    oos_sharpe: 0.784,
    profit_factor: 1.31,
    max_dd: -0.241,
    win_rate: 0.578,
    dsr: 0.8210,
    gate_passed: false,
    gate_reason: 'DSR < 0.95 (0.821)',
    n_positions: 28,
    capital: 42850.12,
    capital_pct: 0.345,
    last_rebalance: '2026-05-04',
    next_rebalance: '2026-06-01',
    trial_id: 14,
    params: { hold_days: 28, lookback_days: 201, skip_days: 30, top_decile_pct: 0.185 },
  },
  {
    id: 'reversion',
    name: 'Reversion',
    kind: 'mean-rev + earnings quality',
    rebalance: 'per-trade (Tier1/Tier2 OCO)',
    state: 'PAPER_TRADING',
    lifecycle: 'PAPER',
    credibility: 58,
    oos_sharpe: 1.174,
    profit_factor: 1.42,
    max_dd: -0.189,
    win_rate: 0.612,
    dsr: 0.8930,
    gate_passed: false,
    gate_reason: 'DSR < 0.95 (0.893)',
    n_positions: 3,
    capital: 38110.55,
    capital_pct: 0.307,
    last_rebalance: '2026-05-15',
    next_rebalance: 'on-signal',
    params: { z_threshold: 2.1, hold_days: 5, tier1_target: 0.025, hard_stop: -0.03 },
  },
  {
    id: 'vector',
    name: 'Vector',
    kind: 'catalyst swing (P/B+D/E)',
    rebalance: 'per-trade',
    state: 'PAPER_TRADING',
    lifecycle: 'PAPER',
    credibility: 61,
    oos_sharpe: 1.257,
    profit_factor: 1.51,
    max_dd: -0.142,
    win_rate: 0.583,
    dsr: 0.9150,
    gate_passed: false,
    gate_reason: 'DSR < 0.95 (0.915)',
    n_positions: 2,
    capital: 22480.00,
    capital_pct: 0.181,
    last_rebalance: '2026-05-13',
    next_rebalance: 'on-signal',
    params: { pb_max: 1.5, de_max: 3.0, catalyst_min: 0.05, hold_days: 30 },
  },
  {
    id: 'sentinel',
    name: 'Sentinel',
    kind: 'macro defense (FRED Bear)',
    rebalance: 'per-cycle',
    state: 'DORMANT',
    lifecycle: 'PAPER',
    credibility: 40,
    oos_sharpe: -12.25,
    profit_factor: 0.11,
    max_dd: -0.394,
    win_rate: 0.250,
    dsr: 0.1097,
    gate_passed: false,
    gate_reason: 'phase=DORMANT (no activation since 2020-04)',
    n_positions: 0,
    capital: 0.0,
    capital_pct: 0.0,
    last_rebalance: null,
    next_rebalance: 'on-activation',
    phase: 'DORMANT',
    bear_score: 22,
    bear_threshold: 60,
  },
  {
    id: 'canary',
    name: 'Canary',
    kind: 'pipeline heartbeat (1-sh SPY)',
    rebalance: 'daily',
    state: 'PAPER_TRADING',
    lifecycle: 'PAPER',
    credibility: null,
    oos_sharpe: null,
    profit_factor: null,
    max_dd: null,
    win_rate: null,
    dsr: null,
    gate_passed: false,
    gate_reason: 'intentionally non-graduating (spec §4b)',
    n_positions: 1,
    capital: 458.20,
    capital_pct: 0.0037,
    last_rebalance: '2026-05-17',
    next_rebalance: 'next trading day',
    note: 'Exercises DA-1/DA-2/AAR/forensics dispatch paths daily without signal risk',
  },
  {
    id: 'lab',
    name: 'Lab (sentinel)',
    kind: 'SDLC SP2 isolation sentinel',
    rebalance: 'never',
    state: 'LAB',
    lifecycle: 'LAB',
    credibility: null,
    oos_sharpe: null,
    profit_factor: null,
    max_dd: null,
    win_rate: null,
    dsr: null,
    gate_passed: false,
    gate_reason: 'LAB state — never dispatched / never allocated',
    n_positions: 0,
    capital: 0.0,
    capital_pct: 0.0,
    last_rebalance: null,
    next_rebalance: '—',
    note: 'Durable sentinel proving LifecycleState.LAB is real (engine_profile two-tier registry, D-SP2-4)',
    hidden_in_grid: true,
  },
];

const ENGINE_BY_ID = Object.fromEntries(ENGINES.map(e => [e.id, e]));

// ─── Account / portfolio ───────────────────────────────────────────────────
const ACCOUNT = {
  equity: 124201.42,
  cash: 20760.75,
  buying_power: 41521.50,
  unrealized_pl: 1842.16,
  unrealized_pl_pct: 0.01508,
  day_pl: 412.81,
  day_pl_pct: 0.00333,
  ytd_pl: 19342.18,
  ytd_pl_pct: 0.18468,
  fetched_at: '2026-05-17T14:32:11Z',
};

// ─── Equity curve (~250 sessions, plausible walk) ─────────────────────────
function generateEquityCurve(seed = 7, start = 100000, days = 320) {
  let s = seed;
  const rand = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
  const out = [];
  let eq = start;
  let benchEq = start;
  const today = new Date('2026-05-17');
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    if (d.getDay() === 0 || d.getDay() === 6) continue;
    // engine has slight positive drift with occasional drawdowns
    const drift = 0.0006;
    const vol = 0.012;
    const shock = (rand() - 0.5) * vol;
    eq *= 1 + drift + shock;
    benchEq *= 1 + 0.0004 + (rand() - 0.5) * 0.009;
    out.push({
      date: d.toISOString().slice(0, 10),
      equity: +eq.toFixed(2),
      benchmark: +benchEq.toFixed(2),
    });
  }
  return out;
}
const EQUITY_CURVE = generateEquityCurve();

// ─── Holdings (across engines, mix of momentum + reversion + vector) ─────
const HOLDINGS = [
  // Momentum — top decile (28 names, showing the lot)
  { engine: 'momentum', ticker: 'NVDA', qty: 41,  avg_entry: 712.40, last: 798.22, pl: 3518.62, pl_pct: 0.1205, weight: 0.0241, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'META', qty: 24,  avg_entry: 487.10, last: 524.66, pl:  901.44, pl_pct: 0.0771, weight: 0.0212, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'AVGO', qty: 8,   avg_entry: 1342.55, last: 1428.10, pl: 684.40, pl_pct: 0.0637, weight: 0.0192, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'AMZN', qty: 18,  avg_entry: 182.45, last: 189.91, pl:  134.28, pl_pct: 0.0409, weight: 0.0149, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'PLTR', qty: 96,  avg_entry: 22.18,  last: 24.82,  pl:  253.44, pl_pct: 0.1190, weight: 0.0142, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'TSM',  qty: 14,  avg_entry: 152.30, last: 161.05, pl:  122.50, pl_pct: 0.0574, weight: 0.0140, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'COIN', qty: 12,  avg_entry: 218.40, last: 245.18, pl:  321.36, pl_pct: 0.1226, weight: 0.0136, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'MELI', qty: 2,   avg_entry: 1612.20, last: 1701.40, pl: 178.40, pl_pct: 0.0553, weight: 0.0124, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'HOOD', qty: 56,  avg_entry: 48.20,  last: 52.81,  pl:  258.16, pl_pct: 0.0957, weight: 0.0121, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'ANET', qty: 7,   avg_entry: 372.10, last: 401.55, pl:  206.15, pl_pct: 0.0791, weight: 0.0121, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'CRWD', qty: 5,   avg_entry: 478.60, last: 502.40, pl:  119.00, pl_pct: 0.0497, weight: 0.0117, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'NFLX', qty: 4,   avg_entry: 624.10, last: 661.92, pl:  151.28, pl_pct: 0.0606, weight: 0.0114, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'WMT',  qty: 15,  avg_entry: 88.40,  last: 91.20,  pl:   42.00, pl_pct: 0.0317, weight: 0.0103, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'COST', qty: 2,   avg_entry: 802.10, last: 818.45, pl:   32.70, pl_pct: 0.0204, weight: 0.0101, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'DASH', qty: 11,  avg_entry: 168.40, last: 172.20, pl:   41.80, pl_pct: 0.0226, weight: 0.0098, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'ANF',  qty: 12,  avg_entry: 162.10, last: 154.40, pl:  -92.40, pl_pct:-0.0475, weight: 0.0095, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'CAVA', qty: 18,  avg_entry: 108.55, last: 102.10, pl: -116.10, pl_pct:-0.0594, weight: 0.0094, entry_date: '2026-05-04' },
  { engine: 'momentum', ticker: 'SMCI', qty: 4,   avg_entry: 412.00, last: 388.20, pl:  -95.20, pl_pct:-0.0578, weight: 0.0090, entry_date: '2026-05-04' },
  // Reversion — 3 active (long/short)
  { engine: 'reversion', ticker: 'XOM',  qty: -24, avg_entry: 118.40, last: 115.20, pl:   76.80, pl_pct: 0.0270, weight: 0.0254, entry_date: '2026-05-12', side: 'short', z: 3.07, rsi: 71.4 },
  { engine: 'reversion', ticker: 'AAPL', qty:  10, avg_entry: 184.10, last: 188.42, pl:   43.20, pl_pct: 0.0235, weight: 0.0214, entry_date: '2026-05-14', side: 'long',  z: -2.85, rsi: 26.1 },
  { engine: 'reversion', ticker: 'KO',   qty: -32, avg_entry: 71.20,  last: 70.45,  pl:   24.00, pl_pct: 0.0105, weight: 0.0212, entry_date: '2026-05-15', side: 'short', z: 2.41,  rsi: 68.9 },
  // Vector — 2 active (catalyst-driven)
  { engine: 'vector', ticker: 'GM',  qty: 38, avg_entry: 42.50, last: 45.80, pl: 125.40, pl_pct: 0.0776, weight: 0.0181, entry_date: '2026-05-08', trigger: 'pullback_to_10ma' },
  { engine: 'vector', ticker: 'PFE', qty: 60, avg_entry: 27.40, last: 28.10, pl:  42.00, pl_pct: 0.0256, weight: 0.0188, entry_date: '2026-05-09', trigger: 'breakout_above_50ma' },
  // Canary — 1 share SPY (heartbeat)
  { engine: 'canary', ticker: 'SPY', qty: 1, avg_entry: 458.20, last: 462.10, pl: 3.90, pl_pct: 0.0085, weight: 0.0037, entry_date: '2026-05-17' },
];

// ─── Today's signals (would-be opens, awaiting next cycle) ───────────────
const SIGNALS = [
  { engine: 'reversion', ticker: 'JNJ',  side: 'short', strength: 0.83, z: 3.21, rsi: 73.4, time: '14:18:02', note: 'z=3.21, rsi=73.4, quality=high'  },
  { engine: 'reversion', ticker: 'DUK',  side: 'short', strength: 0.71, z: 2.84, rsi: 69.8, time: '14:11:44', note: 'z=2.84, rsi=69.8, quality=low BLOCKED'  },
  { engine: 'vector',    ticker: 'CVX',  side: 'long',  strength: 0.62, catalyst_mag: 0.084, time: '13:42:01', note: 'pullback_to_10ma, P/B=1.42'  },
  { engine: 'momentum',  ticker: 'AVGO', side: 'long',  strength: 0.91, time: '09:30:14', note: 'top decile 6.4%, next rebalance 2026-06-01' },
  { engine: 'momentum',  ticker: 'NVDA', side: 'long',  strength: 0.88, time: '09:30:14', note: 'top decile 6.4%, next rebalance 2026-06-01' },
  { engine: 'sentinel',  ticker: '—',    side: 'none',  strength: 0.22, time: '08:00:00', note: 'bear_score=22 (<60), phase=DORMANT' },
];

// ─── Recent AARs (closed trades) ─────────────────────────────────────────
const AARS = [
  { engine: 'reversion', ticker: 'NFLX', dir: 'short', entry: '2026-04-08', exit: '2026-05-13', entry_px: 622.40, exit_px: 581.20, pnl_pct:  0.0662, exit_reason: 'tier2_target',  hold: 25, qty: 6 },
  { engine: 'vector',    ticker: 'GM',   dir: 'long',  entry: '2026-04-23', exit: '2026-05-12', entry_px: 45.01,  exit_px: 41.73,  pnl_pct: -0.0726, exit_reason: 'hard_stop',     hold: 14, qty: 38 },
  { engine: 'reversion', ticker: 'TSLA', dir: 'short', entry: '2026-04-30', exit: '2026-05-08', entry_px: 224.10, exit_px: 243.55, pnl_pct: -0.0868, exit_reason: 'hard_stop',     hold: 6,  qty: 12 },
  { engine: 'momentum',  ticker: 'SOFI', dir: 'long',  entry: '2026-04-04', exit: '2026-05-04', entry_px: 8.40,   exit_px: 9.18,   pnl_pct:  0.0929, exit_reason: 'rebalance_exit',hold: 22, qty: 124 },
  { engine: 'momentum',  ticker: 'DELL', dir: 'long',  entry: '2026-04-04', exit: '2026-05-04', entry_px: 138.20, exit_px: 126.55, pnl_pct: -0.0843, exit_reason: 'rebalance_exit',hold: 22, qty: 8 },
  { engine: 'reversion', ticker: 'KO',   dir: 'short', entry: '2026-04-19', exit: '2026-04-26', entry_px: 73.10,  exit_px: 71.45,  pnl_pct:  0.0226, exit_reason: 'tier1_target',  hold: 5,  qty: 28 },
  { engine: 'vector',    ticker: 'CVX',  dir: 'long',  entry: '2026-03-12', exit: '2026-04-11', entry_px: 152.10, exit_px: 158.40, pnl_pct:  0.0414, exit_reason: 'max_hold',      hold: 22, qty: 18 },
  { engine: 'reversion', ticker: 'AMZN', dir: 'short', entry: '2026-04-02', exit: '2026-04-09', entry_px: 198.30, exit_px: 199.85, pnl_pct: -0.0078, exit_reason: 'time_out',      hold: 5,  qty: 10 },
];

// ─── Data pipeline validation suite (13 checks per CLAUDE.md) ─────────────
const VALIDATION = [
  { id: 'delistings',                       state: 'pass',  rows: 14528, age_min: 47 },
  { id: 'constituent',                      state: 'pass',  rows: 3142,  age_min: 47 },
  { id: 'splits',                           state: 'pass',  rows: 89,    age_min: 47 },
  { id: 'row_integrity',                    state: 'pass',  rows: 2_847_104, age_min: 47 },
  { id: 'fundamentals_integrity',           state: 'pass',  rows: 18421, age_min: 47 },
  { id: 'corporate_actions_integrity',      state: 'pass',  rows: 624,   age_min: 47 },
  { id: 'earnings_events_freshness',        state: 'pass',  rows: 1350,  age_min: 47 },
  { id: 'sec_filings_freshness',            state: 'pass',  rows: 24802, age_min: 47 },
  { id: 'liquidity_tiers_freshness',        state: 'pass',  rows: 3142,  age_min: 47 },
  { id: 'ticker_classifications_coverage',  state: 'pass',  rows: 3142,  age_min: 47 },
  { id: 'macro_indicators_freshness',       state: 'warn',  rows: 41,    age_min: 1422, note: 'FRED last update 23h59m ago' },
  { id: 'prices_daily_freshness',           state: 'pass',  rows: 3142,  age_min: 47 },
  { id: 'prices_daily_completeness',        state: 'pass',  rows: 94260, age_min: 47, note: '30-session window, 0 missing' },
];

// ─── Self-heal recent activity ───────────────────────────────────────────
const HEAL_LOG = [
  { ts: '14:32:11', stage: 'data_validation', result: 'green',  duration_s: 18.4 },
  { ts: '14:31:53', stage: 'daily_bars',      result: 'green',  duration_s: 142.1, note: 'repair_gaps=true, 4 tickers repaired' },
  { ts: '14:31:50', stage: 'data_validation', result: 'red',    duration_s: 17.9,  note: 'prices_daily_completeness: 4 missing (CRWD, ANET, SMCI, COIN)' },
  { ts: '14:28:01', stage: 'fundamentals_refresh', result: 'green', duration_s: 88.2 },
  { ts: '14:26:12', stage: 'earnings_refresh',     result: 'green', duration_s: 41.0 },
  { ts: '14:24:31', stage: 'sec_filings',          result: 'green', duration_s: 24.6 },
];

// ─── Param search trials (momentum, summarised) ──────────────────────────
// Used to render a sharpe heatmap (lookback_days × hold_days, color = sharpe)
const MOM_TRIALS = [];
(function(){
  // grid 200..280 by 10 lookback × 15..30 by 1 hold — fill with plausible sharpes
  let seed = 19;
  const rnd = () => { seed = (seed * 9301 + 49297) % 233280; return seed/233280; };
  for (let lb = 200; lb <= 280; lb += 10) {
    for (let hd = 15; hd <= 30; hd += 1) {
      // peak around lb=240, hd=25
      const peakDist = Math.abs(lb-240)/40 + Math.abs(hd-25)/15;
      const base = 4.4 - peakDist*3.2;
      const sharpe = base + (rnd() - 0.5) * 1.6;
      MOM_TRIALS.push({ lookback: lb, hold: hd, sharpe: +sharpe.toFixed(2) });
    }
  }
})();

// ─── Per-ticker OHLC (synthetic 90-bar series for charts) ────────────────
function generateOHLC(seed, startPrice, days = 90) {
  let s = seed;
  const rand = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
  const out = [];
  let px = startPrice;
  const today = new Date('2026-05-17');
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    if (d.getDay() === 0 || d.getDay() === 6) continue;
    const open = px;
    const vol = 0.018;
    const drift = 0.0008;
    const close = open * (1 + drift + (rand() - 0.5) * vol);
    const high = Math.max(open, close) * (1 + rand() * 0.008);
    const low = Math.min(open, close) * (1 - rand() * 0.008);
    out.push({ date: d.toISOString().slice(0,10), open:+open.toFixed(2), high:+high.toFixed(2), low:+low.toFixed(2), close:+close.toFixed(2) });
    px = close;
  }
  return out;
}

// Precompute OHLC for a few of the held tickers
const OHLC = {
  NVDA: generateOHLC(11, 670),
  XOM:  generateOHLC(23, 122),
  AAPL: generateOHLC(31, 178),
  GM:   generateOHLC(41, 39),
  META: generateOHLC(53, 466),
  COIN: generateOHLC(71, 198),
};

// ─── Sentinel bear-score timeline (180d) ─────────────────────────────────
const BEAR_TIMELINE = (function(){
  let s = 5;
  const rnd = () => { s = (s * 9301 + 49297) % 233280; return s/233280; };
  const out = [];
  const today = new Date('2026-05-17');
  let v = 18;
  for (let i = 180; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    v = Math.max(0, Math.min(100, v + (rnd() - 0.45) * 6));
    out.push({ date: d.toISOString().slice(0,10), score: Math.round(v) });
  }
  return out;
})();

// ─── Risk governor state ────────────────────────────────────────────────
const RISK_STATE = {
  daily_loss_limit_pct: -0.02,
  daily_pnl_pct: 0.0033,
  trades_today: 4,
  max_trades_per_day: 50,
  open_positions: 23,
  max_open_positions: 200,
  consecutive_red_days: 0,
  circuit_breaker_armed: false,
};

// ─── Allocator snapshot ─────────────────────────────────────────────────
const ALLOCATOR = {
  last_rebalance: '2026-05-13T13:00:00Z',
  next_rebalance: '2026-05-20T13:00:00Z',
  method: 'inverse_volatility',
  trigger: 'event-driven (WEEKLY_FIRST_TRADING_DAY in engine_dispatch)',
  total_equity: 124201.42,
  cash_buffer_pct: 0.167,
  weights: [
    { engine: 'momentum',  current: 0.345, target: 0.350, vol_30d: 0.142 },
    { engine: 'reversion', current: 0.307, target: 0.305, vol_30d: 0.158 },
    { engine: 'vector',    current: 0.181, target: 0.175, vol_30d: 0.221 },
    { engine: 'sentinel',  current: 0.000, target: 0.000, vol_30d: 0.000 },
    { engine: 'canary',    current: 0.004, target: 0.000, vol_30d: 0.110, note: 'allocator-excluded by omission' },
    { engine: 'cash',      current: 0.163, target: 0.170, vol_30d: 0.000 },
  ],
};

// ─── Escalation & Hardening Ladder (rungs 1-5) ───────────────────────────
const LADDER = [
  { rung: 1, name: 'Producer-side detection', status: 'covered', count: 4, kind: 'green',
    detail: 'adapter contract-population sentinel armed on fred_macro / iborrowdesk / finra / apewisdom (rest declared+guard_pending)' },
  { rung: 2, name: 'Bounded remediation', status: 'covered', count: 6, kind: 'green',
    detail: 'HealSpecs registered for prices_daily_{completeness,freshness}; rest healable=false (escalate)' },
  { rung: 3, name: 'Disposition + surface', status: 'open', count: 2, kind: 'warn',
    detail: '2 undispositioned past 7d grace — disposition via `python -m ops.weekly_digest disposition <ref> <verb>`' },
  { rung: 4, name: 'Weekly digest ack', status: 'pending', count: 1, kind: 'warn',
    detail: '1 week unacked (live_clearance still green; ≥2 unacked ⇒ live trading de-escalates)' },
  { rung: 5, name: 'LLM triage advisory', status: 'active', count: 1, kind: 'info',
    detail: '1 open novel escalation has an LLM triage proposal awaiting human review (PR #214)' },
];

// ─── Data supervisor — per-source holds ─────────────────────────────────
const SOURCE_HOLDS = [
  // open holds (red)
  { source: 'finnhub_insider_sentiment', held_since: '2026-05-16T04:22:00Z', age_h: 34, cycles_held: 2, reason: 'validation:insider_sentiment_period stale 18h', escalated: false },
  // recently cleared (informational)
];
const SOURCE_CLEAR_HISTORY = [
  { source: 'fmp_fundamentals',         cleared_at: '2026-05-17T08:11:00Z', held_h: 6.2,  auto: true,  reason: 'cleared after canonical fundamentals_refresh' },
  { source: 'apewisdom_social_sentiment', cleared_at: '2026-05-16T22:04:00Z', held_h: 14.1, auto: false, reason: 'operator-converted (volume schema migration)' },
];

// ─── Cross-table audit (auditheal layer) ─────────────────────────────────
const CROSS_TABLE_AUDIT = [
  { source: 'tradier_options_chains',   state: 'pass', last: '2026-05-17T14:32:00Z', note: 'orphan expired chains pruned (auto)' },
  { source: 'sec_filings',              state: 'pass', last: '2026-05-17T14:32:00Z', note: '' },
  { source: 'earnings_events',          state: 'pass', last: '2026-05-17T14:32:00Z', note: '' },
  { source: 'fmp_fundamentals',         state: 'pass', last: '2026-05-17T14:32:00Z', note: '' },
  { source: 'aar_events_orphan_check',  state: 'pass', last: '2026-05-17T14:32:00Z', note: '' },
  { source: 'corp_actions_integrity',   state: 'warn', last: '2026-05-17T14:32:00Z', note: 'NVDA 10:1 split row missing exchange_calendar match (escalate-only)' },
];

// ─── Recent escalations (trailing 7d) ────────────────────────────────────
const RECENT_ESCALATIONS = [
  { ts: '2026-05-17T08:11:00Z', etype: 'DATA_REPAIR_ESCALATED',  ref: 'req-7a3e',   cls: 'validation:insider_sentiment_period', resolved: false,  open: true,  msg: 'finnhub_insider_sentiment period gap exceeds 14d', has_llm_proposal: true,  llm_pr: 'PR #214' },
  { ts: '2026-05-16T22:04:00Z', etype: 'AdapterContractDrift',   ref: 'apewisdom',  cls: 'contract:apewisdom_social_sentiment', resolved: true,   open: false, msg: 'required field `mentions_24h` empty across all 412 rows (vendor schema change)' },
  { ts: '2026-05-15T11:08:00Z', etype: 'DATA_SOURCE_ESCALATED',  ref: 'hold-42',    cls: 'validation:macro_indicators_freshness', resolved: true, open: false, msg: 'FRED initial_claims 25h late (auto-cleared after self-heal)' },
  { ts: '2026-05-14T18:00:00Z', etype: 'DATA_REPAIR_ESCALATED',  ref: 'req-7991',   cls: 'cross_table:tradier_orphan_chains',   resolved: true,  open: false, msg: 'auditheal converged after cross_ref_cleanup (218 orphan chains pruned)' },
];

// ─── Weekly digest state ────────────────────────────────────────────────
const WEEKLY_DIGEST = {
  week_of: '2026-05-12',
  generated_at: '2026-05-18T06:00:00Z',
  acked: false,
  weeks_unacked: 1,
  live_clearance: 'green',                  // becomes amber/red at 2+ unacked weeks
  live_clearance_threshold: 2,
  sections: [
    { id: 'cutovers',         label: 'Provider cutovers',     count: 1, items: [
        'fred_macro: fred_v2 → fred_v3 (parity gate passed, 99.97% match; CUTOVER 2026-05-15 03:00 UTC)',
    ]},
    { id: 'self_heal',        label: 'Self-heal events',      count: 8, items: [
        'prices_daily_completeness: 12 cycles healed via daily_bars repair_gaps=true (median 142s)',
        'macro_indicators_freshness: 3 cycles auto-recovered',
    ]},
    { id: 'near_miss_gates',  label: 'Near-miss gates',       count: 2, items: [
        'momentum trial #14: DSR 0.821 (gate 0.95) — 16% from graduation',
        'vector trial #9: DSR 0.915 (gate 0.95) — 4% from graduation',
    ]},
    { id: 'undispositioned',  label: 'Undispositioned escalations', count: 2, items: [
        '#1 ref=req-7a3e validation:insider_sentiment_period — policy:escalate_operator (open 34h; LLM proposes: structural, conf 0.72)',
        '#2 ref=hold-12   contract:fmp_fundamentals — policy:escalate_operator (open 9d, past grace)',
    ]},
    { id: 'adversarial',      label: 'Most-likely silently-wrong', count: 1, items: [
        'fred_macro initial_claims weekly value is identical to last week (2 cycles) — could be flat series OR vendor cache; cross-check.',
    ]},
  ],
  history: [
    { week_of: '2026-05-05', acked: true,  ts: '2026-05-06T10:14:00Z' },
    { week_of: '2026-04-28', acked: true,  ts: '2026-04-29T08:02:00Z' },
    { week_of: '2026-04-21', acked: true,  ts: '2026-04-22T07:51:00Z' },
    { week_of: '2026-04-14', acked: true,  ts: '2026-04-14T19:31:00Z' },
  ],
};

// ─── LLM triage proposals (data + engine lanes) ──────────────────────────
const LLM_TRIAGE = [
  {
    ts: '2026-05-19T09:14:00Z',
    lane: 'data',
    ref: 'req-7a3e',
    cls: 'validation:insider_sentiment_period',
    persona_version: 'v3',
    model: 'claude-haiku-4-5',
    proposed_disposition: 'structural',
    confidence: 0.72,
    rationale: 'finnhub_insider_sentiment has been period-gap-stale ≥14d in 3 separate cycles over 60d. Pattern indicates vendor reduced reporting cadence (not a transient outage). Recommend: STRUCTURAL — adjust validation period to 21d OR retire feed.',
    pr: 'PR #214',
    pr_status: 'draft (human review required)',
    fence: 'llm-triage-fence',
  },
  {
    ts: '2026-05-19T11:42:00Z',
    lane: 'engine',
    ref: 'hold-eng-19',
    cls: 'data_request_timeout',
    persona_version: 'v1',
    model: 'claude-haiku-4-5',
    proposed_disposition: 'structural',
    confidence: 0.61,
    rationale: 'vector engine repeatedly times out on earnings_events request during pre-open window. Class is policy-marked NOT_THIS_ENGINE — root cause is data-lane fulfillment latency, not engine bug. Recommend STRUCTURAL — bump request_timeout to 60s OR move earnings_refresh earlier in cron.',
    pr: 'PR #247',
    pr_status: 'draft (human review required)',
    fence: 'engine-llm-triage-fence',
  },
];

// ─── The Lab — SDLC SP2 walk-forward search ─────────────────────────────
const LAB_RUNS = [
  {
    id: 'lab-2026-05-19-rev2',
    candidate: 'rev2',
    engine: 'reversion',
    started: '2026-05-19T03:14:00Z',
    finished: '2026-05-19T05:42:18Z',
    duration_min: 148,
    trials: 200,
    walk_windows: 6,
    verdict: 'SURVIVED',
    dsr: 0.9612,
    final_sharpe: 1.314,
    credibility: 64,
    namespace: 'backtest_credibility.lab.rev2',
    dossier: 'docs/lab/2026-05-19-rev2-SURVIVED-seed0.md',
    isolation_violations: 0,
    seed: 0,
    promotion_pending: true,
    note: 'pca_residual signal switch (Avellaneda & Lee)',
    best_params: { z_threshold: 2.4, volume_climax_multiplier: 1.8, max_hold_days: 8, stop_pct: 0.07 },
  },
  {
    id: 'lab-2026-05-18-vec3',
    candidate: 'vec3',
    engine: 'vector',
    started: '2026-05-18T22:08:00Z',
    finished: '2026-05-19T00:31:44Z',
    duration_min: 144,
    trials: 200,
    walk_windows: 6,
    verdict: 'FAILED',
    dsr: 0.8842,
    final_sharpe: 1.041,
    credibility: 54,
    namespace: 'backtest_credibility.lab.vec3',
    dossier: 'docs/lab/2026-05-19-vec3-FAILED-seed0.md',
    isolation_violations: 0,
    seed: 0,
    promotion_pending: false,
    note: 'wider catalyst window (3→10d) — failed DSR by 6.6%',
    best_params: { pb_ceiling: 2.1, de_ceiling: 2.4, catalyst_window_days: 8, swing_score_threshold: 64 },
  },
  {
    id: 'lab-2026-05-17-mom4',
    candidate: 'mom4',
    engine: 'momentum',
    started: '2026-05-17T20:00:00Z',
    finished: '2026-05-17T22:18:09Z',
    duration_min: 138,
    trials: 200,
    walk_windows: 6,
    verdict: 'FAILED',
    dsr: 0.7831,
    final_sharpe: 0.892,
    credibility: 49,
    namespace: 'backtest_credibility.lab.mom4',
    dossier: 'docs/lab/2026-05-17-mom4-FAILED-seed0.md',
    isolation_violations: 0,
    seed: 0,
    promotion_pending: false,
    note: 'shorter lookback (180→220d) — DSR gap unchanged',
    best_params: { lookback_days: 217, skip_days: 24, hold_days: 27, top_decile_pct: 0.142 },
  },
  {
    id: 'lab-2026-05-16-rev1',
    candidate: 'rev1',
    engine: 'reversion',
    started: '2026-05-16T18:00:00Z',
    finished: '2026-05-16T20:24:00Z',
    duration_min: 144,
    trials: 200,
    walk_windows: 6,
    verdict: 'FAILED',
    dsr: 0.8930,
    final_sharpe: 1.174,
    credibility: 58,
    namespace: 'backtest_credibility.lab.rev1',
    dossier: 'docs/lab/2026-05-16-rev1-FAILED-seed0.md',
    isolation_violations: 0,
    seed: 0,
    promotion_pending: false,
    note: 'baseline z-score sweep — established the gap',
    best_params: { z_threshold: 2.1, volume_climax_multiplier: 1.5, max_hold_days: 5, stop_pct: 0.06 },
  },
];

// Currently-running Lab job (none right now)
const LAB_QUEUE = [
  { candidate: 'vec4', engine: 'vector', queued_at: '2026-05-19T13:08:00Z', note: 'catalyst_window 4-6 narrow sweep, post-vec3 FAILED' },
  { candidate: 'sen2', engine: 'sentinel', queued_at: '2026-05-19T13:11:00Z', note: 'fast-VIX-only activation override (lag fix)' },
];

// Walk-forward windows for the survived rev2 run (for the chart)
const LAB_WALK_RESULTS = [
  { window: '2018-2020', train: '2018-2022', holdout_start: '2020-01-01', holdout_end: '2021-12-31', sharpe: 1.42, n_trades: 38, credibility: 62, dsr: 0.92 },
  { window: '2019-2021', train: '2019-2023', holdout_start: '2021-01-01', holdout_end: '2022-12-31', sharpe: 1.28, n_trades: 41, credibility: 60, dsr: 0.90 },
  { window: '2020-2022', train: '2020-2024', holdout_start: '2022-01-01', holdout_end: '2023-12-31', sharpe: 1.18, n_trades: 36, credibility: 58, dsr: 0.88 },
  { window: '2021-2023', train: '2021-2025', holdout_start: '2023-01-01', holdout_end: '2024-12-31', sharpe: 1.51, n_trades: 44, credibility: 66, dsr: 0.94 },
  { window: '2022-2024', train: '2022-2026', holdout_start: '2024-01-01', holdout_end: '2025-12-31', sharpe: 1.46, n_trades: 42, credibility: 65, dsr: 0.95 },
  { window: '2023-2025', train: '2023-2027', holdout_start: '2025-01-01', holdout_end: '2026-12-31', sharpe: 1.31, n_trades: 40, credibility: 64, dsr: 0.96 },
];

// ─── Engine SDLC — Engine Change Requests ────────────────────────────────
const ECR_QUEUE = [
  {
    id: 'ecr-2026-05-19-rev-promote',
    kind: 'MODIFY',
    engine: 'reversion',
    action: 'PROMOTE_LAB_TO_PAPER',
    operator: 'pending',
    submitted: '2026-05-19T05:43:00Z',
    submitter: 'auto (Lab SURVIVED verdict)',
    summary: 'Promote rev2 candidate (Lab SURVIVED, DSR 0.961) → live reversion (PAPER)',
    lab_dossier: 'docs/lab/2026-05-19-rev2-SURVIVED-seed0.md',
    rejection: null,
    auto_validated: true,
    diff: '+pca_residual signal switch, +z_threshold 2.1→2.4, +max_hold_days 5→8',
  },
  {
    id: 'ecr-2026-05-15-aaii-add',
    kind: 'ADD',
    engine: 'aaii_sentiment',
    action: 'ONBOARD_FEED',
    operator: 'pending',
    submitted: '2026-05-15T11:21:00Z',
    submitter: 'operator',
    summary: 'Add aaii_sentiment feed (Investor Sentiment Survey, Thursday vendor cadence)',
    lab_dossier: null,
    rejection: null,
    auto_validated: true,
    diff: '+FeedProfile(aaii_sentiment, publish_weekday=THU), +HealSpec(escalate_only), +adapter contract',
  },
];
const ECR_HISTORY = [
  { id: 'ecr-2026-05-18-canary-add',    kind: 'ADD',    engine: 'canary', action: 'NEW_ENGINE_TO_LAB', decided: '2026-05-17T22:00:00Z', verdict: 'APPROVED', diff: '+canary/ scaffold, lifecycle=PAPER (heartbeat exception)' },
  { id: 'ecr-2026-05-16-sigma-retire', kind: 'RETIRE', engine: 'sigma',  action: 'RETIRE_ENGINE',    decided: '2026-05-16T14:30:00Z', verdict: 'APPROVED', diff: 'sigma/ → archive/sigma/, EULOGY.md' },
  { id: 'ecr-2026-05-14-fred-cutover', kind: 'CUTOVER', engine: 'fred_macro', action: 'AUTO_CUTOVER', decided: '2026-05-15T03:00:00Z', verdict: 'AUTO',    diff: 'fred_v2 → fred_v3 (parity 99.97%)' },
];
const DAEMONS = [
  { id: 'engine-service',       lane: 'engine', pid: 28714, uptime_h: 38.2, last_heartbeat_s: 12,  role: 'Long-lived: data-ops-triggered sweep + trade-monitor stream + day-rollover weekly-digest trigger', status: 'green' },
  { id: 'data_repair_service',  lane: 'data',   pid: 28722, uptime_h: 38.2, last_heartbeat_s: 8,   role: 'Long-lived: bounded repair runner', status: 'green' },
  { id: 'llm_triage_service',   lane: 'advisory', pid: 28733, uptime_h: 38.2, last_heartbeat_s: 18, role: 'Event-driven: two crash-isolated co-tasks (data-lane + engine-lane) off application_log', status: 'green' },
  { id: 'data_operations',      lane: 'data',   pid: null,  uptime_h: 0,    last_heartbeat_s: 0,   role: 'Cron: daily data update (06:00 UTC weekdays)', status: 'cron' },
];

// ─── Provider bindings (Provider Lifecycle, ACTIVE/FALLBACK) ─────────────
const PROVIDERS = [
  { feed: 'daily_bars',                provider: 'alpaca_sip',           status: 'ACTIVE',     since: '2026-04-02', parity: '100.00%' },
  { feed: 'daily_bars',                provider: 'iex_free',             status: 'FALLBACK',   since: '2024-09-01', parity: '99.81%' },
  { feed: 'fred_macro',                provider: 'fred_v3',              status: 'ACTIVE',     since: '2026-05-15', parity: '99.97%' },
  { feed: 'fred_macro',                provider: 'fred_v2',              status: 'DEPRECATED', since: '2024-01-10', parity: '99.97%' },
  { feed: 'insider_sentiment',         provider: 'finnhub',              status: 'ACTIVE',     since: '2024-11-22', parity: '—' },
  { feed: 'fundamentals',              provider: 'fmp_paid',             status: 'ACTIVE',     since: '2025-03-18', parity: '—' },
  { feed: 'social_sentiment',          provider: 'apewisdom',            status: 'ACTIVE',     since: '2024-08-10', parity: '—' },
  { feed: 'short_interest',            provider: 'finra_nasdaq',         status: 'ACTIVE',     since: '2024-09-04', parity: '—' },
  { feed: 'borrow_rates',              provider: 'iborrowdesk',          status: 'ACTIVE',     since: '2024-08-21', parity: '—' },
];

// ─── Forensics triggers (auto-generated from AAR scans) ─────────────────
const FORENSICS = [
  { ts: '2026-05-14T13:21:00Z', trigger: 'consecutive_stops',          engine: 'reversion', severity: 'med',  msg: 'TSLA: 3rd consecutive hard_stop in 14d, z-threshold may be calibrated to pre-2024 vol regime' },
  { ts: '2026-05-13T11:08:00Z', trigger: 'tier1_skew',                 engine: 'reversion', severity: 'low',  msg: 'Tier1 hit rate 41% vs backtest 58% (last 30 closed)' },
  { ts: '2026-05-12T16:42:00Z', trigger: 'momentum_decile_turnover',   engine: 'momentum',  severity: 'low',  msg: 'top-decile turnover 38% (backtest baseline 27%), check skip_days drift' },
  { ts: '2026-05-10T09:30:00Z', trigger: 'sentinel_bear_score_step',   engine: 'sentinel',  severity: 'info', msg: 'bear_score +14 in 5d (18→32), still below activation' },
  { ts: '2026-05-08T18:00:00Z', trigger: 'vector_catalyst_decay',      engine: 'vector',    severity: 'med',  msg: 'GM: catalyst_magnitude decayed 0.218→0.067 in 3d while in position' },
];

// Expose
Object.assign(window, {
  ENGINES, ENGINE_BY_ID, ACCOUNT, EQUITY_CURVE, HOLDINGS, SIGNALS, AARS,
  VALIDATION, HEAL_LOG, MOM_TRIALS, OHLC, BEAR_TIMELINE, RISK_STATE,
  ALLOCATOR, FORENSICS,
  LADDER, SOURCE_HOLDS, SOURCE_CLEAR_HISTORY, CROSS_TABLE_AUDIT,
  RECENT_ESCALATIONS, WEEKLY_DIGEST, LLM_TRIAGE, DAEMONS, PROVIDERS,
  LAB_RUNS, LAB_QUEUE, LAB_WALK_RESULTS, ECR_QUEUE, ECR_HISTORY,
});
