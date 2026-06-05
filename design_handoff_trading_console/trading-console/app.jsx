// app.jsx — main shell + sidebar nav + top bar + tweaks panel

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "operator-dark",
  "accent": "amber",
  "density": "comfortable",
  "monoFont": "JetBrains Mono",
  "showActivityFeed": true,
  "showBenchmark": true
}/*EDITMODE-END*/;

// Nav groups — each item: {id, label, kbd, tone?, badge?}
function buildNav() {
  const openHolds = SOURCE_HOLDS.length;
  const openEsc = RECENT_ESCALATIONS.filter(e => e.open).length;
  const healthBadge = (openHolds + openEsc) || 0;
  const digestBadge = WEEKLY_DIGEST.acked ? 0 : 1;
  const ecrBadge = ECR_QUEUE.length || 0;
  const labBadge = LAB_RUNS.filter(r => r.promotion_pending).length || 0;
  return [
    { kind: 'group', label: 'Portfolio' },
    { kind: 'item',  id: 'overview', label: 'Overview', kbd: 'O' },
    { kind: 'item',  id: 'forensics', label: 'Forensics', kbd: 'F',
      badge: FORENSICS.filter(f=>f.severity==='med'||f.severity==='high').length || null },

    { kind: 'group', label: 'Engines (Live)' },
    { kind: 'item',  id: 'engine:momentum',  label: 'Momentum',  kbd: '1', tone: 'mom' },
    { kind: 'item',  id: 'engine:reversion', label: 'Reversion', kbd: '2', tone: 'rev' },
    { kind: 'item',  id: 'engine:vector',    label: 'Vector',    kbd: '3', tone: 'vec' },
    { kind: 'item',  id: 'engine:sentinel',  label: 'Sentinel',  kbd: '4', tone: 'sen' },
    { kind: 'item',  id: 'engine:canary',    label: 'Canary',    kbd: '5', tone: 'can' },

    { kind: 'group', label: 'Engine SDLC' },
    { kind: 'item',  id: 'lab',   label: 'The Lab',  kbd: 'L', badge: labBadge || null, badgeTone: 'warn' },
    { kind: 'item',  id: 'sdlc',  label: 'ECR Queue', kbd: 'E', badge: ecrBadge || null, badgeTone: 'warn' },

    { kind: 'group', label: 'Capital' },
    { kind: 'item',  id: 'allocator', label: 'Allocator', kbd: 'A' },

    { kind: 'group', label: 'Operations' },
    { kind: 'item',  id: 'health',  label: 'Health',  kbd: 'H', badge: healthBadge || null, badgeTone: 'warn' },
    { kind: 'item',  id: 'digest',  label: 'Weekly Digest', kbd: 'W', badge: digestBadge || null, badgeTone: 'warn' },
    { kind: 'item',  id: 'data',    label: 'Data Pipeline', kbd: 'D' },
    { kind: 'item',  id: 'providers', label: 'Providers', kbd: 'P' },
  ];
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [route, setRoute] = React.useState('overview');
  const [now, setNow] = React.useState(new Date('2026-05-18T14:32:11Z'));

  // tick clock (fake)
  React.useEffect(() => {
    const id = setInterval(() => setNow(d => new Date(d.getTime() + 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  // keyboard nav
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      const map = {
        o: 'overview', f: 'forensics',
        '1': 'engine:momentum', '2': 'engine:reversion',
        '3': 'engine:vector', '4': 'engine:sentinel', '5': 'engine:canary',
        a: 'allocator', h: 'health', w: 'digest', d: 'data', p: 'providers',
        l: 'lab', e: 'sdlc',
      };
      if (map[k]) { setRoute(map[k]); e.preventDefault(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const render = () => {
    if (route === 'overview')         return <OverviewView onNav={setRoute} />;
    if (route.startsWith('engine:'))  return <EngineView id={route.slice(7)} onNav={setRoute} />;
    if (route.startsWith('ticker:'))  return <TickerView ticker={route.slice(7)} onNav={setRoute} />;
    if (route === 'data')             return <DataView />;
    if (route === 'forensics')        return <ForensicsView />;
    if (route === 'allocator')        return <AllocatorView />;
    if (route === 'health')           return <HealthView onNav={setRoute} />;
    if (route === 'digest')           return <DigestView onNav={setRoute} />;
    if (route === 'providers')        return <ProvidersView />;
    if (route === 'lab')              return <LabView onNav={setRoute} />;
    if (route === 'sdlc')             return <SDLCView onNav={setRoute} />;
    return <OverviewView onNav={setRoute} />;
  };

  const rootClass = [
    `theme-${t.theme}`,
    `accent-${t.accent}`,
    `density-${t.density}`,
    !t.showActivityFeed && 'actrail-off',
  ].filter(Boolean).join(' ');
  const rootStyle = { '--mono-font': t.monoFont + ', ui-monospace, monospace' };

  const utcH = now.getUTCHours(), utcM = now.getUTCMinutes();
  const minutes = utcH * 60 + utcM;
  const open = minutes >= 13*60+30 && minutes < 20*60;
  const sessionState = open ? 'OPEN' : (minutes < 13*60+30 ? 'PRE-MKT' : 'AFTER-HRS');

  const nav = buildNav();

  // Breadcrumb
  const crumb = () => {
    if (route === 'overview')        return ['Portfolio', 'Overview'];
    if (route === 'forensics')       return ['Portfolio', 'Forensics'];
    if (route.startsWith('engine:')) return ['Engines', ENGINE_BY_ID[route.slice(7)]?.name || route.slice(7)];
    if (route.startsWith('ticker:')) return ['Tickers', route.slice(7)];
    if (route === 'data')            return ['Operations', 'Data Pipeline'];
    if (route === 'providers')       return ['Operations', 'Providers'];
    if (route === 'health')          return ['Operations', 'Health'];
    if (route === 'digest')          return ['Operations', 'Weekly Digest'];
    if (route === 'allocator')       return ['Capital', 'Allocator'];
    if (route === 'lab')             return ['Engine SDLC', 'The Lab'];
    if (route === 'sdlc')            return ['Engine SDLC', 'ECR Queue'];
    return [route];
  };

  return (
    <div className={`root ${rootClass}`} style={rootStyle} data-screen-label={routeLabel(route)}>
      {/* Top bar */}
      <header className="topbar">
        <div className="topbar-l">
          <div className="brand">
            <div className="brand-mark">◤</div>
            <div className="brand-name">STE<span>·</span>TRADING ENGINE</div>
          </div>
        </div>
        <div className="topbar-c">
          <div className="topbar-status">
            <span className="ts-grp"><StatusDot tone={open ? 'pos' : 'neutral'} /><b>NYSE</b><span className="muted">{sessionState}</span></span>
            <span className="ts-grp"><StatusDot tone="pos" /><b>BROKER</b><span className="muted">alpaca·paper</span></span>
            <span className="ts-grp"><StatusDot tone="pos" /><b>DATA</b><span className="muted">fresh 47m</span></span>
            <span className="ts-grp"><StatusDot tone={RISK_STATE.circuit_breaker_armed ? 'neg' : 'pos'} /><b>RISK</b><span className="muted">{RISK_STATE.circuit_breaker_armed ? 'ARMED' : 'nominal'}</span></span>
            <span className="ts-grp"><StatusDot tone={WEEKLY_DIGEST.live_clearance === 'green' ? 'pos' : 'warn'} /><b>LIVE</b><span className="muted">{WEEKLY_DIGEST.live_clearance}</span></span>
          </div>
        </div>
        <div className="topbar-r">
          <div className="topbar-pl">
            <span className="muted">P&L today</span>
            <Num v={ACCOUNT.day_pl_pct} fmt={fmtPct} signed decimals={2} className="topbar-pl-pct" />
            <Num v={ACCOUNT.day_pl} fmt={fmtUSD} signed decimals={0} className="topbar-pl-usd" />
          </div>
          <div className="topbar-equity">
            <span className="muted">EQUITY</span>
            <b>{fmtUSD(ACCOUNT.equity, 0)}</b>
          </div>
          <div className="topbar-clock num">
            {now.toISOString().slice(11, 19)} <span className="muted">UTC</span>
          </div>
        </div>
      </header>

      <div className="shell">
        {/* Left rail */}
        <nav className="rail">
          {nav.map((n, i) => n.kind === 'group' ? (
            <div key={'g'+i} className="rail-sep">{n.label}</div>
          ) : (
            <button
              key={n.id}
              className={'rail-item' + (route === n.id ? ' rail-item-on' : '') + (n.tone ? ' rail-tone-' + n.tone : '')}
              onClick={() => setRoute(n.id)}
            >
              {n.tone && <span className={`rail-tone-dot rail-tone-dot-${n.tone}`} />}
              <span className="rail-label">{n.label}</span>
              {n.badge != null && <span className={`rail-badge rail-badge-${n.badgeTone || 'neutral'}`}>{n.badge}</span>}
              <span className="rail-kbd">{n.kbd}</span>
            </button>
          ))}
          <div className="rail-foot">
            <div className="rail-foot-row">
              <span className="muted">capital</span>
              <span className="num">{fmtUSD(ACCOUNT.equity, 0)}</span>
            </div>
            <div className="rail-foot-row">
              <span className="muted">unallocated</span>
              <span className="num">{fmtUSD(ACCOUNT.cash, 0)}</span>
            </div>
            <div className="rail-foot-row">
              <span className="muted">heartbeat</span>
              <span className="num heartbeat-dot">●</span>
            </div>
          </div>
        </nav>

        {/* Main content */}
        <main className="main">
          <div className="crumb">
            {crumb().map((c, i, arr) => (
              <React.Fragment key={i}>
                <span className={i === arr.length - 1 ? 'crumb-cur' : 'crumb-anc'}>{c}</span>
                {i < arr.length - 1 && <span className="crumb-sep">›</span>}
              </React.Fragment>
            ))}
          </div>
          {render()}
        </main>

        {/* Right activity rail */}
        <aside className="actrail">
          <div className="actrail-hd">
            <span>ACTIVITY</span>
            <span className="muted">live</span>
          </div>
          <div className="actrail-feed">
            <ActivityItem kind="DIGEST" time="06:00:00" msg="Weekly digest ready — needs ack" tone="warn" />
            <ActivityItem kind="HEAL"   time="14:32:11" msg="data_validation → green" tone="pos" />
            <ActivityItem kind="HEAL"   time="14:31:53" msg="daily_bars → green (142s, 4 repaired)" tone="warn" />
            <ActivityItem kind="SIGNAL" time="14:18:02" msg="reversion · JNJ short z=3.21" tone="pos" />
            <ActivityItem kind="SIGNAL" time="14:11:44" msg="reversion · DUK short BLOCKED" tone="warn" />
            <ActivityItem kind="LLM"    time="09:14:00" msg="triage proposal for req-7a3e" tone="warn" />
            <ActivityItem kind="ALLOC"  time="13:00:00" msg="weekly rebalance · drift &lt; 1%" tone="pos" />
            <ActivityItem kind="AAR"    time="13:18:55" msg="reversion · NFLX → tier2 +6.62%" tone="pos" />
            <ActivityItem kind="AAR"    time="11:04:22" msg="vector · GM → hard_stop −7.26%" tone="neg" />
            <ActivityItem kind="ALERT"  time="09:31:42" msg="forensics · consecutive_stops" tone="warn" />
            <ActivityItem kind="HOLD"   time="04:22:00" msg="finnhub_insider_sentiment HELD" tone="warn" />
            <ActivityItem kind="SYSTEM" time="08:00:00" msg="sentinel · bear_score 22 / 60" />
            <ActivityItem kind="HEAL"   time="07:45:01" msg="earnings_refresh → green" tone="pos" />
            <ActivityItem kind="SYSTEM" time="06:00:00" msg="run_data_operations.sh started" />
          </div>
        </aside>
      </div>

      {/* Tweaks panel */}
      <TweaksPanel>
        <TweakSection label="Theme" />
        <TweakRadio
          label="Theme"
          value={t.theme}
          options={[
            { value: 'operator-dark', label: 'op-dark' },
            { value: 'midnight',      label: 'midnight' },
            { value: 'paper',         label: 'paper' },
          ]}
          onChange={(v) => setTweak('theme', v)}
        />
        <TweakColor
          label="Accent"
          value={t.accent}
          options={[
            { value: 'amber',  swatch: '#d97706' },
            { value: 'green',  swatch: '#10b981' },
            { value: 'cyan',   swatch: '#06b6d4' },
            { value: 'violet', swatch: '#8b5cf6' },
          ]}
          onChange={(v) => setTweak('accent', v)}
        />
        <TweakSection label="Layout" />
        <TweakRadio
          label="Density"
          value={t.density}
          options={['compact', 'comfortable']}
          onChange={(v) => setTweak('density', v)}
        />
        <TweakSelect
          label="Mono font"
          value={t.monoFont}
          options={['JetBrains Mono', 'IBM Plex Mono', 'Berkeley Mono', 'Geist Mono', 'ui-monospace']}
          onChange={(v) => setTweak('monoFont', v)}
        />
        <TweakToggle label="Activity feed" value={t.showActivityFeed} onChange={(v)=>setTweak('showActivityFeed', v)} />
        <TweakToggle label="Equity benchmark line" value={t.showBenchmark} onChange={(v)=>setTweak('showBenchmark', v)} />
      </TweaksPanel>
    </div>
  );
}

function ActivityItem({ kind, time, msg, tone = 'neutral' }) {
  return (
    <div className={`act-row act-row-${tone}`}>
      <span className="act-time">{time}</span>
      <span className={`act-kind act-kind-${kind.toLowerCase()}`}>{kind}</span>
      <span className="act-msg" dangerouslySetInnerHTML={{ __html: msg }} />
    </div>
  );
}

function routeLabel(route) {
  if (route === 'overview')        return '01 Overview';
  if (route === 'data')            return 'Data Pipeline';
  if (route === 'allocator')       return 'Allocator';
  if (route === 'forensics')       return 'Forensics';
  if (route === 'health')          return 'Health';
  if (route === 'digest')          return 'Weekly Digest';
  if (route === 'providers')       return 'Providers';
  if (route === 'lab')             return 'The Lab';
  if (route === 'sdlc')            return 'Engine SDLC';
  if (route.startsWith('engine:')) {
    const id = route.slice(7);
    return 'Engine · ' + id[0].toUpperCase() + id.slice(1);
  }
  if (route.startsWith('ticker:')) return 'Ticker · ' + route.slice(7);
  return route;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
