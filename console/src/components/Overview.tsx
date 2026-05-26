"use client";
import { KPI_TILES, ENGINE_CARDS, HOLDINGS, SIGNALS, AARS, type EngineTone } from "@/lib/mock-data";

const TONE_VAR: Record<EngineTone, string> = {
  mom: "var(--mom)", rev: "var(--rev)", vec: "var(--vec)", sen: "var(--sen)", can: "var(--can)",
};

function ViewHeader() {
  return (
    <div className="hairline-b px-5 py-3" style={{ background: "var(--bg-1)" }}>
      <div className="eyebrow mb-1">PORTFOLIO</div>
      <div className="flex items-baseline gap-3">
        <h1 className="text-[24px] font-medium tracking-tight" style={{ color: "var(--ink)" }}>Overview</h1>
        <span className="text-[12.5px]" style={{ color: "var(--ink-3)" }}>live capital snapshot</span>
      </div>
      <div className="mt-2 flex items-center gap-5 text-[11px]">
        <Meta k="state" v="PAPER" />
        <Meta k="engines live" v="4 / 5" />
        <Meta k="open positions" v="12" />
        <Meta k="cycle" v="2026-W22 · day 4" />
      </div>
    </div>
  );
}

function Meta({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="eyebrow">{k}</span>
      <span className="mono" style={{ color: "var(--ink-2)" }}>{v}</span>
    </div>
  );
}

function KpiStrip() {
  return (
    <div className="grid gap-2 px-5 py-4" style={{ gridTemplateColumns: "repeat(8, minmax(120px, 1fr))" }}>
      {KPI_TILES.map(t => (
        <div key={t.label} className="hairline px-3 py-2.5" style={{ background: "var(--panel)" }}>
          <div className="eyebrow mb-1">{t.label}</div>
          <div
            className="mono text-[20px] leading-tight"
            style={{
              color:
                t.tone === "pos" ? "var(--pos)" :
                t.tone === "neg" ? "var(--neg)" :
                t.tone === "warn" ? "var(--warn)" :
                "var(--ink)",
            }}
          >
            {t.value}
          </div>
          {t.sub && <div className="mono text-[11px] mt-1" style={{ color: "var(--ink-3)" }}>{t.sub}</div>}
        </div>
      ))}
    </div>
  );
}

function EquityCurve() {
  return (
    <div className="hairline mx-5 mb-4" style={{ background: "var(--panel)" }}>
      <div className="hairline-b flex items-center px-4 py-2.5" style={{ background: "var(--panel-hd)" }}>
        <div className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>Equity curve</div>
        <div className="eyebrow ml-3">320 sessions · SPY benchmark dashed</div>
        <div className="ml-auto flex">
          {["30d", "90d", "1y", "all"].map((w, i) => (
            <button
              key={w}
              className="hairline mono text-[11px] px-2.5 py-1"
              style={{
                color: i === 2 ? "var(--ink)" : "var(--ink-3)",
                background: i === 2 ? "var(--bg-3)" : "var(--bg-1)",
                marginLeft: i === 0 ? 0 : -1,
              }}
            >
              {w}
            </button>
          ))}
        </div>
      </div>
      <div className="px-4 py-3" style={{ height: 240 }}>
        <svg viewBox="0 0 800 200" preserveAspectRatio="none" className="h-full w-full">
          {[40, 80, 120, 160].map(y => (
            <line key={y} x1="0" y1={y} x2="800" y2={y} stroke="var(--line)" strokeWidth="0.5" />
          ))}
          <path
            d="M0,160 C100,140 200,130 300,110 C400,100 500,90 600,70 C700,60 750,50 800,40"
            fill="none"
            stroke="var(--accent)"
            strokeWidth="1.5"
          />
          <path
            d="M0,160 C100,140 200,130 300,110 C400,100 500,90 600,70 C700,60 750,50 800,40 L800,200 L0,200 Z"
            fill="var(--accent)"
            opacity="0.08"
          />
          <path
            d="M0,150 C100,145 200,140 300,130 C400,125 500,115 600,100 C700,85 750,75 800,65"
            fill="none"
            stroke="var(--ink-3)"
            strokeWidth="1"
            strokeDasharray="4 4"
          />
          <circle cx="800" cy="40" r="3" fill="var(--accent)" />
        </svg>
      </div>
    </div>
  );
}

function EnginesGrid() {
  return (
    <div className="grid gap-3 px-5 pb-4" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
      {ENGINE_CARDS.map(e => (
        <div
          key={e.id}
          className="hairline relative px-3 py-3 hover:cursor-pointer"
          style={{
            background: "var(--panel)",
            borderTop: `2px solid ${TONE_VAR[e.tone]}`,
          }}
        >
          <div className="mb-1.5 flex items-center gap-1.5">
            <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: e.status === "GATED" ? "var(--warn)" : "var(--pos)" }} />
            <span className="eyebrow" style={{ color: TONE_VAR[e.tone] }}>{e.name}</span>
            <span
              className="mono ml-auto text-[9.5px] px-1.5 py-0.5"
              style={{
                background: e.status === "GATED" ? "var(--warn)" : e.status === "HEARTBEAT" ? "var(--bg-3)" : "var(--pos)",
                color: e.status === "HEARTBEAT" ? "var(--ink-2)" : "var(--bg)",
              }}
            >
              {e.status}
            </span>
          </div>
          <div className="text-[10.5px] italic mb-2" style={{ color: "var(--ink-3)" }}>{e.kind}</div>
          {e.note ? (
            <div className="text-[11px] italic" style={{ color: "var(--ink-3)" }}>{e.note}</div>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-x-2 gap-y-1 text-[11px]">
                <Stat k="cred" v={String(e.credibility)} />
                <Stat k="OOS Sharpe" v={String(e.oosSharpe)} />
                <Stat k="DSR" v={String(e.dsr)} />
                <Stat k="positions" v={String(e.positions)} />
                <Stat k="capital" v={e.capital ?? "-"} />
                <Stat k="alloc" v={e.alloc ?? "-"} />
              </div>
              <div className="mt-2.5 h-1" style={{ background: "var(--bg-3)" }}>
                <div
                  className="h-full"
                  style={{
                    width: `${e.credibility}%`,
                    background: e.credibility >= 60 ? "var(--pos)" : "var(--warn)",
                  }}
                />
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="eyebrow">{k}</span>
      <span className="mono" style={{ color: "var(--ink-2)" }}>{v}</span>
    </div>
  );
}

function Holdings() {
  return (
    <div className="hairline" style={{ background: "var(--panel)" }}>
      <div className="hairline-b px-4 py-2.5" style={{ background: "var(--panel-hd)" }}>
        <div className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>Holdings</div>
      </div>
      <table className="w-full text-[11.5px]">
        <thead>
          <tr style={{ color: "var(--ink-3)" }}>
            {["Engine", "Ticker", "Qty", "Entry", "Last", "P&L", "P&L%", "Wgt", "Held"].map(h => (
              <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {HOLDINGS.map((h, i) => (
            <tr key={i} className="hover:cursor-pointer" style={{ background: i % 2 ? "transparent" : "var(--bg-1)" }}>
              <td className="px-3 py-1.5">
                <span className="mono text-[10px] px-1.5 py-0.5"
                  style={{ background: "var(--bg-3)", color: "var(--ink-2)" }}>{h.engine}</span>
              </td>
              <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{h.ticker}</td>
              <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{h.qty}</td>
              <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>${h.entry}</td>
              <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>${h.last}</td>
              <td className="mono px-3 py-1.5" style={{ color: h.pnl.startsWith("-") ? "var(--neg)" : "var(--pos)" }}>{h.pnl}</td>
              <td className="mono px-3 py-1.5" style={{ color: h.pnlPct.startsWith("-") ? "var(--neg)" : "var(--pos)" }}>{h.pnlPct}</td>
              <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{h.wgt}</td>
              <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{h.held}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Signals() {
  return (
    <div className="hairline" style={{ background: "var(--panel)" }}>
      <div className="hairline-b px-4 py-2.5" style={{ background: "var(--panel-hd)" }}>
        <div className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>Today&apos;s Signals</div>
      </div>
      <div>
        {SIGNALS.map((s, i) => {
          const blocked = s.note.toUpperCase().includes("BLOCKED");
          return (
            <div key={i} className="hairline-b flex items-center gap-2 px-3 py-2 text-[11.5px]"
              style={{ opacity: blocked ? 0.5 : 1 }}>
              <span className="mono text-[9.5px] px-1.5 py-0.5"
                style={{ background: "var(--bg-3)", color: "var(--ink-2)" }}>{s.engine}</span>
              <span className="mono" style={{ color: "var(--ink)" }}>{s.ticker}</span>
              <span className="mono text-[9.5px] px-1.5 py-0.5"
                style={{ background: s.side === "LONG" ? "var(--pos)" : "var(--neg)", color: "var(--bg)" }}>{s.side}</span>
              <span className="text-[11px]" style={{ color: "var(--ink-3)" }}>{s.note}</span>
              <span className="ml-auto mono text-[10.5px]" style={{ color: "var(--ink-3)" }}>{s.time}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Aars() {
  return (
    <div className="hairline" style={{ background: "var(--panel)" }}>
      <div className="hairline-b px-4 py-2.5" style={{ background: "var(--panel-hd)" }}>
        <div className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>Recent AARs</div>
      </div>
      <div>
        {AARS.map((a, i) => (
          <div key={i} className="hairline-b px-3 py-2 text-[11.5px]">
            <div className="flex items-center gap-2">
              <span className="mono text-[9.5px] px-1.5 py-0.5"
                style={{ background: "var(--bg-3)", color: "var(--ink-2)" }}>{a.engine}</span>
              <span className="mono" style={{ color: "var(--ink)" }}>{a.ticker}</span>
              <span className="mono text-[9.5px] px-1.5 py-0.5"
                style={{ background: a.side === "LONG" ? "var(--pos)" : "var(--neg)", color: "var(--bg)" }}>{a.side}</span>
              <span className="eyebrow ml-1">{a.exitReason}</span>
              <span className="ml-auto mono text-[12px]" style={{ color: a.pnlPct.startsWith("-") ? "var(--neg)" : "var(--pos)" }}>{a.pnlPct}</span>
            </div>
            <div className="mono text-[10px] mt-1" style={{ color: "var(--ink-3)" }}>
              {a.dates} · {a.hold} · {a.qty} @ {a.prices}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Overview() {
  return (
    <div>
      <ViewHeader />
      <KpiStrip />
      <EquityCurve />
      <EnginesGrid />
      <div className="grid gap-3 px-5 pb-5" style={{ gridTemplateColumns: "2fr 1fr" }}>
        <Holdings />
        <div className="flex flex-col gap-3">
          <Signals />
          <Aars />
        </div>
      </div>
    </div>
  );
}
