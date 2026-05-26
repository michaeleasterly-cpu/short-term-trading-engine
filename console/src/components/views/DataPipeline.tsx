"use client";
import { ViewHeader, Panel, Kpi, Pill } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

export function DataPipeline() {
  const { data, loading, error } = useApi(() => api.dataPipeline(), []);
  const k = data?.kpis;
  return (
    <div>
      <ViewHeader
        eyebrow="SYSTEM / DATA PIPELINE" title="Data Pipeline"
        meta={[["last update", "21:30 UTC daily"], ["cycle latency", "~25m"], ["self-heal", `${data?.self_heal?.length ?? 0} cycles 24h`]]}
        actions={
          <>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Run data update</button>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Run validation</button>
          </>
        }
      />
      {loading && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
      {error && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
      {!loading && !error && k && (
        <>
          <div className="grid gap-2 px-5 py-4" style={{ gridTemplateColumns: "repeat(8, minmax(120px, 1fr))" }}>
            <Kpi label="Passed" value={String(k.passed)} tone="pos" />
            <Kpi label="Warnings" value={String(k.warnings)} />
            <Kpi label="Failed" value={String(k.failed)} tone={k.failed ? "neg" : "neutral"} />
            <Kpi label="DATA_OPS event" value={k.data_ops_event ? "OK" : "—"} sub={k.data_ops_event ? new Date(k.data_ops_event).toISOString().slice(11, 16) + " UTC" : "never"} tone={k.data_ops_event ? "pos" : "warn"} />
            <Kpi label="Confidence" value={k.confidence} tone="pos" />
            <Kpi label="Tickers tracked" value={k.tickers_tracked.toLocaleString()} />
            <Kpi label="Daily bars (60d)" value={k.daily_bars_60d.toLocaleString()} />
            <Kpi label="Forensics" value={`${k.forensics_open} open`} tone="warn" />
          </div>
          <div className="px-5 pb-4">
            <Panel title="Validation suite — 13 checks">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Check", "Status", "Rows", "Age", "Notes"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{(data?.validation ?? []).map((c, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{c.check}</td>
                    <td className="px-3 py-1.5"><Pill tone={c.status === "PASS" ? "pos" : "neg"}>{c.status}</Pill></td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{c.rows.toLocaleString()}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{c.age}</td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{c.notes}</td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
          </div>
          <div className="px-5 pb-5">
            <Panel title="Self-heal log">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Time", "Stage", "Result", "Duration", "Notes"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{(data?.self_heal ?? []).map((s, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{s.time}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{s.stage}</td>
                    <td className="px-3 py-1.5"><Pill tone={s.result === "HEALED" ? "pos" : "warn"}>{s.result}</Pill></td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{s.duration}</td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{s.notes}</td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
