"use client";
import { ViewHeader, Panel, Kpi, Pill } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

type LadderTone = "pos" | "neg" | "warn" | "accent" | "neutral";

export function Health() {
  const { data, loading, error } = useApi(() => api.healthPage(), []);
  return (
    <div>
      <ViewHeader eyebrow="SYSTEM" title="Health"
        meta={[
          ["platform", "Railway"],
          ["live clearance", "PAPER"],
          ["weeks unacked", "1 / 2"],
          ["services live", String(data?.daemons.length ?? 0)],
        ]}
      />
      {loading && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
      {error && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
      {!loading && !error && data && (
        <>
          <div className="grid gap-2 px-5 py-4" style={{ gridTemplateColumns: "repeat(6, minmax(120px, 1fr))" }}>
            <Kpi label="Open holds" value={String(data.kpis.open_holds)} tone={data.kpis.open_holds ? "warn" : "pos"} />
            <Kpi label="Open escalations (7d)" value={String(data.kpis.open_escalations_7d)} tone={data.kpis.open_escalations_7d ? "warn" : "pos"} />
            <Kpi label="Undispositioned" value={String(data.kpis.undispositioned)} tone="warn" />
            <Kpi label="Cross-table audit" value={data.kpis.cross_table_audit} tone="pos" />
            <Kpi label="LLM proposals open" value={String(data.kpis.llm_proposals_open)} />
            <Kpi label="Self-heal cycles 24h" value={String(data.kpis.self_heal_cycles_24h)} />
          </div>
          <div className="px-5 pb-4">
            <Panel title="Escalation & Hardening Ladder">
              {data.ladder.map(r => (
                <div key={r.rung} className="hairline-b flex items-center gap-4 px-4 py-3 last:border-b-0">
                  <div className="mono text-[18px] w-8" style={{
                    color: r.tone === "pos" ? "var(--pos)" : r.tone === "accent" ? "var(--accent)" : r.tone === "warn" ? "var(--warn)" : "var(--ink-3)",
                  }}>{r.rung}</div>
                  <div className="flex-1">
                    <div className="text-[13.5px]" style={{ color: "var(--ink)" }}>{r.name}</div>
                    <div className="text-[11.5px] mt-0.5" style={{ color: "var(--ink-3)" }}>{r.detail}</div>
                  </div>
                  <Pill tone={r.tone as LadderTone}>{r.status.toUpperCase()}</Pill>
                  <span className="mono text-[11px]" style={{ color: "var(--ink-2)" }}>{r.count}</span>
                </div>
              ))}
            </Panel>
          </div>
          <div className="grid gap-3 px-5 pb-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <Panel title="Data supervisor — open holds">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Source", "Held", "Cycles", "Reason", "Esc"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{data.holds.map((h, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{h.source}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{h.held}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{h.cycles}</td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{h.reason}</td>
                    <td className="mono px-3 py-1.5"><Pill tone="warn">{h.esc}</Pill></td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
            <Panel title="Cross-table audit (auditheal)">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Source", "State", "Last", "Note"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{data.auditheal.map((a, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{a.source}</td>
                    <td className="px-3 py-1.5"><Pill tone="pos">{a.state}</Pill></td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{a.last}</td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{a.note || "—"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
          </div>
          <div className="px-5 pb-4">
            <Panel title="Recent escalations (7d)">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["When", "Type", "Ref", "Class", "Status", "Message"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{data.escalations.map((e, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{e.when}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.type}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{e.ref}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.cls}</td>
                    <td className="mono px-3 py-1.5"><Pill tone={e.open ? "warn" : "neutral"}>{e.open ? "OPEN" : "RESOLVED"}</Pill></td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.msg}</td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
          </div>
          <div className="px-5 pb-5">
            <Panel title="Railway services (TCP project)">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Service", "Lane", "Status", "Last deploy", "Last event", "Restart", "IPv6 egress", "Role"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{data.daemons.map((d, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{d.daemon}</td>
                    <td className="mono px-3 py-1.5" style={{ color: d.lane === "engine" ? "var(--mom)" : d.lane === "data" ? "var(--rev)" : d.lane === "api" ? "var(--vec)" : "var(--ink-3)" }}>{d.lane}</td>
                    <td className="mono px-3 py-1.5"><Pill tone={d.status === "SUCCESS" || d.status === "DEPLOYING" ? "pos" : d.status === "CRASHED" || d.status === "FAILED" ? "neg" : "neutral"}>{d.status}</Pill></td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{d.last_deploy.length > 16 ? d.last_deploy.slice(5, 16).replace("T", " ") : d.last_deploy}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{d.last_event.length > 16 ? d.last_event.slice(5, 16).replace("T", " ") : d.last_event}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{d.restart_policy}</td>
                    <td className="mono px-3 py-1.5" style={{ color: d.ipv6_egress ? "var(--pos)" : "var(--ink-3)" }}>{d.ipv6_egress ? "✓" : "—"}</td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{d.role}</td>
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
