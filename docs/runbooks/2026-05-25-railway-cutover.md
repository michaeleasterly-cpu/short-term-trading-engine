# Railway cutover runbook — 2026-05-25 (prep, no deploy yet)

**Status:** prep-only. Operator directive 2026-05-25: "we dont actually deploy until [engine session work is] completed". Cutover happens once the engine session pings the dev memstore (`/handoffs/2026-05-25-railway-readiness-for-engine-session.md`) with readiness flag.

## Target state

Three Railway services replace the seven Mac launchd agents:

| Railway service | replaces launchd labels | shape | trigger |
|---|---|---|---|
| `platform-pipeline` | `data-operations` cron + `engine-service` sweep | `python ops/platform_pipeline.py` (data refresh → audit → validate → self-heal → engine sweep, sequential one-shot) | cron `30 21 * * MON-FRI` |
| `trade-monitor` | `engine-service`'s co-hosted trade-monitor task | `python -m tpcore.trade_monitor` (Alpaca `trade_updates` websocket) | persistent (`restartPolicyType=ALWAYS`) |
| `allocator` | `allocator` + `allocator-heartbeat` | `python scripts/ops.py --allocate` | cron `0 13 * * MON` |

The 5-service Hobby tier cap leaves 2 slots for a dashboard or health endpoint.

## What does NOT move (operator-stated)

* `ops/llm_aar_critic*.py` + `tpcore/lab/llm_aar/` — operator-local LLM AAR critic; runs on the operator's Claude Max session. Never deployed.
* The three prior Mac-local LLM lanes (LAB-EMITTER / EDGE-FINDER / OUTCOME-MONITOR) — RETIRED 2026-05-25 (no longer in the repo).

## Pre-cutover checklist (operator + data session)

- [x] LLM lab/finder/monitor stack retired ("it is out", 2026-05-25)
- [x] R3 S3 archive backend env-pluggable via `CSV_ARCHIVE_BACKEND=s3` (`tpcore/ingestion/csv_archive_backends.py`)
- [x] 3-service shape committed to `railway.json` (since pre-2026-05-25)
- [ ] Engine session signals "ready" via memstore handoff path
- [ ] Operator re-auths Railway (`railway login`; the OAuth token in `.env` is stale)
- [ ] Operator picks S3-compatible bucket provider (recommended: Cloudflare R2 — free egress, ~$0.01/month for the 422 MB archive corpus)
- [ ] Operator provisions bucket + obtains credentials
- [ ] Bulk-upload existing local archives to bucket: `python scripts/upload_archives_to_s3.py --bucket <name> --endpoint <url> --dry-run` then `--commit`
- [ ] Railway service variables set (see env table below)
- [ ] `python ops/apply_railway_service_config.py --all` (propagates `railway.json` deploy block via GraphQL `serviceInstanceUpdate`)

## Railway service variables

Set in Railway dashboard → Service → Variables. Never tracked in git.

| key | value |
|---|---|
| `DATABASE_URL` | Supabase Postgres pooled connection string |
| `DATABASE_URL_IPV4` | Supabase Postgres IPv4 fallback |
| `ALPACA_KEY` | Alpaca API key (paper) |
| `ALPACA_SECRET` | Alpaca API secret (paper) |
| `ALPACA_PAPER` | `true` |
| `FMP_API_KEY` | FMP Starter tier ($200/yr) |
| `FRED_API_KEY` | FRED |
| `FINNHUB_API_KEY` | Finnhub |
| `GREEKS_API_KEY` | Tradier (options + secondary daily-bars fallback) |
| `FINRA_CLIENT_ID` | FINRA API |
| `FINRA_CLIENT_SECRET` | FINRA API |
| `CSV_ARCHIVE_BACKEND` | `s3` |
| `CSV_ARCHIVE_S3_ENDPOINT` | bucket endpoint URL |
| `CSV_ARCHIVE_S3_BUCKET` | bucket name |
| `CSV_ARCHIVE_S3_KEY_ID` | access key id |
| `CSV_ARCHIVE_S3_SECRET` | secret access key |

No `ANTHROPIC_API_KEY` on Railway — the only LLM caller left (AAR critic) is operator-local. `tests/test_lane_service_no_anthropic.py` is the sentinel.

## Cutover sequence (operator-driven, market-closed window)

Pick a Saturday or Sunday during a non-earnings, non-FOMC weekend.

1. **Confirm engine session ready.** Read `/handoffs/2026-05-25-railway-readiness-for-engine-session.md` from the dev memstore. If no readiness signal, abort.
2. **Stop the Mac launchd agents** (operator terminal):
   ```bash
   for label in engine-service lane-service data-operations allocator-heartbeat; do
     launchctl bootout gui/$(id -u) com.michael.trading.$label 2>/dev/null || true
   done
   launchctl list | grep michael.trading  # confirm empty
   ```
3. **Trigger `platform-pipeline` once manually** in Railway dashboard → Service → Deploy. Watch logs for: `STARTUP` → per-stage `INGESTION_COMPLETE` → `DATA_OPERATIONS_COMPLETE` → engine `STARTUP` × N → `ENGINE_SWEEP_DONE`.
4. **Verify `application_log` posts** match the Mac-side baseline:
   ```sql
   SELECT engine, event_kind, COUNT(*) FROM platform.application_log
     WHERE created_at >= NOW() - INTERVAL '1 hour'
     GROUP BY 1,2 ORDER BY 1,2;
   ```
5. **Verify `ingest_manifest` rows** land with `status='loaded'` and the archive lives in S3 (not local FS):
   ```sql
   SELECT status, source, COUNT(*) FROM platform.ingest_manifest
     WHERE created_at >= NOW() - INTERVAL '1 hour'
     GROUP BY 1,2 ORDER BY 1,2;
   ```
6. **Verify `prices_daily` row counts increased** (per-source breakdown). No `source='alpaca'` rows newly arriving (the `prices_daily_no_new_alpaca` CHECK NOT VALID blocks it at the substrate).
7. **Verify `trade-monitor` Alpaca websocket connects** (Railway logs: `connected to wss://...`).
8. **Verify allocator cron next Monday** (Mon 13:00 UTC). Skip if cutover is mid-week.

## Rollback (if cutover smoke fails)

Same operator-driven sequence in reverse:

1. **Stop Railway services**: pause auto-deploys on each of the 3 services.
2. **Restart Mac launchd agents**:
   ```bash
   bash scripts/install_all_daemons.sh
   ```
3. **Confirm daemon heartbeats resume** (P0 daemon_freshness check goes GREEN within 1 cycle).
4. **Diagnose Railway failure**: pull Railway logs (`railway logs --service platform-pipeline`). File a runbook addendum.

## Post-cutover (one-time cleanup)

* Once stable for 1 full week (5 platform-pipeline runs + 1 allocator + continuous trade-monitor):
  * Delete `scripts/install_all_daemons.sh` + the `install_launchd_*.sh` helpers (local-only artefacts, no longer the operator's deploy path)
  * Delete `scripts/run_data_operations.sh` (the bash wrapper around `ops.py --update`; `platform-pipeline` is the Railway-native equivalent)
  * Add `tests/test_no_local_launchd_scripts.py` sentinel (forbids re-introduction of `com.michael.trading.*` plist references)

## Open items (operator-action)

1. Railway re-auth (CLI `railway login`) — required for `python ops/apply_railway_service_config.py --all`
2. Pick S3-compatible bucket provider
3. Schedule cutover window (market-closed)

## Sibling docs

* `docs/runbooks/csv-archive-retention.md` — archive backend operations + retention policy
* `docs/runbooks/db-snapshots-restore.md` — Supabase backup operations
* `railway.json` — service definitions (do NOT edit without running `python ops/apply_railway_service_config.py --all` after)
