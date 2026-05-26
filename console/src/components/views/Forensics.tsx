"use client";
import { ViewHeader, Panel, EnginePill, Pill } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

export function Forensics() {
  const { data, loading, error } = useApi(() => api.forensics(), []);
  const triggers = data?.triggers ?? [];
  const high = triggers.filter(t => t.severity === "high").length;
  return (
    <div>
      <ViewHeader
        eyebrow="PORTFOLIO" title="Forensics"
        subtitle="drawdown / loss-cluster / outlier-loss triggers — sprint dossier index"
        meta={[["open triggers", String(triggers.length)], ["high severity", String(high)], ["last scan", "2026-05-25 21:31 UTC"]]}
      />
      <div className="px-5 py-4">
        <Panel title="Open triggers">
          {loading && <div className="px-3 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
          {error && <div className="px-3 py-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
          {!loading && !error && (
            <table className="w-full text-[11.5px]">
              <thead><tr style={{ color: "var(--ink-3)" }}>{["ID", "Severity", "Trigger", "Engine", "Note", "When"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
              <tbody>{triggers.map((f, i) => (
                <tr key={i}>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{f.id}</td>
                  <td className="mono px-3 py-1.5"><Pill tone={f.severity === "high" ? "neg" : f.severity === "med" ? "warn" : "neutral"}>{f.severity.toUpperCase()}</Pill></td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{f.trigger}</td>
                  <td className="px-3 py-1.5"><EnginePill engine={f.engine} /></td>
                  <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{f.note}</td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{f.when}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}
