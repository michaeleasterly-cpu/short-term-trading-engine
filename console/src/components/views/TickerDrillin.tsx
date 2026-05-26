"use client";
import { ViewHeader, Panel, Pill, EnginePill } from "./Primitives";

export function TickerDrillin({ ticker = "AAPL" }: { ticker?: string }) {
  return (
    <div>
      <ViewHeader
        eyebrow="TICKER"
        title={`${ticker} $186.42`}
        actions={
          <>
            <EnginePill engine="momentum" />
            <Pill tone="pos">LONG 100</Pill>
            <span className="mono text-[12px]" style={{ color: "var(--pos)" }}>+1.26%</span>
          </>
        }
      />
      <div className="px-5 py-4">
        <Panel title="Price · entry/exit markers">
          <div className="p-4" style={{ height: 320 }}>
            <svg viewBox="0 0 800 260" preserveAspectRatio="none" className="h-full w-full">
              {[40, 80, 120, 160, 200].map(y => (
                <line key={y} x1="0" y1={y} x2="800" y2={y} stroke="var(--line)" strokeWidth="0.5" />
              ))}
              {Array.from({ length: 60 }).map((_, i) => {
                const x = 10 + i * 13;
                const o = 100 + Math.random() * 80;
                const c = 100 + Math.random() * 80;
                const up = c > o;
                const y1 = Math.min(o, c);
                const y2 = Math.max(o, c);
                return (
                  <rect key={i} x={x} y={y1} width={9} height={Math.max(2, y2 - y1)}
                    fill={up ? "var(--pos)" : "var(--neg)"} />
                );
              })}
              <polygon points="200,150 195,160 205,160" fill="var(--pos)" />
              <line x1="200" y1="0" x2="200" y2="220" stroke="var(--pos)" strokeWidth="0.5" strokeDasharray="4 4" />
              <text x="210" y="155" fill="var(--pos)" fontSize="10" fontFamily="monospace">LONG $184.10</text>
            </svg>
          </div>
        </Panel>
      </div>
      <div className="grid gap-3 px-5 pb-5" style={{ gridTemplateColumns: "2fr 1fr" }}>
        <Panel title="Trade ledger">
          <table className="w-full text-[11.5px]">
            <thead>
              <tr style={{ color: "var(--ink-3)" }}>
                {["Engine", "Side", "Entry", "Exit", "Qty", "P&L", "Held", "Exit reason"].map(h => (
                  <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="px-3 py-1.5"><EnginePill engine="momentum" /></td>
                <td className="px-3 py-1.5"><Pill tone="pos">LONG</Pill></td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>$184.10</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>— open —</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>100</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--pos)" }}>+$232</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>12d</td>
                <td className="mono px-3 py-1.5 text-[10px]" style={{ color: "var(--ink-3)" }}>—</td>
              </tr>
              <tr style={{ opacity: 0.6 }}>
                <td className="px-3 py-1.5"><EnginePill engine="momentum" /></td>
                <td className="px-3 py-1.5"><Pill tone="pos">LONG</Pill></td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>$176.40</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>$182.10</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>100</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--pos)" }}>+$570</td>
                <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>21d</td>
                <td className="mono px-3 py-1.5 text-[10px]" style={{ color: "var(--ink-3)" }}>take_profit</td>
              </tr>
            </tbody>
          </table>
        </Panel>
        <Panel title="Signal context">
          <table className="w-full text-[11.5px]">
            <tbody>
              <tr><td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>signal</td><td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>monthly momentum top-8</td></tr>
              <tr><td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>rank</td><td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>3 / 500</td></tr>
              <tr><td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>strength</td><td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>0.86</td></tr>
              <tr><td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>$ volume</td><td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>$8.2B avg</td></tr>
              <tr><td className="eyebrow px-3 py-1.5" style={{ color: "var(--ink-3)" }}>tier</td><td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>T1</td></tr>
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  );
}
