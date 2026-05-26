"use client";
import { useState } from "react";
import { ViewHeader, Panel, Kpi, Pill, EnginePill } from "./Primitives";
import { LAB_RUNS } from "@/lib/mock-data";

export function Lab() {
  const [selected, setSelected] = useState(LAB_RUNS[0].id);
  const run = LAB_RUNS.find(r => r.id === selected) ?? LAB_RUNS[0];
  return (
    <div>
      <ViewHeader
        eyebrow="OPERATIONS"
        title="The Lab"
        subtitle="SP2 walk-forward parameter-search · LabContext isolation · Lab-namespaced credibility"
        meta={[
          ["runs (30d)", "14"], ["survived", "7"], ["failed", "5"],
          ["pending promotion", "1"], ["queued", "2"],
        ]}
        actions={
          <>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>New Lab run</button>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Open Lab dossiers</button>
          </>
        }
      />
      <div className="px-5 py-3">
        <div className="hairline px-4 py-3 text-[11.5px]" style={{ background: "var(--bg-2)", borderLeft: "3px solid var(--accent)", color: "var(--ink-2)" }}>
          <span className="eyebrow mr-1">⚗ ISOLATION</span>
          The Lab is fully isolated from live trading. Every guarded constructor raises <span className="mono">LabIsolationViolation</span> inside an active <span className="mono">LabContext</span>. Credibility writes are Lab-namespaced (<span className="mono">backtest_credibility.lab.&lt;candidate&gt;</span>) and never pollute the live capital gate.
        </div>
      </div>
      <div className="grid gap-3 px-5 py-3" style={{ gridTemplateColumns: "1fr 1.5fr" }}>
        <Panel title="Recent runs">
          {LAB_RUNS.map(r => {
            const sel = r.id === selected;
            return (
              <button key={r.id} onClick={() => setSelected(r.id)}
                className="hairline-b w-full px-4 py-2.5 text-left hover:cursor-pointer block"
                style={{
                  background: sel ? "var(--row-hov)" : "transparent",
                  borderLeft: sel ? "2px solid var(--accent)" : "2px solid transparent",
                }}>
                <div className="flex items-center gap-2">
                  <EnginePill engine={r.engine} />
                  <span className="mono text-[12px]" style={{ color: "var(--ink)" }}>{r.candidate}</span>
                  <Pill tone={r.verdict === "SURVIVED" ? "pos" : "warn"}>{r.verdict}</Pill>
                  <span className="mono ml-auto text-[11px]" style={{ color: "var(--ink-3)" }}>DSR {r.dsr}</span>
                </div>
                <div className="mono text-[10px] mt-1" style={{ color: "var(--ink-3)" }}>
                  {r.date} · seed {r.seed} · {r.duration}
                </div>
              </button>
            );
          })}
        </Panel>
        <Panel title={run.candidate}
          action={run.promotion_pending && <button className="hairline mono text-[11px] px-3 py-1" style={{ background: "var(--accent)", color: "var(--bg)" }}>Promote → ECR</button>}
        >
          <div className="px-4 py-3">
            <div className="flex items-center gap-2 mb-3">
              <Pill tone={run.verdict === "SURVIVED" ? "pos" : "warn"}>{run.verdict}</Pill>
              <span className="text-[12px]" style={{ color: "var(--ink-3)" }}>{run.note}</span>
            </div>
            <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
              <Kpi label="DSR" value={String(run.dsr)} sub="≥ 0.95" tone={run.dsr >= 0.95 ? "pos" : "warn"} />
              <Kpi label="Sharpe" value={String(run.sharpe)} sub="OOS final" tone={run.sharpe >= 1 ? "pos" : "warn"} />
              <Kpi label="Credibility" value={String(run.credibility)} sub="≥ 60" tone={run.credibility >= 60 ? "pos" : "warn"} />
              <Kpi label="Trials" value={String(run.trials)} sub="cum n_trials" />
              <Kpi label="Isolation" value={String(run.isolationViolations)} sub="violations" tone={run.isolationViolations === 0 ? "pos" : "neg"} />
            </div>
            <table className="mt-4 w-full text-[11.5px]">
              <tbody>
                <tr><td className="eyebrow py-1.5" style={{ color: "var(--ink-3)" }}>namespace</td><td className="mono py-1.5" style={{ color: "var(--ink-2)" }}>backtest_credibility.{run.candidate}</td></tr>
                <tr><td className="eyebrow py-1.5" style={{ color: "var(--ink-3)" }}>dossier</td><td className="mono py-1.5" style={{ color: "var(--accent)" }}>docs/lab/{run.id}/dossier.json</td></tr>
                <tr><td className="eyebrow py-1.5" style={{ color: "var(--ink-3)" }}>note</td><td className="py-1.5" style={{ color: "var(--ink-2)" }}>{run.note}</td></tr>
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}
