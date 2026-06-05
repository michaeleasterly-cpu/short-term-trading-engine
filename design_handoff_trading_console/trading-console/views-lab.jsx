// views-lab.jsx — The Lab (SDLC SP2 sandbox) + Engine SDLC (ECR queue)

// ──────────────────────────────────────────────────────────────────────
// LAB — walk-forward parameter search sandbox
// ──────────────────────────────────────────────────────────────────────
function LabView({ onNav }) {
  const [selectedId, setSelectedId] = React.useState(LAB_RUNS[0].id);
  const selected = LAB_RUNS.find(r => r.id === selectedId) || LAB_RUNS[0];
  const survived = LAB_RUNS.filter(r => r.verdict === 'SURVIVED').length;
  const failed = LAB_RUNS.filter(r => r.verdict === 'FAILED').length;
  const pendingPromo = LAB_RUNS.filter(r => r.promotion_pending).length;

  return (
    <div className="view view-lab">
      <ViewHeader
        eyebrow="OPERATIONS"
        title="The Lab"
        subtitle="SDLC SP2 isolation sandbox · walk-forward search · LabContext-fenced · Lab-namespaced credibility writes"
        meta={[
          ['runs (30d)', LAB_RUNS.length, 'neutral'],
          ['survived',   survived, survived ? 'pos' : 'neutral'],
          ['failed',     failed, failed ? 'warn' : 'neutral'],
          ['pending promotion', pendingPromo, pendingPromo ? 'warn' : 'pos'],
          ['queued',     LAB_QUEUE.length, 'neutral'],
        ]}
        actions={
          <>
            <ActionBtn kind="primary" hot>New Lab run</ActionBtn>
            <ActionBtn>Open Lab dossiers</ActionBtn>
          </>
        }
      />

      <div className="banner banner-info">
        <div className="banner-l">
          <span className="banner-icon">⚗</span>
          <div>
            <div className="banner-title">The Lab is fully isolated from live trading.</div>
            <div className="banner-sub">Every guarded constructor (AARWriter, RiskGovernor, DBLogHandler, broker adapter) raises <code className="inline-code">LabIsolationViolation</code> inside an active <code className="inline-code">LabContext</code>. Credibility writes are Lab-namespaced (<code className="inline-code">backtest_credibility.lab.&lt;candidate&gt;</code>) and never pollute the live capital gate.</div>
          </div>
        </div>
      </div>

      {/* Two-column: run list + run detail */}
      <div className="split-lab">
        <Panel title="Recent runs" eyebrow={`${LAB_RUNS.length} runs · 6 walk-forward windows each`} noPad>
          <div className="lab-runs">
            {LAB_RUNS.map(r => (
              <button
                key={r.id}
                className={`lab-run ${r.id === selectedId ? 'lab-run-on' : ''}`}
                onClick={() => setSelectedId(r.id)}
              >
                <div className="lab-run-l">
                  <div className="lab-run-name">
                    <Pill tone={engineTone(r.engine)} dim>{r.engine.slice(0,3).toUpperCase()}</Pill>
                    <b>lab.{r.candidate}</b>
                  </div>
                  <div className="lab-run-meta">
                    {r.started.slice(0, 10)} · seed={r.seed} · {r.duration_min}m
                  </div>
                </div>
                <div className="lab-run-r">
                  <Pill tone={r.verdict === 'SURVIVED' ? 'pos' : 'warn'}>{r.verdict}</Pill>
                  <span className="num lab-run-dsr">DSR {r.dsr.toFixed(3)}</span>
                </div>
              </button>
            ))}
          </div>
        </Panel>

        <div className="stack">
          {selected && <LabRunDetail run={selected} onNav={onNav} />}
        </div>
      </div>

      {/* Queue */}
      <Panel title="Queued candidates" eyebrow={LAB_QUEUE.length ? `${LAB_QUEUE.length} pending` : 'empty'} noPad>
        {LAB_QUEUE.length ? (
          <table className="data-tbl">
            <thead><tr><th>Candidate</th><th>Engine</th><th>Queued</th><th>Note</th><th></th></tr></thead>
            <tbody>
              {LAB_QUEUE.map(q => (
                <tr key={q.candidate}>
                  <td className="num"><b>lab.{q.candidate}</b></td>
                  <td><Pill tone={engineTone(q.engine)} dim>{q.engine}</Pill></td>
                  <td className="num muted">{q.queued_at.slice(5, 16).replace('T', ' ')}</td>
                  <td className="muted">{q.note}</td>
                  <td className="t-right"><ActionBtn>Run now →</ActionBtn></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div className="empty">No queued candidates.</div>}
      </Panel>
    </div>
  );
}

function LabRunDetail({ run, onNav }) {
  return (
    <>
      <Panel
        title={`lab.${run.candidate}`}
        eyebrow={`${run.engine} · ${run.started.slice(0,10)} · seed=${run.seed}`}
        actions={run.promotion_pending && <ActionBtn kind="primary" hot onClick={() => onNav('sdlc')}>Promote → ECR</ActionBtn>}
      >
        <div className="lab-verdict">
          <Pill tone={run.verdict === 'SURVIVED' ? 'pos' : 'warn'}>{run.verdict}</Pill>
          {run.verdict === 'SURVIVED' && (
            <span className="muted">DSR ≥ 0.95 ✓ &nbsp; credibility ≥ 60 ✓ — eligible to promote to PAPER</span>
          )}
          {run.verdict === 'FAILED' && (
            <span className="muted">DSR &lt; 0.95 — candidate stays in Lab (no promotion path)</span>
          )}
        </div>

        <div className="lab-kpis">
          <div className="lab-kpi">
            <div className="lab-kpi-l">DSR (deflated)</div>
            <Num v={run.dsr} fmt={fmtNum} decimals={4} tone={run.dsr >= 0.95 ? 'pos' : 'warn'} className="lab-kpi-v" />
            <div className="lab-kpi-thr">≥ 0.95</div>
          </div>
          <div className="lab-kpi">
            <div className="lab-kpi-l">Final Sharpe</div>
            <Num v={run.final_sharpe} fmt={fmtNum} decimals={3} signed className="lab-kpi-v" />
            <div className="lab-kpi-thr">held-back window</div>
          </div>
          <div className="lab-kpi">
            <div className="lab-kpi-l">Credibility</div>
            <Num v={run.credibility} fmt={fmtNum} decimals={0} tone={run.credibility >= 60 ? 'pos' : 'warn'} className="lab-kpi-v" />
            <div className="lab-kpi-thr">≥ 60</div>
          </div>
          <div className="lab-kpi">
            <div className="lab-kpi-l">Trials</div>
            <span className="lab-kpi-v num">{run.trials}</span>
            <div className="lab-kpi-thr">{run.walk_windows} walk-windows</div>
          </div>
          <div className="lab-kpi">
            <div className="lab-kpi-l">Isolation</div>
            <span className={`lab-kpi-v num num-${run.isolation_violations ? 'neg' : 'pos'}`}>
              {run.isolation_violations} viol.
            </span>
            <div className="lab-kpi-thr">L3 guarded paths</div>
          </div>
        </div>

        <div className="lab-meta-grid">
          <div className="kv-pair"><span className="muted">namespace</span> <code className="inline-code">{run.namespace}</code></div>
          <div className="kv-pair"><span className="muted">dossier</span> <a className="link" href="#">{run.dossier}</a></div>
          <div className="kv-pair"><span className="muted">note</span> <span>{run.note}</span></div>
        </div>
      </Panel>

      <Panel title="Best parameters (winning candidate)" noPad>
        <table className="kv-tbl" style={{ padding: '8px 16px' }}>
          <tbody>
            {Object.entries(run.best_params).map(([k, v]) => (
              <tr key={k}><td className="kv-k">{k}</td><td className="kv-v num">{typeof v === 'number' ? v.toString() : v}</td></tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Walk-forward windows" eyebrow="6 windows · 5y train + 2y holdout · advance 365d">
        <WalkForwardChart data={LAB_WALK_RESULTS} />
        <table className="data-tbl" style={{ marginTop: 12 }}>
          <thead><tr><th>Window</th><th>Holdout</th><th className="t-right">N trades</th><th className="t-right">Sharpe</th><th className="t-right">Credibility</th><th className="t-right">DSR</th></tr></thead>
          <tbody>
            {LAB_WALK_RESULTS.map((w, i) => (
              <tr key={i}>
                <td className="num"><b>{w.window}</b></td>
                <td className="num muted">{w.holdout_start} → {w.holdout_end}</td>
                <td className="t-right num">{w.n_trades}</td>
                <td className="t-right"><Num v={w.sharpe} fmt={fmtNum} decimals={2} signed /></td>
                <td className="t-right"><Num v={w.credibility} fmt={fmtNum} decimals={0} tone={w.credibility >= 60 ? 'pos' : 'warn'} /></td>
                <td className="t-right"><Num v={w.dsr} fmt={fmtNum} decimals={2} tone={w.dsr >= 0.95 ? 'pos' : 'warn'} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}

function WalkForwardChart({ data, height = 160 }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(800);
  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const padL = 56, padR = 12, padT = 18, padB = 26;
  const chartW = Math.max(120, w - padL - padR);
  const chartH = height - padT - padB;
  const xAt = (i) => padL + (i + 0.5) / data.length * chartW;
  // y range: 0.4 — 1.0 fixed for DSR
  const yAt = (v) => padT + (1 - (v - 0.4) / 0.6) * chartH;
  const barW = (chartW / data.length) * 0.5;
  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width={w} height={height}>
        {[0.5, 0.7, 0.9, 0.95, 1.0].map(t => (
          <g key={t}>
            <line x1={padL} x2={w - padR} y1={yAt(t)} y2={yAt(t)} className="grid-line" />
            <text x={padL - 6} y={yAt(t)} className="grid-label" textAnchor="end" dy="0.35em">{t.toFixed(2)}</text>
          </g>
        ))}
        {/* DSR threshold 0.95 — dashed warn line */}
        <line x1={padL} x2={w - padR} y1={yAt(0.95)} y2={yAt(0.95)} stroke="var(--warn)" strokeWidth="0.8" strokeDasharray="4 3" />
        <text x={w - padR - 4} y={yAt(0.95) - 4} className="grid-label" fill="var(--warn)" textAnchor="end">DSR gate</text>
        {data.map((d, i) => {
          const passed = d.dsr >= 0.95;
          const h = (padT + chartH) - yAt(d.dsr);
          const x = xAt(i) - barW / 2;
          return (
            <g key={i}>
              <rect x={x} y={yAt(d.dsr)} width={barW} height={Math.max(2, h)}
                    fill={passed ? 'var(--pos)' : 'var(--warn)'} opacity="0.85" />
              <text x={xAt(i)} y={height - 14} className="grid-label" textAnchor="middle">{d.window}</text>
              <text x={xAt(i)} y={yAt(d.dsr) - 4} className="grid-label" textAnchor="middle"
                    fill={passed ? 'var(--pos)' : 'var(--warn)'}>{d.dsr.toFixed(2)}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// ENGINE SDLC — ECR queue (operator y/n for ADD/RETIRE/MODIFY)
// ──────────────────────────────────────────────────────────────────────
function SDLCView({ onNav }) {
  return (
    <div className="view view-sdlc">
      <ViewHeader
        eyebrow="OPERATIONS"
        title="Engine SDLC"
        subtitle="Engine Change Requests · binary y/n on ADD / MODIFY / RETIRE · auto for CUTOVER / EVALUATE"
        meta={[
          ['pending', ECR_QUEUE.length, ECR_QUEUE.length ? 'warn' : 'pos'],
          ['decided (30d)', ECR_HISTORY.length, 'neutral'],
          ['lifecycle states', 'LAB → PAPER → LIVE → RETIRED', 'neutral'],
        ]}
      />

      <Panel title="Pending change requests" eyebrow="operator decision required" noPad>
        {ECR_QUEUE.length ? (
          <div className="ecr-list">
            {ECR_QUEUE.map(e => (
              <div key={e.id} className="ecr-card">
                <div className="ecr-hd">
                  <div className="ecr-hd-l">
                    <Pill tone={e.kind === 'ADD' ? 'pos' : e.kind === 'RETIRE' ? 'neg' : 'warn'} dim>{e.kind}</Pill>
                    <Pill tone={engineTone(ENGINE_BY_ID[e.engine]?.id || 'mom')} dim>{e.engine}</Pill>
                    <span className="ecr-action">{e.action}</span>
                    {e.auto_validated && <Pill tone="pos" dim>VALIDATED</Pill>}
                  </div>
                  <div className="ecr-hd-r">
                    <span className="muted">submitted {e.submitted.slice(5, 16).replace('T', ' ')} by {e.submitter}</span>
                  </div>
                </div>
                <div className="ecr-summary">{e.summary}</div>
                <div className="ecr-diff">
                  <div className="ecr-diff-label">DIFF</div>
                  <code className="ecr-diff-code">{e.diff}</code>
                </div>
                {e.lab_dossier && (
                  <div className="ecr-dossier">
                    <span className="muted">Lab dossier:</span>
                    <a className="link" href="#" onClick={(ev) => { ev.preventDefault(); onNav('lab'); }}>{e.lab_dossier}</a>
                  </div>
                )}
                <div className="ecr-actions">
                  <ActionBtn kind="primary" hot>Approve →</ActionBtn>
                  <ActionBtn>Reject</ActionBtn>
                  <ActionBtn>View full diff</ActionBtn>
                </div>
              </div>
            ))}
          </div>
        ) : <div className="empty">No pending ECRs.</div>}
      </Panel>

      <Panel title="Recent decisions" eyebrow={`${ECR_HISTORY.length} in last 30d`} noPad>
        <table className="data-tbl">
          <thead><tr><th>Decided</th><th>Kind</th><th>Engine</th><th>Action</th><th>Verdict</th><th>Diff</th></tr></thead>
          <tbody>
            {ECR_HISTORY.map(e => (
              <tr key={e.id} className="row-resolved">
                <td className="num muted">{e.decided.slice(5, 16).replace('T', ' ')}</td>
                <td><Pill tone={e.kind === 'ADD' ? 'pos' : e.kind === 'RETIRE' ? 'neg' : 'neutral'} dim>{e.kind}</Pill></td>
                <td className="num">{e.engine}</td>
                <td className="num muted">{e.action}</td>
                <td><Pill tone={e.verdict === 'APPROVED' ? 'pos' : e.verdict === 'AUTO' ? 'neutral' : 'warn'} dim>{e.verdict}</Pill></td>
                <td className="muted ellipsis">{e.diff}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {/* Lifecycle state map */}
      <Panel title="Engine lifecycle map" eyebrow="LAB → PAPER → LIVE → RETIRED · LAB is the SDLC SP2 sentinel state">
        <div className="lifecycle-map">
          {['LAB', 'PAPER', 'LIVE', 'RETIRED'].map(stage => {
            const here = ENGINES.filter(e => e.lifecycle === stage);
            return (
              <div key={stage} className={`lifecycle-col lifecycle-col-${stage.toLowerCase()}`}>
                <div className="lifecycle-hd">
                  <span className="lifecycle-name">{stage}</span>
                  <span className="num muted">{here.length}</span>
                </div>
                <div className="lifecycle-engines">
                  {here.map(e => (
                    <button key={e.id} className="lifecycle-chip" onClick={() => onNav(e.id === 'lab' ? 'lab' : 'engine:' + e.id)}>
                      <Pill tone={engineTone(e.id)} dim>{e.name}</Pill>
                    </button>
                  ))}
                  {!here.length && <div className="muted lifecycle-empty">— empty —</div>}
                </div>
              </div>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}

Object.assign(window, { LabView, SDLCView });
