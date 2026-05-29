/**
 * Console API client — fetches from the FastAPI service deployed at
 * NEXT_PUBLIC_API_BASE. All endpoints return JSON whose shape mirrors
 * the types in mock-data.ts, so view components can swap their source
 * without refactoring.
 */

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "https://console-api-production-4576.up.railway.app";

async function fetchJSON<T>(
  path: string,
  opts: { useConsoleApi?: boolean } = { useConsoleApi: true },
): Promise<T> {
  // useConsoleApi=true (default) → call console-api directly. Used
  // for read-only status endpoints. useConsoleApi=false → call the
  // Next.js relative path; the Next.js route forwards with the
  // server-side bearer token. Used for endpoints under
  // /api/operations/data-pipeline/* that require auth.
  const url = opts.useConsoleApi === false
    ? path
    : `${API_BASE}${path}`;
  const res = await fetch(url, {
    cache: "no-store",
    credentials: opts.useConsoleApi === false ? "include" : "omit",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = JSON.stringify(await res.json());
    } catch {
      // No JSON body — keep detail empty.
    }
    throw new Error(
      `${path} → HTTP ${res.status} ${res.statusText}${detail ? " " + detail : ""}`,
    );
  }
  return (await res.json()) as T;
}

async function postJSON<T>(
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  // POST always goes through the Next.js route so we get auth +
  // server-side token forwarding. Never call console-api directly
  // from the browser with the token.
  const res = await fetch(path, {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail: Record<string, unknown> = {};
    try {
      detail = await res.json();
    } catch {
      detail = { error: res.statusText };
    }
    const err = new Error(
      `${path} → HTTP ${res.status} ${res.statusText}`,
    ) as Error & { status: number; payload: Record<string, unknown> };
    err.status = res.status;
    err.payload = detail;
    throw err;
  }
  return (await res.json()) as T;
}

// ───── Data Pipeline status + job-control types ─────

export type ChartCheckStatus = "PASS" | "WARN" | "FAIL" | "RUNNING" | "UNKNOWN" | "BLOCKED_VENDOR_ACCESS";

export type RemediationClass =
  | "scoped_auto_heal"
  | "full_stage_required"
  | "blocked_vendor"
  | "operator_required"
  | "unhealable"
  | "bootstrap"
  | "not_implemented";

export interface DataPipelineCheck {
  name: string;
  status: ChartCheckStatus;
  rows: number | null;
  age: string | null;
  notes: string;
  notes_details: Array<Record<string, unknown>> | null;
  last_checked_at: string | null;
  remediation_class: RemediationClass;
  target_stage: string | null;
  scope_kind: "full" | "tickers" | "tickers_dates";
  fallback_stage: string | null;
  vendor: string | null;
  blocker_reason: string | null;
  operator_procedure: string | null;
  operator_note: string | null;
  unhealable_reason: string | null;
  estimated_runtime_seconds: number | null;
  affected_symbols: string[];
  allowed_actions: string[];
  // Legacy compat fields:
  healable: boolean;
  actionable: boolean;
}

export interface DataPipelineSelfHealEntry {
  time: string;
  stage: string;
  result: "HEALED" | "FAILED" | "ESCALATED" | "SKIPPED" | "INFO";
  duration: string | null;
  notes: string;
  severity: string;
  event_type: string;
}

export interface ActiveJob {
  job_id: string;
  run_id: string;
  type: string | null;
  status: "QUEUED" | "RUNNING" | "SUCCESS" | "FAILED" | "CANCELLED" | "TIMEOUT" | "ABORTED";
  started_at: string;
  updated_at: string;
  elapsed_seconds: number;
  current_stage: string | null;
  current_check: string | null;
  completed_stages: Array<{
    stage: string;
    status: string;
    started_at: string | null;
    completed_at: string | null;
    duration_seconds: number | null;
    rows_processed: number | null;
    message: string | null;
  }>;
  pending_stages: string[];
  failed_stage: string | null;
  latest_log: { time: string; event_type: string; severity: string; message: string } | null;
  progress: { stages_total: number | null; stages_completed: number | null; percent: number | null; label: string };
  triggered_by: "operator" | "cron";
}

export interface DataPipelineStatus {
  status: "GREEN" | "WARNING" | "RED" | "RUNNING" | "UNKNOWN";
  last_refreshed_at: string;
  latest_run_id: string | null;
  latest_data_ops_event: { recorded_at: string | null; event_type: string; status: "OK" | "MISSING" | "STALE" };
  summary: {
    passed: number;
    warnings: number;
    failed: number;
    confidence: string;
    tickers_tracked: number;
    daily_bars_60d: number;
    forensics_open: number;
    cycle_latency: string;
  };
  checks: DataPipelineCheck[];
  self_heal_log: DataPipelineSelfHealEntry[];
  active_job: ActiveJob | null;
}

export interface JobDescriptor {
  job_id: string;
  run_id: string;
  action: string;
  stage: string | null;
  status: string;
  queued_at: string;
}

export interface JobStatus {
  job_id: string;
  run_id: string;
  status: "QUEUED" | "RUNNING" | "SUCCESS" | "FAILED" | "ABORTED";
  started_at: string;
  updated_at: string;
  elapsed_seconds: number;
  events: Array<{ time: string; event_type: string; severity: string; message: string }>;
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
  dataPipeline: () => fetchJSON<DataPipelineStatus>("/api/operations/data-pipeline/status"),
  // Action endpoints forward through Next.js routes that verify the
  // NextAuth session + inject the CONSOLE_OPS_TOKEN bearer header
  // server-side (the token NEVER reaches the browser). The Next.js
  // route returns the console-api response verbatim.
  runDataUpdate: () => postJSON<JobDescriptor>("/api/operations/data-pipeline/run-update"),
  runDataValidation: () => postJSON<JobDescriptor>("/api/operations/data-pipeline/run-validation"),
  runDataFeed: (stage: string, opts?: { tickers?: string[]; action?: string; check_name?: string }) =>
    postJSON<JobDescriptor>(
      `/api/operations/data-pipeline/run-feed/${encodeURIComponent(stage)}`,
      opts as Record<string, unknown> | undefined,
    ),
  runFallback: (stage: string, opts?: { tickers?: string[]; check_name?: string }) =>
    postJSON<JobDescriptor>(
      `/api/operations/data-pipeline/run-fallback/${encodeURIComponent(stage)}`,
      opts as Record<string, unknown> | undefined,
    ),
  jobStatus: (jobId: string) => fetchJSON<JobStatus>(`/api/operations/data-pipeline/jobs/${encodeURIComponent(jobId)}`, { useConsoleApi: false }),
  abortJob: (jobId: string) => postJSON<{ job_id: string; status: string }>(`/api/operations/data-pipeline/abort/${encodeURIComponent(jobId)}`),
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
