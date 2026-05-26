"use client";
import { useEffect, useState } from "react";

const STATUS_STRIP = [
  { label: "NYSE",   state: "OPEN",   tone: "pos" as const },
  { label: "BROKER", state: "LIVE",   tone: "pos" as const },
  { label: "DATA",   state: "FRESH",  tone: "pos" as const },
  { label: "RISK",   state: "OK",     tone: "pos" as const },
  { label: "LIVE",   state: "PAPER",  tone: "warn" as const },
];

const TONE_VAR = { pos: "var(--pos)", neg: "var(--neg)", warn: "var(--warn)" };

export function Topbar() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const hh = String(now.getUTCHours()).padStart(2, "0");
  const mm = String(now.getUTCMinutes()).padStart(2, "0");
  const ss = String(now.getUTCSeconds()).padStart(2, "0");

  return (
    <header className="hairline-b flex items-center gap-3 px-4 py-2" style={{ background: "var(--panel-hd)", height: 44 }}>
      <div className="flex items-center gap-3">
        <div className="text-[14px] font-medium tracking-tight" style={{ color: "var(--ink)" }}>Operator Console</div>
        <div className="eyebrow">v0.1</div>
      </div>
      <div className="mx-auto flex items-center gap-5">
        {STATUS_STRIP.map(s => (
          <div key={s.label} className="flex items-center gap-1.5">
            <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: TONE_VAR[s.tone] }} />
            <span className="eyebrow">{s.label}</span>
            <span className="mono text-[10.5px]" style={{ color: "var(--ink-2)" }}>{s.state}</span>
          </div>
        ))}
      </div>
      <div className="flex items-center gap-4">
        <div className="mono text-[12.5px]" style={{ background: "var(--bg-2)", padding: "3px 8px" }}>
          <span style={{ color: "var(--pos)" }}>+$1,538</span>
          <span style={{ color: "var(--ink-3)" }}> · +1.51%</span>
        </div>
        <div className="mono text-[12.5px]" style={{ color: "var(--ink-2)" }}>$103,442</div>
        <div className="mono text-[11px]" style={{ color: "var(--ink-3)" }}>{hh}:{mm}:{ss} UTC</div>
      </div>
    </header>
  );
}
