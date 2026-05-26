"use client";
import { ViewHeader, Panel, Kpi, Pill } from "./Primitives";
import { SOURCE_HOLDS, RECENT_ESCALATIONS, DAEMONS } from "@/lib/mock-data";

type LadderTone = "pos" | "neg" | "warn" | "accent" | "neutral";

const LADDER: Array<{ rung: string; name: string; detail: string; status: string; tone: LadderTone; count: string }> = [
  { rung: "R1", name: "Single-source freshness",  detail: "Every feed has a freshness check + cadence-aware lateness window", status: "covered", tone: "pos",     count: "13/13" },
  { rung: "R2", name: "Cross-table consistency", detail: "auditheal scans for orphan/duplicate keys across 8 tables",         status: "covered", tone: "pos",     count: "8/8" },
  { rung: "R3", name: "Pre-Railway archive",      detail: "CSV-first archive (R3 substrate) before any DB write",             status: "covered", tone: "pos",     count: "ACTIVE" },
  { rung: "R4", name: "Deterministic cascade",    detail: "Waves 1–4 + sentinel; complete self-heal coverage",                status: "active",  tone: "accent",  count: "WAVE 4" },
  { rung: "R5", name: "LLM advisory backstop",    detail: "REMOVED 2026-05-22 — deterministic is the floor",                  status: "removed", tone: "neutral", count: "—" },
];

export function Health() {
  return (
    <div>
      <ViewHeader
        eyebrow="SYSTEM"
        title="Health"
        meta={[
          ["live clearance", "PAPER"],
          ["weeks unacked", "1 / 2"],
          ["daemons live", "3 / 3"],
        ]}
      />
      <div className="grid gap-2 px-5 py-4" style={{ gridTemplateColumns: "repeat(6, minmax(120px, 1fr))" }}>
        <Kpi label="Open holds" value="2" tone="warn" />
        <Kpi label="Open escalations (7d)" value="2" tone="warn" />
        <Kpi label="Undispositioned" value="2" tone="warn" />
        <Kpi label="Cross-table audit" value="GREEN" tone="pos" />
        <Kpi label="LLM proposals open" value="2" tone="neutral" />
        <Kpi label="Self-heal cycles 24h" value="3" tone="neutral" />
      </div>
      <div className="px-5 pb-4">
        <Panel title="Escalation & Hardening Ladder">
          {LADDER.map(r => (
            <div key={r.rung} className="hairline-b flex items-center gap-4 px-4 py-3 last:border-b-0">
              <div className="mono text-[18px] w-8" style={{
                color: r.tone === "pos" ? "var(--pos)" : r.tone === "accent" ? "var(--accent)" : r.tone === "warn" ? "var(--warn)" : "var(--ink-3)",
              }}>{r.rung}</div>
              <div className="flex-1">
                <div className="text-[13.5px]" style={{ color: "var(--ink)" }}>{r.name}</div>
                <div className="text-[11.5px] mt-0.5" style={{ color: "var(--ink-3)" }}>{r.detail}</div>
              </div>
              <Pill tone={r.tone}>{r.status.toUpperCase()}</Pill>
              <span className="mono text-[11px]" style={{ color: "var(--ink-2)" }}>{r.count}</span>
            </div>
          ))}
        </Panel>
      </div>
      <div className="grid gap-3 px-5 pb-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Panel title="Data supervisor — open holds">
          <table className="w-full text-[11.5px]">
            <thead><tr style={{ color: "var(--ink-3)" }}>{["Source", "Held", "Cycles", "Reason", "Esc"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
            <tbody>{SOURCE_HOLDS.map((h, i) => (
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
            <tbody>
              {["prices_daily", "fundamentals_cache", "macro_indicators", "ticker_history"].map((s, i) => (
                <tr key={s}>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{s}</td>
                  <td className="px-3 py-1.5"><Pill tone="pos">GREEN</Pill></td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{i === 0 ? "12m" : i === 1 ? "1h" : i === 2 ? "1d" : "2d"} ago</td>
                  <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>—</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
      <div className="px-5 pb-4">
        <Panel title="Recent escalations (7d)">
          <table className="w-full text-[11.5px]">
            <thead><tr style={{ color: "var(--ink-3)" }}>{["When", "Type", "Ref", "Class", "Status", "Message"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
            <tbody>{RECENT_ESCALATIONS.map((e, i) => (
              <tr key={i}>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{e.when}</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.type}</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{e.refid}</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.cls}</td>
                <td className="mono px-3 py-1.5"><Pill tone={e.open ? "warn" : "neutral"}>{e.open ? "OPEN" : "RESOLVED"}</Pill></td>
                <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.msg}</td>
              </tr>
            ))}</tbody>
          </table>
        </Panel>
      </div>
      <div className="px-5 pb-5">
        <Panel title="Daemon topology">
          <table className="w-full text-[11.5px]">
            <thead><tr style={{ color: "var(--ink-3)" }}>{["Daemon", "Lane", "PID", "Uptime", "Last heartbeat", "Status", "Role"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
            <tbody>{DAEMONS.map((d, i) => (
              <tr key={i}>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{d.daemon}</td>
                <td className="mono px-3 py-1.5" style={{ color: d.lane === "engine" ? "var(--mom)" : d.lane === "data" ? "var(--rev)" : "var(--ink-3)" }}>{d.lane}</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{d.pid}</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{d.uptime}</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{d.last}</td>
                <td className="mono px-3 py-1.5"><Pill tone={d.status === "RUNNING" ? "pos" : "warn"}>{d.status}</Pill></td>
                <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{d.role}</td>
              </tr>
            ))}</tbody>
          </table>
        </Panel>
      </div>
    </div>
  );
}
