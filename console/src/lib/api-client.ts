/**
 * Console API client — fetches from the FastAPI service deployed at
 * NEXT_PUBLIC_API_BASE. All endpoints return JSON whose shape mirrors
 * the types in mock-data.ts, so view components can swap their source
 * without refactoring.
 */

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "https://console-api-production-4576.up.railway.app";

async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`${path} → HTTP ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  overview: () => fetchJSON<{
    kpis: Array<{ label: string; value: string; sub?: string; tone?: "pos" | "neg" | "warn" | "neutral" }>;
    engines: Array<{ id: string; name: string; tone: string; status: string; kind: string; credibility: number; oosSharpe?: number; dsr?: number; positions?: number; capital?: string; alloc?: string; note?: string }>;
    holdings: Array<{ engine: string; ticker: string; qty: number; entry: number; last: number; pnl: string; pnlPct: string; wgt: string; held: string }>;
    signals: Array<{ engine: string; ticker: string; side: string; note: string; strength: number; time: string }>;
    aars: Array<{ engine: string; ticker: string; side: string; exitReason: string; dates: string; hold: string; qty: number; prices: string; pnlPct: string }>;
    latest_data_ops_complete: string | null;
  }>("/api/overview"),
  forensics: () => fetchJSON<{ triggers: Array<{ id: string; severity: string; trigger: string; engine: string; note: string; when: string }> }>("/api/forensics"),
  engine: (id: string) => fetchJSON<{
    card: { id: string; name: string; tone: string; status: string; kind: string };
    gates: Array<{ k: string; v: number; thr: number; passed: boolean }>;
    best_params: Array<[string, string]>;
  }>(`/api/engines/${id}`),
  ticker: (symbol: string) => fetchJSON<{
    symbol: string;
    bars: Array<{ date: string; o: number; h: number; l: number; c: number; v: number }>;
    ledger: Array<{ engine: string; side: string; entry: number; exit: number | null; qty: number; pnl: string; held: string; exit_reason: string | null }>;
    context: Record<string, unknown>;
  }>(`/api/ticker/${symbol}`),
  lab: () => fetchJSON<{
    summary: { runs_30d: number; survived: number; failed: number; pending_promotion: number; queued: number };
    runs: Array<{ id: string; engine: string; candidate: string; date: string; seed: number; duration: string; verdict: string; dsr: number; sharpe: number; credibility: number; trials: number; isolationViolations: number; promotion_pending: boolean; note: string }>;
  }>("/api/lab"),
  ecr: () => fetchJSON<{
    queue: Array<{ id: string; kind: string; engine: string; action: string; submitted_by: string; submitted_when: string; summary: string; diff: string; lab_dossier: string | null }>;
    decided: Array<{ decided: string; kind: string; engine: string; action: string; verdict: string; diff: string }>;
    lifecycle: Record<"LAB" | "PAPER" | "LIVE" | "RETIRED", Array<{ id: string; name: string }>>;
  }>("/api/ecr"),
  allocator: () => fetchJSON<{
    method: string;
    trigger: string;
    last_run: string;
    next_run: string;
    allocations: Array<{ engine: string; pct: number; color: string }>;
  }>("/api/allocator"),
  healthPage: () => fetchJSON<{
    kpis: { open_holds: number; open_escalations_7d: number; undispositioned: number; cross_table_audit: string; llm_proposals_open: number; self_heal_cycles_24h: number };
    ladder: Array<{ rung: string; name: string; detail: string; status: string; tone: string; count: string }>;
    holds: Array<{ source: string; held: string; cycles: number; reason: string; esc: string }>;
    auditheal: Array<{ source: string; state: string; last: string; note: string }>;
    escalations: Array<{ when: string; type: string; ref: string; cls: string; open: boolean; msg: string }>;
    daemons: Array<{ daemon: string; platform: string; lane: string; status: string; last_deploy: string; last_event: string; restart_policy: string; ipv6_egress: boolean; role: string }>;
  }>("/api/health-page"),
  digest: () => fetchJSON<{
    digest: {
      week_of: string; generated_ts: string; acked: boolean; weeks_unacked: number; threshold: number; live_clearance: string;
      sections: Array<{ id: string; label: string; open: boolean; tone: string; items: string[] }>;
      ack_history: Array<{ week: string; acked_at: string; unacked: boolean }>;
    };
    llm_triage: Array<{ id: string; lane: string; ref: string; cls: string; disposition: string; confidence: number; model: string; persona: string; rationale: string; fence: string }>;
  }>("/api/digest"),
  dataPipeline: () => fetchJSON<{
    kpis: { passed: number; warnings: number; failed: number; data_ops_event: string | null; confidence: string; tickers_tracked: number; daily_bars_60d: number; forensics_open: number };
    validation: Array<{ check: string; status: string; rows: number; age: string; notes: string }>;
    self_heal: Array<{ time: string; stage: string; result: string; duration: string; notes: string }>;
  }>("/api/data-pipeline"),
  providers: () => fetchJSON<{ bindings: Array<{ feed: string; provider: string; status: string; adapter: string; note: string }> }>("/api/providers"),
};

/**
 * Generic React hook for fetching from the API. Returns { data, loading, error }.
 * Re-fetches on dependency change. Use inside client components.
 */
import { useEffect, useState } from "react";

export function useApi<T>(fetcher: () => Promise<T>, deps: React.DependencyList = []): { data: T | null; loading: boolean; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetcher()
      .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(String(e?.message ?? e)); setLoading(false); } });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error };
}
