"use client";
import { useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import { Overview } from "@/components/Overview";
import { Forensics } from "@/components/views/Forensics";
import { EngineDetail } from "@/components/views/EngineDetail";
import { Lab } from "@/components/views/Lab";
import { ECR } from "@/components/views/ECR";
import { Allocator } from "@/components/views/Allocator";
import { Health } from "@/components/views/Health";
import { WeeklyDigest } from "@/components/views/WeeklyDigest";
import { DataPipeline } from "@/components/views/DataPipeline";
import { Providers } from "@/components/views/Providers";
import { TickerDrillin } from "@/components/views/TickerDrillin";
import { Stub } from "@/components/views/Primitives";
import type { EngineId } from "@/lib/mock-data";

export default function Home() {
  const [route, setRoute] = useState("overview");

  let view: React.ReactNode;
  if (route === "overview") view = <Overview />;
  else if (route === "forensics") view = <Forensics />;
  else if (route.startsWith("engine:")) {
    const id = route.split(":")[1] as EngineId;
    view = <EngineDetail engineId={id} />;
  }
  else if (route === "lab") view = <Lab />;
  else if (route === "sdlc") view = <ECR />;
  else if (route === "allocator") view = <Allocator />;
  else if (route === "health") view = <Health />;
  else if (route === "digest") view = <WeeklyDigest />;
  else if (route === "data") view = <DataPipeline />;
  else if (route === "providers") view = <Providers />;
  else if (route === "ticker") view = <TickerDrillin />;
  else view = <Stub title={route} />;

  return (
    <div className="flex h-screen w-screen overflow-hidden" style={{ background: "var(--bg)" }}>
      <Sidebar route={route} onRoute={setRoute} />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Topbar />
        <main className="flex-1 overflow-y-auto" style={{ background: "var(--bg)" }}>{view}</main>
      </div>
    </div>
  );
}
