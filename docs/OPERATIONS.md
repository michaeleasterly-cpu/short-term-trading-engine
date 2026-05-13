# Operations Runbook

**Purpose:** Daily health-check procedure for the Short-Term Trading Engine. Any Claude session (or human operator) should be able to read this file and walk through the checklist in §9 in under 5 minutes.

**Railway status (as of 2026-05-12):** Railway is **paused** — auto-deploys are off, cron schedules are unset on every live service, and the `trade-monitor` block in `railway.json` was never deployed. The active execution environment is the operator's local Mac; §1 documents the local-execution model and the residual Railway service state. Re-enabling Railway is deferred until an engine clears the credibility gate (≥ 60/100).

**Engine submission status (as of 2026-05-12):** Trade monitor is live. Engines submit only the **Tier 1** bracket via `AlpacaPaperBrokerAdapter.submit_tier1_only` and persist `decision_data` to `platform.open_orders`. The `tpcore/trade_monitor.py` service consumes Alpaca's `trade_updates` stream and submits the Tier 2 follow-on reactively after the Tier 1 entry fills. AAR + risk-state updates fire on Tier 2 close. The legacy `TPCORE_SCAN_ONLY` env-var guard has been removed from the order managers; deploy of the trade-monitor Railway service is required before engines can fire (see §1 service table). Smoke test (`scripts/smoke_test.py`) still bypasses the order managers and exercises the raw broker path for sanity checks.

**Cross-references:**
- `docs/MASTER_PLAN.md` — what each engine *is* and what success looks like.
- `docs/EDGE_VALIDATION_PLAN.md` — phase tracker for universe + cost-model + replay work. Phase 1 complete 2026-05-12.
- `docs/STYLE_GUIDE.md` — code conventions enforced when fixes are required.
- `docs/glossary.md` — canonical terms (engine names, score names, services).
- `railway.json` — source of truth for the cron schedule and service list.

**Time discipline:** All timestamps are UTC. Cron schedules in `railway.json` are UTC. Don't translate to local time when reading logs — compare in UTC.

**Push discipline (avoid mid-cron deploys):** A push to `main` that touches files matching a service's `watchPatterns` rebuilds that service. The five Railway services have `watchPatterns = ["**/*.py", "**/*.yaml", "pyproject.toml", "railway.json", ".python-version"]`, so doc-only edits (`docs/`, `*.md`, `backtests/*.json`) do *not* trigger rebuilds. For changes that *do* match the patterns: avoid pushing during the cron firing windows so a redeploy can't kill an in-flight job. Cron windows: weekdays 22:00–22:10 UTC (engines), Sun 04:00–04:30 UTC (corp-actions), Sun 06:00–06:30 UTC (validation). For programmatic Railway operations like `railway variable set`, always pass `--skip-deploys`.

**Deploy discipline (git is the source of truth):** Every Railway deployment must correspond to a commit on `main`. Never use `railway redeploy --from-source` or `railway up` to push a new image — they create live deployments with no audit trail and break the "what's on `main` = what's running" invariant. If a push doesn't visibly trigger a rebuild within ~2 minutes, push an empty commit instead: `git commit --allow-empty -m "trigger: <reason>" && git push`.

**Build layout:** Railway builds via Railpack. The runtime Python version is pinned to **3.11.15** by the repo-root `.python-version` file (Railpack reads it through mise). Project deps install into a venv at **`/app/.deps`** — NOT `/app/.venv`. `.gitignore` line 8 (`.venv/`) causes Railpack to silently strip a venv built at the conventional path when copying `/app` from build → runtime, so we use a non-gitignored path. The buildCommand in `railway.json` is:

```
python -m venv /app/.deps && /app/.deps/bin/pip install --upgrade pip && /app/.deps/bin/pip install -e .
```

Each service's `startCommand` invokes `/app/.deps/bin/python <entrypoint>.py`. Do not try to "let Railpack auto-install" by removing the buildCommand: Railpack's Python auto-install only fires when it sees `requirements.txt`, `poetry.lock`, `pdm.lock`, `uv.lock`, or `Pipfile`. Our bare `pyproject.toml` + `setuptools.build_meta` matches none of those — auto-install produces zero pip lines and runtime crashes with `ModuleNotFoundError`. The explicit buildCommand is mandatory.

**Egress (IPv6 for Supabase):** `DATABASE_URL` on Railway is the Supabase *direct* hostname (`db.<project>.supabase.co`) which resolves IPv6-only. Each service must have `ipv6EgressEnabled = true` or asyncpg fails at startup with `OSError: [Errno 101] Network is unreachable`. There is no `railway.json` field for this; toggle per service via the GraphQL `serviceInstanceUpdate` mutation (see project memory `feedback_railway_python_deploy.md`). Required when provisioning any new service that touches the DB.

If a service crashes with `ModuleNotFoundError`, check `railway logs --build` to confirm the `pip install` step ran (look for "Successfully installed …") and that the runtime is using `/app/.deps/bin/python`, not the bare `python` shim.

**Database access:** The local `.env` exposes two URLs (a known gotcha — see the project memory entry). Use `DATABASE_URL_IPV4` (Supabase pooler) for local CLI work; Railway uses the IPv6 direct URL via `DATABASE_URL`. Never copy one into the other.

```bash
# Bootstrap a Postgres connection for the queries below:
export DATABASE_URL=$(grep '^DATABASE_URL_IPV4=' .env | cut -d= -f2-)
```

---

## Daily Maintenance (via ops CLI)

The maintenance CLI `scripts/ops.py` is the single entry point for daily and weekly data work. It replaces the previous mix of ad-hoc shell invocations (`run_daily_bars_all_active.py`, `run_corporate_actions_all_active.py`, etc.) and Railway-dependent ingestion checks. Every stage delegates to existing `tpcore` handlers; the CLI itself owns timeouts, logging, and the summary report.

### Before trading

```bash
python scripts/ops.py --full
```

This will:

- Refresh trading-universe daily bars, corporate actions, and fundamentals.
- Run the Data Validation Suite.
- Run the universe simulation (`scripts/simulate_universe.py`) and report candidate counts per engine.
- Print a health report.

Each stage has a 120-second hard timeout. On timeout an ERROR row lands in `platform.application_log` and the pipeline moves on to the next stage — a single slow upstream never blocks the whole maintenance run.

### Weekly Maintenance

Sunday cron jobs (`fundamentals-refresh-scheduler` 03:00 UTC, `corporate-actions-scheduler` 04:00 UTC, `validation-scheduler` 06:00 UTC — see §1) already perform the weekly heavy lifts on Railway. To re-run them locally:

```bash
# Same command as the daily run; the underlying handlers are idempotent.
python scripts/ops.py --full
```

A future `--update-weekly` flag is reserved for any extended-universe / extended-fundamentals work that is heavier than the daily refresh. It has not been built yet — there are no extra manual steps to run weekly today; the Sunday Railway crons cover it. If a regression in the Sunday crons forces a manual catch-up, re-run `python scripts/ops.py --full` to backfill the same scope the cron would have ingested.

### Interpreting Results

- `python scripts/ops.py --check --pretty` displays the health report seen during daily operation (terminal-friendly).
- `python scripts/ops.py --check` returns JSON on stdout, suitable for scripting and grep-by-key.
- `python scripts/ops.py --update --dry-run` logs every stage to `platform.application_log` without performing any data writes — useful before letting a new credential or schema change touch the live tables.
- If the check reports `DEGRADED`, the offending sub-check has `ok: false` plus a `reason`/`error` field. Cross-reference the relevant section below — §1 (Railway), §2 (application_log), §3 (database), §5 (engine runtime), §6 (validation), §10 (troubleshooting).

The CLI emits a unique `run_id` (UUID) for every invocation. Every row written to `platform.application_log` during the run carries that `run_id`, so the full timeline of a single `--full` run can be reconstructed with:

```sql
SELECT recorded_at, event_type, severity, message, data
FROM platform.application_log
WHERE run_id = '<uuid-from-stdout>'
ORDER BY recorded_at;
```

---

## 1. Execution environment (local Mac; Railway paused)

The active execution environment is the operator's **local Mac**. Railway is paused as of 2026-05-12 — auto-deploys are off, cron schedules are unset on every live service, and the `trade-monitor` service defined in `railway.json` was never deployed. Nothing fires automatically: every engine run, every ingestion sweep, every smoke test starts from a local `python …` invocation.

### What's invoked, by which script

| Job | Local command | Notes |
| --- | --- | --- |
| Daily ops (refresh universe + corp actions + fundamentals + validation + universe sim) | `python scripts/ops.py --full` | See "Daily Maintenance (via ops CLI)" below. Each stage has a 120 s timeout. |
| Broker reachability smoke (single bracket order, cancelled immediately) | `python scripts/smoke_test.py` | Idempotent. Bypasses the engine order managers; just exercises the broker adapter. |
| End-to-end pipeline smoke (engine → broker → trade_monitor → AAR) | `python scripts/pipeline_smoke_test.py` | **The current next-gate check** — must be run during US market hours so the bracket entry fills. Requires `python -m tpcore.trade_monitor` running in a separate terminal. |
| Engine submission run (Sigma / Reversion / Vector) | `python sigma/scheduler.py` (etc.) | Each scheduler is one-shot. Trade monitor must be running. |
| Trade monitor (live order-lifecycle worker) | `python -m tpcore.trade_monitor` | Persistent loop subscribed to Alpaca `TradingStream`. Required for Sigma/Reversion Tier 2 logic. |
| Universe simulation (Sigma 187 / Reversion 4 / Vector 0 today) | `python scripts/simulate_universe.py` | Writes a `UNIVERSE_SIMULATION` event to `application_log`. |
| Tier (re-)assignment | `python scripts/assign_liquidity_tiers.py` | Aggregates `platform.spread_observations` into `platform.liquidity_tiers`. Source-agnostic via `--sources`. |
| FMP fundamentals backfill (hours-long) | `python scripts/backfill_fundamentals.py --all-active` | Optional one-shot; the daily ops CLI keeps fundamentals current. |
| Backtests (tier-aware costs) | `python sigma/backtest.py --start … --end …` (etc.) | Same shape for Reversion + Vector. Reads `liquidity_tiers` at startup. |

All scripts read `DATABASE_URL` from `.env` (use `DATABASE_URL_IPV4`, the Supabase pooler) and `ALPACA_KEY` / `ALPACA_SECRET` / `ALPACA_PAPER` for broker access.

### Railway services (paused — not active)

The four services in the project are still provisioned; none is currently scheduled or running:

| Service | Live config (verified 2026-05-12 via GraphQL) | Local equivalent |
| --- | --- | --- |
| `ingestion-engine` | persistent, `restartPolicyType=NEVER`, no auto-restart, idle | `python scripts/ops.py --full` |
| `sigma-scheduler` | `cronSchedule=null`, last status "Completed", will not refire | `python sigma/scheduler.py` |
| `reversion-scheduler` | `cronSchedule=null`, will not refire | `python reversion/scheduler.py` |
| `vector-scheduler` | `cronSchedule=null`, will not refire | `python vector/scheduler.py` |

The `trade-monitor` block in `railway.json` (added 2026-05-12 for the Phase 1.5 spec) is **defined but not deployed**. The Sunday-cron services that earlier revisions of this doc listed (`fundamentals-refresh-scheduler`, `corporate-actions-scheduler`, `validation-scheduler`) **no longer exist on Railway** — that work consolidates into the persistent `ingestion-engine` service via `platform.ingestion_jobs`.

When Railway is re-enabled, the canonical sync command is `python ops/apply_railway_service_config.py --all` (uses the GraphQL `serviceInstanceUpdate` mutation; commit-and-push alone leaves service-instance fields stale — see project memory `feedback_apply_after_railway_json.md`).

---

## 2. Run Health (`platform.application_log`)

Health checks are now performed by querying `platform.application_log` for the latest `STARTUP` and `SHUTDOWN` events per engine. Railway's dashboard shows deployment status and cron execution history. Healthchecks.io has been removed — for short-lived runs (cron schedulers, ops jobs) the database log is the authoritative health indicator.

Each scheduler / cron emits two rows per run via `tpcore.logging.DBLogHandler`:

- `STARTUP` — written immediately after pool open, with `commit_sha` in `data`.
- `SHUTDOWN` — written in the `finally` block, with `duration_ms` and `exit_code` in `data`.

A clean run is a `STARTUP` + `SHUTDOWN` pair for the same `run_id` with `exit_code = 0`. A missing `SHUTDOWN` for a recent `run_id` means the process crashed before the `finally` block; cross-reference with Railway logs.

### Latest run per engine

```sql
WITH latest AS (
    SELECT engine, run_id, MAX(recorded_at) AS last_event
    FROM platform.application_log
    WHERE recorded_at > now() - INTERVAL '7 days'
    GROUP BY engine, run_id
)
SELECT
    l.engine,
    l.run_id,
    MAX(CASE WHEN a.event_type = 'STARTUP'  THEN a.recorded_at END) AS started_at,
    MAX(CASE WHEN a.event_type = 'SHUTDOWN' THEN a.recorded_at END) AS finished_at,
    MAX(CASE WHEN a.event_type = 'SHUTDOWN' THEN (a.data->>'exit_code')::int END) AS exit_code,
    BOOL_OR(a.severity IN ('ERROR', 'CRITICAL')) AS had_error
FROM platform.application_log a
JOIN latest l USING (engine, run_id)
GROUP BY l.engine, l.run_id
ORDER BY l.engine, started_at DESC;
```

**For each engine row, confirm:**
- `started_at` is within the expected cadence (weekdays 22:00 UTC for engines, Sun 06:00 UTC for validation, Sun 04:00 UTC for corporate-actions).
- `finished_at` is non-NULL and within ~5 minutes of `started_at` for engines, ~30 minutes for ops jobs.
- `exit_code = 0`.
- `had_error = false`.

### Inspect a single run's timeline

```sql
SELECT recorded_at, event_type, severity, message, data
FROM platform.application_log
WHERE run_id = '<uuid>'
ORDER BY recorded_at;
```

### Ingestion engine — last 24 h heartbeat

The ingestion engine is persistent (not a cron), so it doesn't fit the `STARTUP`+`SHUTDOWN` pair model that scheduler runs use. Instead it emits `INGESTION_TICK` every loop (default every 60 s), plus `INGESTION_COMPLETE` / `INGESTION_FAILED` when due jobs fire. A healthy engine produces a steady drip of `TICK` rows. Absence of `TICK` for >5 minutes means the worker is wedged or dead.

```sql
SELECT *
FROM platform.application_log
WHERE engine = 'ingestion'
  AND recorded_at > now() - INTERVAL '24 hours'
ORDER BY recorded_at DESC;
```

For a tighter check ("is the engine alive *right now*?"):

```sql
SELECT max(recorded_at) AS last_event,
       now() - max(recorded_at) AS staleness
FROM platform.application_log
WHERE engine = 'ingestion'
  AND event_type = 'INGESTION_TICK';
-- staleness > 2 minutes → investigate; > 30 minutes → engine is down
```

The handler enforces a 7-day rolling retention on every insert (`DELETE FROM platform.application_log WHERE recorded_at < now() - INTERVAL '7 days'`); older runs are not queryable. For longer audit windows, archive externally before the retention sweep.

---

## 3. Supabase / Database Health

### Reachability

```bash
psql "$DATABASE_URL" -c 'SELECT 1'
# expected: ?column? = 1
```

If this fails, jump to §10 *Troubleshooting → Database is unreachable*.

### Row-count baselines

```sql
-- Save as a one-shot query (or run via psql -c):
SELECT 'prices_daily'           AS table, COUNT(*) FROM platform.prices_daily
UNION ALL SELECT 'fundamentals_quarterly',  COUNT(*) FROM platform.fundamentals_quarterly
UNION ALL SELECT 'corporate_actions',       COUNT(*) FROM platform.corporate_actions
UNION ALL SELECT 'aar_events',              COUNT(*) FROM platform.aar_events
UNION ALL SELECT 'risk_state',              COUNT(*) FROM platform.risk_state
UNION ALL SELECT 'data_quality_log',        COUNT(*) FROM platform.data_quality_log
UNION ALL SELECT 'tradier_options_chains',  COUNT(*) FROM platform.tradier_options_chains
UNION ALL SELECT 'catalyst_events',         COUNT(*) FROM platform.catalyst_events;
```

Expected ranges (cross-reference `MASTER_PLAN.md §6.4`, post-Phase-1 expansion):

| Table | Expected range | Sudden-drop alert |
| --- | --- | --- |
| `platform.prices_daily` | ≥ 20,000,000 (currently ~20.6M, 7,694 tickers) | drop > 1% → investigate |
| `platform.fundamentals_quarterly` | ≥ 178,000 (currently 178,518, 5,981 tickers) | any drop → investigate |
| `platform.corporate_actions` | ≥ 109,000 (currently 109,344, grows weekly) | drop → investigate |
| `platform.aar_events` | grows slowly with live trades; can be 0 | a *drop* is concerning, no growth is normal |
| `platform.risk_state` | one row per engine that has ever traded | drop → investigate |
| `platform.data_quality_log` | grows weekly | drop → investigate |
| `platform.tradier_options_chains` | 122,668 (frozen — should never change) | any change → flag |
| `platform.catalyst_events` | ≥ 683 (universe-expansion catalyst backfill is pending) | drop → investigate |

### Freshness check

```sql
SELECT MAX(date) AS latest_bar FROM platform.prices_daily;
-- Expected: within 5 trading days of today (UTC).
-- Older than that → daily ingestion is stale.

SELECT MAX(timestamp) AS latest_quality_log FROM platform.data_quality_log;
-- Expected: within last 7 days (validation suite runs weekly).
```

### Supabase Pro quotas

- Tier: **Pro ($25/mo)**, 8 GB disk, auto-scaling (+50% at 90% util, capped at +200 GB, max 4 modifications / 24 h). Upgraded 2026-05-11 after Phase 1 pushed `prices_daily` past the free-tier 500 MB read-only lock.
- Current DB size ≈ 2.7 GB. Disk re-locks only at 95% util with auto-scale quota exhausted — different recovery path than the free-tier 500 MB cliff.
- Check via Supabase dashboard → Project Settings → Usage.

---

## 4. Alpaca Broker Status

All execution goes through Alpaca paper-trading API. Verify credentials and account state.

```bash
# Single-shot Python check (uses .env credentials):
.venv/bin/python <<'PY'
import os
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv
load_dotenv()
client = TradingClient(
    api_key=os.environ["ALPACA_API_KEY"],
    secret_key=os.environ["ALPACA_API_SECRET"],
    paper=True,
)
acct = client.get_account()
print(f"status={acct.status}  equity={acct.equity}  buying_power={acct.buying_power}")
print(f"trading_blocked={acct.trading_blocked}  account_blocked={acct.account_blocked}")
positions = client.get_all_positions()
orders = client.get_orders()
print(f"open positions={len(positions)}  open orders={len(orders)}")
for p in positions:
    print(f"  POSITION {p.symbol} qty={p.qty} side={p.side} pnl={p.unrealized_pl}")
for o in orders:
    print(f"  ORDER {o.symbol} side={o.side} type={o.type} status={o.status}")
PY
```

**Confirm:**
- `acct.status == "ACTIVE"`. Any other state (e.g. `INACTIVE`, `ACCOUNT_CLOSED`) → escalate immediately.
- `trading_blocked == False` and `account_blocked == False`.
- Every open *position* has a corresponding active record in the engine's tracking (cross-check against `platform.aar_events` and the engine's order-manager state). Stuck positions from a failed exit are the most common silent failure.
- Open *orders*: usually zero outside of market hours. Bracket-order legs (take-profit + stop-loss) live alongside an open position and are expected; a standalone open order with no parent position is an orphan and needs cancellation.

If an orphaned order is found:

```python
client.cancel_order_by_id(order.id)
```

If a stuck position is found, do **not** liquidate it programmatically without operator approval — flag it in the runbook output and ask.

---

## 5. Engine Runtime Health

For each engine, walk through the most recent execution.

### Per-engine log inspection

```bash
# Most recent run for each engine — link the service first, then logs:
railway service sigma-scheduler && railway logs | tail -100
railway service reversion-scheduler && railway logs | tail -100
railway service vector-scheduler && railway logs | tail -100
```

**Look for:**
- A `<engine>.scheduler.run_done` or equivalent terminal line.
- No tracebacks, no `Error`, no `Exception` in the structlog output.
- The "candidates scanned" / "trades submitted" counts are non-zero on weekdays the cron fired (zero is OK if no setups passed the gates; *missing* counts is a problem).

### Risk Governor state

```sql
SELECT engine, kill_switch_active, kill_switch_reason,
       daily_pnl, weekly_pnl, engine_equity, open_positions,
       updated_at
FROM platform.risk_state
ORDER BY engine;
```

**Confirm for every row:**
- `kill_switch_active = false` (per-engine column). A `true` value means the platform froze new entries — see §10 *Troubleshooting → Risk Governor kill switch*.
- `daily_pnl` ≥ −5% × `engine_equity` (5% daily loss kill, per `tpcore.risk.RiskGovernor`).
- `weekly_pnl` ≥ −10% × `engine_equity` (10% weekly loss kill).
- `open_positions` ≤ the engine's `MAX_CONCURRENT_POSITIONS` (Sigma 4, Reversion 5, Vector 5).
- `updated_at` is recent (last weekday for active engines).

### Credibility score (graduation gate)

```sql
SELECT source, MAX(timestamp) AS latest, ROUND(MAX(confidence) * 100) AS score
FROM platform.data_quality_log
WHERE source LIKE 'backtest_credibility.%'
GROUP BY source
ORDER BY source;
```

**Expected:**
- Each engine's latest score persisted from the most recent backtest. Scores < 60 → engine cannot graduate live (see master plan §9 *Overfitting Diagnostics Status*). This is a known state — current Sigma 50, Reversion 45, Vector 45 — and not an alert by itself; only flag if a previously ≥ 60 score *drops* below 60.

---

## 5.5 Parameter-Search Pipeline

Production edge-discovery runs are driven by `scripts/search_parameters.py`. Random search + walk-forward + final held-back DSR verdict. Imports each engine's `load_*_window_context()` / `run_*_with_context()` programmatically — no subprocess. Per-window data load is shared across all candidates.

**Run a search on one engine:**

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -u scripts/search_parameters.py \
  --engine momentum \
  --trials 50 --per-window-trials 50 \
  --universe-tier-max 2 \
  --train-start 2018-01-01 --holdout-end 2023-12-31 \
  --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31 \
  --output backtests/momentum_search_results.csv
```

- `--engine`: `sigma | reversion | vector | momentum`.
- `--trials`: total parameter combinations pre-sampled (default 200; 50 is the practical sweet spot — DSR multiple-testing correction is friendlier at smaller N).
- `--per-window-trials`: how many of the pre-sampled combos to evaluate per walk-forward window. Setting `--per-window-trials = --trials` makes every candidate run in every window → clean OOS averaging.
- `--universe-tier-max`: pull tickers with tier ≤ N from `platform.liquidity_tiers`. Typical: 2 for T1+T2 (~1,281 names). Omit to use each engine's built-in default universe (~50 mega-caps).
- `--train-years` / `--holdout-years`: walk-forward window sizing (default 3/1).
- `--seed`: deterministic parameter sampling (default 0).

**Backgrounded runs (use these for the long N≥200 sweeps):**

```bash
nohup .venv/bin/python -u scripts/search_parameters.py \
    --engine momentum --trials 200 --per-window-trials 200 \
    --universe-tier-max 2 \
    --train-start 2018-01-01 --holdout-end 2023-12-31 \
    --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31 \
    --database-url "$DATABASE_URL_IPV4" \
  > backtests/momentum_search.log 2>&1 &
echo "pid=$!"

# Watch live:
tail -f backtests/momentum_search.log
```

The `-u` flag forces unbuffered stdout — otherwise nohup buffers all the progress lines and the log stays empty until the run completes.

**Convenience wrappers** in `scripts/`:

- `scripts/run_sigma_search.sh` — Sigma 200-trial sweep.
- `scripts/run_vector_search.sh` — Vector sweep on T1+T2 (currently expected to produce zero trades until catalyst_events backfill).
- `scripts/run_all_searches.sh` — sigma + reversion + vector back-to-back. **Note:** `set -e` is intentionally OFF; a FAILED verdict exits 1 but should not abort the multi-engine sweep.

**Interpreting the verdict:**

The orchestrator prints `VERDICT: SURVIVED` only when both `DSR ≥ --dsr-threshold` (default 0.95) and `credibility ≥ --credibility-threshold` (default 60). For monthly portfolio strategies (Momentum), the default DSR threshold is structurally unreachable with 2 years of held-back data — use held-back portfolio Sharpe + walk-forward consistency as the real signal.

**Outputs:**

- `backtests/<engine>_search_results.csv` — per-trial results (parameters, holdout metrics, full-window credibility).
- Stdout: header, per-trial timing/status, top-5 candidates by mean OOS score, final held-back metrics, verdict line.

---

## 6. Data Validation Suite

The suite runs weekly (Sunday 06:00 UTC). On a non-Sunday, the most recent run should be the prior Sunday.

```sql
-- Latest validation run summary:
SELECT source, timestamp, confidence, notes
FROM platform.data_quality_log
WHERE source LIKE 'validation.%'
ORDER BY timestamp DESC
LIMIT 10;
```

**Confirm:**
- Latest run timestamp is within the last 7 days (within the last 24h on Sunday).
- The three checks (delistings, constituents, splits) each have a row.
- All three should report `confidence = 1.0` and `notes = []`. The suite is fully green as of 2026-05-10 — five unresolvable historic delistings (HTZGQ, WLLBQ, LK, SBNYQ, SI) were removed from the YAML fixtures because no free-tier source carries bars for them. **Any non-empty `notes` field is a real new failure** — investigate immediately.

If `notes` lists unfamiliar tickers, run the suite locally to reproduce and read the full report:

```bash
.venv/bin/python -m tpcore.quality.validation
```

---

## 7. Corporate Actions Pipeline

Two scripts run on `corporate-actions-scheduler` (Sunday 04:00 UTC):
- `tpcore.data.ingest_corporate_actions` — pulls new splits + dividends from Alpaca.
- `tpcore.data.apply_splits` — adjusts historical bars in `platform.prices_daily` for any new splits.

```bash
# Inspect the most recent run:
railway service corporate-actions-scheduler && railway logs | tail -200
```

**Confirm:**
- Both scripts ran in sequence (ingest first, then apply).
- `ingest` reported a non-negative count of new actions; `apply` reported zero or N rows updated.
- No tracebacks.

Cross-check against the validation suite's split result (§6) — the two should agree. If `apply_splits` reports new splits but the next validation run still flags an unadjusted ratio, the pipeline broke.

---

## 8. Costs & Quotas

Fixed monthly cost: **$52** (FMP Starter $22 + Railway Hobby $5 + Supabase Pro $25). See `MASTER_PLAN.md §6.1`.

### Railway

- Dashboard → Project → Usage tab.
- Hobby plan ceiling is generous for cron-style workloads; the six services combined run < 7 min/week total (Mon–Fri schedulers ~30s each, Sunday cron jobs 1–2 min each).
- Watch for: surprise long-running deploy logs, container restarts in a tight loop (would chew through credits).

### FMP

- Dashboard: https://site.financialmodelingprep.com/developer/docs → Account → Usage.
- Starter plan: 300 calls/min, 100k calls/day. The platform's call profile (fundamentals backfill + nightly catalyst-event refresh) is well below this — but if a script regresses into a hot loop, it could blow through the daily cap.
- If usage is unexpectedly high, check `scripts/backfill_*.py` for unbounded retries.

### Supabase

- Dashboard → Project Settings → Usage.
- Pro tier ceilings: 8 GB DB (auto-scales +50% at 90% util, cap +200 GB, max 4 modifications / 24 h), 250 GB egress/month, 60 concurrent connections (pooler keeps count low). Free tier was 500 MB; upgraded 2026-05-11 once Phase 1 pushed `prices_daily` past that.
- Watch DB size — `platform.prices_daily` is the largest table (~2.7 GB at 7,694 tickers) and grows ~500 rows/weekday from the all-active sweep.

### Alpaca

- Paper trading is free and unmetered. No quota check needed beyond §4.

### Unexpected billing

- Scan the email account associated with each service for billing alerts. Anything unexpected (e.g. Alpaca real-time data subscription that wasn't approved, FMP Premium that auto-renewed) needs immediate investigation.

---

## 9. Daily Checklist

The operator works through this from the local Mac while Railway is paused. Substitute today's UTC date.

```
Daily Operations Checklist — YYYY-MM-DD UTC

Local ops run (single command does the data refresh + validation + sim)
[ ] python scripts/ops.py --full       → exit 0, no DEGRADED check, run_id captured
[ ] python scripts/ops.py --check      → JSON shows all sub-checks ok: true

Engine paper trading (only on demand; Railway paused so nothing fires automatically)
[ ] python -m tpcore.trade_monitor     → running in a separate terminal; STREAM_CONNECTED in app log
[ ] python sigma/scheduler.py          → run_done line printed, application_log SHUTDOWN exit_code=0
[ ] python reversion/scheduler.py      → same shape
[ ] python vector/scheduler.py         → same shape

Pipeline smoke (only during US regular session 13:30–20:00 UTC weekdays)
[ ] python scripts/pipeline_smoke_test.py  → exit 0 with PASSED, or 0 with SKIPPED when closed
[ ] python scripts/smoke_test.py       → exit 0 (broker reachability only; no engine path)

Run Health (platform.application_log — see §2)
[ ] sigma:        most recent local run has STARTUP + SHUTDOWN with exit_code=0
[ ] reversion:    same
[ ] vector:       same
[ ] trade_monitor: STREAM_CONNECTED present, no STREAM_RECONNECT loops

Database
[ ] Postgres reachable (SELECT 1 succeeds via DATABASE_URL_IPV4 pooler)
[ ] prices_daily ≥ 20M rows, latest bar within 5 trading days
[ ] No data loss in any key table (row counts within expected ranges per §3)
[ ] Supabase Pro within disk + connection limits (no read-only lock)

Alpaca
[ ] Account ACTIVE, trading_blocked=false, account_blocked=false
[ ] No stuck positions or orphaned orders

Risk
[ ] No kill switches active (all engines: kill_switch_active=false)
[ ] daily_pnl > -5% × engine_equity for every engine
[ ] weekly_pnl > -10% × engine_equity for every engine

Costs
[ ] Railway within Hobby plan (still paying $5/mo even while paused)
[ ] FMP within Starter limits (< 100K calls/day)
[ ] Supabase Pro within plan ceilings
[ ] No unexpected billing alerts

Railway (paused — verify state, do not re-enable without an architecture decision)
[ ] railway service list --json shows the four expected services, none restarted
[ ] No service has been deleted, renamed, or had cronSchedule re-attached without operator approval

Actions Taken
- [list any anomalies found and what was done; "none" if clean]
```

If every box is checked: report **"all green"** and stop. If anything fails: jump to §10.

---

## 10. Verification Scripts

Two standalone harnesses prove specific safety paths work end-to-end against the live database without waiting for an engine to actually fire a real trade. Use these any time a "the wiring exists but the table is empty" question comes up.

### `scripts/test_aar_pipeline.py`

Proves `tpcore.aar.writer.AARWriter` correctly persists `AfterActionReport` rows to `platform.aar_events`. Builds a synthetic AAR with `engine='synthetic_test'` and a UUID `trade_id`, calls `write_aar` twice, asserts (1) first call returns `True` (insert), (2) round-trip read matches the original JSON, (3) second call returns `False` (idempotent skip via the `UNIQUE (engine, trade_id)` constraint), (4) row count is exactly 1, (5) cleanup `DELETE` runs in a `finally` block so the production table never accumulates harness data.

```bash
DATABASE_URL=$(grep '^DATABASE_URL_IPV4=' .env | cut -d= -f2-) \
  .venv/bin/python scripts/test_aar_pipeline.py
```

A `PASS` line + `cleanup: deleted 1 synthetic row(s)` is the green signal.

### `scripts/test_kill_switch.py`

Proves the engine schedulers' startup kill-switch short-circuit fires. Flips `kill_switch_active=true` for the named engine in `platform.risk_state`, runs that engine's `scheduler.run_once()`, asserts `n_candidates == 0` and `n_submitted == 0`, then resets the switch in a `try/finally` so the live engine isn't left frozen on test failure.

```bash
DATABASE_URL=$(grep '^DATABASE_URL_IPV4=' .env | cut -d= -f2-) \
  .venv/bin/python scripts/test_kill_switch.py --engine sigma
```

Run with `--engine reversion` or `--engine vector` for the other two. Each engine's startup check must be exercised independently.

### `scripts/smoke_test.py`

One-shot end-to-end check of the paper-trading pipeline before any engine submits a live paper order. Reads the most recent `UNIVERSE_SIMULATION` row from `platform.application_log` (emitted by `scripts/simulate_universe.py`), picks the first Sigma candidate priced ≤ $100 with ≥ 20 bars, builds a `sigma.models.ExecutionDecision` (qty=2 split 1/1, TP=max(SMA20, entry×1.005), SL=entry×0.97), submits through `AlpacaPaperBrokerAdapter.submit_execution_decision()`, logs `SMOKE_ORDER_SUBMITTED`, immediately cancels both orders, logs `SMOKE_ORDER_CANCELLED`, and prints PASS/FAIL. Cancel failures are warnings, not test failures.

```bash
DATABASE_URL=$(grep '^DATABASE_URL_IPV4=' .env | cut -d= -f2-) \
ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \
  .venv/bin/python scripts/smoke_test.py
```

Idempotent — each run uses fresh UUID-suffixed `client_order_id`s. Validated 2026-05-12 on ACAD (broker IDs round-tripped, both audit events landed in `application_log`).

### `scripts/pipeline_smoke_test.py`

End-to-end live pipeline smoke for the trade-monitor era — exercises **engine submission → broker fill → monitor reaction → Tier 2 submission**. Submits one Tier 1 BUY bracket on SPY (1 share, wide TP/SL so the bracket's exit legs don't fire), inserts a Sigma-shaped row in `platform.open_orders` with `tier2_qty = 1`, then polls for the trade monitor to (a) flip the Tier 1 row to `status='filled'` once Alpaca acks the entry leg, and (b) insert a Tier 2 row after submitting the follow-on bracket. Cleans up by cancelling all open Alpaca orders for SPY and deleting both `open_orders` rows in a `finally` block; reruns are idempotent.

**Prerequisites**:
- US market open per `tpcore.calendar.session_contains` (NYSE/XNYS via `exchange_calendars`) — the script exits 0 with `SKIPPED` and the calendar's next-open timestamp outside the regular session. Half-days, holidays, and DST are handled the same way the engines see them; no hardcoded UTC window.
- Trade monitor running in a second terminal: `DATABASE_URL=$DATABASE_URL_IPV4 ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true python -m tpcore.trade_monitor`.

```bash
DATABASE_URL=$(grep '^DATABASE_URL_IPV4=' .env | cut -d= -f2-) \
ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \
  .venv/bin/python scripts/pipeline_smoke_test.py
```

The mocked-stream tests in `tpcore/tests/test_trade_monitor.py` cover the AAR-write half of the pipeline deterministically; this script proves the live broker + WebSocket legs that mocks can't reach.

---

## 11. Troubleshooting

Each scenario lists the diagnostic command first, then the recovery action. **Always confirm before any destructive action** (cancelling orders, restarting a service that submitted live orders mid-run, resetting risk state).

### A Railway service is in `CRASHED` state

```bash
railway service <name> && railway logs | tail -300
# Look for the traceback or non-zero exit at the bottom.
```

Common causes:
- **Missing env var.** Compare against `.env.example`. Add via the Railway dashboard → Service → Variables.
- **Database unreachable.** See "Database is unreachable" below.
- **Code regression.** Recent deploy broke something. Check `railway deployments --service <name>` for the prior good build and roll back via the dashboard.

Recovery: fix root cause → push (Railway auto-deploys on `main`) → confirm next deploy is `SUCCESS`. Do *not* just retry the failed run; the cron will fire again at its next scheduled time on its own.

### A scheduled run produced no `SHUTDOWN` row

```sql
-- Find recent runs that started but never finished cleanly:
SELECT engine, run_id, MIN(recorded_at) AS started_at,
       BOOL_OR(event_type = 'SHUTDOWN') AS shutdown_seen,
       MAX(CASE WHEN event_type = 'SHUTDOWN' THEN (data->>'exit_code')::int END) AS exit_code,
       BOOL_OR(severity IN ('ERROR', 'CRITICAL')) AS had_error
FROM platform.application_log
WHERE recorded_at > now() - INTERVAL '7 days'
GROUP BY engine, run_id
HAVING NOT BOOL_OR(event_type = 'SHUTDOWN')
    OR MAX(CASE WHEN event_type = 'SHUTDOWN' THEN (data->>'exit_code')::int END) <> 0
ORDER BY started_at DESC;
```

A missing `SHUTDOWN` row means the process died before the `finally` block. Pull Railway logs for the affected service and `run_id` window:

```bash
railway service <corresponding-service> && railway logs | tail -300
```

If a row appears with no matching engine run at all (e.g. cron expected at 22:00 UTC but no STARTUP within the window), the cron didn't fire — investigate via §1 (paused service, crashed deploy, or Railway outage).

### Database is unreachable

```bash
psql "$DATABASE_URL" -c 'SELECT 1'
# Expected: 1 row. If this hangs or errors, the DB is the problem.
```

- **Wrong URL?** The local `.env` has both `DATABASE_URL_IPV4` (pooler — for local CLI) and `DATABASE_URL_IPV6` (direct — for Railway). Confirm you exported the right one.
- **Supabase down?** Check status.supabase.com. Rare but possible.
- **Connection limit hit?** Supabase free tier caps concurrent connections. Restart any process that's holding too many.
- **Auth failure?** Pooler connection strings include the project ref + region; check the URL is intact.

For the local CLI: re-export `DATABASE_URL=$(grep '^DATABASE_URL_IPV4=' .env | cut -d= -f2-)` and retry. On Railway: services use `DATABASE_URL` directly from project variables; if those changed, redeploy.

### Alpaca API key is invalid

```python
# Run the §4 single-shot check. A 401/403 means the key is stale.
```

- Generate new keys in the Alpaca dashboard (Paper → Generate New Key).
- Update three places: local `.env`, Railway service variables (the engine schedulers — sigma, reversion, vector — plus any cron service that calls Alpaca), and any other deploy target.
- Re-run the §4 check to confirm.
- Cancel and re-submit any orders that failed during the outage *only after* operator approval.

### Risk Governor kill switch was tripped

```sql
SELECT engine, kill_switch_active, kill_switch_reason, daily_pnl, weekly_pnl,
       engine_equity, updated_at
FROM platform.risk_state
WHERE kill_switch_active = true;
```

The `kill_switch_reason` column tells you *why* it tripped: daily-loss-cap, weekly-loss-cap, or a manual `RiskGovernor.emergency_kill()` call.

**Do not auto-clear.** A tripped kill switch is the system protecting capital — clearing it without diagnosis would defeat its purpose. The recovery path:
1. Read `tpcore/risk/governor.py` for the reset logic — kill switches reset on `daily_reset_at` / `weekly_reset_at` rollover (defined in `tpcore.calendar`).
2. Confirm with the operator that the underlying loss is real and bounded (not, say, a data-feed bug that mispriced positions).
3. Wait for the natural reset, or — if the operator approves a manual reset — execute it through `RiskGovernor.reset_daily()` / `reset_weekly()`, never via raw SQL `UPDATE`.

### Data Validation Suite found new failures

```sql
SELECT source, timestamp, confidence, notes
FROM platform.data_quality_log
WHERE source LIKE 'validation.%'
  AND timestamp > now() - interval '8 days'
ORDER BY timestamp DESC;
```

There are no known residuals — the suite is green as of 2026-05-10. Any failing ticker in `notes` is a real new failure. If new tickers appear:

1. Reproduce locally: `.venv/bin/python -m tpcore.quality.validation`.
2. Read the failure detail.
3. If the failure is in the split check, the corporate-actions pipeline regressed — see §7.
4. If the failure is in delistings/constituents, it's a coverage gap — extend the fixture or the relevant `tpcore.quality.validation.sources.*` adapter.
5. Open a fix branch; do not silence the check by widening the residual list without operator approval.

### Stuck positions or orphaned orders at Alpaca

Per §4. Default action: **flag, do not auto-fix**. The runbook should record what was found and the operator decides whether to cancel orders or liquidate positions. Programmatic recovery is reserved for confirmed transient bugs (e.g., a cancelled order that re-appeared due to broker-side ack lag).

---

## What this runbook is not

- It is **not** a code-review or strategy-tuning procedure — those live in `MASTER_PLAN.md`.
- It is **not** a substitute for the Risk Governor — automated guardrails come first; this checklist is the human/AI layer that catches what the automated layer can't.
- It is **not** a place for ad-hoc debugging notes. If a failure mode recurs, codify it as a new troubleshooting entry; if it recurs three times, fix the root cause.
