"use client";
import type { ReactNode } from "react";

export function ViewHeader({ eyebrow, title, subtitle, meta, actions }: {
  eyebrow: string; title: string; subtitle?: string;
  meta?: Array<[string, string]>; actions?: ReactNode;
}) {
  return (
    <div className="hairline-b px-5 py-3" style={{ background: "var(--bg-1)" }}>
      <div className="eyebrow mb-1">{eyebrow}</div>
      <div className="flex items-baseline gap-3">
        <h1 className="text-[24px] font-medium tracking-tight" style={{ color: "var(--ink)" }}>{title}</h1>
        {subtitle && <span className="text-[12.5px]" style={{ color: "var(--ink-3)" }}>{subtitle}</span>}
        {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
      </div>
      {meta && (
        <div className="mt-2 flex items-center gap-5 text-[11px]">
          {meta.map(([k, v]) => (
            <div key={k} className="flex items-center gap-1.5">
              <span className="eyebrow">{k}</span>
              <span className="mono" style={{ color: "var(--ink-2)" }}>{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Panel({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="hairline" style={{ background: "var(--panel)" }}>
      {title && (
        <div className="hairline-b flex items-center px-4 py-2.5" style={{ background: "var(--panel-hd)" }}>
          <div className="text-[12.5px]" style={{ color: "var(--ink-2)" }}>{title}</div>
          {action && <div className="ml-auto">{action}</div>}
        </div>
      )}
      {children}
    </div>
  );
}

export function Kpi({ label, value, sub, tone = "neutral" }: { label: string; value: string; sub?: string; tone?: "pos" | "neg" | "warn" | "neutral" }) {
  const color =
    tone === "pos" ? "var(--pos)" :
    tone === "neg" ? "var(--neg)" :
    tone === "warn" ? "var(--warn)" :
    "var(--ink)";
  return (
    <div className="hairline px-3 py-2.5" style={{ background: "var(--panel)" }}>
      <div className="eyebrow mb-1">{label}</div>
      <div className="mono text-[20px] leading-tight" style={{ color }}>{value}</div>
      {sub && <div className="mono text-[11px] mt-1" style={{ color: "var(--ink-3)" }}>{sub}</div>}
    </div>
  );
}

export function Pill({ tone = "neutral", children }: { tone?: "pos" | "neg" | "warn" | "accent" | "neutral"; children: ReactNode }) {
  const bg =
    tone === "pos" ? "var(--pos)" :
    tone === "neg" ? "var(--neg)" :
    tone === "warn" ? "var(--warn)" :
    tone === "accent" ? "var(--accent)" :
    "var(--bg-3)";
  const fg = tone === "neutral" ? "var(--ink-2)" : "var(--bg)";
  return (
    <span className="mono text-[9.5px] px-1.5 py-0.5" style={{ background: bg, color: fg }}>
      {children}
    </span>
  );
}

export function EnginePill({ engine }: { engine: string }) {
  const e = engine.toLowerCase();
  const color =
    e.startsWith("mom") ? "var(--mom)" :
    e.startsWith("rev") ? "var(--rev)" :
    e.startsWith("vec") ? "var(--vec)" :
    e.startsWith("sen") ? "var(--sen)" :
    e.startsWith("can") ? "var(--can)" :
    "var(--bg-3)";
  return (
    <span className="mono text-[9.5px] px-1.5 py-0.5" style={{ background: "var(--bg-3)", color, border: `1px solid ${color}` }}>
      {engine.slice(0, 3).toUpperCase()}
    </span>
  );
}

export function Stub({ title }: { title: string }) {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="text-center">
        <div className="eyebrow mb-2">VIEW STUB</div>
        <div className="mono text-[14px]" style={{ color: "var(--ink-2)" }}>{title}</div>
        <div className="text-[11.5px] mt-2" style={{ color: "var(--ink-3)" }}>
          this view ships in a later commit
        </div>
      </div>
    </div>
  );
}
