# Operator Dashboard

- **Path**: `docs/superpowers/specs/2026-05-13-operator-dashboard.md`
- **Version**: 1.0
- **Date**: 2026-05-13
- **Status**: approved — ready to build
- **Referenced from**: `docs/MASTER_PLAN.md` (Research Tools subsection), `docs/OPERATIONS.md`

---

## Sequencing decision (2026-05-13 expert review)

Both this spec and the **Rolling-Momentum Construction** spec (`2026-05-13-momentum-rolling-construction.md`) are approved. They are sequenced **Dashboard first, Rolling-Momentum after June 1**, on three independent grounds:

1. **Hard blocker**: Rolling-Momentum Phase 3 is explicitly out-of-scope until June 1, 2026 — the running monthly-rebalance paper experiment must accumulate one full cycle of OOS evidence on the validated construction before any change is considered. Dashboard has no such blocker.
2. **Compound value**: when Rolling-Momentum's Phase 3.0 backtest runs, its validation gate produces a side-by-side rolling-vs-monthly comparison. The Dashboard's Credibility Scorecards + Recent SIGNAL panels are *the right place* to inspect that comparison. Building the dashboard first means the inspection surface is ready when the verdict arrives.
3. **Risk asymmetry**: Dashboard is presentation-only (no business logic, no live-capital code path). Rolling-Momentum modifies the only working engine and demands re-validation of the +1.58 Sharpe artifact. Executing a strategy change through a brittle 8-script control surface is double-stacking risk; the dashboard reduces operating-surface risk before the more substantive change lands.

## What this is

A single-page local web dashboard that replaces the operator's current pattern of running ~8 separate `scripts/run_*.sh` files. Runs on the operator's Mac via `streamlit run dashboard.py`; opens a browser tab. Read-mostly view of system state with action buttons for the common operations.

**Not** a public-facing product. **Not** a hosted service. **Not** a Phase 2 publication tool. It's a research console for one operator.

## Stack — finalized

| Layer | Choice | Why |
|---|---|---|
| Web framework | **Streamlit** (pin `<1.50`) | Python-only, ~3 hours from zero to working UI, no JS/HTML stack |
| Financial charts | **`streamlit-lightweight-charts-pro`** | Only Streamlit-compatible wrapper of TradingView Lightweight Charts with first-class trade-marker API (active, last release Nov 2025) |
| Tabular data | `st.dataframe` with `column_config` | Native, sortable, no extra dep |
| Equity curve / simple lines | `st.line_chart` native (Phase 1) | Defers chart-library risk; upgrade to lightweight-charts later if needed |
| Subprocess (short) | `subprocess.run(timeout=120)` | Blocking; user sees output streamed to `st.code()` |
| Subprocess (long ≥10min) | `subprocess.Popen([...], start_new_session=True)` + logfile tail | Detached so Streamlit worker recycles don't SIGTERM the job |

**Explicit non-choices**:
- ❌ `streamlit-lightweight-charts` (the freyastreamlit original) — abandoned May 2023; do not build on it.
- ❌ Plotly as primary — works but loses native trade markers; kept as documented fallback inside the chart adapter.
- ❌ Auto-refresh as primary — `st.autorefresh` + custom components causes browser memory bloat after hours of running. Manual refresh button is the default; auto-refresh is opt-in at 30-60s, never lower.

## Adapter pattern — chart-library replaceable

All chart rendering goes through one module: `dashboard_components/charts.py`. Public API:

```python
def render_ticker_chart(ticker: str, ohlc: pd.DataFrame, fills: list[Fill]) -> None: ...
def render_equity_curve(equity_history: pd.DataFrame) -> None: ...
```

Inside, the implementation calls `streamlit-lightweight-charts-pro`. If the package breaks on a future Streamlit bump (single-maintainer 0.1.x package — realistic risk), swap the body of these two functions for Plotly equivalents. One-file change, no caller updates.

## Layout (single page, top to bottom)

```
┌───────────────────────────────────────────────────────────────────────┐
│  HEADER  (sticky)                                                     │
│  equity $99,989  |  cash $99,236  |  positions 1  |  today P&L $-2.72│
│  [Manual refresh]  [Auto-refresh 30s ☐]                              │
├───────────────────────────────────────────────────────────────────────┤
│  PLATFORM HEALTH                                                      │
│  Bars (prices_daily)   🟢 Latest 2026-05-12 (1d) — 7,323 tickers     │
│  Fundamentals          🟢 Last refresh 0.5d ago — period 2026-Q1     │
│  Corporate actions     🟢 Latest ingest 0.4d ago                     │
│  Universe (momentum)   🟢 Today: 1249 candidates                     │
│  Last ops --update     🔴 2 stage(s) FAILED, 4/6 OK   [▶ stage detail]│
│  Data validation (7d)  🔴 Repeated failures           [▶ detail]      │
├───────────────────────────────────────────────────────────────────────┤
│  ACTIONS  (5 buttons + log pane)                                      │
│  [Run daily update]  [Force-rebalance Momentum]  [Refresh credibility]│
│  [Smoke test]  [Cancel all open orders]                              │
│  ┌─ log output ─────────────────────────────────────────────────────┐ │
│  │ [most recent stdout/stderr from the last action]                 │ │
│  └──────────────────────────────────────────────────────────────────┘ │
├───────────────────────────────────────────────────────────────────────┤
│  CURRENTLY HOLDING  (Momentum)                                        │
│  table: ticker / qty / entry / current / pnl_$ / pnl_%  (sortable)   │
├───────────────────────────────────────────────────────────────────────┤
│  TICKER DETAIL  (collapsible, click a row in Currently Holding)       │
│  candlestick chart, last 90 days, entry/exit markers from AARs       │
├───────────────────────────────────────────────────────────────────────┤
│  EQUITY CURVE  (last 60 days, from EQUITY_SNAPSHOT events)            │
│  line chart                                                           │
├───────────────────────────────────────────────────────────────────────┤
│  CREDIBILITY SCORECARDS  (one per engine)                             │
│  ┌─ Momentum 40/100 BLOCKED ┐ ┌─ Sigma 55/100 BLOCKED ┐ ...           │
│  │ rubric checklist          │ │ rubric checklist        │            │
│  └───────────────────────────┘ └─────────────────────────┘            │
├───────────────────────────────────────────────────────────────────────┤
│  TODAY'S RECOMMENDATIONS (Momentum)                                   │
│  table: rank / ticker / score / last_close / tier                    │
├───────────────────────────────────────────────────────────────────────┤
│  RECENT SIGNALS + AARs  (last 30 days, side by side)                  │
└───────────────────────────────────────────────────────────────────────┘
```

## Subprocess pattern — explicit

Two patterns; one per script class.

### Pattern A — Short script (smoke, refresh-credibility, cancel-all-orders)

```python
result = subprocess.run(
    [script_path], capture_output=True, timeout=120, text=True,
)
st.code(result.stdout + result.stderr, language="bash")
```

Blocking, ≤120s timeout, output rendered inline. Operator expects to wait.

### Pattern B — Long script (`run_daily_update.sh` 30-45 min)

```python
import subprocess, os, datetime as _dt
logfile = f"/tmp/dashboard_{script_name}_{_dt.datetime.now():%Y%m%d_%H%M%S}.log"
proc = subprocess.Popen(
    [script_path],
    stdout=open(logfile, "w"),
    stderr=subprocess.STDOUT,
    start_new_session=True,  # CRITICAL — detaches from Streamlit's process group
)
st.session_state[f"running_{script_name}_pid"] = proc.pid
st.session_state[f"running_{script_name}_logfile"] = logfile
```

Then on subsequent reruns, tail the logfile into a `st.code()` block and check `proc.poll()` for completion. Operator can close the browser tab; job keeps running.

## Build phases (per expert ordering — operational payoff first)

### Phase 1 — Skeleton (~3 hours)

* `dashboard.py` (single file)
* Header (equity / cash / positions / P&L)
* Currently-holding table — `st.dataframe`, reuses `fetch_engine_holdings` from `scripts/generate_tip_sheet.py`
* Equity curve — `st.line_chart` from `EQUITY_SNAPSHOT` events in `application_log` (native, defers chart-lib install)
* Manual-refresh button only — no auto-refresh yet
* No actions, no per-ticker chart, no credibility cards — just the read view

Ship in one session. Validates the framework works against the live data layer.

### Phase 2 — Action buttons (~1-2 hours, highest operational payoff)

Five buttons. Pattern A for the short four; Pattern B for `run_daily_update.sh`. Status panel for the long-running job. Confirmation modal for `force-rebalance` and `cancel-all-orders`.

After this phase, **the 8 separate shell scripts are no longer the primary interface.** The dashboard is.

### Phase 3 — Per-ticker chart with entry/exit markers (~1-2 hours)

Install `streamlit-lightweight-charts-pro`. Build `render_ticker_chart` adapter. Wire to the holdings-table row-click: select a ticker, drill in to see its 90-day candles with AAR-derived entry/exit markers.

This is where the chart library earns its keep. Generic tools can't render trade markers on a candlestick chart cleanly; `lightweight-charts-pro` does it as a first-class API.

### Phase 4 — Credibility scorecards + recommendations + signals/AARs feeds (~1 hour)

`st.dataframe` panels for each. Reuse the existing tip-sheet query functions verbatim.

### Phase 5 — Auto-refresh + polish (~1 hour, optional)

`streamlit-autorefresh` opt-in at 30-60s. Theme tweaks. Persistent `st.session_state` for selected ticker.

### Deferred (don't build now)

* Sector concentration view (no taxonomy yet — see momentum Phase 2.5 #4)
* Engine-side comparison view (only Momentum is live)
* Trade-by-trade replay tool
* User authentication / multi-tenant (single operator, local Mac)

## Dependency constraints

Add to `pyproject.toml`:

```
streamlit<1.50,>=1.30
streamlit-lightweight-charts-pro>=0.1.8
```

Optional later: `streamlit-autorefresh` for Phase 5.

Pin Streamlit `<1.50` because `lightweight-charts-pro` is at 0.1.x and minor-bump compatibility isn't proven. Reassess on each pro release.

## HCI requirements (added per 2026-05-13 expert review)

Per Nielsen's usability heuristics, Norman's gulfs of execution / evaluation, and Fitts' Law. These are not polish; they materially affect operating discipline.

### Visibility of system status (heuristic #1)

* **Data-freshness timestamp on every panel**: "data as of HH:MM:SS" — manual-refresh-default silently shows stale numbers without it.
* **Long-running job heartbeat**: detached `Popen` jobs must surface last-log-line + elapsed time + logfile-mtime. Stale indicator (no log update for >5 min) renders amber; >15 min renders red. Without this, the operator can't distinguish "still running" from "silently dead."
* **Header P&L formatting**: show absolute *and* percent (`$-2.72  (-0.003%)`). Pure-dollar reads differently than pure-percent; both anchor differently.
* **Platform-health panel (added 2026-05-13)**: positioned *between* the header and the Actions panel — the operator sees data freshness + last-update status *before* being tempted to push a button. Six rows, each glyph + color + short string:

  | Row | Signal | Green / Amber / Red thresholds |
  |---|---|---|
  | Bars (`prices_daily`) | `MAX(date)`, `COUNT(DISTINCT ticker)` | ≤1d / 2-3d / ≥4d |
  | Fundamentals | `MAX(recorded_at)` from `fundamentals_quarterly` | ≤8d / ≤14d / >14d |
  | Corporate actions | `MAX(recorded_at)` from `corporate_actions` | ≤2d / ≤7d / >7d |
  | Universe (momentum) | `COUNT` for today + `MAX(as_of_date)` | today's count ≥500 / today missing / never populated |
  | Last `ops --update` | Per-stage `INGESTION_COMPLETE` vs `INGESTION_FAILED` events from the newest run's `run_id` | all 6 OK / some missing / any failed |
  | Data validation (7d) | `data_quality_log` rows where `stale OR confidence<1.0` | 0 failed / ≤2 failed / ≥3 failed |

  The last two rows expand to a per-stage / per-source detail table — auto-expanded when red so the operator sees the failure without having to click.

  The expected stage list is the constant `_OPS_UPDATE_STAGES` in `dashboard.py`; keep it in lockstep with the `cmd_update` orchestrator in `scripts/ops.py`. If a stage is added/removed there, update the constant — the panel will otherwise show false "missing" amber rows.

### Error recovery distinct from normal output

* Subprocess non-zero exit code: render in a red-bordered panel with the return code displayed prominently. Don't append stderr into the same `st.code()` block as stdout — that's how operators miss failures.
* If the process is killed externally (SIGTERM not from us), surface that too — `proc.returncode == -15`.

### Confirmation modals — typed, not Yes/No

Generic Yes/No invites muscle-memory click-through. For destructive actions:

* **Force-rebalance**: modal shows "About to recompute and submit ~54 orders against ~$99,000 of paper capital. Type REBALANCE to confirm." Free-text input must match exactly (case-sensitive).
* **Cancel all open orders**: modal shows "About to cancel N open orders (M sells totaling $X, K buys totaling $Y). Type CANCEL to confirm."
* Pre-fill the dollar amount and position count *from live broker data*, not hard-coded strings. If the broker can't be reached, the modal won't render — fail-safe.

### Undo / recall affordance

After a successful order-submission action (force-rebalance, etc.), render a 5-second "submitted — click to recall any orders not yet filled" affordance. Hooks to `broker.cancel_order()` on each broker_order_id from the submission. After Alpaca's fill window (typically ~seconds for market-open orders), the affordance is no-op but the operator at least sees they had a recourse.

### Information hierarchy

* P&L color-coding: green / red are not enough — pair with arrow glyph (▲ for positive, ▼ for negative) for accessibility (WCAG AA contrast fails for some operators on Streamlit's default red/green).
* Header is the most important — pin sticky, bold the dollar values, smaller font for the cash and position-count secondaries.
* Action buttons in a single fixed cluster at top (Fitts' Law — predictable target locations). Destructive actions (force-rebalance, cancel-all) styled distinctly from safe ones (smoke test, refresh).

### Keyboard shortcuts

* `r` — manual refresh
* `Esc` — dismiss any open modal
* These are cheap to add via Streamlit's `st.shortcut` or a custom JS component. Halves common-action time.

### Accessibility

* Color + glyph on every red/green signal (P&L, gate status, signal direction).
* Don't rely on color alone for any status indicator.

## What this dashboard does NOT do

* No order entry. Period. Operator triggers rebalances via the action buttons; the buttons call existing schedulers; the schedulers contain all the business logic. Dashboard is presentation + dispatch only.
* No business logic. Every value displayed is read from the existing DB tables or fetched via existing helper functions (`fetch_engine_holdings`, `fetch_credibility`, etc.).
* No new persistence. State that survives across page loads lives in `platform.application_log` (`EQUITY_SNAPSHOT`, `SIGNAL`, `ORDER_SUBMITTED`) — same schema the tip sheet already reads.
* No public exposure. Streamlit runs on `localhost:8501` by default; do not bind `0.0.0.0`.

## Acceptance criteria (Phase 1+2 complete = MVP)

- [ ] `streamlit run dashboard.py` opens a browser tab to a working page
- [ ] Header shows current Alpaca paper-account state
- [ ] Currently-holding table populates from broker
- [ ] All five action buttons function: smoke test (blocking), force-rebalance (blocking with confirm), refresh credibility (blocking), cancel-all-orders (blocking with confirm), daily update (detached + logfile tail)
- [ ] Long-running daily-update job survives closing the browser tab
- [ ] `pytest scripts/tests/ momentum/tests/ tpcore/tests/` still 100% green (dashboard adds no regressions)
- [ ] Streamlit `<1.50` pinned in `pyproject.toml`
