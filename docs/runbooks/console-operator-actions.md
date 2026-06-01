# Console operator actions — deploy + runbook

The Operations → Data Pipeline page surfaces three operator-trigger
buttons (`Run data update` / `Run validation` / per-row `Run feed`)
that enqueue a row in `platform.application_log` (event_type
`OPERATOR_RUN_REQUESTED`). The deployed `lane-service` daemon polls
the bus and shells out to the canonical `scripts/run_data_operations.sh`
or `python scripts/ops.py --stage <name>`.

Architecture is the **modified Option D** from the 2026-05-29 architect
review (no new Railway service — co-task added to the existing
`lane-service` daemon). Two-layer auth:

  1. Browser → Next.js: NextAuth Credentials JWT (cookie).
  2. Next.js → console-api: shared-secret bearer token (`CONSOLE_OPS_TOKEN`).

Concurrency is arbitrated by `pg_try_advisory_lock(hashtext(
'data_ops_run'))` acquired by `scripts/run_data_operations.sh` itself,
which means cron + operator-trigger runs cannot overlap across
containers.

## Initial deploy

The code is already on `main` (and so on Railway after the next
deploy). Three things need configuration before the buttons go live:

### 1. Generate the shared-secret token

```bash
openssl rand -hex 32   # produces 64-hex-char string
```

Treat as a credential. Copy ONCE.

### 2. Set the token on both Railway services + the Vercel console

The token must match across THREE places. Set the SAME value on:

  * **Railway → console-api** service variables:
    `CONSOLE_OPS_TOKEN=<the-token>`

  * **Vercel → ste-console** project (Production) env vars:
    `CONSOLE_OPS_TOKEN=<the-token>` (NOT `NEXT_PUBLIC_*` — server-only).

After setting on Vercel, redeploy the console (or trigger from the
Vercel UI). After setting on Railway, console-api redeploys
automatically because of the variable change.

### 3. Verify

```bash
# console-api status endpoint — unauthenticated, should return live data
curl -fsS https://console-api-production-4576.up.railway.app/api/data-pipeline | jq .status

# operator-action endpoint without auth — should 503
# (until token is set) or 401 (after token is set, but missing bearer)
curl -i -X POST \
  https://console-api-production-4576.up.railway.app/api/operations/data-pipeline/run-update
```

In the browser:

  1. Open the console.
  2. Navigate to Operations → Data Pipeline.
  3. Click `Refresh` — page re-fetches, last_refreshed timestamp updates.
  4. Click `Run data update` — banner appears with QUEUED → RUNNING.
  5. Poll the active job — `GET /api/operations/data-pipeline/jobs/{id}`
     should show OPERATOR_RUN_STARTED then OPERATOR_RUN_COMPLETED.

## On-call playbook

### "A run is stuck"

The `RunningBanner` displays the active job's `elapsed_seconds`. If
it exceeds the `ACTIVE_RUN_WATCHDOG_MINUTES` window (90 min by
default in `console-api/data_pipeline.py`), the lane status flips to
RED (TIMEOUT) and the operator should abort.

  1. Click `Abort` in the banner. This inserts an
     `OPERATOR_RUN_ABORTED` row.
  2. The `lane-service` daemon's `operator_trigger` co-task sees the
     ABORTED row on its next 5-second poll and SIGTERMs the
     subprocess.
  3. The subprocess exit is recorded as `OPERATOR_RUN_FAILED` with
     the abort context.
  4. Refresh the page — the active_job clears.

If the daemon itself is dead, the abort row will not be acted upon.
Check daemon liveness on the Health page (daemons table) or run:

```sql
SELECT * FROM platform.daemon_heartbeats
WHERE daemon = 'lane_service'
ORDER BY recorded_at DESC LIMIT 5;
```

### "Buttons return 503"

`CONSOLE_OPS_TOKEN` is not configured. See deploy step 2 above. The
no-false-success contract (REQ-008) means a misconfigured deploy
fails closed rather than silently accepting actions while the auth
layer is half-installed.

### "Buttons return 401"

The NextAuth session has expired. Re-login and try again. If the
problem persists, check the Vercel logs for the `/api/operations/...`
route handlers — they call `auth()` and may surface a stale JWT.

### "Buttons return 409 (active_run)"

Another run is in flight — either the daily cron, or an earlier
operator click that hasn't completed. The 409 response body includes
the active job's id, started_at, and the action that's blocking. The
UI displays this in the alert banner.

You can NOT trigger a second run while one is active. The
`pg_advisory_lock` arbitrates across containers — even if the daily
cron lands while you're clicking, it'll see the lock held and exit
with `data-operations advisory lock held by another container`
(logged at INFO).

### "DATA_OPERATIONS_COMPLETE not emitting after an operator run"

The lane will only emit DATA_OPERATIONS_COMPLETE when the
FinalLaneVerdict is GREEN. If the run reports
`OPERATOR_RUN_COMPLETED` but no DOC event lands, look in
application_log for the verdict trace:

```sql
SELECT recorded_at, event_type, message
FROM platform.application_log
WHERE run_id = '<the-run-id>'
  AND event_type LIKE 'INGESTION_AUTO_RECOVERY%'
  OR  event_type LIKE 'INGESTION_AUTO_RECOVERED%'
  OR  event_type LIKE 'INGESTION_FAILED'
ORDER BY recorded_at;
```

If the verdict went RED, the message will name the unrecovered
checks. Fix the upstream issue (vendor outage, data drift,
unhealable check, etc.) and re-trigger.

### "I want to disable operator actions temporarily"

UNSET `CONSOLE_OPS_TOKEN` on the Railway console-api service. The
endpoint returns 503 immediately; the UI surfaces "operator token
not configured" and disables the buttons. Re-set the token to
re-enable.

## Audit trail

Every operator action writes ONE durable row to
`platform.application_log` with:

  * `engine = 'ops_console'`
  * `event_type = 'OPERATOR_RUN_REQUESTED'`
  * `run_id = <new UUID>` (also serves as job_id)
  * `data = jsonb {actor, action, stage, params, source: 'console', requested_at}`

The lane daemon's lifecycle events use the SAME run_id:
`OPERATOR_RUN_STARTED` / `OPERATOR_RUN_COMPLETED` /
`OPERATOR_RUN_FAILED` / `OPERATOR_RUN_ABORTED`. Query by run_id to
get the complete timeline.

## Rotating the shared secret

Quarterly cadence (or any time a deploy log appears in a public
context). Steps:

  1. Generate a new token (`openssl rand -hex 32`).
  2. Set on both Railway console-api AND Vercel ste-console.
  3. Wait for both deploys to roll over (Railway auto-redeploys on
     variable change; Vercel re-deploys via the dashboard).
  4. Verify with the curl smoke above.
  5. Audit: there is no token in any tracked file. `gitleaks` runs on
     every push and would block a leak.

## Auth boundary — read vs write

* **Read endpoints** (`GET /api/data-pipeline`,
  `GET /api/operations/data-pipeline/status`) are intentionally
  **unauthenticated** at the console-api layer. They surface
  validation counts, latest DATA_OPS event, recent self-heal log,
  and per-check status. This data is not market-moving or capital-
  positional — it's an operational-state telemetry view. Anyone with
  the Railway URL can read it.
  - If we ever surface position/PnL/strategy-specific data on this
    page, the read endpoint MUST be gated. Track that as a follow-up.
* **Write endpoints** (`POST .../run-update`, `.../run-validation`,
  `.../run-feed/{stage}`, `.../abort/{id}`) are gated by the
  two-layer auth above. The NextAuth session cookie is required for
  the Next.js route to forward; the shared-secret bearer token is
  required for console-api to accept the forward. There is NO path
  where a write action lands without both.

## Restoring a blocked vendor (runtime toggle)

When a check is classified as ``blocked_vendor`` (the
``CHECK_REMEDIATION`` class for vendor-disabled feeds; no live
producers as of 2026-06-01 after the ``greeks_max_pain`` retirement,
but the class infrastructure is preserved for future use), the
console rewrites its status to ``BLOCKED_VENDOR_ACCESS`` regardless
of what the underlying ``data_quality_log`` row says. This is the
honest "lane is known-broken" surface.

When you restore vendor access, you do NOT need to redeploy or
edit code. Set the ``CONSOLE_VENDOR_ENABLED`` Railway env var on
the ``console-api`` service:

  * Var name: ``CONSOLE_VENDOR_ENABLED``
  * Value: comma-list of vendor names to treat as RESTORED (e.g.
    ``greeks.pro`` or ``greeks.pro,iborrow``).
  * Save → Railway auto-redeploys console-api → the check returns
    to its honest derived status from ``data_quality_log``.

To re-block (e.g. vendor outage recurs): remove the vendor from
the comma-list and save. Same redeploy cycle, ~60 s.

The vendor name in the env var must match the ``vendor`` field on
the ``CHECK_REMEDIATION`` entry exactly (case-insensitive,
whitespace-trimmed). Current vendor classifications:

  * ``options_max_pain_freshness`` → ``greeks.pro``

If a vendor isn't in the env-var list, the rewrite stays — fail-
closed contract (operator must explicitly opt back in).

## What's not yet wired

  * Operator-action endpoints honor the bearer + cookie check, but
    the cookie-validation path inside Next.js uses `auth()` from
    `@/auth`. If a future refactor renames or moves the auth module,
    the forwarder under `console/src/app/api/operations/data-pipeline/_forward.ts`
    must be updated in lockstep.

  * The active_job's `current_stage` is best-effort — it's read from
    the latest application_log row with a `stage` field. Some
    handlers don't emit `stage`, so for those the field is null.
    Operator can read the latest_log line instead.

  * The 90-minute watchdog is a coarse signal. The data-ops script
    normally completes in ~25 min; if it exceeds 90 min the lane
    flips to TIMEOUT in the UI but the subprocess keeps running
    until the lane daemon's 90-min asyncio.wait_for cap, OR the
    operator aborts. Future enhancement: surface the daemon-side
    timeout to the UI so the operator sees the precise cap.
