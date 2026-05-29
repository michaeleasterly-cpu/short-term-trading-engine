"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ViewHeader, Panel, Kpi, Pill } from "./Primitives";
import {
  api, useApi,
  type ChartCheckStatus,
  type DataPipelineStatus,
  type DataPipelineCheck,
  type JobStatus,
} from "@/lib/api-client";

/**
 * Operations → Data Pipeline.
 *
 * Live-data + operator-trigger console. Spec:
 * ``docs/specs/2026-05-29-data-pipeline-console.md`` (built same PR
 * 2026-05-29). REQ-001..011 of the
 * ``build_real_data_pipeline_operations_console`` task spec.
 *
 * Static rendering disabled — every page open re-fetches from
 * console-api (`cache: 'no-store'` + Cache-Control headers on the
 * backend). The summary block is the AUTHORITATIVE backend rollup;
 * "previous run" snapshot is NEVER conflated with "current run"
 * (REQ-010 no-false-green: while a run is active, the badge says
 * RUNNING and the displayed checks are explicitly labeled as the
 * prior-cycle result).
 */
export function DataPipeline() {
  const [refreshTick, setRefreshTick] = useState(0);
  const { data, loading, error } = useApi(
    () => api.dataPipeline(),
    [refreshTick],
  );
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<{
    tone: "info" | "ok" | "warn" | "err"; text: string;
  } | null>(null);
  const [actionInFlight, setActionInFlight] = useState(false);

  // If status reports an active_job, lock onto its job_id. If the
  // operator just clicked a button we already set activeJobId locally.
  useEffect(() => {
    if (data?.active_job?.job_id && !activeJobId) {
      setActiveJobId(data.active_job.job_id);
    }
  }, [data?.active_job?.job_id, activeJobId]);

  const refresh = useCallback(() => {
    setRefreshTick(t => t + 1);
  }, []);

  // Job polling — drives the active-run badge + progress timeline.
  const job = useJobPolling(activeJobId, {
    onTerminal: () => {
      // Once a run terminates, clear the lock + reload the status so
      // the page shows the freshly proven state.
      setActiveJobId(null);
      refresh();
    },
  });

  const lane = data?.status ?? "UNKNOWN";
  const hasActiveRun = lane === "RUNNING" || activeJobId !== null;
  const buttonsDisabled = actionInFlight || hasActiveRun;

  const runAction = useCallback(async (
    label: string, fn: () => Promise<{ job_id: string }>,
  ) => {
    setActionInFlight(true);
    setActionMessage({ tone: "info", text: `${label}…` });
    try {
      const job = await fn();
      setActiveJobId(job.job_id);
      setActionMessage({
        tone: "ok",
        text: `${label} queued — job_id=${job.job_id.slice(0, 8)}…`,
      });
      refresh();
    } catch (e) {
      const err = e as Error & { status?: number; payload?: Record<string, unknown> };
      if (err.status === 409) {
        setActionMessage({
          tone: "warn",
          text: `${label} blocked — a run is already active. Wait for it to finish or abort it.`,
        });
      } else if (err.status === 503) {
        setActionMessage({
          tone: "err",
          text: `${label} blocked — operator token not configured. See runbook.`,
        });
      } else if (err.status === 401) {
        setActionMessage({
          tone: "err",
          text: `${label} blocked — not authenticated.`,
        });
      } else {
        setActionMessage({
          tone: "err", text: `${label} failed: ${err.message}`,
        });
      }
    } finally {
      setActionInFlight(false);
    }
  }, [refresh]);

  return (
    <div>
      <ViewHeader
        eyebrow="OPERATIONS / DATA PIPELINE"
        title="Data Pipeline"
        meta={buildMeta(data)}
        actions={
          <>
            <button
              className="hairline mono text-[11px] px-3 py-1.5"
              style={{ background: "var(--accent)", color: "var(--bg)", opacity: buttonsDisabled ? 0.45 : 1 }}
              disabled={buttonsDisabled}
              onClick={() => runAction("run data update", () => api.runDataUpdate())}
              title={hasActiveRun ? "a run is already active" : "trigger the canonical 15-stage data-ops pipeline"}
            >Run data update</button>
            <button
              className="hairline mono text-[11px] px-3 py-1.5"
              style={{ color: "var(--ink-2)", opacity: buttonsDisabled ? 0.45 : 1 }}
              disabled={buttonsDisabled}
              onClick={() => runAction("run validation", () => api.runDataValidation())}
              title={hasActiveRun ? "a run is already active" : "trigger data_validation only"}
            >Run validation</button>
            <button
              className="hairline mono text-[11px] px-3 py-1.5"
              style={{ color: "var(--ink-2)" }}
              onClick={refresh}
              title="re-fetch live status"
            >Refresh</button>
          </>
        }
      />

      {actionMessage && (
        <div
          className="mx-5 mt-2 px-3 py-2 text-[11.5px] hairline"
          style={{
            background:
              actionMessage.tone === "ok" ? "rgba(16,185,129,0.08)"
              : actionMessage.tone === "warn" ? "rgba(245,158,11,0.08)"
              : actionMessage.tone === "err" ? "rgba(239,68,68,0.08)"
              : "rgba(99,102,241,0.06)",
            color:
              actionMessage.tone === "ok" ? "var(--pos)"
              : actionMessage.tone === "warn" ? "var(--warn)"
              : actionMessage.tone === "err" ? "var(--neg)"
              : "var(--ink)",
          }}
        >
          {actionMessage.text}
          {actionMessage.tone !== "info" && (
            <button
              className="ml-3 mono text-[10px]"
              style={{ color: "var(--ink-3)" }}
              onClick={() => setActionMessage(null)}
            >dismiss</button>
          )}
        </div>
      )}

      {loading && (
        <div className="px-5 py-4 text-[11px]" style={{ color: "var(--ink-3)" }}>
          loading…
        </div>
      )}
      {error && (
        <div className="px-5 py-4 text-[11px]" style={{ color: "var(--neg)" }}>
          status fetch failed: {error}
        </div>
      )}

      {data && (
        <>
          {hasActiveRun && (
            <RunningBanner
              activeJob={data.active_job}
              jobStatus={job}
              onAbort={async () => {
                if (!activeJobId) return;
                try {
                  await api.abortJob(activeJobId);
                  setActionMessage({
                    tone: "warn",
                    text: "abort requested — the lane will SIGTERM the subprocess on its next poll tick",
                  });
                  refresh();
                } catch (e) {
                  setActionMessage({
                    tone: "err",
                    text: `abort failed: ${(e as Error).message}`,
                  });
                }
              }}
            />
          )}

          <div className="grid gap-2 px-5 py-4" style={{ gridTemplateColumns: "repeat(8, minmax(120px, 1fr))" }}>
            <Kpi
              label="Lane status"
              value={data.status}
              tone={laneTone(data.status)}
              sub={hasActiveRun ? "while running" : undefined}
            />
            <Kpi
              label="Passed"
              value={String(data.summary.passed)}
              tone={data.summary.passed > 0 ? "pos" : "neutral"}
            />
            <Kpi
              label="Warnings"
              value={String(data.summary.warnings)}
              tone={data.summary.warnings > 0 ? "warn" : "neutral"}
            />
            <Kpi
              label="Failed"
              value={String(data.summary.failed)}
              tone={data.summary.failed > 0 ? "neg" : "neutral"}
            />
            <Kpi
              label="DATA_OPS event"
              value={docKpiValue(data.latest_data_ops_event)}
              sub={docKpiSub(data.latest_data_ops_event)}
              tone={docKpiTone(data.latest_data_ops_event)}
            />
            <Kpi label="Confidence" value={data.summary.confidence} tone={confidenceTone(data.summary.confidence)} />
            <Kpi label="Tickers tracked" value={data.summary.tickers_tracked.toLocaleString()} />
            <Kpi label="Daily bars (60d)" value={data.summary.daily_bars_60d.toLocaleString()} />
          </div>

          <div className="px-5 pb-4">
            <Panel title={`Validation suite — ${data.checks.length} checks${hasActiveRun ? " (previous completed run)" : ""}`}>
              <ValidationTable
                checks={data.checks}
                disabled={buttonsDisabled}
                onRepair={(req) => {
                  if (req.action === "run_fallback_source") {
                    runAction(
                      req.label,
                      () => api.runFallback(req.stage, {
                        tickers: req.tickers,
                        check_name: req.checkName,
                      }),
                    );
                  } else {
                    runAction(
                      req.label,
                      () => api.runDataFeed(req.stage, {
                        tickers: req.tickers,
                        action: req.action,
                        check_name: req.checkName,
                      }),
                    );
                  }
                }}
              />
            </Panel>
          </div>

          <div className="px-5 pb-5">
            <Panel title={`Self-heal log (${data.self_heal_log.length} events, last 24 h)`}>
              <SelfHealTable rows={data.self_heal_log} />
            </Panel>
          </div>

          <LastRefreshedFooter ts={data.last_refreshed_at} />
        </>
      )}
    </div>
  );
}

// ──────────── meta builder ────────────

function buildMeta(data: DataPipelineStatus | null): Array<[string, string]> {
  if (!data) return [["status", "loading…"]];
  return [
    ["lane", data.status],
    ["cycle latency", data.summary.cycle_latency],
    ["self-heal", `${data.self_heal_log.length} events 24h`],
    ["forensics open", String(data.summary.forensics_open)],
  ];
}

// ──────────── running banner ────────────

function RunningBanner({
  activeJob,
  jobStatus,
  onAbort,
}: {
  activeJob: DataPipelineStatus["active_job"];
  jobStatus: JobStatus | null;
  onAbort: () => void;
}) {
  const j = activeJob;
  const live = jobStatus;
  const startedAt = j?.started_at ?? live?.started_at;
  const elapsed = j?.elapsed_seconds ?? live?.elapsed_seconds ?? 0;
  const triggered = j?.triggered_by ?? "operator";
  const action = j?.type ?? "data-ops run";
  const stage = j?.current_stage;
  const progress = j?.progress;
  const latest = j?.latest_log ?? live?.events?.[live.events.length - 1] ?? null;
  return (
    <div
      className="mx-5 mt-3 px-4 py-3 hairline"
      style={{
        background: "rgba(99,102,241,0.08)",
        borderLeftWidth: "3px",
        borderLeftStyle: "solid",
        borderLeftColor: "var(--accent)",
      }}
    >
      <div className="flex items-baseline justify-between mb-2">
        <div className="mono text-[12px]" style={{ color: "var(--accent)" }}>
          ▶ RUNNING — {triggered}-triggered {action}
        </div>
        <div className="mono text-[10px]" style={{ color: "var(--ink-3)" }}>
          started {fmtTime(startedAt)} • elapsed {fmtElapsed(elapsed)}
        </div>
      </div>
      {progress && (
        <div className="mb-2">
          <div className="flex justify-between text-[10px] mb-1 mono" style={{ color: "var(--ink-3)" }}>
            <span>{progress.label}</span>
            {progress.percent !== null && <span>{progress.percent}%</span>}
          </div>
          <div className="hairline" style={{ background: "var(--ink-3)/10", height: "4px" }}>
            <div
              style={{
                width: `${progress.percent ?? 0}%`,
                background: "var(--accent)",
                height: "100%",
                transition: "width 0.5s ease",
              }}
            />
          </div>
        </div>
      )}
      {stage && (
        <div className="text-[11px] mb-1" style={{ color: "var(--ink-2)" }}>
          current stage: <span className="mono" style={{ color: "var(--ink)" }}>{stage}</span>
        </div>
      )}
      {latest && (
        <div className="text-[11px] mono" style={{ color: "var(--ink-3)" }}>
          [{latest.event_type}] {latest.message}
        </div>
      )}
      <div className="mt-2 flex gap-2">
        <button
          className="hairline mono text-[10px] px-2 py-1"
          style={{ color: "var(--neg)" }}
          onClick={onAbort}
        >Abort</button>
        <div className="mono text-[10px]" style={{ color: "var(--ink-3)" }}>
          job_id: {j?.job_id.slice(0, 16) ?? "—"}
        </div>
      </div>
    </div>
  );
}

// ──────────── validation table ────────────

function ValidationTable({
  checks, disabled, onRepair,
}: {
  checks: DataPipelineCheck[];
  disabled: boolean;
  onRepair: (req: RepairRequest) => void;
}) {
  return (
    <table className="w-full text-[11.5px]">
      <thead><tr style={{ color: "var(--ink-3)" }}>
        {["Check", "Status", "Class", "Age", "Notes", "Actions"].map(h => (
          <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
        ))}
      </tr></thead>
      <tbody>
        {checks.map((c) => (
          <tr key={c.name}>
            <td className="mono px-3 py-1.5" style={{ color: "var(--ink)" }}>{c.name}</td>
            <td className="px-3 py-1.5"><CheckStatusPill status={c.status} /></td>
            <td className="px-3 py-1.5"><RemediationClassPill cls={c.remediation_class} /></td>
            <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{c.age ?? "—"}</td>
            <td className="px-3 py-1.5" style={{ color: "var(--ink-3)" }}>
              <NotesCell check={c} />
            </td>
            <td className="px-3 py-1.5">
              <CheckActionMenu check={c} disabled={disabled} onRepair={onRepair} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RemediationClassPill({ cls }: { cls: DataPipelineCheck["remediation_class"] }) {
  const labelMap: Record<DataPipelineCheck["remediation_class"], { label: string; tone: "pos" | "neg" | "warn" | "accent" | "neutral" }> = {
    scoped_auto_heal: { label: "scoped", tone: "accent" },
    full_stage_required: { label: "full stage", tone: "neutral" },
    blocked_vendor: { label: "vendor blocked", tone: "neg" },
    operator_required: { label: "operator", tone: "warn" },
    unhealable: { label: "unhealable", tone: "neutral" },
    bootstrap: { label: "bootstrap", tone: "warn" },
    not_implemented: { label: "not impl", tone: "neutral" },
  };
  const { label, tone } = labelMap[cls];
  return <Pill tone={tone}>{label}</Pill>;
}

function NotesCell({ check }: { check: DataPipelineCheck }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = (check.notes_details?.length ?? 0) > 0;
  if (!check.notes && !hasDetails) {
    return <span style={{ color: "var(--ink-3)" }}>—</span>;
  }
  return (
    <div>
      <div>{check.notes}</div>
      {hasDetails && (
        <button
          className="mono text-[10px] mt-1"
          style={{ color: "var(--accent)" }}
          onClick={() => setExpanded(e => !e)}
        >
          {expanded ? "hide details" : `${check.notes_details!.length} item${check.notes_details!.length === 1 ? "" : "s"} →`}
        </button>
      )}
      {expanded && hasDetails && (
        <ul
          className="mono text-[10px] mt-1 pl-3 max-h-56 overflow-auto"
          style={{ color: "var(--ink-3)" }}
        >
          {check.notes_details!.slice(0, 50).map((d, i) => {
            const obj = d as Record<string, string>;
            return (
              <li key={i} className="my-0.5">
                {obj.ticker ?? "<row>"}: {obj.reason ?? obj.observed ?? JSON.stringify(d)}
              </li>
            );
          })}
          {(check.notes_details!.length > 50) && (
            <li style={{ color: "var(--ink-3)" }}>
              … {check.notes_details!.length - 50} more
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

function CheckStatusPill({ status }: { status: ChartCheckStatus }) {
  const tone =
    status === "PASS" ? "pos"
    : status === "WARN" ? "warn"
    : status === "FAIL" ? "neg"
    : status === "RUNNING" ? "accent"
    : status === "BLOCKED_VENDOR_ACCESS" ? "neg"
    : "neutral";
  const label = status === "BLOCKED_VENDOR_ACCESS" ? "BLOCKED" : status;
  return <Pill tone={tone}>{label}</Pill>;
}

function CheckActionMenu({
  check, disabled, onRepair,
}: {
  check: DataPipelineCheck;
  disabled: boolean;
  onRepair: (req: RepairRequest) => void;
}) {
  // Render exactly the buttons that match the remediation class.
  // The data_pipeline backend tells us what's appropriate; the UI
  // never invents a button that the backend hasn't classified as OK.
  const cls = check.remediation_class;
  if (cls === "scoped_auto_heal" && check.target_stage) {
    const symN = check.affected_symbols.length;
    if (symN > 0 && symN <= 100) {
      // Surgical fix path.
      return (
        <div className="flex flex-col gap-1">
          <button
            className="hairline mono text-[10px] px-2 py-0.5"
            style={{ color: "var(--accent)", opacity: disabled ? 0.45 : 1 }}
            disabled={disabled}
            onClick={() => onRepair({
              stage: check.target_stage as string,
              tickers: check.affected_symbols,
              action: "repair_failed_scope",
              label: `Repair ${symN} ticker${symN === 1 ? "" : "s"}`,
              checkName: check.name,
            })}
            title={`Dispatches ${check.target_stage} --param tickers=${check.affected_symbols.slice(0, 5).join(",")}${symN > 5 ? "..." : ""}`}
          >Repair {symN} ticker{symN === 1 ? "" : "s"}</button>
          {check.fallback_stage && (
            <button
              className="hairline mono text-[10px] px-2 py-0.5"
              style={{ color: "var(--ink-2)", opacity: disabled ? 0.45 : 1 }}
              disabled={disabled}
              onClick={() => onRepair({
                stage: check.fallback_stage as string,
                tickers: check.affected_symbols,
                action: "run_fallback_source",
                label: `${check.fallback_stage}`,
                checkName: check.name,
              })}
              title={`Falls back to ${check.fallback_stage} for the same tickers`}
            >Fallback: {check.fallback_stage}</button>
          )}
        </div>
      );
    }
    // No symbols emitted OR too many → fall through to full-stage.
    return (
      <button
        className="hairline mono text-[10px] px-2 py-0.5"
        style={{ color: "var(--ink-2)", opacity: disabled ? 0.45 : 1 }}
        disabled={disabled}
        onClick={() => onRepair({
          stage: check.target_stage as string,
          action: "run_scoped_feed",
          label: `Run ${check.target_stage}`,
          checkName: check.name,
        })}
        title={symN === 0
          ? "No specific tickers identified — runs full stage"
          : `${symN} tickers — too many for scoped repair, runs full stage`
        }
      >Run {check.target_stage}</button>
    );
  }
  if (cls === "full_stage_required" && check.target_stage) {
    const tooltip = check.operator_note
      || "Full-stage sweep — cannot be scoped per the validator's contract";
    return (
      <button
        className="hairline mono text-[10px] px-2 py-0.5"
        style={{ color: "var(--accent)", opacity: disabled ? 0.45 : 1 }}
        disabled={disabled}
        onClick={() => onRepair({
          stage: check.target_stage as string,
          action: "run_scoped_feed",
          label: `Run ${check.target_stage}`,
          checkName: check.name,
        })}
        title={tooltip}
      >Run {check.target_stage}</button>
    );
  }
  if (cls === "bootstrap" && check.target_stage) {
    return (
      <button
        className="hairline mono text-[10px] px-2 py-0.5"
        style={{ color: "var(--warn)", opacity: disabled ? 0.45 : 1 }}
        disabled={disabled}
        onClick={() => onRepair({
          stage: check.target_stage as string,
          action: "bootstrap_baseline",
          label: `Write baseline (${check.target_stage})`,
          checkName: check.name,
        })}
        title={check.operator_note || "One-time bootstrap write"}
      >Write baseline</button>
    );
  }
  if (cls === "blocked_vendor") {
    return (
      <span
        className="mono text-[10px]"
        style={{ color: "var(--neg)" }}
        title={check.blocker_reason || "Vendor access blocked"}
      >
        BLOCKED: {check.vendor || "vendor"}
      </span>
    );
  }
  if (cls === "operator_required") {
    return (
      <span
        className="mono text-[10px]"
        style={{ color: "var(--warn)" }}
        title={check.operator_procedure || "Operator manual procedure required"}
      >
        operator action
      </span>
    );
  }
  if (cls === "not_implemented") {
    return (
      <span
        className="mono text-[10px]"
        style={{ color: "var(--ink-3)" }}
        title={check.unhealable_reason || "Not implemented"}
      >
        not implemented
      </span>
    );
  }
  // unhealable / default
  return (
    <span
      className="mono text-[10px]"
      style={{ color: "var(--ink-3)" }}
      title={check.unhealable_reason || "Definitionally unhealable"}
    >—</span>
  );
}

interface RepairRequest {
  stage: string;
  tickers?: string[];
  action: string;
  label: string;
  checkName: string;
}

// ──────────── self-heal table ────────────

function SelfHealTable({ rows }: { rows: DataPipelineStatus["self_heal_log"] }) {
  if (rows.length === 0) {
    return (
      <div className="px-3 py-3 text-[11.5px]" style={{ color: "var(--ink-3)" }}>
        no self-heal events in the last 24 h
      </div>
    );
  }
  return (
    <table className="w-full text-[11.5px]">
      <thead><tr style={{ color: "var(--ink-3)" }}>
        {["Time", "Stage", "Result", "Duration", "Event", "Notes"].map(h => (
          <th key={h} className="eyebrow hairline-b px-3 py-2 text-left">{h}</th>
        ))}
      </tr></thead>
      <tbody>
        {rows.map((s, i) => (
          <tr key={`${s.time}-${i}`}>
            <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>
              {new Date(s.time).toISOString().slice(11, 19)} UTC
            </td>
            <td className="mono px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{s.stage}</td>
            <td className="px-3 py-1.5">
              <Pill tone={
                s.result === "HEALED" ? "pos"
                : s.result === "FAILED" ? "neg"
                : s.result === "ESCALATED" ? "warn"
                : "neutral"
              }>{s.result}</Pill>
            </td>
            <td className="mono px-3 py-1.5" style={{ color: "var(--ink-3)" }}>{s.duration ?? "—"}</td>
            <td className="mono px-3 py-1.5 text-[10px]" style={{ color: "var(--ink-3)" }}>{s.event_type}</td>
            <td className="px-3 py-1.5" style={{ color: "var(--ink-2)" }}>{s.notes}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LastRefreshedFooter({ ts }: { ts: string }) {
  return (
    <div className="px-5 pb-4 text-[10px] mono" style={{ color: "var(--ink-3)" }}>
      last refreshed: {new Date(ts).toISOString().slice(0, 19)} UTC
      <span style={{ marginLeft: "8px" }}>(no-store cached — every page open re-fetches)</span>
    </div>
  );
}

// ──────────── job-polling hook ────────────

function useJobPolling(
  jobId: string | null,
  opts: { onTerminal?: () => void },
): JobStatus | null {
  const [status, setStatus] = useState<JobStatus | null>(null);
  const onTerminal = opts.onTerminal;
  useEffect(() => {
    if (!jobId) {
      setStatus(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await api.jobStatus(jobId);
        if (cancelled) return;
        setStatus(s);
        const terminal = ["SUCCESS", "FAILED", "ABORTED"].includes(s.status);
        if (terminal && onTerminal) {
          onTerminal();
          return;  // stop polling
        }
        timer = setTimeout(tick, 4000);
      } catch {
        if (cancelled) return;
        timer = setTimeout(tick, 8000);
      }
    };
    let timer = setTimeout(tick, 0);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, onTerminal]);
  return status;
}

// ──────────── helpers ────────────

function laneTone(status: DataPipelineStatus["status"]): "pos" | "neg" | "warn" | "neutral" {
  if (status === "GREEN") return "pos";
  if (status === "RED") return "neg";
  if (status === "RUNNING" || status === "WARNING") return "warn";
  return "neutral";
}

function docKpiValue(e: DataPipelineStatus["latest_data_ops_event"]): string {
  if (e.status === "MISSING") return "MISSING";
  if (e.status === "STALE") return "STALE";
  return "OK";
}

function docKpiSub(e: DataPipelineStatus["latest_data_ops_event"]): string {
  if (!e.recorded_at) return "never";
  return new Date(e.recorded_at).toISOString().slice(11, 16) + " UTC";
}

function docKpiTone(e: DataPipelineStatus["latest_data_ops_event"]): "pos" | "neg" | "warn" | "neutral" {
  if (e.status === "OK") return "pos";
  if (e.status === "STALE") return "warn";
  return "neg";
}

function confidenceTone(c: string): "pos" | "warn" | "neg" | "neutral" {
  if (c === "—" || c === "") return "neutral";
  const n = Number.parseInt(c, 10);
  if (Number.isNaN(n)) return "neutral";
  if (n >= 100) return "pos";
  if (n >= 50) return "warn";
  return "neg";
}

function fmtTime(ts: string | undefined): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toISOString().slice(11, 19) + " UTC";
  } catch {
    return ts;
  }
}

function fmtElapsed(secs: number): string {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m${(secs % 60).toString().padStart(2, "0")}s`;
  return `${Math.floor(secs / 3600)}h${Math.floor((secs % 3600) / 60).toString().padStart(2, "0")}m`;
}

// Re-exported so tests can import the inner table/banner without
// pulling in the React app router.
export const _DataPipelineInternals = {
  RunningBanner,
  ValidationTable,
  SelfHealTable,
  CheckStatusPill,
  CheckActionMenu,
};
