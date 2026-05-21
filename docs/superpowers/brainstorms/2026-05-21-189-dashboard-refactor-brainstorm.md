# #189 Dashboard Refactor — Brainstorm

**Date:** 2026-05-21
**Status:** brainstorm (this doc). Next: spec PR after operator review.
**Author:** Claude (Opus 4.7)
**Inputs read in full:**

- `design_handoff_trading_console/README.md` (385 lines — operator intent)
- `design_handoff_trading_console/Trading Console.html` (canonical CSS tokens; `oklch()`, JetBrains Mono / IBM Plex Sans, 11-view structure, theme variants)
- `design_handoff_trading_console/trading-console/app.jsx` (shell, routing, keyboard, activity rail, tweaks)
- `design_handoff_trading_console/trading-console/data.jsx` (the canonical *backend-contract* — every export is a real query)
- `dashboard.py` (3382 lines, 100+ `_q_*` / `_fetch_*` / `render_*` functions)
- `dashboard_components/{__init__,charts,defect_register,escalation,health}.py` (567 lines of pure `classify_*` functions)
- `docs/superpowers/specs/2026-05-18-dashboard-escalation-audit-panel-design.md` (read-only-renderer principle)
- `.claude/rules/dashboard.md` (tests target `dashboard_components/` only; never import `dashboard.py` from CI)

This is the **think-it-through-honestly** phase. Verdicts, not menus. The spec PR will pick up from here.

---

## §1 Framework choice — verdict: **React + Vite, talking to FastAPI**

### Verdict

**React + Vite (no Next.js, no SSR) + thin FastAPI read-only JSON layer + TanStack Query.** No SvelteKit. No HTMX. No `dashboard.py`-on-life-support routes.

### 5-line rationale (tied to codebase reality, not framework fashion)

1. **The handoff ships React.** `app.jsx`, `views*.jsx`, `components.jsx` are working React (Babel-in-browser) prototypes. Choosing anything else is voluntary re-creation work; choosing React costs us porting Babel-in-browser to a real Vite project — straight transliteration, no design re-derivation.
2. **`dashboard_components/*.py` already speak JSON.** Every `classify_*` returns the same `(color, summary, detail)` tuple shape; every `_q_*` returns a list[dict]. The FastAPI layer is `return await _q_validation(pool)` — no logic, no translation. That's the *least*-new-paradigm path because Python+JSON+browser are all in-repo idioms already.
3. **No SSR is needed.** This is an *operator console* — single user, behind a localhost wall, no SEO, no first-paint cost concern. Next.js's app-router complexity is pure-overhead in this context. Vite ships a `<script type="module">` bundle and we're done.
4. **HTMX would let us reuse Streamlit/Jinja templating, but** would force us to write the candle-chart + the parameter-search heatmap + the equity-curve + the bear-score timeline as server-rendered SVG-strings on every interaction — a strict regression vs the client-side render the design assumes. Hover-tooltips on a 320-bar candle chart cannot be HTMX-shaped.
5. **SvelteKit's smaller bundle isn't worth the second-language tax.** The repo has zero Svelte; the handoff has zero Svelte; the operator runs the console on localhost where a 200KB delta is invisible. Bundle size is the wrong axis to optimise.

### One stress-test condition under which the verdict flips

**If FastAPI cannot be added to the live deployment surface without breaking the existing Mac launchd / Streamlit launcher topology, drop FastAPI and use HTMX inside Streamlit-server-mode (or inside the existing `dashboard.py` process via `streamlit_extras`).** The verdict assumes FastAPI runs as a *new* sibling daemon (a 2-line addition to `scripts/install_all_daemons.sh` + a `console_service.py` parallel to `engine_service.py` / `data_repair_service.py` / `llm_triage_service.py`). If the operator's standing event-driven-on-application_log-bus rule rejects "another long-lived daemon," the path of least resistance becomes "render React from Streamlit's static-files route" — uglier, slower, but doesn't add a daemon.

### What this means concretely

- **No Node.js in CI.** Vite + npm + Vitest live in a separate `console/` subtree (the React app). CI runs Python-only against the FastAPI endpoints — see §5.
- **The frontend is `console/`** (working name) at the repo root. Build artefact ends up at `console/dist/` and FastAPI serves it as a static-files mount at `/`. That's the only coupling between the two: a `StaticFiles` mount + the JSON API.
- **The Streamlit `dashboard.py` stays alive on its current port (8501) until the new console reaches feature-parity + ~1 trading week green.** Parallel-deploy, not rip-and-replace. See §4.
- **FastAPI port:** 8502 (next port up). Operator runs both during the rollout via a new `scripts/run_console.sh` (the next-port choice avoids any need to retrain muscle-memory on the old port).

---

## §2 Backend shape — the API contract the new frontend reads

Strict invariants:

1. **FastAPI is `app = FastAPI()` with NO `PUT`/`POST`/`DELETE` routes.** Read-only renderer of the SoT. Enforced by a startup check that fails the app if any non-`GET` route is registered (see §6).
2. **Every endpoint is a thin wrapper around an existing Python function.** Where no SoT exists, the brainstorm flags it (does NOT invent one).
3. **Response shapes are frozen Pydantic v2 models.** `model_config = ConfigDict(frozen=True)`. Schema drift breaks the build, not silently the frontend.
4. **Every query is `asyncpg`-async.** The pool is `tpcore.db.pool()` (the same pool `dashboard.py` already opens).

### 2.1 Endpoint roster — view-by-view

| View | Endpoint | Existing Python SoT | Status |
|---|---|---|---|
| **1. Overview**  ||||
| KPI strip + topbar status | `GET /api/v1/account` | `dashboard.py:_fetch_account_state` (`AlpacaPaperBrokerAdapter.get_account()`) | Exists |
| Equity curve | `GET /api/v1/equity-curve?days=320` | `dashboard.py:_fetch_equity_history` | Exists; needs `?days=` param |
| Holdings | `GET /api/v1/holdings` | `dashboard.py:_fetch_holdings_for_engine` × all engines, merged | Exists, fan-out shim |
| Engines grid | `GET /api/v1/engines` | `tpcore.engine_profile._PROFILE` + `dashboard.py:_fetch_credibility_all_engines` | Exists |
| Today's signals | `GET /api/v1/signals?days=30` | `dashboard.py:_fetch_signals_all_engines` + `scripts.generate_tip_sheet.fetch_today_recommendations` | Exists |
| Recent AARs | `GET /api/v1/aars?days=30` | `dashboard.py:_fetch_trades_all_engines` (`tpcore.aar.AARReader`) | Exists |
| **2. Engine Detail (×5)**  ||||
| Engine state | `GET /api/v1/engines/{engine_id}` | `_PROFILE[engine]` + `_fetch_credibility(engine)` + `_fetch_holdings_for_engine(engine)` + `_fetch_today_recommendations(engine)` + `_fetch_recent_trades(engine)` | Exists (composed) |
| Momentum heatmap | `GET /api/v1/engines/momentum/param-search` | `backtests/momentum_search_results_t12.csv` parsed → JSON | **Gap:** need DB persistence (see §2.2) |
| Sentinel bear-score timeline | `GET /api/v1/engines/sentinel/bear-score?days=180` | `backtests/sentinel_phase_history.csv` OR live FRED Bear-Score derivation | **Gap:** need DB or live derivation (see §2.2) |
| **3. Ticker Drill-In**  ||||
| OHLC | `GET /api/v1/tickers/{sym}/ohlc?days=90` | `dashboard.py:_fetch_ohlc` (`platform.prices_daily`) | Exists |
| Closed trades | `GET /api/v1/tickers/{sym}/trades?days=365` | `dashboard.py:_fetch_closed_trades_for_ticker` | Exists |
| Active entry | `GET /api/v1/tickers/{sym}/active-entry` | `dashboard.py:_fetch_active_entry_for_ticker` | Exists |
| Signal context | `GET /api/v1/tickers/{sym}/signal-context` | Derived from `_fetch_signals` ∩ `{sym}` + AAR notes | Exists (compose) |
| **4. The Lab**  ||||
| Recent runs | `GET /api/v1/lab/runs?days=30` | `platform.application_log` `event_type='LAB_RUN_COMPLETE'` + `tpcore.lab.models.LabResult` sidecars on disk | **Partial gap:** SoT exists; needs a single `lab.list_runs()` accessor that joins the log row + the on-disk sidecar (the dossier path is in `data->>'dossier'`). See §2.2. |
| Run detail | `GET /api/v1/lab/runs/{run_id}` | `tpcore.lab.models.LabResult` + `LabResult.windows` (walk-forward) | Same gap as above — sidecar deserialise behind an accessor |
| Queued candidates | `GET /api/v1/lab/queue` | `platform.lab_queue` table OR in-memory queue | **Gap:** the design names the SoT TBD ("design assumed"). See §2.2. |
| **5. Engine SDLC / ECR Queue**  ||||
| Pending ECRs | `GET /api/v1/ecr/pending` | `ops.engine_sdlc.planner` — the brainstorm-named `pending_ecrs(pool)` doesn't exist yet. Closest existing: the `ecr.py` `EngineChangeRequest` parser + the `_PROFILE` two-tier registry. ECR files live in `docs/superpowers/checklists/*.md` (text). | **Gap:** no `pending_ecrs(pool)` accessor; needs a thin `ops.engine_sdlc.list_pending()` that scans the ECR submission table (`platform.application_log` `event_type='ECR_SUBMITTED'` filtered by no matching `ECR_DECIDED`). See §2.2. |
| Recent decisions | `GET /api/v1/ecr/history?days=30` | `application_log` `event_type='ECR_DECIDED'` | Exists; trivial query |
| Lifecycle map | `GET /api/v1/engines/lifecycle-map` | `tpcore.engine_profile._PROFILE` grouped by `LifecycleState` | Exists |
| **6. Weekly Digest**  ||||
| Current digest | `GET /api/v1/weekly-digest` | `ops.weekly_digest.build_weekly_digest(pool)` — **verbatim, no reparse** | Exists |
| LLM triage proposals | `GET /api/v1/llm-triage` | `application_log` `event_type IN ('DATA_LLM_TRIAGE_PROPOSAL','ENGINE_LLM_TRIAGE_PROPOSAL')`, open only | Exists (query) |
| Ack history | `GET /api/v1/weekly-digest/history?weeks=12` | `application_log` `event_type='WEEKLY_DIGEST_ACK'` | Exists |
| **7. Health**  ||||
| 6-KPI strip | `GET /api/v1/health/summary` | `dashboard.py:_fetch_platform_health` + `_fetch_escalation_state` | Exists |
| Ladder rungs | `GET /api/v1/health/ladder` | `docs/ESCALATION_HARDENING_LADDER.md` rung defs + per-rung SoT queries (already used by `dashboard_components/escalation.py`) | Exists (compose) |
| Source holds | `GET /api/v1/health/source-holds` | The hold-anti-join query in `_fetch_escalation_state` (the `DATA_SOURCE_HELD` with no later `DATA_SOURCE_CLEARED`) | Exists |
| Cross-table audit | `GET /api/v1/health/cross-table-audit` | `data_quality_log` rows `source LIKE 'cross_table_audit.%'` (used by `classify_cross_table_audit`) | Exists |
| Recent escalations | `GET /api/v1/health/escalations?days=7` | `application_log` `DATA_REPAIR_ESCALATED` + `AdapterContractDrift` + their terminals | Exists |
| Daemon topology | `GET /api/v1/health/daemons` | `dashboard.py:_fetch_daemon_state` (already builds the 4-row list) | Exists |
| Defect register | `GET /api/v1/health/defects` | `ops.defect_register.consolidated_defects(pool)` | Exists |
| **8. Data Pipeline**  ||||
| 8-KPI strip + last-run | `GET /api/v1/data-pipeline/summary` | `_fetch_platform_health` subset (`_q_update_run`, `_q_validation`) | Exists |
| Validation suite (13 checks) | `GET /api/v1/data-pipeline/validation` | `_q_validation` (`data_quality_log` `source LIKE 'validation.%'`) | Exists |
| Self-heal log | `GET /api/v1/data-pipeline/heal-log?days=7` | `application_log` `event_type IN ('ops.stage.complete','SELFHEAL_*')` | Exists |
| **9. Allocator**  ||||
| Snapshot + drift | `GET /api/v1/allocator` | `tpcore.allocator.service.AllocatorService` last-rebalance + `application_log` `event_type='ALLOCATOR_REBALANCE_COMPLETE'` | Exists |
| **10. Forensics**  ||||
| Active triggers | `GET /api/v1/forensics/triggers` | `tpcore.forensics.service` + `platform.forensics_triggers` | Exists |
| **11. Providers**  ||||
| Provider bindings | `GET /api/v1/providers` | `tpcore.providers.PROVIDER_BINDINGS` registry | Exists (in-memory) |
| **Cross-cutting**  ||||
| Activity rail (live stream) | `GET /api/v1/activity?limit=50` **or** `WS /ws/activity` (push) | `application_log` `event_type IN (...union of HEAL / SIGNAL / AAR / ALLOC / DIGEST / LLM / HOLD / ALERT / SYSTEM events)` | Exists; see §6 on polling-vs-push |

### 2.2 Identified gaps — and their canonical query

Every gap is real (the SoT doesn't yet exist or is partial); every gap has an existing-substrate path that doesn't invent business logic.

#### Gap 2.2a — Momentum parameter-search persistence

Today the search results live in `backtests/momentum_search_results_t12.csv` — a file emitted by `scripts/search_parameters.py` whenever the operator runs a parameter search. The dashboard heatmap needs them queryable.

**Canonical query:** `tpcore.backtest.search.list_recent_searches(engine='momentum', limit=1)` reading from a new `platform.parameter_search_results` table (one row per `(engine, trial_id, params_json, oos_sharpe, dsr, …)`). Persist via a tiny `--persist` flag added to `scripts/search_parameters.py` that writes both the CSV (for diff/audit) and the DB row (for the dashboard).

**Brainstorm note:** this is a 1-day adjacent change, NOT a #189 prereq. The fallback is "parse the CSV every refresh" (acceptable; the file is ~50KB). The frontend doesn't care which path the backend chose.

#### Gap 2.2b — Sentinel bear-score timeline

`backtests/sentinel_phase_history.csv` is the historical (backtest-time) bear score. The dashboard wants **live, ongoing** bear-score writes. Today `sentinel/lifecycle_analysis.py` computes the score on each cycle but doesn't persist a per-day row.

**Canonical query:** new write site — `sentinel/lifecycle_analysis.py` emits `application_log event_type='SENTINEL_BEAR_SCORE'` once per cycle. Dashboard reads via `GET /api/v1/engines/sentinel/bear-score?days=180` → `SELECT recorded_at, data->>'score' FROM application_log WHERE event_type='SENTINEL_BEAR_SCORE' AND recorded_at > now() - interval '180 days' ORDER BY recorded_at`. No new table; uses the existing event substrate.

**Brainstorm note:** also adjacent, also 1-day, also not a #189 prereq. Fallback is "render the backtest CSV until live data accumulates 180 days" — i.e. ship the endpoint reading the file, switch the source to the log later. Identical response shape; the frontend never knows.

#### Gap 2.2c — Lab runs accessor

The data exists in two places (log row for the metadata + sidecar file for `LabResult.windows`). No single accessor joins them.

**Canonical query:** new pure function `tpcore.lab.list_runs(pool, days=30)` that:
1. SELECTs `application_log` `event_type='LAB_RUN_COMPLETE'` in window
2. For each row, deserialises the sidecar at `data->>'dossier_path'.replace('.md','.json')` (the `LabResult` JSON sidecar that `tpcore.lab.target` already writes alongside the dossier)
3. Returns `list[LabRunSummary]` with the metadata flattened + the windows nested

This is **read-only composition over existing SoT** — no business logic. It collapses two existing reads into one accessor.

#### Gap 2.2d — Lab queue

The handoff `data.jsx` has `LAB_QUEUE` (queued candidates not-yet-run). The repo has no `platform.lab_queue` table and no in-memory queue. The operator-decision-point: "is queue a real concept or design fiction?"

**My read:** **design fiction at the moment.** Today a Lab run is launched by the operator running `python -m ops.lab <candidate>`. There's no queue daemon waiting on a table. The brainstorm should push back: drop the queue view from M0 and revisit only if/when an operator-facing queue daemon is built (it's not on the master sequence; it's a sibling future task).

**Action:** the spec writes "Lab Queue panel: STUB — renders 'No queue daemon configured' until an SoT exists. Endpoint returns `{queue: [], note: 'queue daemon not configured'}`." That matches the handoff README §"design's purpose is to surface state, not to invent it" — pushing back rather than fabricating.

#### Gap 2.2e — ECR pending accessor

The ECR text files live in `docs/superpowers/checklists/`. The `EngineChangeRequest` parser exists (`ops.engine_sdlc.ecr.parse_ecr`). What's missing: a registry of *open* ECRs.

**Canonical query:** new pure function `ops.engine_sdlc.list_pending(pool)` that SELECTs `application_log` `event_type='ECR_SUBMITTED'` minus matching `event_type='ECR_DECIDED'` rows (same anti-join shape as `DATA_SOURCE_HELD / DATA_SOURCE_CLEARED`).

This requires that submission goes through `application_log` — TODAY it doesn't (ECRs are launched by the operator running `python -m ops.engine_sdlc --ecr <file>` which jumps straight to validation). The fix is a 1-line `_emit(ECR_SUBMITTED)` at the head of the planner — adjacent change, not a refactor.

#### Gap 2.2f — Engine-detail "Open backtest →" action

The handoff has a button. The repo has `scripts/run_<engine>_backtest.sh` for each engine. The console is read-only — clicking the button **does not** dispatch the script. Instead, the button copies the canonical CLI command to clipboard and shows a toast ("Run `scripts/run_momentum_backtest.sh` in your terminal"). That respects the read-only-renderer principle while still being useful.

### 2.3 Frozen response schemas

Every endpoint returns a Pydantic v2 model with `frozen=True`. The Python module `console_api/schemas.py` (new) holds them. Examples:

```python
class AccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    unrealized_pl: Decimal
    unrealized_pl_pct: Decimal
    day_pl: Decimal
    day_pl_pct: Decimal
    ytd_pl: Decimal
    ytd_pl_pct: Decimal
    fetched_at: datetime
```

The frontend's TypeScript types are generated by `datamodel-code-generator` (or `openapi-typescript` from the FastAPI-emitted OpenAPI spec) so the contract is single-source.

---

## §3 Frontend rollout cadence — 12 PRs, one view per PR

### Why 12 and not "ship the whole thing"

11 views × ~250-400 LOC of React each + atoms + shell ≈ ~4500 LOC. A single PR at that size is unreviewable, untestable, and unmergeable. The repo's standing rule (subagent-driven, single review per task, lean default) reads as **one PR = one view = one shipped slice the operator can use the moment it merges**.

### Milestone list

| Milestone | Scope | PR size (LOC est.) | First-shipped value |
|---|---|---|---|
| **M0** | **Scaffolding.** `console/` Vite project with the design tokens copied verbatim from `Trading Console.html`, the `<Pill> <StatusDot> <Num> <Panel> <KPI>` atoms (no data, static demos in Storybook), Vitest + Playwright wired. `console_api/` FastAPI app with `/api/v1/health` (literal `{"ok": true}`) endpoint + read-only sentinel test. `scripts/run_console.sh` launcher. Sentinel-fenced manifest in CLAUDE.md or `.claude/rules/dashboard.md`. | ~500 | The new console boots at `localhost:8502` with one trivial endpoint; the rest is empty shell + design tokens proven. |
| **M1** | **Health view.** First fully-working slice. Endpoint: `GET /api/v1/health/{summary,ladder,source-holds,cross-table-audit,escalations,daemons,defects}` (7 thin wrappers around `dashboard_components/escalation.py` + `dashboard_components/health.py` + `dashboard_components/defect_register.py`). Frontend: `HealthView` rendering the 6-KPI strip + Ladder + 2-column split + Recent Escalations + Daemon Topology. | ~800 | **Operator's daily home.** The escalation/integrity audit panel is the spec-defined heuristic-#1 surface; it's where the operator lives. Ship this first; rotate the operator's morning ritual to the new console. |
| **M2** | **Overview view.** Endpoints: `/account`, `/equity-curve`, `/holdings`, `/engines` (the grid), `/signals`, `/aars`. Frontend: `OverviewView` with KPI strip + equity curve (custom SVG, see §6) + Engines grid + Holdings table + Signals + AARs lists. | ~900 | Full portfolio surface; this is the second-most-used view. |
| **M3** | **Activity rail (`/api/v1/activity`).** Shipped as a separate PR because it's cross-cutting (renders on every view) + may need WebSocket push (see §6). Initial implementation: polling at 5s. WebSocket upgrade lives behind a `?ws=1` query-string toggle for A/B until the polling load is measured. | ~400 | The right-rail event stream every view depends on; ship before the engine views so they all benefit. |
| **M4** | **Ticker Drill-In.** Endpoints: `/tickers/{sym}/{ohlc,trades,active-entry,signal-context}`. Frontend: `TickerView` with custom-SVG candle chart + entry/exit markers + trade ledger + signal-context panel. | ~700 | Click-through path from Overview Holdings → ticker detail completes. |
| **M5** | **Engine Detail (×1: Momentum).** Endpoint: `/engines/momentum` + `/engines/momentum/param-search`. Frontend: `EngineView` with `GateRow` × 6 + best-params + heatmap. Heatmap is a SVG `<g>` grid; values are colored via diverging palette. | ~700 | The Momentum view is the largest engine + the heatmap is the design's distinctive panel. Get it right once; the other 4 engines reuse the shell. |
| **M6** | **Engine Detail (×4: Reversion / Vector / Sentinel / Canary).** Reuses the M5 shell. Sentinel adds the bear-score timeline; Canary skips the gates panel + adds the non-graduating banner. | ~500 | Engine roster complete; the operator can drill into any engine. |
| **M7** | **Health (depth) + Data Pipeline view.** Endpoint: `/data-pipeline/{summary,validation,heal-log}`. Frontend: `DataView` with 8-KPI strip + Validation suite table + Self-heal log table. | ~500 | Data lane fully surfaced. |
| **M8** | **Allocator + Forensics.** Endpoints: `/allocator`, `/forensics/triggers`. Frontend: `AllocatorView` (allocation bar + drift table) + `ForensicsView` (triggers table). | ~400 | Capital + AAR-derived triggers surfaced. |
| **M9** | **Providers.** Endpoint: `/providers`. Frontend: `ProvidersView` (grouped-by-feed bindings table). | ~250 | DFCR lane surfaced. |
| **M10** | **Weekly Digest + LLM Triage.** Endpoints: `/weekly-digest`, `/weekly-digest/history`, `/llm-triage`. Frontend: `DigestView` with warning banner + Sections grid + LLM triage cards (2-lane) + Ack history. | ~700 | Non-skippable state-comprehension floor surfaced. |
| **M11** | **The Lab + Engine SDLC / ECR Queue.** Endpoints: `/lab/{runs,runs/{id},queue}`, `/ecr/{pending,history}`, `/engines/lifecycle-map`. Frontend: `LabView` (recent runs + run detail + KPI strip + walk-forward chart + queued candidates STUB) + `SDLCView` (pending ECRs + recent decisions + lifecycle map). | ~900 | Engine SDLC fully surfaced; new console reaches feature-parity. |
| **M12 (gate)** | **Streamlit retirement.** After ≥ 5 trading days of the new console green + operator-confirmed no missing capability vs old console: remove `dashboard.py` + `dashboard_components/{charts,defect_register,escalation,health}.py` (the latter three move into `console_api/classifiers/` since the FastAPI endpoints depend on them); remove Streamlit + `streamlit-lightweight-charts-pro` + `streamlit-autorefresh` from `pyproject.toml`; update `scripts/run_dashboard.sh` → alias to `scripts/run_console.sh`. CLAUDE.md dashboard line rewritten. | ~200 (net deletion) | Single-console world; the parallel-deploy phase ends. |

### Sentinel-fenced manifest

Inside `.claude/rules/dashboard.md` (or a new `docs/CONSOLE_MANIFEST.md`), a sentinel-fenced block lists `view → endpoint → existing Python helper` for every view. The fence is regenerated by a new `scripts/gen_console_manifest.py` from a single declarative source (probably the OpenAPI spec FastAPI itself emits). Drift between "what the frontend imports" and "what the backend exposes" becomes a CI failure.

### First view to ship — **Health (M1)**

The operator's already-live escalation audit panel (`render_escalation_audit()` shipped 2026-05-18) proves the operator opens the Health tab every morning. Shipping Health first means:

1. **Smallest fully-working slice that delivers immediate operator value.** Everything else can wait; Health cannot.
2. **All four `classify_*` functions ported in M1 are pure** — they have unit tests already (`tests/test_dashboard_escalation.py`, `tests/test_dashboard_health.py`). The FastAPI wrapper is `return classify_X(rows)` — testing-friction is near zero.
3. **The new console gets its credibility from the first day.** If the Health view matches `dashboard.py`'s Health tab byte-for-byte on every metric, the operator trusts the rest of the rollout. If Overview shipped first and looked subtly different, the rollout would feel like a risk for every following PR.
4. **Counter-arguments considered:** Overview is more visually impressive but doesn't drive operator action (KPIs are passive). The Lab is a new SDLC surface but isn't yet a daily-driver. Engine Detail is engine-specific. Health is the universal entry-point.

---

## §4 Streamlit deprecation — parallel-deploy, not rip-and-replace

### Why parallel

The `dashboard.py` Streamlit app is the operator's daily-driver. Replacing it with an unproven console while live trading is happening would be reckless. Parallel-deploy lets the operator A/B for as long as needed.

### Parallel-deploy mechanics

- **`dashboard.py` continues running on port 8501.** Untouched until M12.
- **The new console runs on port 8502** (FastAPI + the Vite static-files mount). New launcher: `scripts/run_console.sh`.
- **Both share the same `tpcore.db.pool()`** — no schema duplication, no data divergence. The new endpoints read the same SoT the Streamlit panels read.
- **Operator can A/B side-by-side** in two browser tabs. Mismatches → bug in the new console (the old one is the reference until M12).

### Deprecation gate

M12 (Streamlit retirement) requires:

1. **Every view ported.** M1–M11 all merged + the operator-checklist confirms each view's content matches the old dashboard.
2. **≥ 5 trading days of the new console with zero "missing capability" reports.** The operator runs both consoles for a working trading week; any "the old one shows X, the new one doesn't" entry resets the clock.
3. **No P0 / P1 console bugs open** in the new console's first 5 trading days.

Once M12 lands:

- `dashboard.py` → deleted (one big `git rm` commit in M12; the file is single-use and has no callers outside `scripts/run_dashboard.sh`).
- `dashboard_components/{charts,defect_register,escalation,health}.py` → moved into `console_api/classifiers/` (they're the load-bearing pure logic; the new endpoints already depend on them).
- `streamlit`, `streamlit-lightweight-charts-pro`, `streamlit-autorefresh` → removed from `pyproject.toml`.
- `scripts/run_dashboard.sh` → kept as an alias to `scripts/run_console.sh` for muscle-memory (the alias is one `exec` line + a 1-line deprecation note).
- `.claude/rules/dashboard.md` → rewritten to reflect the FastAPI + React reality. The "never import dashboard.py in CI" rule becomes "never import the FastAPI app at collection time without the `console_api` extras installed."

### Non-symmetry risk

The new console will have *minor* visual differences (different chart library, different table sort behavior, different keyboard shortcuts). The deprecation gate doesn't require pixel-equality; it requires *operator-confirmed* feature-parity. The handoff is high-fidelity but not byte-identical to Streamlit. That's by design — Streamlit's `st.dataframe` is being intentionally replaced by a denser custom table. Document this in M12's PR body so the deletion isn't read as a regression.

---

## §5 Tests — Python-side endpoint tests in CI; frontend tests operator-on-demand

### The lean choice

**CI gates only the FastAPI endpoints + the structural sentinels.** Frontend tests (Vitest unit + Playwright e2e) live in the repo but run via a separate `make console-test` target the operator invokes locally; CI never runs them.

### Why lean wins

The CI venv has zero JS tooling. Adding Node 20 + Vite + Vitest to CI:

1. **Doubles CI runtime** (Vite-build alone is ~30s on cold cache, ~10s warm; Vitest ~30s for ~100 component tests; Playwright headless Chromium ~60s; plus the Node install on every job).
2. **Adds a new dependency-source-of-truth** (`package.json`) that drifts independently from `pyproject.toml` and breaks in different ways.
3. **Gates an operator-only surface on JS toolchain hiccups.** The operator runs the console; the engines/data/aar lanes don't depend on it.

The repo's standing rule is "ops-package-shadow full-suite + order-flip authoritative gate." JS tests don't sit in that gate. They sit in a separate workflow that, if anything, the operator runs as a pre-PR check via the Make target.

### What CI runs (Python side)

1. **Endpoint shape tests** (`tests/test_console_api.py`) — pytest + `httpx.AsyncClient` against the FastAPI app, same `MockTransport` pattern as the existing AAII adapter tests. For each endpoint:
   - Smoke (200 OK).
   - Schema (response validates against the frozen Pydantic v2 model).
   - **Read-only-renderer assertion** — the response equals `classify_X(rows)` byte-for-byte for an injected `rows` fixture (no predicate recomputation in the endpoint).
2. **No-mutation invariant** (`tests/test_console_api_readonly.py`) — startup-time scan: `app.routes` contains zero `methods` set including `POST | PUT | PATCH | DELETE`. If any non-`GET` route is registered, the test fails the build.
3. **Sentinel-fenced manifest** (`tests/test_console_manifest.py`) — `view → endpoint → helper` manifest is in-sync (`scripts/gen_console_manifest.py --check` returns 0).
4. **Existing `dashboard_components/` tests** — unchanged. They test the classifiers; the FastAPI tests test the endpoint wrapping them.

### What lives in the repo but doesn't run in CI

- `console/src/**/*.test.tsx` — Vitest + React Testing Library component tests. Run via `make console-test-unit` (alias for `cd console && npm test`).
- `console/e2e/**/*.spec.ts` — Playwright e2e screenshot tests against a running console. Run via `make console-test-e2e`. The baseline screenshots are committed (one-time generation against `Trading Console.html`'s rendered output) so visual regressions surface as image diffs in PR review.
- The operator's pre-PR checklist (added to the new rule `.claude/rules/console-build.md`) is: `make console-test-unit` + `make console-test-e2e` green before merging any M1–M11 PR.

### Tradeoff statement (honest)

This is a lean choice, not a free one. **A future regression in the React layer will land in main and be discovered by the operator at console-launch time, not by CI.** Mitigations:

- Component tests are operator-on-demand-required at PR time (the rule, not the harness).
- Playwright screenshot diff catches visual regressions when the operator runs `make console-test-e2e`.
- The Python endpoint tests catch the contract drift (the most-likely-to-break boundary).
- The console is single-user / localhost — a 30-minute regression isn't a live-trading risk; it's an inconvenience.

If the operator vetoes this lean choice, the alternative is: add a `console-ci.yml` GitHub Actions workflow with Node 20 that runs `npm test` + Playwright on every PR. The cost is ~90s additional CI time + the dependency drift; the value is "JS regression caught at PR time, not at launch time."

---

## §6 Risks + structural issues to flag

### 6.1 Operator workflow disruption

**Mitigated by parallel-deploy.** The risk is real (rolling out 12 PRs over weeks while live trading is happening) but the parallel-deploy chooses to absorb it via the "old console always available" guarantee. M12 is the only moment of disruption — and it lands only after the operator's own ≥ 5-day soak.

### 6.2 JSON-endpoint cost vs Streamlit direct-in-page DB reads

Streamlit was a single Python process; every page render hit `asyncpg` once and rendered in-place. The new console is N HTTP requests per page load (one per endpoint visible on the active view).

**Quantification (worst case — Overview view on a 60s refresh interval):**

- 6 endpoints visible (account, equity-curve, holdings, engines, signals, aars).
- TanStack Query default `staleTime: 0` means refetch-on-mount-after-stale.
- At 15s refresh for the live-state endpoints: 5 endpoints × 4 refetches/min = 20 RPS to FastAPI.
- Each endpoint = 1 asyncpg query (avg ~20ms p50). Pool size = default 10. 20 RPS × 20ms = 400ms pool-second per second ⇒ 40% pool utilisation. Headroom is OK.

The Streamlit comparison: Streamlit refreshes the whole page on every interaction, including the panels you're not looking at. The new console refreshes only the visible view. **The new console is cheaper on the steady state, more expensive on the burst (initial view-switch).** The asyncpg pool size may need bumping from 10 → 20 to absorb the burst; this is a `tpcore.db` change, not a console change.

### 6.3 Activity rail — polling vs WebSocket push

`application_log` is the natural event substrate. Polling at 5s × 1 endpoint = 12 RPS minimum (sustained, all views). This is the single hottest endpoint.

**Two paths:**

- **Path A (lean):** polling at 5s. Acceptable load. The activity rail caches in TanStack Query; rate-limited to 1 RPS server-side via `slowapi` or a custom dependency. Simple, no new infra.
- **Path B (push):** Postgres LISTEN/NOTIFY on `application_log` INSERTs + a WebSocket fan-out from FastAPI. Eliminates polling. Requires the asyncpg pool to hold a long-lived LISTEN connection (1 connection permanently in `pool.acquire()` for the lifetime of the console). One-line trigger in migrations: `CREATE OR REPLACE FUNCTION notify_application_log() RETURNS trigger AS $$ BEGIN PERFORM pg_notify('app_log', NEW.event_type); RETURN NEW; END $$ LANGUAGE plpgsql; CREATE TRIGGER ... AFTER INSERT ON platform.application_log ...`. The FastAPI worker LISTENs + fans out via WebSocket.

**Brainstorm verdict:** ship Path A in M3. **Measure for 1 trading week.** If the polling load is invisible (which I expect — 12 RPS on a localhost FastAPI is nothing), Path B is YAGNI. If it's hot, M3.5 is a 1-day upgrade to Path B with zero frontend changes (TanStack Query swaps the polling fetch for a `useEffect` WebSocket subscription).

### 6.4 Chart rendering — sparse SVG, no heavy lib

The handoff README says `Trading Console.html`'s charts are pure CSS + inline SVG. Adding a chart lib (TradingView Lightweight, Recharts, Plotly, ApexCharts) re-introduces the same complexity Streamlit had with `streamlit-lightweight-charts-pro` (the 0.x-versioned lib `dashboard_components/charts.py` documents as a known-fragile dependency).

**Verdict — write a custom SVG renderer for each chart type. They are simple:**

- **Equity curve:** 1 `<path d="M ..." />` for the curve, 1 dashed `<path>` for the benchmark, 4 `<line>`s for grid, 5 `<text>` for date ticks, 1 `<circle>` for the hover dot. ~80 LOC of TypeScript + JSX. No lib.
- **Candle chart:** 1 `<g>` per bar (320 bars × 1 `<rect>` body + 1 `<line>` wick = 640 SVG elements). Entry/exit markers = `<polygon>` triangles + `<line>` vertical dashes. ~150 LOC.
- **Param-search heatmap:** 1 `<rect>` per cell + 1 `<text>` per cell. 9 lookbacks × 16 holds = 144 cells. Diverging palette = OKLCH mix of two stops. ~100 LOC.
- **Bear-score timeline:** identical shape to equity curve, 1 dashed line at threshold. ~80 LOC.
- **Walk-forward DSR bars:** 6 bars + threshold line. ~60 LOC.

**Total custom-chart LOC: ~470.** That's less than dropping in a charting lib + writing the integration glue + writing the chart-lib-versioning policy. The chart components are pure functions of data — they trivially Vitest-test against a known input.

**Counter:** if interactive crosshairs / zoom / pan are required, the math gets nontrivial. The handoff doesn't ask for those (hover dot is the only interaction). If the operator later asks, drop in `visx` (D3-based, React-native, MIT) — but only when asked.

### 6.5 Theme switching state

The handoff has `operator-dark` / `midnight` / `paper` themes + `comfortable` / `compact` density + accent picker. State lives in `localStorage`, not server-side. Server-side persistence would be silly — it's single-user, the laptop is the device, `localStorage` survives reboots, and any sync is YAGNI.

`document.documentElement.classList.add('theme-operator-dark', 'accent-amber', 'density-comfortable')` — the same CSS hooks the prototype uses. `useLocalStorage('console-tweaks', defaults)` hydrates on mount.

### 6.6 Read-only principle — must be load-bearing

The escalation-audit-panel spec (`2026-05-18-dashboard-escalation-audit-panel-design.md`) is explicit: **the console reimplemented predicates and drifted; never again.** The new console's `console_api/endpoints/*.py` files are **2-line wrappers** around the existing `classify_*` / `_q_*` / `tpcore.*` accessors. No predicate logic.

Enforcement (CI):

1. **No-mutation route check** (§5).
2. **Sentinel test** (`tests/test_console_api_predicate_freedom.py`) — for each endpoint, the response equals the existing helper's output for an injected fixture (exact-match assertion).
3. **Forbidden-import lint** — `console_api/endpoints/*.py` may import from `tpcore.*`, `ops.*`, `dashboard_components.*`, but **may not import `pandas`, `numpy`, or any helper from `console_api/endpoints/*.py` other than via re-export from `tpcore.*`** — i.e. no place to write business logic.

### 6.7 The Tweaks system

The handoff includes a `TweaksPanel` for design-time customisation via postMessage. **Drop it entirely in production.** The prototype shipped it because it's a Claude-generated design environment; the real console has a Settings page or skips the customisation entirely (operator picks the defaults at first launch, never changes them).

### 6.8 Engine-roster + DFCR rules

The handoff renders the engines grid + the providers table. The roster is the ECR-gated `_PROFILE` SoT; the provider bindings are the DFCR-gated `PROVIDER_BINDINGS` SoT. **The console renders both verbatim; it never modifies them.** This is structurally identical to the existing read-only-renderer principle, but worth flagging because the engine grid + the providers table are exactly the two surfaces that the operator might be tempted to add a write button to ("add engine," "deprecate feed"). The answer is *no* — those actions go through `/ecr` and `/dfcr` skills + the existing CLI invocations.

### 6.9 The Canary + Lab-sentinel quirks

- **Canary** never calls `write_credibility_score`; the engine grid renders Canary's card with no gates panel + a "intentionally non-graduating (spec §4b)" italic note.
- **Lab sentinel** (`LifecycleState.LAB` durable entry) is hidden from the engine grid (the `hidden_in_grid: true` flag in `data.jsx`) — it surfaces only in the lifecycle map in the SDLC view.

These are handoff-explicit, not invented. The spec captures both as explicit branches.

### 6.10 Path conflict with the engine-build sentinel-fenced docstring

`scripts/gen_engine_manifest.py` regenerates the smoke loop + `run_all_engines.sh` + `ops/platform_pipeline.py` docstrings. The new console manifest (§3) regenerates a different fence. They don't collide (different files, different fence markers), but the spec should explicitly verify that `tests/test_claude_rules_present.py` + `tests/test_claude_skills_present.py` continue to pass after adding the console-manifest sentinel. (Same pattern; should be a no-op.)

### 6.11 Risk we'll re-implement the bug we're trying to fix

The 2026-05-18 spec is explicit: the *current* `dashboard.py` drifted because it **reimplemented** predicates that changed underneath it. The new console must structurally prevent the same drift. The §6.6 enforcement is the structural fix — but the brainstorm should call out the *anti-pattern* explicitly: **the temptation in the frontend will be to "clean up" a response shape before rendering** (e.g. "the API returns confidence as a float 0-1, the UI wants 0-100, so multiply in the frontend"). That's the seed of drift. The rule: **all derivation lives in the FastAPI Pydantic model**, and the frontend renders strings/floats verbatim. If a multiplier is needed, the response field is `confidence_pct` (already × 100); the frontend reads `.confidence_pct` and renders it.

### 6.12 Authentication

None. The console is `localhost`-only. The FastAPI app binds to `127.0.0.1:8502` (same convention `scripts/run_dashboard.sh` uses). No auth, no CORS, no TLS. If the operator ever exposes it remotely, that's a future structural change (likely Tailscale-network-bound, not console-bound) — but explicitly out of scope here.

### 6.13 Mobile / responsive

The handoff README is desktop-first (1700px breakpoints, 1280px breakpoint for the activity rail). **No mobile support.** Operator works on a Mac. Adding mobile would force a second navigation pattern + a second density mode + a second activity rail UX — pure scope creep. Mark as YAGNI; flag explicitly in the spec.

### 6.14 Accessibility

The handoff doesn't speak to a11y. **No ARIA, no keyboard-screen-reader pairing, no high-contrast mode.** Single-operator-localhost is the user model; pretending otherwise wastes effort. Single keyboard-shortcut layer (the design's `O / F / 1-5 / L / E / A / H / W / D / P`) is implemented; tab-order is left to default DOM order. Flag explicitly in the spec; the operator can override.

---

## §7 Project-tracking surface

### TODO.md update

After this brainstorm merges, `TODO.md` gets a new H2 under master sequence §5:

```markdown
## #189 — Dashboard Refactor (master sequence §5, in-flight)

Brainstorm: docs/superpowers/brainstorms/2026-05-21-189-dashboard-refactor-brainstorm.md
Spec: docs/superpowers/specs/2026-05-22-189-dashboard-refactor-design.md (TBW after brainstorm review)

Status: brainstorm shipped 2026-05-21; awaiting operator open-question
decision (§8) before spec.

Milestones (M0–M12, target ~2 PRs/week → ~6 calendar weeks):
- [ ] M0 — Scaffolding (Vite + FastAPI sentinel + Storybook atoms)
- [ ] M1 — Health view (FIRST OPERATOR VALUE)
- [ ] M2 — Overview view
- [ ] M3 — Activity rail (polling; WebSocket upgrade conditional)
- [ ] M4 — Ticker drill-in
- [ ] M5 — Engine Detail (Momentum) + heatmap
- [ ] M6 — Engine Detail (Reversion / Vector / Sentinel / Canary)
- [ ] M7 — Data Pipeline view
- [ ] M8 — Allocator + Forensics
- [ ] M9 — Providers
- [ ] M10 — Weekly Digest + LLM Triage
- [ ] M11 — Lab + Engine SDLC
- [ ] M12 — Streamlit retirement (gate: ≥ 5 trading days new-console green)

Risk register (from §6):
- Activity rail polling load (mitigation: 5s + rate limit; WS upgrade if hot)
- JSON-endpoint pool burst on view switch (mitigation: pool 10→20)
- Frontend tests CI-deferred (mitigation: operator pre-PR Make targets)
- Streamlit deprecation gate is operator-discretionary (≥ 5-day soak)
- Adjacent gaps (param-search persistence, sentinel bear-score log,
  ECR_SUBMITTED event, lab.list_runs accessor) — fall-back paths
  documented; #189 doesn't block on them.
```

### MASTER_PLAN.md

Today the dashboard refactor isn't a row in §9 Build Order — it's owned via `.claude/rules/dashboard.md` + the master sequence. The brainstorm doesn't add a Build Order row; the spec PR can decide whether the table needs one.

---

## §8 Single open question for operator

> **Do we ship the new console on a different port (8502; parallel-deploy until ≥ 5 trading days green) — *or* do we rip-and-replace `dashboard.py` at the existing port (8501) the moment M11 lands, accepting that any missing-capability regression is a live operator problem?**
>
> The brainstorm recommends parallel-deploy (§4). The trade-off is: parallel-deploy adds ~6 weeks of "both consoles open in two tabs" overhead but eliminates rollout risk; rip-and-replace ships faster but bets the operator's daily-driver UX on a 12-PR rollout without a soak period.
>
> Everything else in this brainstorm is author-decided.

(Author-decided items the operator does NOT need to weigh in on, for explicitness:
React + Vite + FastAPI + TanStack Query, 12-milestone rollout, Health view first, custom-SVG charts no chart-lib, polling activity rail with WS-upgrade conditional, frontend tests CI-deferred to operator-pre-PR Make targets, drop the TweaksPanel, drop Lab Queue panel until SoT exists, no auth, no mobile, no a11y. If the operator disagrees with any of those, the spec absorbs the change; the §8 question is only the parallel-vs-rip choice because it's the one with no obviously-right answer.)
