"use client";
import { ViewHeader, Panel, Pill, EnginePill } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

const STATE_TONE: Record<string, { bg: string; ink: string }> = {
  LAB:     { bg: "color-mix(in oklch, var(--mom) 12%, var(--bg-1))", ink: "var(--mom)" },
  PAPER:   { bg: "color-mix(in oklch, var(--rev) 12%, var(--bg-1))", ink: "var(--rev)" },
  LIVE:    { bg: "color-mix(in oklch, var(--pos) 12%, var(--bg-1))", ink: "var(--pos)" },
  RETIRED: { bg: "var(--bg-2)",                                       ink: "var(--ink-3)" },
};

export function ECR() {
  const { data, loading, error } = useApi(() => api.ecr(), []);
  return (
    <div>
      <ViewHeader
        eyebrow="OPERATIONS" title="Engine SDLC"
        subtitle="Engine Change Requests · binary y/n on ADD / MODIFY / RETIRE"
        meta={[["pending", String(data?.queue.length ?? 0)], ["decided (7d)", String(data?.decided.length ?? 0)]]}
      />
      {loading && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
      {error && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
      {!loading && !error && data && (
        <>
          <div className="px-5 py-4 space-y-3">
            {data.queue.map(ecr => (
              <div key={ecr.id} className="hairline" style={{ background: "var(--panel)" }}>
                <div className="hairline-b flex items-center gap-2 px-4 py-2.5" style={{ background: "var(--panel-hd)" }}>
                  <Pill tone={ecr.kind === "ADD" ? "pos" : ecr.kind === "MODIFY" ? "accent" : "warn"}>{ecr.kind}</Pill>
                  <EnginePill engine={ecr.engine} />
                  <span className="mono text-[12.5px] uppercase" style={{ color: "var(--ink)" }}>{ecr.action}</span>
                  <Pill tone="pos">VALIDATED</Pill>
                  <div className="ml-auto text-[11px]" style={{ color: "var(--ink-3)" }}>{ecr.submitted_by} · {ecr.submitted_when}</div>
                </div>
                <div className="px-4 py-3">
                  <div className="text-[12.5px] mb-3" style={{ color: "var(--ink-2)" }}>{ecr.summary}</div>
                  <div className="mb-1">
                    <span className="eyebrow">DIFF</span>
                    <pre className="mono text-[10.5px] mt-1 px-3 py-2" style={{ background: "var(--bg-2)", color: "var(--ink-2)" }}>{ecr.diff}</pre>
                  </div>
                  {ecr.lab_dossier && <div className="text-[11px] mt-2" style={{ color: "var(--accent)" }}>Lab dossier: <span className="mono">{ecr.lab_dossier}</span></div>}
                </div>
                <div className="hairline-t flex items-center gap-2 px-4 py-2.5" style={{ background: "var(--bg-1)" }}>
                  <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Approve →</button>
                  <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Reject</button>
                  <button className="mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-3)" }}>View full diff</button>
                </div>
              </div>
            ))}
          </div>
          <div className="px-5 pb-4">
            <Panel title="Recent decisions">
              <table className="w-full text-[11.5px]" style={{ opacity: 0.6 }}>
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Decided", "Kind", "Engine", "Action", "Verdict", "Diff"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{data.decided.map((e, i) => (
                  <tr key={i}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{e.decided}</td>
                    <td className="px-3 py-1.5"><Pill>{e.kind}</Pill></td>
                    <td className="px-3 py-1.5"><EnginePill engine={e.engine} /></td>
                    <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{e.action}</td>
                    <td className="px-3 py-1.5"><Pill tone="pos">{e.verdict}</Pill></td>
                    <td className="mono px-3 py-1.5 text-[10px]" style={{ color: "var(--ink-3)" }}>{e.diff}</td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
          </div>
          <div className="px-5 pb-5">
            <Panel title="Engine lifecycle map">
              <div className="grid gap-2 p-4" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
                {(["LAB", "PAPER", "LIVE", "RETIRED"] as const).map(state => {
                  const engines = data.lifecycle[state];
                  const tone = STATE_TONE[state];
                  return (
                    <div key={state} className="hairline px-3 py-3" style={{ background: tone.bg }}>
                      <div className="flex items-center mb-3">
                        <span className="eyebrow" style={{ color: tone.ink }}>{state}</span>
                        <span className="mono text-[11px] ml-auto" style={{ color: tone.ink }}>{engines.length}</span>
                      </div>
                      <div className="space-y-1.5">
                        {engines.length === 0 ? (
                          <div className="text-[11px] italic" style={{ color: "var(--ink-4)" }}>— empty —</div>
                        ) : engines.map(e => (
                          <div key={e.id} className="mono text-[11.5px]" style={{ color: "var(--ink-2)" }}>{e.name}</div>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
