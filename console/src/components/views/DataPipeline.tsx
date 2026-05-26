"use client";
import { ViewHeader, Panel, Kpi, Pill } from "./Primitives";
import { DATA_VALIDATION, SELF_HEAL_LOG } from "@/lib/mock-data";

export function DataPipeline() {
  const passed = DATA_VALIDATION.filter(c => c.status === "PASS").length;
  const failed = DATA_VALIDATION.filter(c => c.status === "FAIL").length;
  return (
    <div>
      <ViewHeader
        eyebrow="SYSTEM / DATA PIPELINE"
        title="Data Pipeline"
        meta={[
          ["last update", "21:30 UTC daily"],
          ["cycle latency", "~25m"],
          ["self-heal", "3 cycles 24h"],
        ]}
        actions={
          <>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ background: "var(--accent)", color: "var(--bg)" }}>Run data update</button>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Run validation</button>
            <button className="hairline mono text-[11px] px-3 py-1.5" style={{ color: "var(--ink-2)" }}>Audit pipeline</button>
          </>
        }
      />
      <div className="grid gap-2 px-5 py-4" style={{ gridTemplateColumns: "repeat(8, minmax(120px, 1fr))" }}>
        <Kpi label="Passed" value={String(passed)} tone="pos" />
        <Kpi label="Warnings" value="0" tone="neutral" />
        <Kpi label="Failed" value={String(failed)} tone={failed ? "neg" : "neutral"} />
        <Kpi label="DATA_OPS event" value="OK" sub="last 21:30 UTC" tone="pos" />
        <Kpi label="Confidence" value="100%" tone="pos" />
        <Kpi label="Tickers tracked" value="7,643" />
        <Kpi label="Daily bars" value="1.84M" />
        <Kpi label="Forensics" value="3 triggers" tone="warn" />
      </div>
      <div className="px-5 pb-4">
        <Panel title="Validation suite — 13 checks">
          <table className="w-full text-[11.5px]">
            <thead>
              <tr style={{ color: "var(--ink-3)" }}>
                {["Check", "Status", "Rows", "Age", "Notes"].map(h => (
                  <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {DATA_VALIDATION.map((c, i) => (
                <tr key={i}>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{c.check}</td>
                  <td className="px-3 py-1.5"><Pill tone={c.status === "PASS" ? "pos" : "neg"}>{c.status}</Pill></td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{c.rows.toLocaleString()}</td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{c.age}</td>
                  <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{c.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
      <div className="px-5 pb-5">
        <Panel title="Self-heal log">
          <table className="w-full text-[11.5px]">
            <thead>
              <tr style={{ color: "var(--ink-3)" }}>
                {["Time", "Stage", "Result", "Duration", "Notes"].map(h => (
                  <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {SELF_HEAL_LOG.map((s, i) => (
                <tr key={i}>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{s.time}</td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{s.stage}</td>
                  <td className="px-3 py-1.5"><Pill tone={s.result === "HEALED" ? "pos" : "warn"}>{s.result}</Pill></td>
                  <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{s.duration}</td>
                  <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{s.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  );
}
