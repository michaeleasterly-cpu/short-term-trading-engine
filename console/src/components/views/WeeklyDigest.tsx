"use client";
import { useState, useEffect } from "react";
import { ViewHeader, Panel, Pill } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

export function WeeklyDigest() {
  const { data, loading, error } = useApi(() => api.digest(), []);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (data) setOpen(Object.fromEntries(data.digest.sections.map(s => [s.id, s.open])));
  }, [data]);
  return (
    <div>
      <ViewHeader
        eyebrow="OPERATIONS / WEEKLY DIGEST"
        title={data ? `Week of ${data.digest.week_of}` : "Weekly Digest"}
        subtitle={data ? `generated ${data.digest.generated_ts}` : undefined}
        meta={data ? [
          ["weeks unacked", `${data.digest.weeks_unacked} / ${data.digest.threshold}`],
          ["live clearance", data.digest.live_clearance],
        ] : []}
        actions={
          data?.digest.acked
            ? <Pill tone="pos">ACKNOWLEDGED</Pill>
            : <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Acknowledge week</button>
        }
      />
      {loading && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
      {error && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
      {!loading && !error && data && (
        <>
          {!data.digest.acked && (
            <div className="px-5 py-3">
              <div className="hairline flex items-center gap-3 px-4 py-3" style={{ background: "var(--bg-2)", borderLeft: "3px solid var(--warn)" }}>
                <span className="text-[14px]" style={{ color: "var(--warn)" }}>⚠</span>
                <div className="flex-1">
                  <div className="text-[12.5px]" style={{ color: "var(--ink)" }}>This week&apos;s digest needs your acknowledgment.</div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--ink-3)" }}>Two consecutive unacked weeks automatically de-escalate live trading clearance.</div>
                </div>
                <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--warn)", color: "var(--bg)" }}>Acknowledge</button>
              </div>
            </div>
          )}
          <div className="grid gap-2 px-5 py-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(360px, 1fr))" }}>
            {data.digest.sections.map(s => (
              <div key={s.id} className="hairline" style={{ background: "var(--panel)" }}>
                <button onClick={() => setOpen(o => ({ ...o, [s.id]: !o[s.id] }))}
                  className="w-full flex items-center gap-2 px-4 py-2.5 text-left hover:cursor-pointer" style={{ background: "var(--panel-hd)" }}>
                  <span className="mono text-[11px]" style={{ color: "var(--ink-3)" }}>{open[s.id] ? "▾" : "▸"}</span>
                  <span className="text-[12.5px]" style={{ color: s.tone === "warn" ? "var(--warn)" : "var(--ink-2)" }}>{s.label}</span>
                  <Pill tone={s.tone === "warn" ? "warn" : "neutral"}>{s.items.length}</Pill>
                </button>
                {open[s.id] && (
                  <div className="px-4 py-2 space-y-1.5">
                    {s.items.map((it, i) => (
                      <div key={i} className="mono text-[11.5px] hairline-b py-1.5" style={{ color: "var(--ink-2)", borderStyle: "dashed" }}>{it}</div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="px-5 py-3">
            <Panel title="LLM triage proposals">
              {data.llm_triage.map(p => (
                <div key={p.id} className="hairline-b px-4 py-3 last:border-b-0"
                  style={{ borderLeft: `3px solid ${p.lane === "data" ? "oklch(72% 0.14 200)" : "var(--accent)"}` }}>
                  <div className="flex items-center gap-2">
                    <Pill tone="accent">{p.lane.toUpperCase()} LANE</Pill>
                    <span className="mono text-[11px]" style={{ color: "var(--ink-3)" }}>ref {p.ref} · class {p.cls}</span>
                    <div className="ml-auto flex items-center gap-2">
                      <span className="text-[11px]" style={{ color: "var(--ink-3)" }}>{p.model} · persona {p.persona}</span>
                      <Pill tone="warn">draft (human review)</Pill>
                    </div>
                  </div>
                  <div className="mt-2 text-[13px]" style={{ color: "var(--ink)" }}>
                    <span style={{ color: "var(--warn)" }}>Proposed:</span> <span className="mono">{p.disposition}</span>
                    <span className="mono ml-3 text-[11px]" style={{ color: "var(--ink-3)" }}>conf {(p.confidence * 100).toFixed(0)}%</span>
                  </div>
                  <div className="text-[12.5px] mt-2 px-3 py-2" style={{ color: "var(--ink-2)", borderLeft: "2px solid var(--accent)", background: "var(--bg-2)" }}>{p.rationale}</div>
                </div>
              ))}
            </Panel>
          </div>
          <div className="px-5 pb-5">
            <Panel title="Ack history">
              <table className="w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Week", "Acked at", "Status"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{data.digest.ack_history.map((a, i) => (
                  <tr key={i} style={{ opacity: i === 0 ? 1 : 0.5 }}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{a.week}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{a.acked_at}</td>
                    <td className="px-3 py-1.5"><Pill tone={a.unacked ? "warn" : "pos"}>{a.unacked ? "UNACKED" : "ACKED"}</Pill></td>
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
