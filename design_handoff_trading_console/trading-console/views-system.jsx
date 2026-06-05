// views-system.jsx — Health (Ladder/Supervisor/Auditheal/Escalations) +
// Weekly Digest + Daemons + Providers views

// ──────────────────────────────────────────────────────────────────────
// HEALTH — Escalation Ladder + holds + cross-table audit + recent escalations
// ──────────────────────────────────────────────────────────────────────
function HealthView({ onNav }) {
  const openHolds = SOURCE_HOLDS.length;
  const openEsc = RECENT_ESCALATIONS.filter(e => e.open).length;
  const undisp = LADDER.find(l => l.rung === 3)?.count || 0;
  const auditWarn = CROSS_TABLE_AUDIT.filter(c => c.state !== 'pass').length;
  const liveClearance = WEEKLY_DIGEST.live_clearance;

  return (
    <div className="view view-health">
      <ViewHeader
        eyebrow="SYSTEM"
        title="Health"
        meta={[
          ['live clearance', liveClearance.toUpperCase(), liveClearance === 'green' ? 'pos' : 'warn'],
          ['weeks unacked', WEEKLY_DIGEST.weeks_unacked, WEEKLY_DIGEST.weeks_unacked >= 2 ? 'neg' : WEEKLY_DIGEST.weeks_unacked >= 1 ? 'warn' : 'pos'],
          ['daemons', `${DAEMONS.filter(d=>d.status==='green').length}/${DAEMONS.length} live`, 'pos'],
        ]}
        actions={
          <>
            <ActionBtn onClick={() => onNav('digest')}>Open weekly digest</ActionBtn>
            <ActionBtn onClick={() => onNav('data')}>Data pipeline</ActionBtn>
          </>
        }
      />

      {/* Top-level rollup */}
      <div className="kpi-strip">
        <KPI label="Open holds"             value={openHolds}        tone={openHolds ? 'warn' : 'pos'}
             sub={openHolds ? 'data supervisor' : 'all clear'} />
        <KPI label="Open escalations (7d)"  value={openEsc}          tone={openEsc ? 'warn' : 'pos'} />
        <KPI label="Undispositioned"        value={undisp}           tone={undisp ? 'warn' : 'pos'}
             sub="past 7-day grace" />
        <KPI label="Cross-table audit"      value={auditWarn ? `${auditWarn} warn` : 'clean'} tone={auditWarn ? 'warn' : 'pos'} />
        <KPI label="LLM proposals open"     value={LLM_TRIAGE.length} sub="awaiting human review" />
        <KPI label="Self-heal cycles 24h"   value="6"                tone="pos" sub="median 142s" />
      </div>

      {/* Escalation & Hardening Ladder */}
      <Panel title="Escalation & Hardening Ladder" eyebrow="data lane · 5 rungs · clockwork-enforced">
        <div className="ladder">
          {LADDER.map(l => (
            <div key={l.rung} className={`ladder-row ladder-kind-${l.kind}`}>
              <div className="ladder-num">R{l.rung}</div>
              <div className="ladder-main">
                <div className="ladder-name">{l.name}</div>
                <div className="ladder-detail">{l.detail}</div>
              </div>
              <div className="ladder-status">
                <Pill tone={l.kind === 'green' ? 'pos' : l.kind === 'warn' ? 'warn' : 'neutral'} dim>{l.status}</Pill>
                <span className="ladder-count num">{l.count}</span>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {/* Two-column: open holds + cross-table audit */}
      <div className="split-2">
        <Panel
          title="Data supervisor — open holds"
          eyebrow={openHolds ? `${openHolds} source held · drive to disposition` : 'no open holds'}
          noPad
        >
          {openHolds ? (
            <table className="data-tbl">
              <thead><tr><th>Source</th><th>Held</th><th className="t-right">Cycles</th><th>Reason</th><th>Esc</th></tr></thead>
              <tbody>
                {SOURCE_HOLDS.map(h => (
                  <tr key={h.source}>
                    <td className="num"><b>{h.source}</b></td>
                    <td className="num">{h.age_h.toFixed(0)}h</td>
                    <td className="t-right num">{h.cycles_held}</td>
                    <td className="muted">{h.reason}</td>
                    <td>{h.escalated ? <Pill tone="neg" dim>ESCALATED</Pill> : <Pill tone="warn" dim>HELD</Pill>}</td>
                  </tr>
                ))}
                {SOURCE_CLEAR_HISTORY.map(h => (
                  <tr key={h.source} className="row-resolved">
                    <td className="num muted">{h.source}</td>
                    <td className="num muted">cleared</td>
                    <td className="t-right num muted">—</td>
                    <td className="muted">{h.reason}</td>
                    <td><Pill tone={h.auto ? 'pos' : 'neutral'} dim>{h.auto ? 'AUTO' : 'OPERATOR'}</Pill></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="empty">No open holds.</div>}
        </Panel>

        <Panel
          title="Cross-table audit (auditheal)"
          eyebrow={`${CROSS_TABLE_AUDIT.length} sources · ${auditWarn ? auditWarn + ' warning' : 'all clean'}`}
          noPad
        >
          <table className="data-tbl">
            <thead><tr><th>Source</th><th>Status</th><th>Last</th><th>Note</th></tr></thead>
            <tbody>
              {CROSS_TABLE_AUDIT.map(c => (
                <tr key={c.source}>
                  <td className="num"><b>{c.source}</b></td>
                  <td><Pill tone={c.state === 'pass' ? 'pos' : c.state === 'warn' ? 'warn' : 'neg'} dim>{c.state.toUpperCase()}</Pill></td>
                  <td className="num muted">{c.last.slice(11,16)}</td>
                  <td className="muted">{c.note || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      {/* Recent escalations table */}
      <Panel
        title="Recent escalations (last 7d)"
        eyebrow={`${RECENT_ESCALATIONS.length} total · ${openEsc} open · ${LLM_TRIAGE.length} with LLM proposal`}
        noPad
      >
        <table className="data-tbl">
          <thead>
            <tr>
              <th>When</th><th>Type</th><th>Ref</th><th>Class</th><th>Status</th><th>Message</th><th>LLM</th>
            </tr>
          </thead>
          <tbody>
            {RECENT_ESCALATIONS.map((e, i) => (
              <tr key={i} className={e.open ? '' : 'row-resolved'}>
                <td className="num muted">{e.ts.slice(5,16).replace('T',' ')}</td>
                <td className="num">{e.etype}</td>
                <td className="num"><b>{e.ref}</b></td>
                <td className="num muted">{e.cls}</td>
                <td>
                  <Pill tone={e.open ? 'warn' : 'pos'} dim>{e.open ? 'OPEN' : 'RESOLVED'}</Pill>
                </td>
                <td className="muted ellipsis">{e.msg}</td>
                <td>{e.has_llm_proposal ? <a href="#" onClick={(ev)=>{ev.preventDefault(); onNav('digest');}} className="link">{e.llm_pr}</a> : <span className="muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {/* Daemons */}
      <Panel title="Daemon topology" eyebrow="two-daemon invariant · one long-lived per lane + data-ops cron" noPad>
        <table className="data-tbl">
          <thead><tr><th>Daemon</th><th>Lane</th><th>PID</th><th className="t-right">Uptime</th><th className="t-right">Last beat</th><th>Status</th><th>Role</th></tr></thead>
          <tbody>
            {DAEMONS.map(d => (
              <tr key={d.id}>
                <td className="num"><b>{d.id}</b></td>
                <td><Pill tone={d.lane === 'engine' ? 'mom' : 'rev'} dim>{d.lane}</Pill></td>
                <td className="num muted">{d.pid ?? '—'}</td>
                <td className="t-right num">{d.uptime_h ? d.uptime_h.toFixed(1) + 'h' : '—'}</td>
                <td className="t-right num">{d.last_heartbeat_s ? d.last_heartbeat_s + 's' : '—'}</td>
                <td><Pill tone={d.status === 'green' ? 'pos' : d.status === 'cron' ? 'neutral' : 'warn'} dim>{d.status === 'cron' ? 'CRON' : 'LIVE'}</Pill></td>
                <td className="muted">{d.role}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// WEEKLY DIGEST — operator ack flow + LLM triage proposals
// ──────────────────────────────────────────────────────────────────────
function DigestView({ onNav }) {
  const [acked, setAcked] = React.useState(WEEKLY_DIGEST.acked);
  const liveTone = WEEKLY_DIGEST.weeks_unacked >= 2 ? 'neg' : WEEKLY_DIGEST.weeks_unacked >= 1 ? 'warn' : 'pos';

  return (
    <div className="view view-digest">
      <ViewHeader
        eyebrow="SYSTEM"
        title="Weekly Digest"
        subtitle={`Week of ${WEEKLY_DIGEST.week_of} · generated ${WEEKLY_DIGEST.generated_at.slice(0,16).replace('T',' ')} UTC`}
        meta={[
          ['weeks unacked', WEEKLY_DIGEST.weeks_unacked, liveTone],
          ['threshold', `≥${WEEKLY_DIGEST.live_clearance_threshold} ⇒ de-escalate live`, 'neutral'],
          ['live clearance', WEEKLY_DIGEST.live_clearance.toUpperCase(), liveTone],
        ]}
        actions={
          acked ? <Pill tone="pos">ACKNOWLEDGED</Pill> :
          <ActionBtn kind="primary" hot onClick={() => setAcked(true)}>Acknowledge week</ActionBtn>
        }
      />

      {!acked && (
        <div className="banner banner-warn">
          <div className="banner-l">
            <span className="banner-icon">⚠</span>
            <div>
              <div className="banner-title">This week's digest needs your acknowledgment.</div>
              <div className="banner-sub">Two consecutive unacked weeks automatically de-escalate live trading clearance.</div>
            </div>
          </div>
          <button className="action-btn action-btn-primary" onClick={() => setAcked(true)}>Acknowledge</button>
        </div>
      )}

      {/* Digest sections as expandable cards */}
      <div className="digest-grid">
        {WEEKLY_DIGEST.sections.map(s => (
          <DigestSection key={s.id} section={s} onNav={onNav} />
        ))}
      </div>

      {/* LLM triage proposals (two lanes — data + engine) */}
      <Panel title="LLM triage proposals" eyebrow="advisory · human-merge-only · two crash-isolated co-tasks (data + engine)">
        {LLM_TRIAGE.length === 0 ? (
          <div className="empty">No open LLM triage proposals.</div>
        ) : LLM_TRIAGE.map((p, i) => (
          <div key={i} className={`triage-card triage-lane-${p.lane}`}>
            <div className="triage-hd">
              <div>
                <div className="triage-eyebrow">
                  <Pill tone={p.lane === 'data' ? 'rev' : 'mom'} dim>{p.lane === 'data' ? 'DATA LANE' : 'ENGINE LANE'}</Pill>
                  <span className="muted">ref</span> <b>{p.ref}</b>
                  <span className="muted">· class</span> <span className="num">{p.cls}</span>
                </div>
                <div className="triage-title">Proposed: <b className="num-warn">{p.proposed_disposition}</b> <span className="muted">(confidence {(p.confidence * 100).toFixed(0)}%)</span></div>
              </div>
              <div className="triage-meta">
                <span className="muted">{p.model} · {p.persona_version}</span>
                <Pill tone="warn" dim>{p.pr_status}</Pill>
                <span className="triage-fence">CI fence: <code className="inline-code">{p.fence}</code></span>
              </div>
            </div>
            <p className="triage-rationale">{p.rationale}</p>
            <div className="triage-actions">
              <ActionBtn>View {p.pr}</ActionBtn>
              <ActionBtn>Override disposition</ActionBtn>
              <ActionBtn>Reject proposal</ActionBtn>
            </div>
          </div>
        ))}
      </Panel>

      {/* Ack history */}
      <Panel title="Ack history" eyebrow={`${WEEKLY_DIGEST.history.length} prior weeks`} noPad>
        <table className="data-tbl">
          <thead><tr><th>Week of</th><th>Status</th><th>Acknowledged</th></tr></thead>
          <tbody>
            <tr>
              <td className="num"><b>{WEEKLY_DIGEST.week_of}</b></td>
              <td>{acked ? <Pill tone="pos" dim>ACKED</Pill> : <Pill tone="warn" dim>PENDING</Pill>}</td>
              <td className="num muted">{acked ? 'just now' : '—'}</td>
            </tr>
            {WEEKLY_DIGEST.history.map(h => (
              <tr key={h.week_of} className="row-resolved">
                <td className="num muted">{h.week_of}</td>
                <td><Pill tone="pos" dim>ACKED</Pill></td>
                <td className="num muted">{h.ts.slice(0,16).replace('T',' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

function DigestSection({ section, onNav }) {
  const [open, setOpen] = React.useState(section.id === 'undispositioned' || section.id === 'adversarial');
  const tone = section.id === 'undispositioned' ? 'warn' : section.id === 'adversarial' ? 'warn' : 'neutral';
  return (
    <div className={`digest-card digest-card-${tone}`}>
      <button className="digest-hd" onClick={() => setOpen(o => !o)}>
        <div className="digest-hd-l">
          <span className="digest-caret">{open ? '▾' : '▸'}</span>
          <span className="digest-name">{section.label}</span>
        </div>
        <div className="digest-hd-r">
          <Pill tone={tone} dim>{section.count}</Pill>
        </div>
      </button>
      {open && (
        <ul className="digest-items">
          {section.items.map((it, i) => <li key={i} className="digest-item">{it}</li>)}
        </ul>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// PROVIDERS — Data Provider Lifecycle
// ──────────────────────────────────────────────────────────────────────
function ProvidersView() {
  // group by feed
  const feeds = {};
  PROVIDERS.forEach(p => { (feeds[p.feed] ||= []).push(p); });
  return (
    <div className="view">
      <ViewHeader
        eyebrow="SYSTEM"
        title="Providers"
        subtitle="Data Provider Lifecycle · feed/provider decoupled via ProviderBinding registry"
        actions={<ActionBtn>Open feed change request</ActionBtn>}
      />
      <Panel title="Provider bindings" eyebrow="exactly one ACTIVE per feed · FALLBACK must be parity-verified" noPad>
        <table className="data-tbl">
          <thead><tr><th>Feed</th><th>Provider</th><th>Status</th><th>Since</th><th className="t-right">Parity</th></tr></thead>
          <tbody>
            {Object.entries(feeds).map(([feed, providers]) =>
              providers.map((p, i) => (
                <tr key={feed + '-' + p.provider}>
                  <td className="num"><b>{i === 0 ? feed : ''}</b></td>
                  <td className="num">{p.provider}</td>
                  <td><Pill tone={p.status === 'ACTIVE' ? 'pos' : p.status === 'FALLBACK' ? 'warn' : 'neutral'} dim>{p.status}</Pill></td>
                  <td className="num muted">{p.since}</td>
                  <td className="t-right num">{p.parity}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Shared view header (used across new views)
// ──────────────────────────────────────────────────────────────────────
function ViewHeader({ eyebrow, title, subtitle, meta, actions }) {
  return (
    <header className="view-hd">
      <div className="view-hd-l">
        {eyebrow && <div className="view-hd-eyebrow">{eyebrow}</div>}
        <h1 className="view-hd-title">{title}</h1>
        {subtitle && <div className="view-hd-sub">{subtitle}</div>}
        {meta && (
          <div className="view-hd-meta">
            {meta.map(([k, v, tone], i) => (
              <span key={i} className="view-hd-meta-item">
                <span className="view-hd-meta-k">{k}</span>
                <span className={`view-hd-meta-v ${tone ? 'num-' + tone : ''}`}>{v}</span>
              </span>
            ))}
          </div>
        )}
      </div>
      {actions && <div className="view-hd-r">{actions}</div>}
    </header>
  );
}

Object.assign(window, {
  HealthView, DigestView, ProvidersView, ViewHeader,
});
