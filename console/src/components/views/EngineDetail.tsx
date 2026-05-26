"use client";
import { ViewHeader, Panel, Pill } from "./Primitives";
import { api, useApi } from "@/lib/api-client";

export function EngineDetail({ engineId }: { engineId: string }) {
  const { data, loading, error } = useApi(() => api.engine(engineId), [engineId]);
  const card = data?.card;
  const gates = data?.gates ?? [];
  const allPassed = gates.length > 0 && gates.every(g => g.passed);
  const toneVar = card ? `var(--${card.tone})` : undefined;
  return (
    <div>
      <ViewHeader
        eyebrow={`ENGINE · ${engineId.toUpperCase()}`}
        title={card ? `${card.name} — ${card.kind}` : engineId}
        meta={[["state", "PAPER"], ["rebalance", engineId === "momentum" ? "monthly" : engineId === "reversion" ? "daily" : engineId === "sentinel" ? "monthly" : "event"], ["last", "2026-05-22"]]}
        actions={
          <>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Run engine now</button>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Open backtest →</button>
          </>
        }
      />
      {loading && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>loading…</div>}
      {error && <div className="px-5 py-4 text-[11px]" style={{ color: "var(--neg)" }}>{error}</div>}
      {!loading && !error && card && (
        <>
          {engineId === "canary" && (
            <div className="px-5 py-3">
              <div className="hairline px-4 py-3" style={{ background: "var(--bg-2)", borderLeft: "3px solid var(--accent)" }}>
                <span className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>
                  <span className="eyebrow">INFO</span> &nbsp;Canary is intentionally non-graduating — platform liveness probe only.
                </span>
              </div>
            </div>
          )}
          <div className="grid gap-3 px-5 py-4" style={{ gridTemplateColumns: "1.6fr 1fr" }}>
            {engineId !== "canary" ? (
              <Panel title="Credibility & graduation gates">
                <div className="px-3 py-3">
                  {gates.map(g => (
                    <div key={g.k} className="flex items-center gap-3 py-2 text-[11.5px]">
                      <div className="w-[180px] text-[11px]" style={{ color: "var(--ink-2)" }}>{g.k}</div>
                      <div className="flex-1 relative" style={{ background: "var(--bg-3)", height: 5 }}>
                        <div className="absolute inset-y-0 left-0" style={{
                          width: `${Math.min(100, (g.v / (g.thr * 2)) * 100)}%`,
                          background: g.passed ? "var(--pos)" : "var(--warn)",
                        }} />
                        <div className="absolute inset-y-[-3px] w-[2px]" style={{
                          left: `${Math.min(100, (g.thr / (g.thr * 2)) * 100)}%`,
                          background: "var(--ink-3)",
                        }} />
                      </div>
                      <div className="mono text-[11px] w-[110px] text-right" style={{ color: "var(--ink-2)" }}>{g.v} / {g.thr}</div>
                    </div>
                  ))}
                  <div className="hairline-t mt-3 pt-3 flex items-center gap-2 text-[12px]">
                    <Pill tone={allPassed ? "pos" : "warn"}>{allPassed ? "PASSED" : "GATED"}</Pill>
                    <span style={{ color: "var(--ink-3)" }}>{allPassed ? "all gates clear" : "one or more gates open"}</span>
                  </div>
                </div>
              </Panel>
            ) : <div />}
            <Panel title="Best trial parameters">
              <table className="w-full text-[11.5px]">
                <tbody>
                  {(data?.best_params ?? []).map(([k, v]) => (
                    <tr key={k}>
                      <td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{k}</td>
                      <td className="mono px-3 py-1.5 text-right" style={{ color: "var(--ink-2)" }}>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
