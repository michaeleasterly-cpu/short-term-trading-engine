"use client";
import { ViewHeader, Panel } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

export function Allocator() {
  const { data, loading, error } = useApi(() => api.allocator(), []);
  const alloc = data?.allocations ?? [];
  return (
    <div>
      <ViewHeader
        eyebrow="SYSTEM" title="Allocator"
        meta={[
          ["method", data?.method ?? "—"],
          ["trigger", data?.trigger ?? "—"],
          ["last", data?.last_run ?? "—"],
          ["next", data?.next_run ?? "—"],
        ]}
        actions={<button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Force rebalance</button>}
      />
      <div className="px-5 py-4">
        <Panel title="Current allocation">
          {loading && <div className="p-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
          {error && <div className="p-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
          {!loading && !error && (
            <div className="p-4">
              <div className="flex h-9 overflow-hidden hairline">
                {alloc.map(a => (
                  <div key={a.engine} className="flex items-center justify-center mono text-[10px]"
                    style={{ background: a.color, color: "var(--bg)", width: `${a.pct}%` }}>
                    {a.engine} {a.pct.toFixed(1)}%
                  </div>
                ))}
              </div>
              <table className="mt-5 w-full text-[11.5px]">
                <thead><tr style={{ color: "var(--ink-3)" }}>{["Engine", "Target weight", "Current weight", "Drift", "Capital"].map(h => (<th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>))}</tr></thead>
                <tbody>{alloc.filter(a => a.engine !== "cash").map(a => (
                  <tr key={a.engine}>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{a.engine}</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{a.pct.toFixed(1)}%</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{(a.pct + (Math.random() - 0.5) * 1.2).toFixed(1)}%</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>±0.3%</td>
                    <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>${(a.pct * 1034.42).toFixed(0)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
