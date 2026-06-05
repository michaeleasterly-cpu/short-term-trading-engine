// components.jsx — shared atoms for the trading console

// ─── Pill / status indicator ───────────────────────────────────────────
function Pill({ tone = 'neutral', children, dim = false }) {
  return <span className={`pill pill-${tone}${dim ? ' pill-dim' : ''}`}>{children}</span>;
}

// ─── Status dot (small filled circle) ─────────────────────────────────
function StatusDot({ tone = 'neutral' }) {
  return <span className={`dot dot-${tone}`} />;
}

// ─── Number formatting helpers ────────────────────────────────────────
const fmtUSD = (n, decimals = 2) => (n < 0 ? '-' : '') + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
const fmtNum = (n, d = 2) => n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtPct = (n, d = 2) => (n >= 0 ? '+' : '') + (n * 100).toFixed(d) + '%';
const fmtInt = (n) => n.toLocaleString('en-US');
const fmtCompact = (n) => {
  if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1) + 'k';
  return n.toFixed(0);
};
const toneFor = (n) => n > 0 ? 'pos' : n < 0 ? 'neg' : 'neutral';

// ─── Tabular cell — auto-tones numeric P&L ───────────────────────────
function Num({ v, fmt = fmtNum, decimals = 2, signed = false, suffix = '', tone, className = '' }) {
  const t = tone ?? (signed ? toneFor(v) : 'neutral');
  const out = fmt === fmtPct ? fmtPct(v, decimals) :
              fmt === fmtUSD ? (signed ? (v >= 0 ? '+' : '') + fmtUSD(v, decimals) : fmtUSD(v, decimals)) :
              fmt(v, decimals);
  return <span className={`num num-${t} ${className}`}>{out}{suffix}</span>;
}

// ─── Section panel ────────────────────────────────────────────────────
function Panel({ title, eyebrow, actions, children, className = '', noPad = false }) {
  return (
    <section className={`panel ${className}`}>
      {(title || eyebrow || actions) && (
        <header className="panel-hd">
          <div className="panel-hd-l">
            {eyebrow && <span className="eyebrow">{eyebrow}</span>}
            {title && <h2>{title}</h2>}
          </div>
          {actions && <div className="panel-hd-r">{actions}</div>}
        </header>
      )}
      <div className={`panel-body${noPad ? ' panel-body-nopad' : ''}`}>{children}</div>
    </section>
  );
}

// ─── Sparkline ─────────────────────────────────────────────────────────
function Sparkline({ values, width = 80, height = 22, stroke, fill = true }) {
  if (!values || values.length === 0) return <svg width={width} height={height} />;
  const mn = Math.min(...values), mx = Math.max(...values);
  const range = mx - mn || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - ((v - mn) / range) * height;
    return [x, y];
  });
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const last = values[values.length - 1], first = values[0];
  const tone = last >= first ? 'var(--pos)' : 'var(--neg)';
  const fillD = fill ? `${d} L${width},${height} L0,${height} Z` : null;
  return (
    <svg width={width} height={height} className="spark">
      {fill && <path d={fillD} fill={stroke || tone} opacity="0.12" />}
      <path d={d} fill="none" stroke={stroke || tone} strokeWidth="1.2" />
    </svg>
  );
}

// ─── Equity curve (multi-line, big) ────────────────────────────────────
function EquityChart({ data, height = 220, showBenchmark = true }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(800);
  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const padL = 56, padR = 12, padT = 12, padB = 22;
  const chartW = Math.max(120, w - padL - padR);
  const chartH = height - padT - padB;
  const allVals = data.flatMap(d => showBenchmark ? [d.equity, d.benchmark] : [d.equity]);
  const mn = Math.min(...allVals), mx = Math.max(...allVals);
  const range = mx - mn || 1;
  const xAt = (i) => padL + (i / (data.length - 1)) * chartW;
  const yAt = (v) => padT + (1 - (v - mn) / range) * chartH;
  const path = (key) => data.map((d, i) => `${i ? 'L' : 'M'}${xAt(i).toFixed(1)},${yAt(d[key]).toFixed(1)}`).join(' ');
  const yTicks = 4;
  const ticks = Array.from({length: yTicks + 1}, (_, i) => mn + (range * i / yTicks));
  // x ticks: 5 evenly spaced dates
  const xTickIdx = Array.from({length: 5}, (_, i) => Math.floor(i * (data.length - 1) / 4));
  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width={w} height={height} className="eq-chart">
        {/* grid */}
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={padL} x2={w - padR} y1={yAt(t)} y2={yAt(t)} className="grid-line" />
            <text x={padL - 6} y={yAt(t)} className="grid-label" textAnchor="end" dy="0.35em">
              ${(t/1000).toFixed(0)}k
            </text>
          </g>
        ))}
        {/* x labels */}
        {xTickIdx.map(i => (
          <text key={i} x={xAt(i)} y={height - 6} className="grid-label" textAnchor="middle">
            {data[i].date.slice(2)}
          </text>
        ))}
        {/* benchmark */}
        {showBenchmark && <path d={path('benchmark')} fill="none" stroke="var(--ink-3)" strokeWidth="1" strokeDasharray="2 2" />}
        {/* equity area */}
        <path d={`${path('equity')} L${xAt(data.length-1)},${padT + chartH} L${padL},${padT + chartH} Z`} fill="var(--accent)" opacity="0.08" />
        <path d={path('equity')} fill="none" stroke="var(--accent)" strokeWidth="1.4" />
        {/* last value */}
        <circle cx={xAt(data.length-1)} cy={yAt(data[data.length-1].equity)} r="3" fill="var(--accent)" />
      </svg>
    </div>
  );
}

// ─── Candle chart ──────────────────────────────────────────────────────
function CandleChart({ data, height = 280, markers = [] }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(800);
  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const padL = 48, padR = 12, padT = 12, padB = 22;
  const chartW = Math.max(120, w - padL - padR);
  const chartH = height - padT - padB;
  const allVals = data.flatMap(d => [d.high, d.low]);
  const mn = Math.min(...allVals), mx = Math.max(...allVals);
  const range = (mx - mn) || 1;
  const yAt = (v) => padT + (1 - (v - mn) / range) * chartH;
  const xAt = (i) => padL + (i / (data.length - 1)) * chartW;
  const cw = Math.max(1, (chartW / data.length) * 0.7);
  const yTicks = 5;
  const ticks = Array.from({length: yTicks + 1}, (_, i) => mn + (range * i / yTicks));
  const xTickIdx = Array.from({length: 6}, (_, i) => Math.floor(i * (data.length - 1) / 5));

  // map marker dates → indices
  const dateIdx = new Map(data.map((d, i) => [d.date, i]));

  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width={w} height={height} className="candle-chart">
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={padL} x2={w - padR} y1={yAt(t)} y2={yAt(t)} className="grid-line" />
            <text x={padL - 6} y={yAt(t)} className="grid-label" textAnchor="end" dy="0.35em">
              {t.toFixed(t >= 100 ? 0 : 2)}
            </text>
          </g>
        ))}
        {xTickIdx.map(i => (
          <text key={i} x={xAt(i)} y={height - 6} className="grid-label" textAnchor="middle">
            {data[i].date.slice(5)}
          </text>
        ))}
        {data.map((d, i) => {
          const up = d.close >= d.open;
          const color = up ? 'var(--pos)' : 'var(--neg)';
          const x = xAt(i);
          const yH = yAt(d.high), yL = yAt(d.low);
          const yO = yAt(d.open), yC = yAt(d.close);
          const bodyT = Math.min(yO, yC), bodyH = Math.max(1, Math.abs(yC - yO));
          return (
            <g key={i}>
              <line x1={x} x2={x} y1={yH} y2={yL} stroke={color} strokeWidth="0.8" />
              <rect x={x - cw/2} y={bodyT} width={cw} height={bodyH} fill={color} />
            </g>
          );
        })}
        {/* markers */}
        {markers.map((m, k) => {
          const i = dateIdx.get(m.date);
          if (i == null) return null;
          const x = xAt(i);
          const y = yAt(m.price);
          const color = m.type === 'entry'
            ? (m.side === 'short' ? 'var(--neg)' : 'var(--pos)')
            : (m.exit_pl >= 0 ? 'var(--pos)' : 'var(--neg)');
          return (
            <g key={k}>
              <line x1={x} x2={x} y1={padT} y2={padT + chartH} stroke={color} strokeWidth="0.5" strokeDasharray="2 2" opacity="0.6" />
              <polygon
                points={
                  m.type === 'entry'
                    ? `${x},${y-7} ${x-5},${y+3} ${x+5},${y+3}`
                    : `${x-5},${y-3} ${x+5},${y-3} ${x},${y+7}`
                }
                fill={color}
                stroke="var(--bg)"
                strokeWidth="0.8"
              />
              <text x={x + 7} y={y + (m.type === 'entry' ? -4 : 12)} className="candle-marker-label" fill={color}>
                {m.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ─── Heatmap (param search) ────────────────────────────────────────────
function Heatmap({ data, xKey, yKey, vKey, xLabel, yLabel, vLabel, cellW = 26, cellH = 20 }) {
  const xs = [...new Set(data.map(d => d[xKey]))].sort((a,b)=>a-b);
  const ys = [...new Set(data.map(d => d[yKey]))].sort((a,b)=>a-b);
  const vs = data.map(d => d[vKey]);
  const mn = Math.min(...vs), mx = Math.max(...vs);
  const grid = new Map(data.map(d => [`${d[xKey]}-${d[yKey]}`, d[vKey]]));
  const colorFor = (v) => {
    const t = (v - mn) / (mx - mn || 1);
    // diverging blue → amber for negative/positive sharpe
    const neg = v < 0;
    const intensity = neg ? (mn ? Math.min(1, Math.abs(v - 0) / Math.abs(mn)) : 0) : Math.min(1, v / mx);
    if (neg) return `oklch(${(0.50 - intensity * 0.20).toFixed(3)} 0.05 245)`;
    return `oklch(${(0.55 + intensity * 0.25).toFixed(3)} ${(0.04 + intensity * 0.10).toFixed(3)} 60)`;
  };
  return (
    <div className="heatmap-wrap">
      <div className="heatmap">
        <div className="hm-corner" />
        {xs.map(x => <div key={x} className="hm-xhead">{x}</div>)}
        {ys.map(y => (
          <React.Fragment key={y}>
            <div className="hm-yhead">{y}</div>
            {xs.map(x => {
              const v = grid.get(`${x}-${y}`);
              return (
                <div
                  key={`${x}-${y}`}
                  className="hm-cell"
                  style={{ background: v != null ? colorFor(v) : 'transparent', width: cellW, height: cellH }}
                  title={`${xLabel}=${x}, ${yLabel}=${y}, ${vLabel}=${v?.toFixed(2)}`}
                >
                  <span className="hm-val">{v != null ? v.toFixed(1) : ''}</span>
                </div>
              );
            })}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

// ─── Credibility gate bar ──────────────────────────────────────────────
function GateBar({ score, threshold = 60, label = 'credibility' }) {
  const pct = Math.min(100, Math.max(0, score));
  const passed = score >= threshold;
  return (
    <div className="gate-bar">
      <div className="gate-bar-track">
        <div className="gate-bar-fill" style={{ width: pct + '%' }} />
        <div className="gate-bar-threshold" style={{ left: threshold + '%' }} />
      </div>
      <div className="gate-bar-meta">
        <span>{label}</span>
        <span className={`num num-${passed ? 'pos' : 'warn'}`}>{score}<span className="muted">/{threshold}</span></span>
      </div>
    </div>
  );
}

// ─── Action button ──────────────────────────────────────────────────────
function ActionBtn({ kind = 'default', children, onClick, hot = false, last_run, disabled }) {
  return (
    <button
      className={`action-btn action-btn-${kind}${hot ? ' action-btn-hot' : ''}`}
      onClick={onClick}
      disabled={disabled}
    >
      <span className="action-btn-label">{children}</span>
      {last_run && <span className="action-btn-meta">last: {last_run}</span>}
    </button>
  );
}

// ─── KPI tile ───────────────────────────────────────────────────────────
function KPI({ label, value, sub, tone, hint }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${tone ? 'num-' + tone : ''}`}>{value}</div>
      {sub && <div className={`kpi-sub ${tone ? 'num-' + tone : ''}`}>{sub}</div>}
      {hint && <div className="kpi-hint">{hint}</div>}
    </div>
  );
}

// Expose globals
Object.assign(window, {
  Pill, StatusDot, fmtUSD, fmtNum, fmtPct, fmtInt, fmtCompact, toneFor,
  Num, Panel, Sparkline, EquityChart, CandleChart, Heatmap, GateBar, ActionBtn, KPI,
});
