"use client";
import { ViewHeader, Panel, Pill } from "./Primitives";
import { PROVIDERS } from "@/lib/mock-data";

export function Providers() {
  return (
    <div>
      <ViewHeader
        eyebrow="SYSTEM"
        title="Providers"
        subtitle="ProviderBinding lifecycle — exactly one ACTIVE per feed"
        meta={[
          ["bindings", String(PROVIDERS.length)],
          ["active", String(PROVIDERS.filter(p => p.status === "ACTIVE").length)],
          ["deprecated", String(PROVIDERS.filter(p => p.status === "DEPRECATED").length)],
        ]}
      />
      <div className="px-5 py-4">
        <Panel title="Feed / provider bindings">
          <table className="w-full text-[11.5px]">
            <thead>
              <tr style={{ color: "var(--ink-3)" }}>
                {["Feed", "Provider", "Status", "Adapter", "Note"].map(h => (
                  <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {PROVIDERS.map((p, i) => (
                <tr key={i}>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{p.feed}</td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{p.provider}</td>
                  <td className="px-3 py-1.5">
                    <Pill tone={p.status === "ACTIVE" ? "pos" : p.status === "FALLBACK" ? "neutral" : "warn"}>{p.status}</Pill>
                  </td>
                  <td className="mono px-3 py-1.5 text-[10.5px]" style={{ color: "var(--ink-3)" }}>{p.adapter}</td>
                  <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{p.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  );
}
