"use client";
import { useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import { Overview } from "@/components/Overview";

export default function Home() {
  const [route, setRoute] = useState("overview");

  return (
    <div className="flex h-screen w-screen overflow-hidden" style={{ background: "var(--bg)" }}>
      <Sidebar route={route} onRoute={setRoute} />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Topbar />
        <main className="flex-1 overflow-y-auto" style={{ background: "var(--bg)" }}>
          {route === "overview" && <Overview />}
          {route !== "overview" && (
            <div className="flex h-full items-center justify-center">
              <div className="text-center">
                <div className="eyebrow mb-2">VIEW STUB</div>
                <div className="mono text-[14px]" style={{ color: "var(--ink-2)" }}>{route}</div>
                <div className="text-[11.5px] mt-2" style={{ color: "var(--ink-3)" }}>
                  this view ships in a later commit
                </div>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
