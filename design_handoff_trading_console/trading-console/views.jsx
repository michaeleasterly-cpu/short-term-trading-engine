// views.jsx — view components for the trading console

// ──────────────────────────────────────────────────────────────────────
// OVERVIEW
// ──────────────────────────────────────────────────────────────────────
function OverviewView({ onNav }) {
  const total = ACCOUNT.equity;
  return (
    <div className="view view-overview">
      {/* KPI strip */}
      <div className="kpi-strip">
        <KPI label="Equity"          value={fmtUSD(ACCOUNT.equity, 2)}
             sub={`${fmtPct(ACCOUNT.day_pl_pct)} today`} tone={toneFor(ACCOUNT.day_pl)} />
        <KPI label="Day P&L"         value={(ACCOUNT.day_pl >= 0 ? '+' : '') + fmtUSD(ACCOUNT.day_pl, 2)}
             sub={fmtPct(ACCOUNT.day_pl_pct)} tone={toneFor(ACCOUNT.day_pl)} />
        <KPI label="Unrealized P&L"  value={(ACCOUNT.unrealized_pl >= 0 ? '+' : '') + fmtUSD(ACCOUNT.unrealized_pl, 2)}
             sub={fmtPct(ACCOUNT.unrealized_pl_pct)} tone={toneFor(ACCOUNT.unrealized_pl)} />
        <KPI label="YTD P&L"         value={(ACCOUNT.ytd_pl >= 0 ? '+' : '') + fmtUSD(ACCOUNT.ytd_pl, 2)}
             sub={fmtPct(ACCOUNT.ytd_pl_pct)} tone={toneFor(ACCOUNT.ytd_pl)} />
        <KPI label="Cash"            value={fmtUSD(ACCOUNT.cash, 2)}
             sub={`${(ACCOUNT.cash/ACCOUNT.equity*100).toFixed(1)}% of book`} />
        <KPI label="Buying Power"    value={fmtUSD(ACCOUNT.buying_power, 2)} sub="2× margin" />
        <KPI label="Open Positions"  value={RISK_STATE.open_positions}
             sub={`${RISK_STATE.open_positions}/${RISK_STATE.max_open_positions} of risk limit`} />
        <KPI label="Trades Today"    value={RISK_STATE.trades_today}
             sub={`${RISK_STATE.trades_today}/${RISK_STATE.max_trades_per_day}`} />
      </div>

      {/* Equity curve */}
      <Panel
        title="Equity curve"
        eyebrow="320 sessions · vs SPY (dashed)"
        actions={
          <div className="seg">
            {['30d', '90d', '1y', 'all'].map((k, i) => (
              <button key={k} className={'seg-btn' + (i === 2 ? ' seg-btn-on' : '')}>{k}</button>
            ))}
          </div>
        }
      >
        <EquityChart data={EQUITY_CURVE} height={240} />
      </Panel>

      {/* Engine grid */}
      <Panel title="Engines" eyebrow={`${ENGINES.length} built · 0 graduated · 1 non-graduating (canary)`}>
        <div className="engine-grid">
          {ENGINES.map(e => (
            <button key={e.id} className={`engine-card engine-tone-${engineTone(e.id)}`} onClick={() => onNav('engine:' + e.id)}>
              <div className="engine-card-hd">
                <div className="engine-card-name">
                  <StatusDot tone={e.state === 'PAPER_TRADING' ? 'pos' : e.state === 'DORMANT' ? 'neutral' : 'warn'} />
                  <b>{e.name.toUpperCase()}</b>
                </div>
                <Pill tone={e.gate_passed ? 'pos' : e.credibility == null ? 'neutral' : 'warn'} dim>
                  {e.gate_passed ? 'GRADUATED' : e.credibility == null ? 'HEARTBEAT' : 'GATED'}
                </Pill>
              </div>
              <div className="engine-card-kind">{e.kind}</div>
              {e.credibility == null ? (
                <div className="engine-card-stats engine-card-stats-canary">
                  <div className="stat">
                    <span className="stat-l">positions</span>
                    <Num v={e.n_positions} fmt={fmtNum} decimals={0} />
                  </div>
                  <div className="stat">
                    <span className="stat-l">capital</span>
                    <span className="num">{fmtUSD(e.capital, 0)}</span>
                  </div>
                  <div className="stat">
                    <span className="stat-l">cadence</span>
                    <span className="num">daily</span>
                  </div>
                </div>
              ) : (
                <div className="engine-card-stats">
                  <div className="stat">
                    <span className="stat-l">credibility</span>
                    <Num v={e.credibility} fmt={fmtNum} decimals={0} />
                  </div>
                  <div className="stat">
                    <span className="stat-l">OOS Sharpe</span>
                    <Num v={e.oos_sharpe} fmt={fmtNum} decimals={3} signed />
                  </div>
                  <div className="stat">
                    <span className="stat-l">DSR</span>
                    <Num v={e.dsr} fmt={fmtNum} decimals={4} tone={e.dsr >= 0.95 ? 'pos' : 'warn'} />
                  </div>
                  <div className="stat">
                    <span className="stat-l">positions</span>
                    <Num v={e.n_positions} fmt={fmtNum} decimals={0} />
                  </div>
                  <div className="stat">
                    <span className="stat-l">capital</span>
                    <span className="num">{fmtUSD(e.capital, 0)}</span>
                  </div>
                  <div className="stat">
                    <span className="stat-l">alloc</span>
                    <span className="num">{(e.capital_pct * 100).toFixed(1)}%</span>
                  </div>
                </div>
              )}
              {e.credibility != null && (
                <GateBar score={e.credibility} threshold={60} label="credibility / 60 → graduate" />
              )}
              {e.credibility == null && (
                <div className="canary-note">{e.note}</div>
              )}
            </button>
          ))}
        </div>
      </Panel>

      {/* Two-up: holdings + signals/AAR */}
      <div className="split-2">
        <Panel
          title="Holdings"
          eyebrow={`${HOLDINGS.length} positions · ${fmtUSD(HOLDINGS.reduce((s,h)=>s+h.pl,0), 0)} unrealized`}
          noPad
        >
          <HoldingsTable rows={HOLDINGS} onPick={(t) => onNav('ticker:' + t)} />
        </Panel>

        <div className="stack">
          <Panel title="Today's signals" eyebrow={`${SIGNALS.length} pending · ${SIGNALS.filter(s=>s.note?.includes('BLOCKED')).length} blocked`} noPad>
            <SignalsList rows={SIGNALS} onPick={(t) => onNav('ticker:' + t)} />
          </Panel>

          <Panel title="Recent AARs" eyebrow={`${AARS.length} closed · ${AARS.filter(a=>a.pnl_pct>0).length}W ${AARS.filter(a=>a.pnl_pct<0).length}L`} noPad>
            <AARList rows={AARS} onPick={(t) => onNav('ticker:' + t)} />
          </Panel>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// HOLDINGS TABLE
// ──────────────────────────────────────────────────────────────────────
function HoldingsTable({ rows, onPick }) {
  const [sort, setSort] = React.useState({ key: 'pl', dir: 'desc' });
  const sorted = [...rows].sort((a, b) => {
    const m = sort.dir === 'asc' ? 1 : -1;
    const va = a[sort.key], vb = b[sort.key];
    if (typeof va === 'string') return va.localeCompare(vb) * m;
    return ((va ?? 0) - (vb ?? 0)) * m;
  });
  const flip = (k) => setSort(s => s.key === k ? { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: 'desc' });
  const H = ({ k, children, right }) => (
    <th className={right ? 't-right' : ''} onClick={() => flip(k)}>
      {children} {sort.key === k && <span className="sort-arr">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
    </th>
  );
  return (
    <table className="data-tbl">
      <thead>
        <tr>
          <th>Engine</th>
          <H k="ticker">Ticker</H>
          <H k="qty" right>Qty</H>
          <H k="avg_entry" right>Entry</H>
          <H k="last" right>Last</H>
          <H k="pl" right>P&L</H>
          <H k="pl_pct" right>P&L %</H>
          <H k="weight" right>Wgt</H>
          <th>Held</th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((r, i) => (
          <tr key={i} onClick={() => onPick(r.ticker)} className="row-clickable">
            <td><Pill tone={engineTone(r.engine)} dim>{r.engine.slice(0,3).toUpperCase()}</Pill></td>
            <td className="ticker-cell">
              {r.qty < 0 && <span className="short-marker">S</span>}
              <b>{r.ticker}</b>
            </td>
            <td className="t-right num">{r.qty}</td>
            <td className="t-right num">{fmtUSD(r.avg_entry, 2)}</td>
            <td className="t-right num">{fmtUSD(r.last, 2)}</td>
            <td className="t-right"><Num v={r.pl} fmt={fmtUSD} decimals={2} signed /></td>
            <td className="t-right"><Num v={r.pl_pct} fmt={fmtPct} decimals={2} signed /></td>
            <td className="t-right num muted">{(r.weight * 100).toFixed(2)}%</td>
            <td className="num muted">{daysSince(r.entry_date)}d</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function engineTone(id) {
  return id === 'momentum' ? 'mom' : id === 'reversion' ? 'rev' : id === 'vector' ? 'vec' : id === 'sentinel' ? 'sen' : 'can';
}

function daysSince(d) {
  const a = new Date(d), b = new Date('2026-05-17');
  return Math.round((b - a) / (1000 * 60 * 60 * 24));
}

// ──────────────────────────────────────────────────────────────────────
// SIGNALS LIST
// ──────────────────────────────────────────────────────────────────────
function SignalsList({ rows, onPick }) {
  return (
    <div className="feed">
      {rows.map((s, i) => {
        const blocked = s.note?.includes('BLOCKED');
        return (
          <div key={i} className={`feed-row ${blocked ? 'feed-row-dim' : ''}`} onClick={() => s.ticker !== '—' && onPick(s.ticker)}>
            <div className="feed-l">
              <Pill tone={engineTone(s.engine)} dim>{s.engine.slice(0,3).toUpperCase()}</Pill>
              <div className="feed-main">
                <div className="feed-title">
                  <b>{s.ticker}</b>
                  <span className={`side side-${s.side}`}>{s.side.toUpperCase()}</span>
                  {blocked && <Pill tone="warn" dim>BLOCKED</Pill>}
                </div>
                <div className="feed-sub muted">{s.note}</div>
              </div>
            </div>
            <div className="feed-r">
              <div className="strength-bar"><div style={{ width: (s.strength*100) + '%' }} /></div>
              <div className="feed-time muted">{s.time}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// AAR LIST
// ──────────────────────────────────────────────────────────────────────
function AARList({ rows, onPick }) {
  return (
    <div className="feed">
      {rows.map((a, i) => (
        <div key={i} className="feed-row" onClick={() => onPick(a.ticker)}>
          <div className="feed-l">
            <Pill tone={engineTone(a.engine)} dim>{a.engine.slice(0,3).toUpperCase()}</Pill>
            <div className="feed-main">
              <div className="feed-title">
                <b>{a.ticker}</b>
                <span className={`side side-${a.dir}`}>{a.dir.toUpperCase()}</span>
                <span className="feed-reason">{a.exit_reason}</span>
              </div>
              <div className="feed-sub muted">
                {a.entry} → {a.exit} · {a.hold}d hold · {a.qty} @ {fmtUSD(a.entry_px, 2)} → {fmtUSD(a.exit_px, 2)}
              </div>
            </div>
          </div>
          <div className="feed-r">
            <Num v={a.pnl_pct} fmt={fmtPct} decimals={2} signed className="feed-pl" />
          </div>
        </div>
      ))}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// ENGINE DETAIL
// ──────────────────────────────────────────────────────────────────────
function EngineView({ id, onNav }) {
  const e = ENGINE_BY_ID[id];
  if (!e) return <div className="view">Unknown engine: {id}</div>;
  const engineHoldings = HOLDINGS.filter(h => h.engine === id);
  const engineAARs = AARS.filter(a => a.engine === id);
  const engineSignals = SIGNALS.filter(s => s.engine === id);
  const isCanary = id === 'canary';

  return (
    <div className="view view-engine">
      <ViewHeader
        eyebrow={<span><StatusDot tone={e.state === 'PAPER_TRADING' ? 'pos' : 'neutral'} /> ENGINE · {e.id.toUpperCase()}</span>}
        title={<>{e.name} <span className="view-hd-kind">— {e.kind}</span></>}
        meta={[
          ['state', e.state, e.state === 'PAPER_TRADING' ? 'pos' : 'neutral'],
          ['rebalance', e.rebalance, 'neutral'],
          ['last', e.last_rebalance || '—', 'neutral'],
          ['next', e.next_rebalance, 'neutral'],
        ]}
        actions={
          <>
            {!isCanary && <ActionBtn kind="primary" hot>Run engine now</ActionBtn>}
            <ActionBtn>Open backtest →</ActionBtn>
          </>
        }
      />

      {isCanary && (
        <div className="banner banner-info">
          <div className="banner-l">
            <span className="banner-icon">●</span>
            <div>
              <div className="banner-title">Canary is intentionally non-graduating.</div>
              <div className="banner-sub">{e.note} — never writes credibility, allocator-excluded by omission (spec §4b, §5a).</div>
            </div>
          </div>
        </div>
      )}

      {/* Credibility / gate panel */}
      {!isCanary && (
        <div className="split-engine-top">
          <Panel title="Credibility & graduation gates" eyebrow="DSR ≥ 0.95 · credibility ≥ 60">
            <div className="gates">
              <GateRow label="Credibility score" value={e.credibility} threshold={60} fmt={(v)=>v.toFixed(0)} />
              <GateRow label="Deflated Sharpe Ratio (DSR)" value={e.dsr} threshold={0.95} fmt={(v)=>v.toFixed(4)} max={1} />
              <GateRow label="OOS Sharpe" value={e.oos_sharpe} threshold={1.0} fmt={(v)=>v.toFixed(3)} max={3} />
              <GateRow label="Profit factor" value={e.profit_factor} threshold={1.3} fmt={(v)=>v.toFixed(2)} max={2.5} />
              <GateRow label="Win rate" value={e.win_rate} threshold={0.50} fmt={(v)=>(v*100).toFixed(1)+'%'} max={1} />
              <GateRow label="Max drawdown" value={Math.abs(e.max_dd)} threshold={0.25} fmt={(v)=>(v*100).toFixed(1)+'%'} max={0.5} invert />
            </div>
            <div className="gate-summary">
              <Pill tone={e.gate_passed ? 'pos' : 'warn'} dim>{e.gate_passed ? 'PASSED' : 'GATED'}</Pill>
              <span className="muted">→ {e.gate_reason}</span>
            </div>
          </Panel>

          <Panel title="Best trial parameters" eyebrow={e.trial_id != null ? `trial #${e.trial_id}` : 'no trial'}>
            {e.params ? (
              <table className="kv-tbl">
                <tbody>
                  {Object.entries(e.params).map(([k, v]) => (
                    <tr key={k}><td className="kv-k">{k}</td><td className="kv-v num">{typeof v === 'number' ? v.toString() : v}</td></tr>
                  ))}
                </tbody>
              </table>
            ) : <div className="muted">No parameters logged.</div>}
          </Panel>
        </div>
      )}

      {/* engine-specific extra */}
      {id === 'momentum' && (
        <Panel title="Parameter search heatmap" eyebrow="lookback_days × hold_days · cell color = OOS Sharpe">
          <Heatmap
            data={MOM_TRIALS} xKey="hold" yKey="lookback" vKey="sharpe"
            xLabel="hold" yLabel="lookback" vLabel="sharpe"
          />
          <div className="heatmap-legend">
            <span className="muted">low</span>
            <div className="legend-grad" />
            <span className="muted">high</span>
            <span className="muted" style={{marginLeft: 'auto'}}>{MOM_TRIALS.length} trials · best Sharpe {Math.max(...MOM_TRIALS.map(t=>t.sharpe)).toFixed(2)}</span>
          </div>
        </Panel>
      )}

      {id === 'sentinel' && (
        <Panel
          title="Bear Score timeline"
          eyebrow={`phase=${e.phase} · score ${e.bear_score} / ${e.bear_threshold} activation`}
        >
          <BearScoreChart data={BEAR_TIMELINE} threshold={e.bear_threshold} height={180} />
        </Panel>
      )}

      <div className="split-2">
        <Panel title={`Active positions (${engineHoldings.length})`} noPad>
          {engineHoldings.length ? (
            <HoldingsTable rows={engineHoldings} onPick={(t)=>onNav('ticker:'+t)} />
          ) : (
            <div className="empty">No active positions.</div>
          )}
        </Panel>
        <Panel title={`Recent AARs (${engineAARs.length})`} noPad>
          {engineAARs.length ? <AARList rows={engineAARs} onPick={(t)=>onNav('ticker:'+t)} /> : <div className="empty">No closed trades yet.</div>}
        </Panel>
      </div>

      {engineSignals.length > 0 && (
        <Panel title="Today's signals from this engine" noPad>
          <SignalsList rows={engineSignals} onPick={(t)=>onNav('ticker:'+t)} />
        </Panel>
      )}
    </div>
  );
}

function GateRow({ label, value, threshold, fmt, max = 100, invert = false }) {
  const passed = invert ? value <= threshold : value >= threshold;
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  const thrPct = Math.min(100, Math.max(0, (threshold / max) * 100));
  return (
    <div className="gate-row">
      <div className="gate-row-label">{label}</div>
      <div className="gate-row-bar">
        <div className="gate-bar-track">
          <div className={`gate-bar-fill ${passed ? 'pass' : 'fail'}`} style={{ width: pct + '%' }} />
          <div className="gate-bar-threshold" style={{ left: thrPct + '%' }} />
        </div>
      </div>
      <div className="gate-row-val">
        <span className={`num num-${passed ? 'pos' : 'warn'}`}>{fmt(value)}</span>
        <span className="muted"> / {fmt(threshold)}</span>
      </div>
    </div>
  );
}

function BearScoreChart({ data, threshold, height = 180 }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(800);
  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const padL = 36, padR = 12, padT = 12, padB = 22;
  const chartW = Math.max(120, w - padL - padR);
  const chartH = height - padT - padB;
  const mn = 0, mx = 100;
  const xAt = (i) => padL + (i / (data.length - 1)) * chartW;
  const yAt = (v) => padT + (1 - (v - mn) / (mx - mn)) * chartH;
  const linePath = data.map((d, i) => `${i ? 'L' : 'M'}${xAt(i).toFixed(1)},${yAt(d.score).toFixed(1)}`).join(' ');
  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width={w} height={height}>
        {[0, 25, 50, 75, 100].map(t => (
          <g key={t}>
            <line x1={padL} x2={w-padR} y1={yAt(t)} y2={yAt(t)} className="grid-line" />
            <text x={padL-6} y={yAt(t)} className="grid-label" textAnchor="end" dy="0.35em">{t}</text>
          </g>
        ))}
        <line x1={padL} x2={w-padR} y1={yAt(threshold)} y2={yAt(threshold)} stroke="var(--warn)" strokeWidth="0.8" strokeDasharray="3 3" />
        <text x={w-padR-4} y={yAt(threshold) - 4} className="grid-label" textAnchor="end" fill="var(--warn)">activation {threshold}</text>
        <path d={`${linePath} L${xAt(data.length-1)},${padT+chartH} L${padL},${padT+chartH} Z`} fill="var(--accent)" opacity="0.1" />
        <path d={linePath} fill="none" stroke="var(--accent)" strokeWidth="1.2" />
      </svg>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// TICKER DRILL-IN
// ──────────────────────────────────────────────────────────────────────
function TickerView({ ticker, onNav }) {
  const ohlc = OHLC[ticker];
  const holding = HOLDINGS.find(h => h.ticker === ticker);
  const aars = AARS.filter(a => a.ticker === ticker);
  const sigs = SIGNALS.filter(s => s.ticker === ticker);

  const markers = [];
  if (holding) {
    markers.push({
      date: holding.entry_date, price: holding.avg_entry, type: 'entry',
      side: holding.qty < 0 ? 'short' : 'long',
      label: (holding.qty < 0 ? 'SHORT ' : 'LONG ') + fmtUSD(holding.avg_entry, 2),
    });
  }
  aars.forEach(a => {
    markers.push({ date: a.entry, price: a.entry_px, type: 'entry', side: a.dir, label: a.dir.toUpperCase() + ' ' + fmtUSD(a.entry_px, 2) });
    markers.push({ date: a.exit, price: a.exit_px, type: 'exit', exit_pl: a.pnl_pct, label: a.exit_reason });
  });

  return (
    <div className="view view-ticker">
      <ViewHeader
        eyebrow="TICKER"
        title={<>{ticker} <span className="view-hd-px">{ohlc ? fmtUSD(ohlc[ohlc.length-1].close, 2) : '—'}</span></>}
        actions={
          holding && (
            <>
              <Pill tone={engineTone(holding.engine)}>{holding.engine.toUpperCase()}</Pill>
              <Pill tone={holding.qty < 0 ? 'neg' : 'pos'} dim>{holding.qty < 0 ? 'SHORT' : 'LONG'} · {Math.abs(holding.qty)} sh</Pill>
              <Num v={holding.pl_pct} fmt={fmtPct} signed decimals={2} />
            </>
          )
        }
      />

      <Panel title="Price & trade history" eyebrow={ohlc ? `${ohlc.length} sessions` : ''}>
        {ohlc ? <CandleChart data={ohlc} markers={markers} height={320} /> : <div className="empty">No price data.</div>}
        <div className="legend-row">
          <span><span className="lg-dot lg-entry" /> entry</span>
          <span><span className="lg-dot lg-exit" /> exit</span>
          <span><span className="lg-dash lg-short" /> short</span>
          <span><span className="lg-dash lg-long"  /> long</span>
        </div>
      </Panel>

      <div className="split-2">
        <Panel title="Trade ledger" noPad>
          {aars.length || holding ? (
            <table className="data-tbl">
              <thead>
                <tr><th>Side</th><th>Entry</th><th>Exit</th><th className="t-right">Entry $</th><th className="t-right">Exit $</th><th className="t-right">Hold</th><th className="t-right">P&L %</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {holding && (
                  <tr className="row-open">
                    <td><span className={`side side-${holding.qty<0?'short':'long'}`}>{holding.qty<0?'SHORT':'LONG'}</span></td>
                    <td>{holding.entry_date}</td>
                    <td className="muted">— open —</td>
                    <td className="t-right num">{fmtUSD(holding.avg_entry,2)}</td>
                    <td className="t-right num">{fmtUSD(holding.last,2)}</td>
                    <td className="t-right num">{daysSince(holding.entry_date)}d</td>
                    <td className="t-right"><Num v={holding.pl_pct} fmt={fmtPct} signed /></td>
                    <td className="muted">—</td>
                  </tr>
                )}
                {aars.map((a,i) => (
                  <tr key={i}>
                    <td><span className={`side side-${a.dir}`}>{a.dir.toUpperCase()}</span></td>
                    <td>{a.entry}</td>
                    <td>{a.exit}</td>
                    <td className="t-right num">{fmtUSD(a.entry_px,2)}</td>
                    <td className="t-right num">{fmtUSD(a.exit_px,2)}</td>
                    <td className="t-right num">{a.hold}d</td>
                    <td className="t-right"><Num v={a.pnl_pct} fmt={fmtPct} signed /></td>
                    <td className="muted">{a.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="empty">No trade history.</div>}
        </Panel>

        <div className="stack">
          {sigs.length > 0 && (
            <Panel title="Active signals" noPad>
              <SignalsList rows={sigs} onPick={()=>{}} />
            </Panel>
          )}
          <Panel title="Signal context" eyebrow="At last evaluation">
            <table className="kv-tbl">
              <tbody>
                {holding?.z != null && <tr><td className="kv-k">z-score at entry</td><td className="kv-v num">{holding.z.toFixed(2)}</td></tr>}
                {holding?.rsi != null && <tr><td className="kv-k">RSI at entry</td><td className="kv-v num">{holding.rsi.toFixed(1)}</td></tr>}
                {holding?.trigger && <tr><td className="kv-k">trigger</td><td className="kv-v">{holding.trigger}</td></tr>}
                <tr><td className="kv-k">last bar</td><td className="kv-v num">{ohlc ? ohlc[ohlc.length-1].date : '—'}</td></tr>
                <tr><td className="kv-k">90d range</td><td className="kv-v num">{ohlc ? fmtUSD(Math.min(...ohlc.map(d=>d.low)),2) + ' – ' + fmtUSD(Math.max(...ohlc.map(d=>d.high)),2) : '—'}</td></tr>
              </tbody>
            </table>
          </Panel>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// DATA PIPELINE
// ──────────────────────────────────────────────────────────────────────
function DataView() {
  const passed = VALIDATION.filter(v => v.state === 'pass').length;
  const warned = VALIDATION.filter(v => v.state === 'warn').length;
  const failed = VALIDATION.filter(v => v.state === 'fail').length;
  return (
    <div className="view view-data">
      <ViewHeader
        eyebrow="SYSTEM"
        title="Data Pipeline"
        meta={[
          ['last update', '2026-05-17 14:32 UTC', 'neutral'],
          ['cycle latency', '312s', 'neutral'],
          ['self-heal', 'green', 'pos'],
        ]}
        actions={
          <>
            <ActionBtn kind="primary" hot>Run data update</ActionBtn>
            <ActionBtn>Run validation</ActionBtn>
            <ActionBtn>Audit pipeline</ActionBtn>
          </>
        }
      />

      <div className="kpi-strip">
        <KPI label="Checks passed"   value={`${passed}/${VALIDATION.length}`} tone="pos" />
        <KPI label="Warnings"        value={warned} tone={warned ? 'warn' : 'neutral'} />
        <KPI label="Failed"          value={failed} tone={failed ? 'neg' : 'pos'} />
        <KPI label="DATA_OPS event"  value="EMITTED" tone="pos" sub="14:32:11 UTC" />
        <KPI label="Confidence"      value="1.000" tone="pos" sub="validation gate" />
        <KPI label="Tickers tracked" value="3,142" />
        <KPI label="Daily bars"      value="2.85M" sub="rows in prices_daily" />
        <KPI label="Forensics"       value={`${FORENSICS.length} triggers`} tone={FORENSICS.some(f=>f.severity==='med')?'warn':'neutral'} />
      </div>

      <Panel title="Validation suite" eyebrow="13 checks · zero-tolerance invariants">
        <table className="data-tbl">
          <thead>
            <tr><th>Check</th><th>Status</th><th className="t-right">Rows</th><th className="t-right">Age</th><th>Notes</th></tr>
          </thead>
          <tbody>
            {VALIDATION.map(v => (
              <tr key={v.id}>
                <td className="num">{v.id}</td>
                <td>
                  <Pill tone={v.state === 'pass' ? 'pos' : v.state === 'warn' ? 'warn' : 'neg'} dim>
                    {v.state.toUpperCase()}
                  </Pill>
                </td>
                <td className="t-right num">{fmtInt(v.rows)}</td>
                <td className="t-right num">{v.age_min < 60 ? `${v.age_min}m` : `${(v.age_min/60).toFixed(1)}h`}</td>
                <td className="muted">{v.note || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Self-heal log" eyebrow="last 6 stage executions">
        <table className="data-tbl">
          <thead>
            <tr><th>Time</th><th>Stage</th><th>Result</th><th className="t-right">Duration</th><th>Notes</th></tr>
          </thead>
          <tbody>
            {HEAL_LOG.map((h, i) => (
              <tr key={i}>
                <td className="num">{h.ts}</td>
                <td className="num">{h.stage}</td>
                <td>
                  <Pill tone={h.result === 'green' ? 'pos' : 'neg'} dim>{h.result.toUpperCase()}</Pill>
                </td>
                <td className="t-right num">{h.duration_s.toFixed(1)}s</td>
                <td className="muted">{h.note || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// FORENSICS
// ──────────────────────────────────────────────────────────────────────
function ForensicsView() {
  return (
    <div className="view view-forensics">
      <ViewHeader
        eyebrow="SYSTEM"
        title="Forensics"
        meta={[
          ['AAR scanner', 'auto', 'pos'],
          ['runs', 'after every data cycle', 'neutral'],
          ['last', '2026-05-17 14:33 UTC', 'neutral'],
        ]}
        actions={
          <>
            <ActionBtn>Re-scan now</ActionBtn>
            <ActionBtn>Open sprint dossiers</ActionBtn>
          </>
        }
      />

      <Panel title="Active triggers" eyebrow={`${FORENSICS.length} triggers · ${FORENSICS.filter(f=>f.severity==='med').length} medium`} noPad>
        <table className="data-tbl">
          <thead>
            <tr><th>Time</th><th>Engine</th><th>Severity</th><th>Trigger</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {FORENSICS.map((f, i) => (
              <tr key={i}>
                <td className="num">{f.ts.slice(5,16).replace('T',' ')}</td>
                <td><Pill tone={engineTone(f.engine)} dim>{f.engine.slice(0,3).toUpperCase()}</Pill></td>
                <td><Pill tone={f.severity === 'med' ? 'warn' : f.severity === 'high' ? 'neg' : 'neutral'} dim>{f.severity.toUpperCase()}</Pill></td>
                <td className="num">{f.trigger}</td>
                <td className="muted">{f.msg}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// ALLOCATOR
// ──────────────────────────────────────────────────────────────────────
function AllocatorView() {
  return (
    <div className="view view-allocator">
      <ViewHeader
        eyebrow="SYSTEM"
        title="Allocator"
        meta={[
          ['method', ALLOCATOR.method, 'neutral'],
          ['trigger', ALLOCATOR.trigger, 'neutral'],
          ['last', ALLOCATOR.last_rebalance.slice(0,16).replace('T',' '), 'neutral'],
          ['next', ALLOCATOR.next_rebalance.slice(0,16).replace('T',' '), 'neutral'],
        ]}
        actions={<ActionBtn kind="primary">Force rebalance</ActionBtn>}
      />

      <Panel title="Capital allocation" eyebrow="current vs target · inverse-vol weighting">
        <div className="alloc-bar">
          {ALLOCATOR.weights.map(w => (
            <div
              key={w.engine}
              className={`alloc-seg alloc-${w.engine}`}
              style={{ flexBasis: (w.current * 100) + '%' }}
              title={`${w.engine}: ${(w.current*100).toFixed(1)}%`}
            >
              <span>{w.engine}</span>
              <span>{(w.current * 100).toFixed(1)}%</span>
            </div>
          ))}
        </div>

        <table className="data-tbl" style={{ marginTop: 16 }}>
          <thead>
            <tr><th>Engine</th><th className="t-right">Current</th><th className="t-right">Target</th><th className="t-right">Drift</th><th className="t-right">30d vol</th><th>Status</th></tr>
          </thead>
          <tbody>
            {ALLOCATOR.weights.map(w => {
              const drift = w.current - w.target;
              return (
                <tr key={w.engine}>
                  <td className="num"><Pill tone={w.engine === 'cash' ? 'neutral' : engineTone(w.engine)} dim>{w.engine.toUpperCase()}</Pill></td>
                  <td className="t-right num">{(w.current * 100).toFixed(2)}%</td>
                  <td className="t-right num">{(w.target * 100).toFixed(2)}%</td>
                  <td className="t-right"><Num v={drift} fmt={(v)=>fmtPct(v,2)} decimals={2} signed /></td>
                  <td className="t-right num muted">{(w.vol_30d * 100).toFixed(1)}%</td>
                  <td>{Math.abs(drift) < 0.01 ? <Pill tone="pos" dim>BALANCED</Pill> : <Pill tone="warn" dim>DRIFT</Pill>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

Object.assign(window, {
  OverviewView, EngineView, TickerView, DataView, ForensicsView, AllocatorView,
});
