"use client";
import { NAV_BADGES } from "@/lib/mock-data";

type NavItem =
  | { kind: "group"; label: string }
  | { kind: "item"; id: string; label: string; kbd: string; tone?: string; badge?: number };

const NAV: NavItem[] = [
  { kind: "group", label: "Portfolio" },
  { kind: "item", id: "overview", label: "Overview", kbd: "O" },
  { kind: "item", id: "forensics", label: "Forensics", kbd: "F", badge: NAV_BADGES.forensics },
  { kind: "group", label: "Engines (Live)" },
  { kind: "item", id: "engine:momentum", label: "Momentum", kbd: "1", tone: "mom" },
  { kind: "item", id: "engine:reversion", label: "Reversion", kbd: "2", tone: "rev" },
  { kind: "item", id: "engine:vector", label: "Vector", kbd: "3", tone: "vec" },
  { kind: "item", id: "engine:sentinel", label: "Sentinel", kbd: "4", tone: "sen" },
  { kind: "item", id: "engine:canary", label: "Canary", kbd: "5", tone: "can" },
  { kind: "group", label: "Engine SDLC" },
  { kind: "item", id: "lab", label: "The Lab", kbd: "L", badge: NAV_BADGES.lab },
  { kind: "item", id: "sdlc", label: "ECR Queue", kbd: "E", badge: NAV_BADGES.ecr },
  { kind: "group", label: "Capital" },
  { kind: "item", id: "allocator", label: "Allocator", kbd: "A" },
  { kind: "group", label: "Operations" },
  { kind: "item", id: "health", label: "Health", kbd: "H", badge: NAV_BADGES.health },
  { kind: "item", id: "digest", label: "Weekly Digest", kbd: "W", badge: NAV_BADGES.digest },
  { kind: "item", id: "data", label: "Data Pipeline", kbd: "D" },
  { kind: "item", id: "providers", label: "Providers", kbd: "P" },
];

const TONE_VAR: Record<string, string> = {
  mom: "var(--mom)", rev: "var(--rev)", vec: "var(--vec)", sen: "var(--sen)", can: "var(--can)",
};

interface SidebarProps {
  route: string;
  onRoute: (id: string) => void;
}

export function Sidebar({ route, onRoute }: SidebarProps) {
  return (
    <aside className="hairline-r flex flex-col" style={{ width: 208, background: "var(--bg-1)" }}>
      <div className="hairline-b px-4 py-3" style={{ background: "var(--panel-hd)" }}>
        <div className="flex items-center gap-2">
          <div className="h-4 w-4" style={{ background: "var(--accent)" }} />
          <div className="text-[13px] font-medium tracking-tight" style={{ color: "var(--ink)" }}>STE</div>
          <div className="eyebrow ml-auto">CONSOLE</div>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto py-2">
        {NAV.map((n, i) =>
          n.kind === "group" ? (
            <div key={i} className="eyebrow px-4 pt-3 pb-1">{n.label}</div>
          ) : (
            <button
              key={n.id}
              onClick={() => onRoute(n.id)}
              className="group relative flex w-full items-center px-4 py-1.5 text-left hover:cursor-pointer"
              style={{
                color: route === n.id ? "var(--ink)" : "var(--ink-2)",
                background: route === n.id ? "var(--row-hov)" : "transparent",
                borderLeft: route === n.id ? "2px solid var(--accent)" : "2px solid transparent",
              }}
            >
              {n.tone && (
                <span className="mr-2 inline-block h-1.5 w-1.5 rounded-full" style={{ background: TONE_VAR[n.tone] }} />
              )}
              <span className="text-[12.5px]">{n.label}</span>
              <span className="ml-auto flex items-center gap-2">
                {n.badge ? (
                  <span
                    className="mono rounded-sm px-1.5 py-0.5 text-[10px]"
                    style={{ background: "var(--warn)", color: "var(--bg)" }}
                  >
                    {n.badge}
                  </span>
                ) : null}
                <span className="mono text-[10px]" style={{ color: "var(--ink-4)" }}>{n.kbd}</span>
              </span>
            </button>
          )
        )}
      </nav>
      <div className="hairline-t px-4 py-3" style={{ background: "var(--bg-1)" }}>
        <div className="eyebrow">CAPITAL</div>
        <div className="mono text-[14px] mt-1" style={{ color: "var(--ink)" }}>$103,442</div>
        <div className="mono text-[10.5px] mt-0.5" style={{ color: "var(--ink-3)" }}>$24,118 unallocated</div>
        <div className="mt-2 flex items-center gap-1.5">
          <span className="relative inline-block h-1.5 w-1.5 rounded-full" style={{ background: "var(--pos)" }}>
            <span className="absolute inset-0 animate-ping rounded-full" style={{ background: "var(--pos)", opacity: 0.4 }} />
          </span>
          <span className="text-[10.5px]" style={{ color: "var(--ink-3)" }}>heartbeat 12s ago</span>
        </div>
      </div>
    </aside>
  );
}
