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

### Before trading — the one-button operator command

```bash
scripts/run_data_operations.sh
```

This runs the canonical data-operations workflow in order:

1. **DOWNLOAD + UPLOAD** — `scripts/ops.py --update` (7 stages):
   `daily_bars` → `corporate_actions` → `coverage_fill` → `fundamentals_refresh` → `data_validation` → `universe_prescreener` → `universe_simulation`.
   Failed stages auto-retry once on transient errors. Refuses to run during the NYSE regular session.
2. **VERIFY** — `scripts/run_audit_all_tables.sh` (cross-table integrity audit).
3. **VERIFY** — re-confirms the validation suite is green.
4. **FIX** — self-heal retry is built into `--update`; if anything stays red after that, the script exits non-zero and points the operator at the dashboard for per-failure detail.
5. **COMPRESS** — gzips any uncompressed CSVs under `data/{alpaca,fmp,corp_actions}_backfill/`.

**Faster path — operator dashboard.** `scripts/run_dashboard.sh` launches a Streamlit UI whose **Platform health** panel surfaces every signal at a glance: bars freshness, fundamentals freshness, corporate-actions freshness, universe candidates today, last `--update` per-stage breakdown, validation-suite latest-run, universe coverage gaps, open orders, and the cross-table integrity audit. Each red row has an inline 🔧 Fix button. Use this before pushing **Run daily update** — it tells you whether a re-run is even needed.

### Full backfill (after major data-quality events)

```bash
scripts/run_full_backfill.sh
```

The CSV-first download → upload → verify → fix → compress pattern across all three sources (Alpaca bars, FMP fundamentals, Alpaca corp actions). Used after large cleanups or universe expansion. Long-running (30-60 min). NOT the daily cadence — that's `run_data_operations.sh`.

### Targeted backfill / special pull (parameterised stage — NOT a script)

A single-stage backfill or special pull is the canonical stage run
through `ops.py` with `--param KEY=VALUE` overrides — **never a one-off
`scripts/foo.py`**. `--param` is repeatable; values coerce
int/float/bool/str and overlay the `platform.ingestion_jobs` config.

```bash
# Re-pull a 10-day window for the full active universe (e.g. to fill a
# coverage hole). force_refresh bypasses daily_bars' skip-fast;
# end_offset_days=1 keeps it market-hours-safe.
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py \
  --stage daily_bars --param universe=active --param lookback_days=10 \
  --param end_offset_days=1 --param force_refresh=true --force
```

`daily_bars` uses Alpaca's multi-symbol endpoint (100/chunk) so a full
~7,669-ticker re-pull is ~2 min, not the ~2 h the old per-symbol path
took. If a backfill needs a knob the stage lacks, add it to the
handler's config contract — do not fork a script. (Full rationale +
the standard: `docs/superpowers/pipelines/data_adapter_pipeline.md`
§"Parameterised backfills".)

### Closed by design (not gaps)

Two items from the 2026-05-13 hangnail review are explicitly NOT being built. Recording the reasoning so they don't keep getting reopened.

1. **CSV-first for daily corp_actions + fundamentals stages** — not done. The daily `--update` writes those stages direct-to-DB. CSV intermediate is for non-trivial pulls (full backfills, source switches). A ~5,000-ticker FMP refresh that already supports skip-if-refreshed-within-24h is small enough that the CSV step adds overhead without much audit benefit. The pattern lives in `scripts/run_full_backfill.sh` for when it matters.

2. **`scripts/replay_history.py` (EDGE_VALIDATION_PLAN Phase 3)** — never built. Per the plan itself: *"Phase 4 (was: run a single historical replay, feed trade lists into OverfittingDiagnostic, decide go/no-go) fired across all four engines via the Phase 2.5 parameter-search pipeline."* The search runs the entire historical period across many parameter combinations; a single-replay script is duplicative. If we ever need engine-level smoke against a frozen historical window, the search scripts (`scripts/run_sigma_search.sh`, etc.) provide it with finer-grained output.

### Feed-driven scheduling & per-engine gating (2026-05-16)

**Scheduling is feed-driven, not a blanket cron.** The data-ops daemon
still fires at the launchd time, but `run_data_operations.sh` now asks
`python -m tpcore.feeds` which feeds are *due* per their `FeedProfile`
(vendor-anchored trigger/cadence, UTC) and runs `ops.py --update
--only <due>` — only those stages. Inspect/debug:

```bash
python -m tpcore.feeds --reasons   # due stage \t feed \t why
```

Fallbacks (safe by design): dispatcher error → full sweep; empty due
list → infra + Step-4 self-heal only (NONE_DUE sentinel). `ops.py
--update` with no `--only` = the old full sweep (unchanged). The
single source of truth is `tpcore/feeds/profile.py`; freshness
thresholds are read from there (no per-check guessed constants).

**Per-engine data gate + operator override.** Graduation/trade gating
is now per-engine: an engine is blocked only if a validation source
IT reads is red (per `EngineProfile.data_dependencies` — folded from
the prior `ENGINE_TABLES` dict 2026-05-20 via PRs #171/#191/#195).
To force the old global all-green behaviour (block every engine on
ANY red — e.g. during an incident):

```bash
export CAPITAL_GATE_REQUIRE_ALL_GREEN=1   # unset/0 = per-engine (default)
```

The env var is read by each engine's capital-gate plug; no restart of
anything else required.

### Daemons — one-button install (2026-05-14)

The platform runs three launchd LaunchAgents on the operator's Mac. After this single command, nothing else needs operator attention:

```bash
scripts/install_all_daemons.sh
```

| Daemon | What it does | Schedule |
|---|---|---|
| `trade_monitor` | Persistent. Watches Alpaca `TradingStream` for fills, submits Tier 2 cascade for Sigma/Reversion. `KeepAlive=true` → respawns on any non-zero exit (Python tracebacks included). | persistent |
| `engine_service` | Persistent. Polls `platform.application_log` every 60s for `DATA_OPERATIONS_COMPLETE` events; on new event, shells out to `scripts/run_all_engines.sh` for the engine sweep. `KeepAlive=true`. | persistent (event-driven) |
| `data_operations` | Daily refresh: 15-stage `ops --update` (final stage `forensics`) → audit → validation → compress → emits `DATA_OPERATIONS_COMPLETE` (engine sweep is fired by `engine_service`, not inline). | Mon-Fri 21:30 UTC |
| `allocator` | Cross-engine capital rebalance (retired as launchd daemon 2026-05-17; now the first gated step in `ops/engine_dispatch.py`) | event-driven via engine_dispatch (WEEKLY_FIRST_TRADING_DAY) |

The dashboard's **Daemons (launchd)** row goes 🔴 when any agent isn't installed, with an inline 🔧 Install all daemons button. Logs at `~/Library/Logs/short-term-trading-engine/{engine-service,data-repair-service,data-operations}.{log,err}`. (Allocator is now event-driven via `engine_dispatch`; re-run `install_all_daemons.sh` to remove its live plist.)

**Local vs. Railway execution shapes (2026-05-15; DA-3 consolidation 2026-05-18).** The launchd daemons above are the canonical Mac path: `engine-service` (consolidated — co-hosts the trade-monitor stream + the day-rollover weekly-digest trigger), `data-repair-service`, and `data-operations` (3 daemons total; allocator is event-driven via `engine_dispatch`, not launchd; trade-monitor + the weekly-digest cron-trigger folded into `engine-service` per DA-3). Railway uses a different shape — see `railway.json` and `ops/platform_pipeline.py`: **three** services (`platform-pipeline`, `trade-monitor`, `allocator`), where `platform-pipeline` runs `ops.py --update` followed by `run_all_engines.sh` sequentially in a single process. The consolidation eliminates the inter-daemon `DATA_OPERATIONS_COMPLETE` polling dependency, which is unnecessary overhead in a stateless container environment. Stays under the Hobby-tier 5-service cap with headroom. Railway is currently paused (per `project_railway_hobby_tier.md`); the consolidated definitions are committed so a future re-enable just needs `python ops/apply_railway_service_config.py --all` + a git push. See MASTER_PLAN §8.1 for the full architecture.

Uninstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.michael.trading.*.plist
rm ~/Library/LaunchAgents/com.michael.trading.*.plist
```

### Allocator (2026-05-14)

Cross-engine capital allocation per MASTER_PLAN §5. Runs weekly (Monday pre-open), inverse-realized-volatility weighting with [0.10, 0.50] caps, freeze on drawdown.

```bash
scripts/run_allocator.sh                   # paper mode (no kill_switch writes)
scripts/run_allocator.sh --enforce-freeze  # live mode (writes risk_state.kill_switch_active)
scripts/run_allocator.sh --platform-capital 50000  # adjust total
```

* Reads engine equity from `platform.aar_events` (paper or live fills both count).
* Bootstrap: 25% each until an engine has ≥20 completed AARs; then switches to σ-based.
* Soft freeze at trailing-peak DD ≥ 15%; hard freeze at DD ≥ 25% or 30 sessions in soft state.
* Atomicity: `allocations` row + `risk_state.engine_equity` UPDATE wrapped in one transaction.
* Engines consume `engine_equity` automatically via `RiskStateStore.get()` — no engine-side code change.

**Rebalance gating (audit items 44 + 45, 2026-05-14).** Before every persist the allocator runs a four-branch decision tree:

| Condition | Action | Event logged |
|---|---|---|
| `max_drift < 25%` | Skip rebalance for active engines (frozen rows still persist) | `ALLOCATOR_SKIPPED` (`drift_below_threshold`) |
| `25% ≤ drift < 50%` AND CHOP transitional (38.2–61.8) | Skip rebalance | `ALLOCATOR_SKIPPED` (`regime_transitional`) |
| `25% ≤ drift < 50%` AND CHOP favorable | Rebalance | `ALLOCATOR_REBALANCED` (`soft_band`) |
| `drift ≥ 50%` | Force rebalance regardless of regime | `ALLOCATOR_REBALANCED` (`hard_band_override`) |

CHOP is computed from the trailing-120-day SPY series via `tpcore.indicators.chop.compute_chop` (same canonical implementation Sigma's setup_detection uses). Drift per engine = `abs(new_weight - prior_weight) / prior_weight`; max across active engines is the gate. First run (no prior allocation) → drift = 1.0 → forced rebalance. Frozen engines bypass the gate entirely so a `soft_frozen`/`hard_frozen` state change always lands.

Audit the rebalance history:

```sql
SELECT recorded_at, event_type, data->>'reason' AS reason,
       data->>'max_drift_pct' AS drift, data->>'regime' AS regime
FROM platform.application_log
WHERE engine = 'allocator'
ORDER BY recorded_at DESC LIMIT 20;
```

### Trade Monitor daemon

`tpcore.trade_monitor` watches Alpaca's `TradingStream` for fills. Required for Sigma + Reversion's Tier 2 cascade (limit-sell on Tier 1 fill); Momentum doesn't need it. As of DA-3 (2026-05-18) the trade-monitor is **no longer a standalone daemon** — it is co-hosted inside the consolidated engine-service daemon (`ops/engine_service.py`); there is no `install_launchd_trade_monitor.sh`. Install via `scripts/install_all_daemons.sh`.

```bash
scripts/run_trade_monitor.sh                       # foreground (manual / debug only)
scripts/install_all_daemons.sh                     # installs the consolidated engine-service daemon (co-hosts trade-monitor)
```

Without the monitor running, Tier-1 fills don't trigger Tier-2 submission — orders sit indefinitely in `platform.open_orders` (the YUMC orphan pattern, 2026-05-12).

### Engine sweep (paper trading)

```bash
scripts/run_all_engines.sh                  # sigma → reversion → vector → momentum → sentinel
scripts/run_all_engines.sh --force          # bypass validation-green guard
```

Refuses to run if `data_validation` has any red row in its latest result. Each scheduler is one-shot; the trade-monitor (co-hosted in the consolidated engine-service daemon as of DA-3) handles the Tier 2 cascade.

### Forensics (2026-05-14, ops-integrated 2026-05-15)

Runs as the final stage (`forensics`) of `ops.py --update`, which is the first step of `scripts/run_data_operations.sh`. Scans every engine's AAR history in `platform.aar_events` and emits triggers when it detects:

* **Outlier loss** — a trade with `pnl_net` more than 3σ below the engine's mean (requires ≥5 historical AARs).
* **Loss cluster** — 3+ consecutive losing trades.
* **Drawdown period** — ≥10% peak-to-trough decline sustained ≥14 days.

Each new trigger is INSERTed into `platform.forensics_triggers` (idempotent via `payload.fingerprint`) and an auto-generated Sprint Dossier is written to `docs/sprints/<date>-<kind>-<engine>-<id>.md`. The dossier is a markdown template prefilled with the trigger payload — the operator fills in **Hypothesis** + **Fix** sections, ships the change, then clicks **Mark resolved** on the dashboard (Health tab → Forensics expander) which sets `resolved_at = NOW()`.

```bash
# Run standalone via the ops CLI (preferred — matches every other stage):
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py --stage forensics

# Or directly via the module entrypoint (legacy form, still supported):
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m tpcore.forensics
```

The dashboard's `forensics` probe (added 2026-05-15) surfaces open dossiers, the last fire timestamp, the set of engines under review, and per-kind counts. Probe output is always `ok=True` — open dossiers are findings to review, not platform errors.

Service-level error handling: each engine and each trigger is isolated — a single malformed AAR or transient INSERT failure logs a warning but doesn't stop the rest of the run. CLI-level retry: one retry on pool-build failure.

### Alerting on failure (2026-05-14)

`scripts/run_data_operations.sh` now fires a macOS Notification Center alert (`osascript`) on any non-zero exit — from `ops.py --update`, the cross-table audit, validation suite red, or a trap-caught unexpected exit. The notification points at `~/Library/Logs/short-term-trading-engine/data-operations.log`. Safe no-op on non-Mac hosts (CI).

### Lessons learned (2026-05-13 data-cleanup post-mortem)

The post-mortem captured these principles as durable patterns; the data-operations + full-backfill scripts above codify them. Treat any deviation as the start of the next mess.

1. **CSV-first for any non-trivial pull.** Source → CSV (audit-able artifact) → validate-each-row → upsert. Re-runnable; the CSV remains the permanent record of what the source returned. Daily pulls (~7k bars/day) can write direct; full backfills go CSV-first.
2. **Physical-truth at write time.** Every row passes the same physical-truth predicate (`close > 0`, OHLC consistent, no future dates) AT the CSV write step, not just at the destination. Bad rows never reach the database.
3. **Self-healing > manual retry.** Stages that hit transient errors (timeout, 429, ReadError) auto-retry once inside `--update`. Resumable stages (fundamentals: skip-if-refreshed-within-24h, daily_bars: skip-if-already-ingested) make every re-run faster than the previous.
4. **SIP > IEX.** Alpaca's free IEX feed silently misses tickers that trade off-IEX. The account is subscribed to SIP; default everywhere is `feed="sip"`. The `coverage_fill` stage closes any remaining gap nightly.
5. **Validation suite is the gate.** 12 checks: delistings, constituent, splits, row_integrity, fundamentals_integrity, corporate_actions_integrity, earnings_events_freshness, sec_filings_freshness, liquidity_tiers_freshness, ticker_classifications_coverage, macro_indicators_freshness, prices_daily_freshness (the last added 2026-05-15 after the SPY-silent-gap incident — fires when a registered critical ticker stale > 5d or universe-wide staleness > 2%). Validation green is the operational definition of "data is clean enough to trade on."
6. **Cross-references are integrity too.** `audit_all_tables.sh` (and the dashboard's "Cross-table integrity" row) catches orphan tickers across every dependent table — the kind of failure the validation suite's per-table predicates miss.
7. **Latest-run beats rolling aggregates on dashboards.** A 7-day rolling failure count surfaces stale history as current state. Show LATEST per source — stale aggregates lie.
8. **Compress confirmed artifacts.** After a successful upsert, gzip the CSV (~80% disk savings). Loaders read `.gz` transparently on re-run.
9a. **CSV-first archive on every ingest.** All five ingest handlers
(`handle_daily_bars`, `handle_corporate_actions`,
`handle_fundamentals_refresh`, `handle_macro_indicators`,
`_stage_earnings_refresh`) write a gzipped CSV to
`data/<source>_archive/<source>_<stamp>.csv.gz` before/after the DB
upsert via `tpcore.ingestion.csv_archive`. This is the defence against
a vendor retroactively truncating history (FRED BAMLH0A0HYM2,
2026-05-15) — the archive is the permanent record of what the source
returned at a given moment. **Shrinkage detection** runs on the two
full-snapshot sources (`fred_macro`, `alpaca_corporate_actions`, which
re-pull all history every run): if the new archive is > 20% smaller
than the prior one, a `csv_archive.shrinkage_detected` WARNING fires —
the BAMLH0A0HYM2 detector. The three incremental sources get the
audit-trail archive but no shrinkage alarm (variable pull windows make
row-count comparison noise). Baseline snapshots for the two
full-snapshot sources are seeded via `scripts/run_dump_baseline_archives.sh`.

9. **Comprehensive DATA-pipeline audit beyond validation.** `python scripts/audit_data_pipeline.py` (or `scripts/run_audit_data_pipeline.sh`) runs the 4-phase data-pipeline audit covering: explicit checks (known-knowns) including freshness for every data source / Sentinel basket / credit_spread / hy_spread decommission; documented gaps (known-unknowns) including GLD tier quirk, hy_spread freeze, prices_daily gaps, ETF AR noise; latent data (unknown-knowns) including filter-diagnostics distribution, cross-engine ticker overlap, application_log event-type distribution, empty platform tables, macro correlations; anomaly heuristics (unknown-unknowns) including row-count velocity, 3σ macro stoppage, tier distribution shift, engine signal silence, DB size, correlated multi-source staleness. Findings persist to `data_quality_log` for dashboard surfacing. Run on demand when investigating; operator can also schedule it daily. **Canonical command** — when asked to "audit data pipeline" / "audit pipeline" the operator (and Claude in any session) runs this script, not a manual re-audit. (Data service only — the engine and AAR services have their own smoke/forensics coverage.)

Per-stage timeouts (`scripts/ops.py`): **120 s** for the light stages (`data_validation`, `universe_simulation`) and **3,600 s** (1 hour) for the heavy ingestion stages (`daily_bars`, `corporate_actions`, `fundamentals_refresh`). The heavy-stage budget was raised twice after the Phase 1 universe expansion (7,300 tickers) — `120s → 1200s → 3600s` (commits `d924491` and `57ec234`) — because the underlying FMP-backed handlers iterate ~73 batches with rate-limit sleeps and need real headroom. On timeout an ERROR row lands in `platform.application_log` and the pipeline moves on to the next stage; a single slow upstream never blocks the whole run.

### Weekly Maintenance

The daily `python scripts/ops.py --update` pipeline already includes the heavy weekly stages with built-in skip-guards, so no separate weekly command is required. The two stages that effectively run weekly:

| Stage | Cadence | Skip-guard | What it does |
|---|---|---|---|
| `fundamentals_refresh` | Effectively weekly | skip-if-refreshed-within-24h per ticker | FMP → `platform.fundamentals_quarterly`. Iterates the all-active universe; ~1 hr full pass. |
| `earnings_refresh` | Effectively weekly | short-circuits when `earnings_events.max(event_date)` < 6 days old | FMP earnings-history → `platform.earnings_events` for the T1+T2 stock subset. Added 2026-05-14 alongside the `earnings_events_freshness` validation check. |
| `sec_filings` | Effectively every 3 days (tightened 2026-05-14) | short-circuits when either SEC table was touched within 3 days | SEC EDGAR Form 4 (insider) + 8-K (material events) → `platform.sec_insider_transactions` + `platform.sec_material_events`. Reference implementation of the standard 5-stage data-adapter pipeline; CSV-first (download → validate-at-CSV → load → compress). Requires `SEC_EDGAR_USER_AGENT` env var per SEC fair-access policy. |
| `tier_refresh` | Quarterly | skip-if-refreshed-within-90-days (outer) + 60d on Corwin-Schultz spread observations (phase 1) | **Two-phase autonomous** (audit G-2 fix, 2026-05-14): phase 1 re-writes `spread_observations` from Corwin-Schultz, phase 2 aggregates `liquidity_tiers`. Replaces the manual `scripts/run_tier_refresh.sh` invocation; the wrapper still works for manual one-shots. |
| `classify_tickers` | Monthly | skip-if-refreshed-within-30-days AND ≥95%-coverage | Re-runs the Alpaca-asset-name classifier into `platform.ticker_classifications`. Skip-guard is two-clause: forces a re-run when a universe expansion has introduced unclassified tickers even within the 30-day window. |
| `macro_indicators` | Weekly (2026-05-14, last data source from §6.1) | short-circuits when `MAX(recorded_at)` is within 7 days | FRED time-series → `platform.macro_indicators` for the five canonical series (sahm_rule, industrial_production, initial_claims, yield_curve, credit_spread). Requires `FRED_API_KEY` env var (free signup at https://fred.stlouisfed.org/docs/api/api_key.html). Unblocks the Sentinel macro-defense engine. The credit-stress indicator is BAA10Y (Moody's Baa minus 10Y Treasury) — swapped in 2026-05-15 after FRED truncated `BAMLH0A0HYM2`. |
| `greeks_max_pain` | Daily (2026-05-16) | same-day skip-guard: no-op if today's `observed_date` already present for the symbol | greeks.pro free-tier max-pain → `platform.options_max_pain` for 1 tracked symbol (SPY). Requires `GREEKS_API_KEY` (greeks.pro free tier, 10/min·600/day). `/flow`/`/greeks`/`/gex` are paid (403) and intentionally not ingested. |
| `finnhub_insider_sentiment` | ~Monthly (2026-05-16); 25-day skip-guard | no-op if `MAX(recorded_at)` within 25d | Finnhub free-tier insider-sentiment MSPR → `platform.insider_sentiment` for T1/T2 stock universe. Requires `FINNHUB_API_KEY` (free signup at finnhub.io). `/news-sentiment`/`/social-sentiment` are premium (403) and not ingested. |
| `apewisdom_social_sentiment` | Daily (2026-05-16); 24h skip-guard | no-op if `MAX(recorded_at)` within 24h | ApeWisdom Reddit social sentiment (no auth) → `platform.social_sentiment` for T1/T2 universe (all pages, local filter). API refreshes ~2h. |
| `fear_greed` | Daily after close (2026-05-16) | recompute is idempotent (ON CONFLICT DO UPDATE) | 4-component Fear & Greed from existing platform data (no provider) → `platform.fear_greed`. `--param backfill=true` computes full 2001→today history. |
| `finra_short_interest` | Bi-monthly (2026-05-16); 12-day skip-guard | no-op if `MAX(recorded_at)` within 12d | FINRA consolidated short interest (OAuth2; `FINRA_API_CLIENT_ID`/`FINRA_API_SECRET_KEY`) → `platform.short_interest` for T1/T2 stocks. PIT: `release_date` stored separate from `settlement_date` (~9 NYSE-session lag); `short_interest_pct` from PIT shares_outstanding (NULL if none). `--param skip_guard_days=0` forces re-pull. |
| `iborrowdesk_borrow_rates` | Daily (2026-05-16); 24h skip-guard | no-op if `MAX(recorded_at)` within 24h | IBorrowDesk daily borrow-fee % (no auth, scrape-fragile) → `platform.borrow_rates` per-ticker over T1/T2. 3 consecutive anti-bot drops → CRITICAL log + skip, never crashes. `--param max_tickers=N` bounds the run; `--param skip_guard_hours=0` forces re-pull. **Completes the master-plan data layer.** |
| `aaii_sentiment` | Weekly (2026-05-16); 5-day skip-guard | no-op if `MAX(recorded_at)` within 5d | AAII Sentiment Survey (no auth) — full-history `.xls` workbook → `platform.aaii_sentiment` (idempotent ON CONFLICT DO UPDATE, whole series refreshed each run). Published Thursdays; pulled Friday. `--param skip_guard_days=0` forces re-pull (self-heal). Contrarian indicator. |

To force-run locally (same command as the daily run; underlying handlers are idempotent):

```bash
python scripts/ops.py --update           # full daily pipeline (auto-skips fresh stages)
python scripts/ops.py --stage earnings_refresh   # one-stage manual fire
python scripts/ops.py --stage fundamentals_refresh
python scripts/ops.py --stage sec_filings        # SEC EDGAR Form 4 + 8-K
python scripts/ops.py --stage tier_refresh       # quarterly liquidity-tier rebuild
python scripts/ops.py --stage classify_tickers   # monthly classification refresh
python scripts/ops.py --stage macro_indicators   # FRED macro time-series
```

#### Quarterly: liquidity tier refresh

Liquidity tiers (T1–T5) are recomputed from `spread_observations` on demand — there is no `--update` stage for this because spread snapshots drift slowly. Re-run when adding tickers in bulk or after a spread-observation backfill:

```bash
scripts/run_tier_refresh.sh
```

Wrapped script around `scripts/assign_liquidity_tiers.py` (see §3 of `docs/DATABASE_AND_DATAFLOW.md`). Re-aggregation is idempotent.

#### Quarterly / one-off: ticker classifications

`platform.ticker_classifications` (asset-class taxonomy: stock/etf/spac/fund) is near-static — re-run only after a universe expansion or when a new ETF/SPAC needs classifying:

```bash
python scripts/classify_tickers.py
```

A future `--update-weekly` flag is reserved for any explicitly-weekly work heavier than the daily refresh's skip-guarded stages. Not built yet — the current daily pipeline covers it.

### Interpreting Results

- The dashboard `--check` output now includes 19 probes (FRED `macro_indicators_freshness` added 2026-05-14 — last data source):
  - `missed_data_operations` — warns when no automated **data_operations daemon** run in 30h (launchd misfire watchdog). Filters on `data->>'source' = 'data_operations_daemon'` so manual `ops.py --check` / `--update` invocations don't mask a missed daemon fire. The daemon's wrapper (`scripts/run_data_operations.sh`) passes `--source data_operations_daemon` to ops.py for this tag.
  - `supabase_backup` — probes `pg_stat_archiver.last_archived_time`; warns at 26h staleness; fails soft if the role lacks system-view access.
  - `disk_space` — warns when free disk on the repo's filesystem drops below 5 GB (audit-fix D6-1, 2026-05-14).
  - `trade_monitor_heartbeat` — reads from `platform.daemon_heartbeats` (the trade-monitor writes an UPSERT every 15 min via `_heartbeat_writer`, four-attempts margin against this probe's 60-min staleness threshold). Reports the daemon's self-reported `status` (`healthy` / `degraded` / `down`); green only when `status='healthy'` AND age ≤ 60 min. Rewritten 2026-05-15 to replace the prior `application_log MAX(recorded_at)` query that went red on quiet trading days (no fills = no events = false alarm).
  - `daemon_progress` — live stage-by-stage view of the most recent `data_operations_daemon` run. Reads `INGESTION_START` / `INGESTION_COMPLETE` / `INGESTION_FAILED` events for the run's `run_id`. States: `no_recent_run` (no STARTUP within 25h), `running` (STARTUP present, no SHUTDOWN), `completed_clean` (SHUTDOWN exit_code=0 with no failures), `completed_with_failures` (SHUTDOWN non-zero OR any INGESTION_FAILED). Shows the **complete end-to-end workflow** (added 2026-05-15): the 15 stages inside `ops.py --update` AND the bash-wrapper steps (`wrapper_audit`, `wrapper_validation_recheck`, `wrapper_matview_refresh`, `wrapper_compress`, `wrapper_emit_event`) — total of 20 rows when running end-to-end. The wrapper steps share the run_id via `scripts/_log_event.py`, invoked by `run_data_operations.sh`. Mirrored as the dashboard's "Daemon progress" panel in the Platform health section.
  - `forensics` — counts open Sprint Dossiers in `platform.forensics_triggers` where `resolved_at IS NULL`; reports last fire timestamp, distinct engines under review, and per-kind counts. Always `ok=True` (dossiers are review work, not platform errors).
  - `macro_indicators_freshness` — green if every FRED series ≤ 90d old; yellow 90-180d; red > 180d or any indicator missing.
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
| End-to-end pipeline smoke (engine → broker → trade_monitor → AAR) | `python scripts/pipeline_smoke_test.py` | **The current next-gate check** — branches on `tpcore.calendar`: LIVE mode (market open, full fill round-trip with quote-anchored TP/SL via `AlpacaDataAdapter.get_quote`) or WIRE mode (market closed, far-below limit + EVENT_* poll). Runnable any hour. Requires the consolidated engine-service daemon running (co-hosts the trade-monitor stream). |
| Engine submission run (Sigma / Reversion / Vector / Sentinel) | `python sigma/scheduler.py` (etc.) | Each scheduler is one-shot. Trade monitor must be running for sigma/reversion/vector Tier 2 cascade. Sentinel + Momentum use day-market orders only — no Tier 2 dependency. |
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
UNION ALL SELECT 'earnings_events',         COUNT(*) FROM platform.earnings_events;
```

Expected ranges (cross-reference `MASTER_PLAN.md §6.4`, post-Phase-1 expansion):

| Table | Expected range | Sudden-drop alert |
| --- | --- | --- |
| `platform.prices_daily` | ≥ 20,000,000 (currently 20,654,889 — 7,694 tickers, audited 2026-05-14) | drop > 1% → investigate |
| `platform.fundamentals_quarterly` | ≥ 178,000 (currently 178,608 — 5,984 tickers, audited 2026-05-14) | any drop → investigate |
| `platform.corporate_actions` | ≥ 109,000 (currently 109,413, grows weekly — audited 2026-05-14) | drop → investigate |
| `platform.earnings_events` | ≥ 1,000 (currently 1,350 — 137 tickers, audited 2026-05-14) | drop → investigate; weekly `earnings_refresh` stage owns growth |
| `platform.ticker_classifications` | 13,669 (audited 2026-05-14, near-static) | unexpected change → investigate |
| `platform.aar_events` | grows slowly with live trades; can be 0 | a *drop* is concerning, no growth is normal |
| `platform.risk_state` | one row per engine that has ever traded | drop → investigate |
| `platform.data_quality_log` | grows weekly | drop → investigate |
| `platform.tradier_options_chains` | 122,668 (frozen — should never change) | any change → flag |
| `platform.earnings_events` | ≥ 683 (universe-expansion catalyst backfill is pending) | drop → investigate |

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
- `open_positions` ≤ the engine's per-engine position cap, resolved from the declarative SoT `tpcore.risk.limits_profile.limits_for()`: momentum 200, sentinel 5, all others (reversion/vector) default 8.
- `updated_at` is recent (last weekday for active engines).

**Governor is now real and uniformly enforced (2026-05-17).** Previously the `RiskGovernor` gated only the 2 per-trade engines and ran against frozen placeholder equity. Now all 4 live engines reach `check_trade()` + `record_fill()` on every order: per-trade engines (reversion/vector) via `BaseOrderManager.submit_decision`, batch engines (momentum/sentinel) via the shared `tpcore.risk.batch_gate.gate_batch_order()` in their scheduler submit loop (with `record_fill(position_delta=-1)` on rebalance-driven exits). Per-engine `RiskLimits` come from `tpcore.risk.limits_profile`; the governor emits a `tpcore.risk.equity_unallocated` WARNING while an engine's effective equity is still the 10000 placeholder. **Known limitation:** for batch engines `open_positions` is a conservative slot proxy — `+1` per gated order, `−1` per submitted close, but stale prior-holding slots are not reconciled against actual broker positions, so the cap errs tight/conservative and never fails open; full broker-position reconciliation is a documented follow-up.

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

## 5.4 Momentum Paper Trading

Momentum is the only engine in the bench currently paper-trading. Cadence: monthly rebalance on the first NYSE trading session of each calendar month. Universe: T1+T2 from `platform.liquidity_tiers` (~1,281 names). Signal: 12-1 momentum (231-day lookback, 21-day skip). Top decile equal-weighted; ~50-130 positions concurrent.

**One-shot kickoff** (forces a mid-month rebalance — use once to start paper-trading mid-month, then move to the daily-cron pattern below):

```bash
scripts/run_momentum_kickoff.sh
```

Watch for the `RunSummary(..., submitted=N)` line. `N` should equal the order count (1 close per existing position + ~50-130 new opens).

**Daily run** (call every weekday — scheduler no-ops on non-rebalance days):

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m momentum.scheduler
```

Output on a non-rebalance day: a single `RunSummary(as_of=..., action=no_rebalance)` line. On the first NYSE session of a calendar month: the same orders flow as the kickoff.

**Dry-run preview** before any real submission:

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m momentum.scheduler --dry-run --force-rebalance
```

**Verify positions at Alpaca paper** any time after fills:

```bash
.venv/bin/python -c "
import asyncio
from tpcore.alpaca import AlpacaPaperBrokerAdapter
async def main():
    b = AlpacaPaperBrokerAdapter()
    a = await b.get_account()
    p = await b.get_positions()
    print(f'equity=\${a.equity}  positions={len(p)}')
    for x in p[:20]: print(f'  {x.symbol:<6} qty={x.qty} mv=\${x.market_value}')
asyncio.run(main())
"
```

**Graduation gate** — Momentum graduates from paper to live when:
* `n_rebalances >= 6` (≈ 6 months of live paper data — looser than Sigma's 50-trade threshold because monthly cadence accrues fewer events)
* `sharpe_annualized >= 1.0` and `profit_factor >= 1.5`
* Data Validation Suite is fresh and clean
* Credibility rubric ≥ 60 (currently structurally unreachable for monthly strategies with the default DSR ≥ 0.95; consider a frequency-adjusted threshold after live data lands)

## 5.45 Tip Sheet — private operator research tool

`scripts/generate_tip_sheet.py` is a terminal-only research report. **Not a publication, not a public feed, not a product.** It prints, per engine: a layman-readable description, the credibility-rubric breakdown, recent signals from `platform.application_log` (`event_type='SIGNAL'`), and recent trade outcomes from `platform.aar_events`. A mandatory disclaimer is appended to every run.

**Usage (Phase 1 — current scope):**

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/generate_tip_sheet.py --engine momentum
```

**Flags:**

- `--engine <name>` — required. One of `sigma | reversion | vector | momentum | s2 | catalyst | sentinel`.
- `--days <N>` — optional. Lookback window for signals + trades (default 30).
- `--since <ISO date>` — optional. Overrides `--days` with an explicit lower bound.
- `--force` — bypass the credibility ≥ 60 gate. **Intended for private review of unproven engines.** Output still prints the disclaimer; do not share.
- `--no-broker` — skip the live Alpaca query. "Currently holding" section is suppressed. Useful for offline review.

**Sections rendered, in order:** header → credibility rubric → **currently holding** (live broker positions filtered to the engine) → **today's recommendations** (what the engine would trade right now — Momentum-only in Phase 1) → recent signals → recent completed trades → disclaimer.

**Momentum smoke test** — single command that exercises every Phase 2 / Phase 2.5 component without submitting real orders:

```bash
scripts/run_momentum_smoke.sh
```

Three stages: (1) momentum plug unit tests, (2) scheduler `--dry-run --force-rebalance` against the live DB + paper broker, (3) tip-sheet render. Any failure aborts with non-zero exit. Designed as the canonical 'did the last commit break anything?' gate.

**Gates (enforced):**

- Credibility ≥ 60 required by default.
- `--force` lifts the gate but does NOT lift the disclaimer.
- **No `--publish` flag in Phase 1.** Output is terminal-only.

Publication-gated phases (Phase 2 / 3) are *not built yet* — they're tracked in `docs/EDGE_VALIDATION_PLAN.md` Phase 4 and require a securities-attorney-reviewed disclaimer plus ≥ 30 documented paper trades on a credibility-passing engine before any shareable output is enabled.

Full design rationale: `docs/superpowers/specs/2026-05-13-tip-sheet-plan.md`.

## 5.4a Engine SDLC — the Engine Change Request + The Lab

Trading engines have a lifecycle: `LAB → PAPER → LIVE → RETIRED`
(`tpcore.engine_profile.LifecycleState`). The roster SoT is
`tpcore.engine_profile._PROFILE`. Canonical spec:
`docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`.

**The Engine Change Request (ECR) — the single operator touchpoint.**
Fill `docs/superpowers/checklists/engine_change_request.md` and run:

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m ops.engine_sdlc --ecr docs/superpowers/checklists/engine_change_request.md
```

The operator approves **exactly two** operations — **ADD** an engine
(new scaffold or Lab-graduated) and **REMOVE** one (retire/archive) —
a binary `APPROVE? (y/n)` on a proven-consistent, dry-run-green diff
(fail-closed: non-TTY / EOF / anything not `y`/`yes` ⇒ declined,
nothing changed, audit emitted). A **MODIFY** (re-tuned params that
already cleared DSR≥0.95 ∧ credibility≥60) and a **LAB→PAPER promote**
(`--promote <engine>`; capital gate already green) are automated,
deterministic, no approval. A request that cannot produce a consistent
diff is rejected with the exact reason — never handed to the operator
to force. Every terminal outcome emits one
`platform.application_log` `ENGINE_CHANGE_REQUEST` row. This tool is
on-demand, operator-driven, **NEVER wired into any daemon / dispatch /
engine_service** (parity with `python -m ops.lab`).

**The snap-out (REMOVE).** A REMOVE is atomic-or-abort: the SoT entry
flips to RETIRED (AST-validated single-entry rewrite), the
`ENGINE_TABLES` orphan is removed, the non-Python shadows are
regenerated, the package CONTENTS are physically moved to
`archive/<engine>/`, and an EULOGY is rendered from
`tpcore/templates/eulogy_template.md`. A failed transition leaves ZERO
trace (journaled byte-identical rollback).

**The consistency clockwork + the manifest gate.**
`tpcore/tests/test_engine_lifecycle_consistency.py` is the N-way
half-state-fails-CI oracle (a new/removed/archived engine fails the
build unless coherently wired or fully offboarded in the same change).
`scripts/gen_engine_manifest.py --check` is the CI-divergence gate that
regenerates every non-Python shadow from the SoT and fails on drift —
run `python scripts/gen_engine_manifest.py` after any roster change.

**Known-limitations (recorded, NOT fixed in SP4):** (a) MODIFY is
reversion-only today (`planner._ENGINE_DEFAULT_CONSTS` maps only
`reversion`; a vector/momentum MODIFY is a documented fail-loud
reject). (b) `_validate_modify`'s `type(want)(v)` coercion is a bool
footgun, harmless today (every Lab-swept param is numeric). Future-work
only; out of SP4 scope.

### The Lab runbook (`python -m ops.lab`)

The Lab is the operable form of `LifecycleState.LAB`: an isolated,
concurrent, shadow/candidate backtest harness for hunting parameter
edges WITHOUT touching the live platform. It is the canonical on-demand
edge-hunt entrypoint.

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m ops.lab \
  --candidate myexp \
  --target-engine reversion \
  --intent fold_existing \
  [--param-overrides '{"z_threshold": 3.1}'] [--trials 50] [--seed 0]
```

- A separate OS process, operator-driven, **NEVER wired into any
  daemon / dispatch / engine_service**. No DSN ⇒ explicit non-zero rc
  + a logged error (never a silent 0).
- Isolation: `tpcore.lab.context.LabContext` forces the server pool
  read-only for the duration, provides the single allowlisted RW
  credibility pool, and installs a fail-closed reentrancy guard at
  every live-side-effect boundary.
- Output: a rendered `docs/lab/<day>-<candidate>-<verdict>-seed<seed>.md`
  PLUS a byte-frozen `.json` sidecar (the machine-readable evidence the
  ECR re-derives every gate number from). Credibility persists under
  the `lab.<candidate>` namespace.
- The dossier **recommends** a next step (`promote_new` → ADD a new
  engine; `fold_existing` → MODIFY the target; `none` → iterate) but
  the Lab **never applies it** — the ECR does, gated. Recommendation-
  only.

#### Currently shipped Lab candidates (2026-05-20)

Five Lab candidates exist on `main`; each ships with its own spec at `docs/superpowers/specs/2026-05-20-*-lab-candidate.md`. The canonical invocations:

| Candidate | Target engine | Intent | Canonical command | Spec |
|---|---|---|---|---|
| `vector_composite` | vector | `fold_existing` | `python -m ops.lab --candidate vector_composite --target-engine vector --intent fold_existing` | `2026-05-20-vector-composite-lab-candidate.md` |
| `catalyst_insider_event` | catalyst | `fold_existing` | `python -m ops.lab --candidate catalyst_insider_event --target-engine catalyst --intent fold_existing --param-overrides '{"event_confirmation_mode":"positive_beat_30d"}'` | `2026-05-20-catalyst-insider-cluster-event-lab-candidate.md` |
| `momentum_vol_managed` | momentum | `fold_existing` | `python -m ops.lab --candidate momentum_vol_managed --target-engine momentum --intent fold_existing` | `2026-05-20-momentum-vol-managed-lab-candidate.md` |
| `sentinel_maxdd` | sentinel | `fold_existing` | `python -m ops.lab --candidate sentinel_maxdd --target-engine sentinel --intent fold_existing` | `2026-05-20-sentinel-maxdd-lab-candidate.md` |
| reversion PCA-residual (override mode) | reversion | `fold_existing` | `python -m ops.lab --candidate reversion --target-engine reversion --intent fold_existing --param-overrides '{"signal_mode":"pca_residual"}'` | `2026-05-20-reversion-pca-residual-lab-candidate.md` |

Each probe spends honestly against the SP-A cumulative `n_trials` ledger (`tpcore.lab.ledger`); the autonomous Lab criteria spec (`docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md`) is the adjudication path for any `fold_existing` MODIFY that follows a SURVIVED dossier. The Sentinel graduated Bear Score candidate (TODO §Deep-research-adjudication) is BLOCKED on the SOS substrate decision + CFNAIMA3 wire (the latter shipped via PR #184; SOS is operator-pick); the catalyst insider-cluster 8-K leg is BLOCKED on 8-K item-code parsing verification (out of scope per the catalyst candidate spec §10).

## 5.5 Parameter-Search Pipeline

The canonical on-demand edge-hunt entrypoint is now **`python -m ops.lab`** (§5.4a — isolated, recommendation-only, ECR-gated). `scripts/search_parameters.py` is NOT deleted: it remains a thin compatibility shim preserving the historical `python scripts/search_parameters.py` CLI (and every public/underscore symbol the characterization oracle pins), delegating to `ops.lab.run` — which now hosts the walk-forward Lab engine (SDLC SP2 T5, H-S2-1). Random search + walk-forward + final held-back DSR verdict; imports each engine's `load_*_window_context()` / `run_*_with_context()` programmatically — no subprocess; per-window data load is shared across all candidates. The direct invocation below is the lower-level harness; prefer `python -m ops.lab` for an operator edge-hunt.

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

- `--engine`: `reversion | vector | momentum`.
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

- `scripts/run_vector_search.sh` — Vector sweep on T1+T2. (`scripts/run_sigma_search.sh` was removed when Sigma was archived 2026-05-16.)
- `scripts/run_all_searches.sh` — reversion + vector back-to-back. **Note:** `set -e` is intentionally OFF; a FAILED verdict exits 1 but should not abort the multi-engine sweep. These wrappers are the lower-level harness; the operator edge-hunt is `python -m ops.lab` (§5.4a).

**Interpreting the verdict:**

The orchestrator prints `VERDICT: SURVIVED` only when both `DSR ≥ --dsr-threshold` (default 0.95) and `credibility ≥ --credibility-threshold` (default 60). For monthly portfolio strategies (Momentum), the default DSR threshold is structurally unreachable with 2 years of held-back data — use held-back portfolio Sharpe + walk-forward consistency as the real signal.

**Outputs:**

- `backtests/<engine>_search_results.csv` — per-trial results (parameters, holdout metrics, full-window credibility).
- Stdout: header, per-trial timing/status, top-5 candidates by mean OOS score, final held-back metrics, verdict line.

---

## 6. Data Validation Suite

The suite runs as part of every `python scripts/ops.py --update` (stage 12 of 14 — see `_STAGE_SPECS` in `scripts/ops.py`) and also on demand via `scripts/run_stage.sh data_validation`.

### Acceptance criterion — "100% validated"

> **Data is clean when every row in every persistence table satisfies its physical-truth predicate, and the validation suite returns `passed=True` with `confidence=1.000` on every one of its twelve checks. No allowlist, no `WHERE clause` skipping known-bad ticker-date pairs. If the predicate fires, the row gets fixed (re-pulled from source) or deleted.**

Verifiable with one SQL:

```sql
-- Returns 0 when the entire data layer is clean.
SELECT SUM(failed) AS total_failed
FROM (
    SELECT CASE WHEN stale OR confidence < 1.0 THEN 1 ELSE 0 END AS failed
    FROM platform.data_quality_log q
    JOIN (
        SELECT source, MAX(timestamp) AS t
        FROM platform.data_quality_log
        WHERE source LIKE 'validation.%'
        GROUP BY source
    ) latest ON latest.source = q.source AND latest.t = q.timestamp
) x;
```

### The eleven checks

| Check | What it asserts | What "fail" means |
|---|---|---|
| `delistings` | every fixture entry is properly delisted in prices_daily | a known bankruptcy ticker is missing or not flagged |
| `constituent` | S&P 500 membership matches the constituent fixture | a removal isn't reflected in our data |
| `splits` | close-ratio across each fixture split day is in `[0.85, 1.15]` | an adjustment is broken |
| `row_integrity` | every prices_daily row: `close > 0`, `close <= 100M`, OHLC consistent (`high >= GREATEST(open, close, low)`, `low <= LEAST(open, close, high)`), non-NULL OHLCV, no future dates | a physically-impossible bar exists |
| `fundamentals_integrity` | every fundamentals_quarterly row: non-NULL ticker/filing_date, `period_end_date <= filing_date`, `shares_outstanding > 0 OR NULL`, no future filings | filings reversed in time, placeholder zeros, etc. |
| `corporate_actions_integrity` | every corporate_actions row: non-NULL cols, `ratio` in `(0, 1000]`, no far-future dates | implausible split ratios or NULL action_type |
| `earnings_events_freshness` *(NEW 2026-05-14)* | `earnings_events.max(event_date)` ≥ today − 7 days for active T1+T2 stock tickers (ETFs/funds/SPACs filtered via `ticker_classifications`) | catalyst pipeline stalled; Vector engine running on stale events |
| `sec_filings_freshness` *(NEW 2026-05-14)* | `sec_insider_transactions` + `sec_material_events` newest `filing_date` ≥ today − 14 days; ≥ 30% of T1+T2 stocks have a filing in last 180 days | SEC EDGAR ingest stalled or universe coverage thin |
| `liquidity_tiers_freshness` *(NEW 2026-05-14, L-2)* | `liquidity_tiers.max(last_updated)` ≥ today − 100 days; ≥ 3% of active universe in T1+T2 | quarterly tier refresh missed; cost model rotting |
| `ticker_classifications_coverage` *(NEW 2026-05-14, T-2)* | ≥ 90% of active prices_daily tickers have a row in `ticker_classifications` | universe expansion without re-running the classifier; ETF/SPAC filters silently failing |
| `macro_indicators_freshness` *(NEW 2026-05-14, FRED adapter)* | All five FRED series present; newest observation ≤ 90d old per series | `FRED_API_KEY` expired, FRED schema break, or stalled weekly stage |
| `options_max_pain_freshness` *(NEW 2026-05-16, greeks.pro adapter)* | Tracked symbol (SPY) has a max-pain snapshot ≤ 7d old | `GREEKS_API_KEY` invalid, greeks.pro outage, or stalled `greeks_max_pain` stage; self-heal re-runs the bounded stage |
| `insider_sentiment_freshness` *(NEW 2026-05-16, Finnhub adapter)* | Newest insider-sentiment (year,month) period ≤ 3 months old | `FINNHUB_API_KEY` invalid, Finnhub outage, or stalled stage; self-heal re-runs the bounded stage |
| `social_sentiment_freshness` *(NEW 2026-05-16, ApeWisdom adapter)* | Latest data ≤ 7d old AND ≥ 30% of T1+T2 stocks covered | ApeWisdom outage or stalled stage; self-heal re-runs the bounded stage |
| `fear_greed_freshness` *(NEW 2026-05-16)* | Most-recent fear_greed row ≤ 3 NYSE sessions old | stale macro/SPY inputs or stalled `fear_greed` stage; self-heal recomputes |
| `short_interest_freshness` *(NEW 2026-05-16, FINRA adapter)* | Newest `settlement_date` ≤ 35d old | FINRA OAuth creds invalid, FINRA outage, or stalled `finra_short_interest` stage; self-heal re-runs the stage |
| `borrow_rates_freshness` *(NEW 2026-05-16, IBorrowDesk adapter)* | Newest `date` ≤ 5d old | IBorrowDesk anti-bot block or stalled `iborrowdesk_borrow_rates` stage; self-heal re-runs the stage |
| `aaii_sentiment_freshness` *(NEW 2026-05-16, AAII adapter)* | Newest survey `date` ≤ 10d old | AAII anti-bot block, workbook moved, or stalled `aaii_sentiment` stage; self-heal re-runs the stage |

### Cross-table audit (added 2026-05-13)

The validation suite covers physical-truth checks. Cross-reference checks live in `scripts/run_audit_all_tables.sh`:

* `ticker_not_in_prices` across `earnings_events`, `corporate_actions`, `fundamentals_quarterly`, `liquidity_tiers`, `universe_candidates`, `tradier_options_chains` — every dependent table's ticker must exist in `prices_daily`.
* `tradier_options_chains.expired` — contracts past their expiration date.
* `liquidity_tiers.stale_30d` — tier assignments older than 30 days.

The dashboard's **Cross-table integrity** row (Platform Health panel) runs the same checks and surfaces any non-zero count.

### The fourteen `--update` stages (FRED added 2026-05-14)

`scripts/ops.py --update` runs the following stages in order (source of truth: `_STAGE_SPECS` in `scripts/ops.py`). Stage order was corrected 2026-05-14 (audit O-1/O-2/O-3 fix): `tier_refresh` + `classify_tickers` now run **before** `earnings_refresh` + `sec_filings` because the latter two filter by `ticker_classifications.asset_class`. `reconcile` was added at #3 (audit G-3 fix).

1. `daily_bars` — Alpaca SIP feed → `prices_daily`. Underlying fetcher wrapped with `@with_retry`.
2. `corporate_actions` — Alpaca → `corporate_actions`, applies splits to `prices_daily`. `@with_retry` resolves the 2026-05-12 Sunday-cron 429 failure.
3. `reconcile` — **NEW 2026-05-14**: heals `open_orders` against Alpaca's authoritative state. Same code path TradeMonitor uses on startup; runs daily so orphan orders never accumulate between daemon restarts. Idempotent, cheap (~5s typical).
4. `coverage_fill` — any tier ≤ 2 ticker missing a bar in the last 7 days gets a targeted 14-day SIP pull.
5. `cross_ref_cleanup` — deletes expired `tradier_options_chains` rows + orphan-ticker rows across dependent tables.
6. `fundamentals_refresh` — FMP → `fundamentals_quarterly`. Resumable: skip-if-refreshed-within-24h. Adapter uses `@with_retry`.
7. `tier_refresh` — **enhanced 2026-05-14 (audit G-2 fix)**: two-phase. Phase 1 = Corwin-Schultz spread bootstrap → `spread_observations` (60-day skip guard). Phase 2 = `assign_tiers` aggregates into `liquidity_tiers`. Outer 90-day skip guard governs whether either phase runs. Before 2026-05-14 only phase 2 ran, silently aggregating stale spread data.
8. `classify_tickers` — re-runs the Alpaca-asset-name classifier → `ticker_classifications`. Skip-guard: refreshed within 30 days AND ≥95% coverage of the active universe.
9. `earnings_refresh` — FMP earnings-history → `earnings_events` for T1+T2 stocks. Skip-guard: short-circuits when last refresh < 6 days ago.
10. `sec_filings` — SEC EDGAR Form 4 + 8-K → `sec_insider_transactions` + `sec_material_events` for T1+T2 stocks. CSV-first. **Skip-guard tightened 6→3 days 2026-05-14** (cadence finding): Form 4 has a 2-business-day filing deadline so 6d staleness was half-stale on average.
11. `macro_indicators` — **added 2026-05-14 (FRED adapter, last data source from §6.1)**: FRED time-series → `platform.macro_indicators`. Weekly cadence with 7-day skip guard. Unblocks Sentinel.
12. `data_validation` — the 12-check suite above (added prices_daily_freshness 2026-05-15).
13. `universe_prescreener` — writes today's `momentum` rows to `universe_candidates`.
14. `universe_simulation` — diagnostic; runs `scripts/simulate_universe.py`.

`--update` refuses to run during the NYSE regular session (corrupts intraday bars). `--force` bypasses. Failed stages auto-retry once if the error matches a known-transient class (timeout, ReadError, 429).

### Two-phase CSV-first ingest (added 2026-05-13)

For full historical backfills, every source uses an audit-able two-phase pattern:

| Source | Phase 1 (download → CSV) | Phase 2 (CSV → DB) |
|---|---|---|
| Alpaca bars | `scripts/run_backfill_alpaca_csv.sh` | `scripts/run_load_alpaca_csv.sh` |
| FMP fundamentals | `scripts/run_backfill_fmp_csv.sh` | `scripts/run_load_fmp_csv.sh` |
| Alpaca corp actions | `scripts/run_backfill_corp_actions_csv.sh` | `scripts/run_load_corp_actions_csv.sh` |

CSV is the permanent audit record under `data/{alpaca,fmp,corp_actions}_backfill/`. Each row passes the same physical-truth predicate the validation suite enforces at write time; bad rows are filtered AT the CSV layer. Loaders auto-compress source CSVs after successful upsert (`scripts/run_compress_backfill_csvs.sh` for one-shot cleanup) and read `.gz` transparently on re-runs.

### Cleanup scripts (run on red findings)

| Script | What it does |
|---|---|
| `scripts/run_cleanup_bad_price_rows.sh` | DELETE prices_daily rows that violate `row_integrity` |
| `scripts/run_cleanup_fundamentals_integrity.sh` | DELETE period-after-filing rows; UPDATE `shares_outstanding<=0` to NULL |

Both are idempotent and dry-run by default (`--confirm` to apply). Every deletion is audit-logged to `application_log` with the full row payload.

### Status as of 2026-05-13

The validation suite is fully green after the initial cleanup:
- 94,979 prices_daily rows deleted (all from the deprecated `source=tradier`; OHLC inconsistency + scale corruption up to $99T)
- 88 fundamentals_quarterly rows deleted (period_end > filing_date)
- 857 fundamentals_quarterly rows had `shares_outstanding` set to NULL (was 0 or negative)
- 4 corporate_actions rows deleted (MCHB ratio > 1000)

**Any non-empty `notes` field is a real new failure** — investigate immediately. The historic delisting fixture omits HTZGQ/WLLBQ/LK/SBNYQ/SI because no free-tier source carries their bars.

### Known gap: prices_daily historical coverage

The 94,979 deletions create gaps in `prices_daily` for the affected ticker-date pairs (mostly 2012-2024 dates that were tradier-only). Backtests that read those exact dates will see no row. Gap-fill via Alpaca historical pull is tracked separately — operator decides when to run the backfill. The dashboard's "Universe coverage" row surfaces tier ≤ 2 tickers missing recent bars.

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

## 7.5 SEC EDGAR pipeline (Form 4 + 8-K)

Reference implementation of the standard 5-stage data-adapter pipeline. Added 2026-05-14. Once the operator runs the bootstrap below, the weekly `sec_filings` ops stage handles ongoing maintenance.

### One-time bootstrap

```bash
# 1. Apply the migration (creates platform.sec_insider_transactions
#    and platform.sec_material_events with physical-truth CHECK constraints).
alembic upgrade head

# 2. Set the SEC user agent (required by SEC fair-access policy).
#    Must contain a real contact email.
export SEC_EDGAR_USER_AGENT="STE <your-email@example.com>"

# 3. Smoke-test against a small lookback window before the full backfill.
python scripts/ops.py --stage sec_filings
```

After step 3, run `python scripts/ops.py --check --pretty` and confirm the `sec_filings_freshness` row flips from `ok=false / reason=tables empty` to `ok=true` with `latest_filing` populated and non-zero `insider_rows` / `material_rows`.

### Historical backfill from 2018-01-01

Two datasets, two mechanisms (a bulk **file** download is a different
pipeline than a per-issuer **API** crawl — #132, 2026-05-16):

**Insider transactions — bulk Form-345 ETL** (the ~30h per-ticker
Form-4 XML crawl was the wrong tool):

```bash
python scripts/ops.py --stage sec_filings --backfill
```

Real two-phase ETL: **Phase 1 Extract** downloads the ~33 quarterly
`*_form345.zip` datasets (~336 MB) to a durable `data/sec_backfill/raw/`
cache (a valid zip already on disk is *not* re-downloaded — resumable,
replayable offline). **Phase 2** transforms → validates-at-CSV →
idempotent `ON CONFLICT` load → gzip, one short txn per quarter
(pooler-safe). Runtime **~2.5 min**. Verified 2026-05-16: insider
**646,107 rows, 1,262/1,501 T1+T2 stocks (84.1%)**, 2018→2026.

**8-K material events — historical API backfill** (no bulk 8-K dataset
exists; item codes live only in the per-issuer submissions index):

```bash
python scripts/ops.py --stage sec_filings --param eight_k_backfill=true
```

Per-issuer submissions crawl, 8-K only (no per-document XML), chunked +
idempotent + CSV-first. `full_history=True` follows the older
`filings.files` shards so a 2018→now pull is complete for prolific
filers (not just whatever is still in SEC's ~1000-filing `recent`
block). Runtime **~14 min**. Verified 2026-05-16: material events
**237,680 rows, 1,278/1,501 T1+T2 stocks (85.1%)**, 2018→2026.

The per-ticker `SECEdgarAdapter` (default, `recent`-only, no
`full_history`) remains the cheap daily/weekly incremental — unchanged.

**Self-verification** (emitted as `ops.stage.sec_filings.done` + STAGE
SUMMARY): `rows_loaded`, table-wide totals, distinct-ticker counts,
date span. CSV artifacts land under `data/sec_backfill/`; raw bulk zips
under `data/sec_backfill/raw/`. `@with_retry` handles transient
429/5xx; permanent 4xx raises `DataProviderOutage`.

### Verification queries

```sql
-- Total rows per table:
SELECT 'insider' AS table, COUNT(*) AS rows,
       COUNT(DISTINCT ticker) AS tickers,
       MIN(filing_date) AS earliest,
       MAX(filing_date) AS latest
FROM platform.sec_insider_transactions
UNION ALL
SELECT 'material', COUNT(*), COUNT(DISTINCT ticker),
       MIN(filing_date), MAX(filing_date)
FROM platform.sec_material_events;

-- Distribution by transaction type:
SELECT transaction_type, COUNT(*) AS n
FROM platform.sec_insider_transactions
GROUP BY transaction_type;

-- 8-K item code histogram:
SELECT event_type, COUNT(*) AS n
FROM platform.sec_material_events
GROUP BY event_type
ORDER BY n DESC;
```

### Ongoing maintenance

After the backfill, no operator action is required — `python scripts/ops.py --update` runs the `sec_filings` stage with its 6-day skip-guard. The dashboard's `sec_filings_freshness` row stays green as long as the weekly run succeeds.

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

End-to-end pipeline smoke for the trade-monitor era — **runs in two modes** depending on `tpcore.calendar.session_contains`:

- **LIVE mode (market open)** — submits one Tier 1 BUY bracket on SPY (1 share). TP/SL are anchored to the **live quote** from `tpcore.alpaca.AlpacaDataAdapter.get_quote` at ±1% — no drift from yesterday's close. Inserts a sigma-shaped `platform.open_orders` row with `tier2_qty=1`, polls for the trade-monitor to flip Tier 1 to `status='filled'` and then submit a Tier 2 row.
- **WIRE mode (market closed)** — submits one SIMPLE LIMIT BUY on SPY at `mid * 0.5` (far below market, cannot fill). Polls `platform.application_log` for any `engine='trade_monitor'` `EVENT_*` row tagged with our `alpaca_order_id`. Proves the stream → `_lookup_open_order` → `_db_log` path is healthy without needing a real fill — runnable any hour.

Cleans up by cancelling all open Alpaca orders for SPY and deleting smoke `open_orders` rows in a `finally` block; reruns are idempotent.

**Live-mode quote feed.** `AlpacaDataAdapter` is instantiated with `feed="iex"` inside the smoke test because our Alpaca subscription tier permits IEX but not SIP for recent quote data. SPY is heavily traded on IEX so the quote is reliable; this is local config (the smoke test only — tpcore's adapter is feed-agnostic).

**Broker-vendor coupling.** The smoke test imports `AlpacaPaperBrokerAdapter` + `AlpacaDataAdapter` + `Order`/`OrderClass`/`OrderSide`/`OrderType`/`TimeInForce` directly. If/when the platform swaps brokers, this file is the largest single point that needs to migrate — abstract the broker/data adapter via factory + env-var config at that point.

**Prerequisites**:
- The consolidated engine-service daemon running (`scripts/install_all_daemons.sh`) — it co-hosts the trade-monitor stream; the wire mode still requires the stream to be live since that's what we're testing.
- `platform.open_orders` migration applied (20260512_0000).

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
