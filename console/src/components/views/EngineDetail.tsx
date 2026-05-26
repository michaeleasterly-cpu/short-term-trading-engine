"use client";
import { ViewHeader, Panel, Pill } from "./Primitives";
import { ENGINE_CARDS, CREDIBILITY_GATES, type EngineId } from "@/lib/mock-data";

export function EngineDetail({ engineId }: { engineId: EngineId }) {
  const card = ENGINE_CARDS.find(e => e.id === engineId);
  if (!card) return null;
  const gates = CREDIBILITY_GATES[engineId];
  const allPassed = gates.gates.length > 0 && gates.gates.every(g => g.passed);
  const toneVar = `var(--${card.tone})`;

  return (
    <div>
      <ViewHeader
        eyebrow={`ENGINE · ${engineId.toUpperCase()}`}
        title={`${card.name} — ${card.kind}`}
        meta={[
          ["state", "PAPER"],
          ["rebalance", engineId === "momentum" ? "monthly" : engineId === "reversion" ? "daily" : engineId === "sentinel" ? "monthly" : "event"],
          ["last", "2026-05-22"],
          ["next", engineId === "momentum" ? "2026-06-23" : "n/a"],
        ]}
        actions={
          <>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Run engine now</button>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Open backtest →</button>
          </>
        }
      />
      {engineId === "canary" && (
        <div className="px-5 py-3">
          <div className="hairline px-4 py-3" style={{ background: "var(--bg-2)", borderLeft: "3px solid var(--accent)" }}>
            <span className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>
              <span className="eyebrow">INFO</span> &nbsp;Canary is intentionally non-graduating — platform liveness probe only. Never gets capital, never calls write_credibility_score.
            </span>
          </div>
        </div>
      )}
      <div className="grid gap-3 px-5 py-4" style={{ gridTemplateColumns: "1.6fr 1fr" }}>
        {engineId !== "canary" ? (
          <Panel title="Credibility & graduation gates">
            <div className="px-3 py-3">
              {gates.gates.map(g => (
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
                  <div className="mono text-[11px] w-[110px] text-right" style={{ color: "var(--ink-2)" }}>
                    {g.v} / {g.thr}
                  </div>
                </div>
              ))}
              <div className="hairline-t mt-3 pt-3 flex items-center gap-2 text-[12px]">
                <Pill tone={allPassed ? "pos" : "warn"}>{allPassed ? "PASSED" : "GATED"}</Pill>
                <span style={{ color: "var(--ink-3)" }}>{allPassed ? "all 6 gates clear" : "one or more gates open — see bars"}</span>
              </div>
            </div>
          </Panel>
        ) : (
          <div />
        )}
        <Panel title="Best trial parameters">
          <table className="w-full text-[11.5px]">
            <tbody>
              {gates.best_params.map(([k, v]) => (
                <tr key={k}>
                  <td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{k}</td>
                  <td className="mono px-3 py-1.5 text-right" style={{ color: "var(--ink-2)" }}>{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
      {engineId === "momentum" && (
        <div className="px-5 pb-4">
          <Panel title="Parameter-search heatmap — lookback_days × hold_days (OOS Sharpe)">
            <div className="p-4">
              <div className="grid gap-[2px]" style={{ gridTemplateColumns: "repeat(6, 1fr)" }}>
                {Array.from({ length: 30 }).map((_, i) => {
                  const sharpe = -0.4 + Math.random() * 1.8;
                  const isBest = i === 14;
                  return (
                    <div key={i} className="text-center text-[10px] py-2 mono"
                      style={{
                        background: sharpe > 1 ? "var(--accent)" : sharpe > 0 ? "var(--bg-3)" : "var(--bg-2)",
                        color: sharpe > 1 ? "var(--bg)" : "var(--ink-3)",
                        border: isBest ? "1px solid var(--pos)" : "none",
                      }}>
                      {sharpe.toFixed(2)}
                    </div>
                  );
                })}
              </div>
              <div className="text-[11px] mt-3" style={{ color: "var(--ink-3)" }}>
                lookback ∈ [126, 252, 378, 504, 630] · hold ∈ [5, 10, 15, 21, 30, 45] · 64 trials · best Sharpe 1.31 at (252, 21)
              </div>
            </div>
          </Panel>
        </div>
      )}
      {engineId === "sentinel" && (
        <div className="px-5 pb-4">
          <Panel title="Bear Score — 180-day timeline">
            <div className="p-4" style={{ height: 180 }}>
              <svg viewBox="0 0 800 140" preserveAspectRatio="none" className="h-full w-full">
                <line x1="0" y1="56" x2="800" y2="56" stroke="var(--warn)" strokeWidth="1" strokeDasharray="4 4" />
                <text x="8" y="50" fill="var(--warn)" fontSize="10" fontFamily="monospace">60 (activation)</text>
                <path d="M0,90 C100,80 200,70 300,75 C400,72 500,68 600,55 C700,42 750,38 800,32"
                  fill="var(--accent)" opacity="0.18" stroke="none" />
                <path d="M0,90 C100,80 200,70 300,75 C400,72 500,68 600,55 C700,42 750,38 800,32"
                  fill="none" stroke="var(--accent)" strokeWidth="1.5" />
              </svg>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
