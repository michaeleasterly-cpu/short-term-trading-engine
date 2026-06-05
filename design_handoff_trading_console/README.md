# Handoff: STE Operator Console

## Overview

A high-fidelity operator console for the **short-term-trading-engine** platform (STE) — a multi-engine automated trading platform. Replaces the existing single-page Streamlit dashboard with a denser, multi-view operator UI covering:

- Live portfolio overview (equity, P&L, holdings, signals, AARs)
- Per-engine detail (Momentum / Reversion / Vector / Sentinel / Canary), including credibility gates, parameter heatmaps, and the Sentinel Bear-Score timeline
- Ticker drill-in with candlestick chart + entry/exit markers + trade ledger
- **The Lab** (SDLC SP2) — walk-forward parameter-search sandbox with isolation contract surfacing
- **Engine SDLC / ECR Queue** — operator approve/reject for ADD / MODIFY / RETIRE change requests + lifecycle map
- **Weekly Digest** — non-skippable ack flow + LLM triage proposals (data lane + engine lane, two co-tasks)
- **Health** — Escalation & Hardening Ladder rungs, Data Supervisor holds, cross-table auditheal, recent escalations, daemon topology
- **Data Pipeline** — 13-check validation suite + self-heal log
- **Allocator** — inverse-vol capital weights, current vs target drift
- **Providers** — feed/provider bindings (Provider Lifecycle)

## About the Design Files

The files in this bundle are **design references created in HTML/React via in-browser Babel**. They are **prototypes showing intended look and behavior, not production code to copy directly**.

The repo you're integrating into (`short-term-trading-engine`) currently has a Streamlit dashboard (`dashboard.py` + `dashboard_components/`). The task is to **recreate these designs in whatever framework you choose for the new operator console**. Streamlit is unlikely to be a good fit — this design assumes a real client-side stack. Reasonable choices:

- **React + Vite/Next.js** with a thin FastAPI read-only JSON layer on top of the existing `asyncpg` queries already in `dashboard.py` / `dashboard_components/health.py` / `dashboard_components/escalation.py`. This is the path of least resistance — `dashboard_components/*.py` are already pure Streamlit-free classifiers returning `(color, summary, detail)` tuples, so they port cleanly behind a JSON endpoint.
- **HTMX + Jinja** if you want to stay closer to server-rendered and reuse the Python helpers verbatim.
- **SvelteKit** if you want a smaller bundle than React.

Whatever you pick, the **read-only-renderer principle** from the spec (`docs/superpowers/specs/2026-05-18-dashboard-escalation-audit-panel-design.md`) is non-negotiable: never recompute a predicate in the frontend — render the existing SoT (the same rows `weekly_digest.build_weekly_digest()`, `tpcore.datasupervisor.list_open_holds()`, `engine_ladder.list_undispositioned()`, and the `*classify_*` functions already produce). The HTML uses synthetic mock data in `trading-console/data.jsx`; in production every dataset listed there maps to a real query that already exists.

## Fidelity

**High-fidelity.** Pixel-perfect mockups with final colors, typography, spacing, and interactions. Recreate the UI pixel-perfectly using the codebase's chosen libraries. The CSS in `Trading Console.html` is the canonical source for design tokens — copy values out, do not handwave them.

## Aesthetic system

**Bloomberg-inspired operator console.** Dense, mostly monochrome warm-neutral palette, monospace-led typography, semantic color **only** for state (green = pos / red = neg / amber = warn). 1px hairline borders, no decorative shadows, no rounded corners larger than 4px.

- **Type pairing**: JetBrains Mono (all numeric/data) + IBM Plex Sans (UI labels, headings)
- **Default theme**: `operator-dark` (warm dark neutrals)
- **Alternate themes**: `midnight` (cool blue-tinted dark), `paper` (light)
- **Density**: `comfortable` (default) and `compact`
- **Accent**: amber (default), green, cyan, violet

All themes are defined as CSS custom properties on `:root` / `.theme-*` in `Trading Console.html`. Use `oklch()` everywhere — do not translate to hex or hsl.

## Navigation structure

Left rail, 208px wide, grouped sections with single-letter keyboard shortcuts. Active item gets an accent-colored left border + filled background. Engine items get a colored dot per engine identity color. Action-required items show a colored count badge.

```
Portfolio
  ◉ Overview         O
  ◉ Forensics        F   [badge: trigger count if med/high]

Engines (Live)
  • Momentum         1   tone=mom (amber)
  • Reversion        2   tone=rev (teal)
  • Vector           3   tone=vec (violet)
  • Sentinel         4   tone=sen (red-orange)
  • Canary           5   tone=can (yellow-green)

Engine SDLC
  ◉ The Lab          L   [badge: pending promotions]
  ◉ ECR Queue        E   [badge: pending ECR count]

Capital
  ◉ Allocator        A

Operations
  ◉ Health           H   [badge: open holds + open escalations]
  ◉ Weekly Digest    W   [badge: 1 if unacked]
  ◉ Data Pipeline    D
  ◉ Providers        P
```

Below the nav, a footer block shows `capital / unallocated / heartbeat` (pulsing dot).

A top bar runs across the full width: brand mark + name (left), centered status strip (NYSE / BROKER / DATA / RISK / LIVE — each a dot+label+state), P&L pill + equity + UTC clock (right).

Right activity rail (300px, hideable via Tweaks toggle, hidden under 1280px) shows a live event stream: HEAL / SIGNAL / AAR / ALLOC / DIGEST / LLM / HOLD / ALERT / SYSTEM events, each tagged with its kind in a colored chip.

## Screens / Views

For every view, render a `<ViewHeader>` at the top: small uppercase eyebrow, large 24px title (with optional inline subtitle), a meta row of key/value pairs (e.g. `state · PAPER_TRADING`, `rebalance · monthly`), and right-aligned action buttons. The breadcrumb (e.g. `OPERATIONS › THE LAB`) lives above the view in the main container.

### 1. Overview

- **KPI strip** (8 tiles at ≥1700px, 4 below): Equity, Day P&L, Unrealized P&L, YTD P&L, Cash, Buying Power, Open Positions, Trades Today. Each tile: tiny uppercase label, 20px mono value, 11px mono sub-line (e.g. `+1.51% today`). Value tinted green/red/amber when signed.
- **Equity curve** panel (320 sessions, ~240px height) with an SPY benchmark line dashed in `--ink-3`. Engine equity is solid accent color + 8% opacity area fill. 4 horizontal grid lines (`$Xk` labels). 5 evenly-spaced date ticks. Hover dot at the latest value. Header right has a 4-button segmented `30d / 90d / 1y / all` (third selected).
- **Engines grid** (5-up at ≥1600px, 3-up below). Each card: top eyebrow row (status dot + uppercase engine name + GRADUATED/GATED/HEARTBEAT pill on right), 11px italic kind, 6-stat grid (credibility / OOS Sharpe / DSR / positions / capital / alloc) in mono, a credibility gate-bar at the bottom. Canary's card shortens to 3 stats and replaces the gate-bar with a short italic note. Cards have a 2px top border in their engine color. Hover background `--bg-2`.
- **Bottom split (2/3 + 1/3)**: Holdings table (left) + stacked Today's Signals + Recent AARs (right).
  - **Holdings table**: sortable columns Engine / Ticker / Qty / Entry / Last / P&L / P&L% / Wgt / Held. Engine column is a 3-letter pill. Short positions get a small red `S` chip before the ticker. Rows are clickable → ticker drill-in.
  - **Signals**: feed rows — engine pill on left, ticker + side chip + note, strength bar + time on right. Rows with `BLOCKED` in the note get 50% opacity.
  - **AARs**: feed rows — engine pill + ticker + side chip + exit_reason eyebrow + dates/hold/qty/prices on a second line, P&L% on right tinted green/red.

### 2. Engine Detail (Momentum / Reversion / Vector / Sentinel / Canary)

- ViewHeader with eyebrow `ENGINE · <ID>` + status dot, title `<Name> — <kind>`, meta row (`state / rebalance / last / next`), right-side actions: "Run engine now" (primary, hot) + "Open backtest →".
- **Canary** gets an info banner: "Canary is intentionally non-graduating." It skips the gates panel entirely.
- **Credibility & graduation gates** panel (left, takes 1.6fr of a 1.6fr/1fr split): 6 horizontal `<GateRow>`s — each is a 200px label + a 5px-tall progress bar (showing current value as a fill, with a 2px vertical line at the threshold), and a right-aligned `value / threshold` reading. The fill is green if passed, amber if failed. Use the bar to convey "how far from threshold" at a glance. Below the rows, a single-line summary: pill (PASSED/GATED) + muted reason.
- **Best trial parameters** panel (right, 1fr): a 2-column key/value table.
- **Momentum** gets a parameter-search heatmap panel. Grid of `lookback_days × hold_days` cells, each colored by OOS Sharpe (diverging palette: cool blue for negative, warm amber for positive). Cell shows the numeric value. Legend below: gradient + N trials + best Sharpe.
- **Sentinel** gets a 180-day Bear Score timeline: line chart, y-axis 0-100, dashed warn line at activation threshold (60), accent-color area fill below the line.
- **Bottom 2-column split**: Active positions table + Recent AARs feed (engine-filtered).

### 3. Ticker Drill-In

- ViewHeader with eyebrow `TICKER`, title `<TICKER> <last-px>`. If currently held: engine pill + LONG/SHORT pill (with qty) + P&L% on the right.
- **Candle chart** (320px) with green/red candles (up = pos, down = neg), grid lines + price labels, x-axis date labels. Entry/exit markers as small filled triangles (up-pointing green for long entry, up-pointing red for short entry, down-pointing for exits), with a dashed vertical line through the marker and a labeled text marker beside it (e.g. `LONG $184.10` or `tier2_target`). Legend row below the chart explains the markers.
- **Bottom 2-column split**: Trade ledger table (open positions first with `— open —` exit) + signal-context kv panel.

### 4. The Lab

- ViewHeader with eyebrow `OPERATIONS`, title `The Lab`, subtitle explaining SDLC SP2 + LabContext + Lab-namespaced credibility. Meta row: `runs (30d) / survived / failed / pending promotion / queued`. Actions: "New Lab run" (primary) + "Open Lab dossiers".
- **Info banner** at the top (⚗ icon, accent-colored): "The Lab is fully isolated from live trading. Every guarded constructor… raises `LabIsolationViolation` inside an active `LabContext`. Credibility writes are Lab-namespaced (`backtest_credibility.lab.<candidate>`) and never pollute the live capital gate." Inline code styling for the namespace literal.
- **Two-column split** (1fr / 1.5fr):
  - Left: **Recent runs** panel as a vertical list. Each row: engine pill + `lab.<candidate>` name on the left (subline: date + seed + duration), SURVIVED/FAILED pill + `DSR 0.961` on the right. Selected row gets an accent left border + background highlight.
  - Right: **Run detail** — selected run.
    - First panel: title `lab.<candidate>`, header action "Promote → ECR" when SURVIVED + pending. Verdict line: SURVIVED/FAILED pill + muted "DSR ≥ 0.95 ✓ credibility ≥ 60 ✓ — eligible to promote" or failure note.
    - **5-up Lab KPI strip**: DSR / Final Sharpe / Credibility / Trials / Isolation violations. Each: tiny label, 17px mono value, 10px threshold reminder (e.g. `≥ 0.95`).
    - kv-pair meta block: `namespace`, `dossier` (link), `note`.
    - **Best parameters** panel — kv table.
    - **Walk-forward windows** panel — bar chart, one bar per window, height = DSR, colored green if ≥ 0.95 else amber, with the 0.95 gate as a dashed warn-line and "DSR gate" label. Below the chart, a table: Window / Holdout / N trades / Sharpe / Credibility / DSR.
- **Queued candidates** panel at the bottom — table: Candidate / Engine / Queued / Note / "Run now →" action.

### 5. Engine SDLC / ECR Queue

- ViewHeader with eyebrow `OPERATIONS`, title `Engine SDLC`, subtitle "Engine Change Requests · binary y/n on ADD / MODIFY / RETIRE · auto for CUTOVER / EVALUATE".
- **Pending change requests** — vertical card list. Each card:
  - Header row: kind pill (ADD/MODIFY/RETIRE) + engine pill + uppercase action name + VALIDATED pill on the left; submitted-by/submitted-when muted on the right.
  - 13px summary line.
  - **DIFF block** — tiny `DIFF` label + monospace code block with the diff string.
  - Lab dossier link (if applicable, links to The Lab view).
  - Action row: "Approve →" (primary hot) + "Reject" + "View full diff".
- **Recent decisions** panel — table at 55% opacity (resolved style): Decided / Kind / Engine / Action / Verdict / Diff.
- **Engine lifecycle map** — 4-column grid (LAB / PAPER / LIVE / RETIRED), each column tinted with that state's color (LAB amber, PAPER teal, LIVE green, RETIRED muted). Header row: state name + count. Stack of engine name pills below (clickable → engine detail or Lab). Empty columns show muted "— empty —".

### 6. Weekly Digest

- ViewHeader with `OPERATIONS / Weekly Digest`, subtitle `Week of <date> · generated <ts> UTC`. Meta row: `weeks unacked / threshold / live clearance`. Action area: either an "ACKNOWLEDGED" pill or an "Acknowledge week" primary button.
- **Warning banner** (only if unacked): ⚠ icon + title "This week's digest needs your acknowledgment." + sub "Two consecutive unacked weeks automatically de-escalate live trading clearance." + right-side Acknowledge button.
- **Digest sections grid** — auto-fill at 360px min, gap 10px. Each card collapses to its header by default; `undispositioned` and `adversarial` open by default. Header: caret + section label + count pill. Open body: list of items, each item is mono 11.5px with a dashed bottom border. Section tone: `warn` for undispositioned + adversarial, neutral otherwise.
- **LLM triage proposals** panel — list of triage cards (see below).
- **Ack history** table — current week + dimmed history rows.

### 7. LLM Triage Card

A bordered card inside the Digest panel, one per open proposal. Two lanes, each with a 3px-thick colored left border:
- **DATA LANE** (teal `oklch(72% 0.14 200)`) — for data-lane escalations
- **ENGINE LANE** (amber `oklch(74% 0.16 60)`) — for engine-lane escalations

Card layout:
- Header row, split:
  - Left: lane pill + `ref <id>` + `· class <cls>`, then a title line: `Proposed: <disposition>` (warn-colored bold) + confidence %.
  - Right: model + persona_version muted, "draft (human review required)" warn pill, "CI fence: `<fence-name>`" small mono.
- Rationale paragraph: 12.5px body text in a quote-style block with a 2px accent left border.
- Action row: View PR / Override disposition / Reject proposal.

### 8. Health

- ViewHeader with eyebrow `SYSTEM`, title `Health`. Meta: live clearance / weeks unacked / daemons live.
- **6-KPI strip**: Open holds / Open escalations (7d) / Undispositioned / Cross-table audit / LLM proposals open / Self-heal cycles 24h. Tints green/warn based on counts.
- **Escalation & Hardening Ladder** panel — 5 rows (R1–R5), each: large 18px rung number (color: green = covered, amber = open/pending, accent = info/active), name (13.5px), 11.5px detail line, on the right a status pill + count.
- **2-column split**:
  - Data supervisor open holds: source / held / cycles / reason / esc-pill. Cleared sources show as dimmed rows with AUTO/OPERATOR pills.
  - Cross-table audit (auditheal): source / state pill / last time / note.
- **Recent escalations (7d)** table: when / type / ref / class / status (OPEN/RESOLVED) / message / LLM link.
- **Daemon topology** table: daemon / lane (engine = amber, data = teal, advisory = neutral) / PID / uptime / last heartbeat / status pill / role. 4 rows total. The `llm_triage_service` row must mention "two crash-isolated co-tasks (data-lane + engine-lane)" in the role field.

### 9. Data Pipeline

- ViewHeader with `SYSTEM / Data Pipeline`, meta `last update / cycle latency / self-heal`. Actions: "Run data update" (primary) + "Run validation" + "Audit pipeline".
- 8-KPI strip (passed/warnings/failed/DATA_OPS event/confidence/tickers tracked/daily bars/forensics).
- **Validation suite** table: 13 checks. Columns: Check / Status pill / Rows / Age / Notes.
- **Self-heal log** table: time / stage / result pill / duration / notes.

### 10. Allocator

- ViewHeader `SYSTEM / Allocator`. Meta: `method / trigger / last / next`. Action: "Force rebalance" (primary).
- **Allocation bar** — horizontal flex bar 36px tall, one segment per engine + cash, sized by current weight. Each segment displays its engine name + percentage in mono, colored in its engine identity color (cash is `--bg-3`).
- **Drift table** — Engine pill / Current % / Target % / Drift % (signed, tinted) / 30d vol / BALANCED-vs-DRIFT pill.

### 11. Forensics

- ViewHeader `SYSTEM / Forensics`. Meta: `AAR scanner / runs / last`. Actions: "Re-scan now" + "Open sprint dossiers".
- **Active triggers** table: time / engine pill / severity pill (LOW/MED/HIGH) / trigger name / detail message.

### 12. Providers

- ViewHeader `SYSTEM / Providers`, subtitle "Data Provider Lifecycle · feed/provider decoupled via ProviderBinding registry". Action: "Open feed change request".
- **Provider bindings** table grouped by feed. Columns: Feed / Provider / Status pill (ACTIVE / FALLBACK / DEPRECATED) / Since / Parity %.

## Interactions & Behavior

- **Keyboard shortcuts** — single letters listed in the nav (`O`, `F`, `1-5`, `L`, `E`, `A`, `H`, `W`, `D`, `P`). Mod-key combinations are ignored. Letters typed inside `<input>`/`<textarea>` are ignored.
- **Routing** — internal state `route` string. Routes: `overview`, `engine:<id>`, `ticker:<sym>`, `data`, `forensics`, `allocator`, `health`, `digest`, `providers`, `lab`, `sdlc`. In production wire this to URL hash or pathname.
- **Holdings rows** — clickable, navigate to `ticker:<sym>`.
- **Signal/AAR rows** — clickable, navigate to `ticker:<sym>`.
- **Engine cards** — clickable, navigate to `engine:<id>`.
- **Tweaks panel** — when the host turns on Tweaks (via `__activate_edit_mode` postMessage), render a draggable panel bottom-right with: Theme radio, Accent swatch picker, Density radio, Mono font select, Activity feed toggle, Equity benchmark line toggle. Persist via `__edit_mode_set_keys` postMessage. Defaults wrapped in `/*EDITMODE-BEGIN*/{...}/*EDITMODE-END*/`. The Tweaks system is for the design prototype — in production replace with a real settings page or drop entirely.
- **Animation** — heartbeat dot pulses (1.2s `opacity` cycle 0.4 → 1 → 0.4). No other motion. Hover transitions on rows/cards are instant — no transition timing.
- **Clock** — top bar UTC clock ticks every 1s. In production read actual system time, not the synthetic `setNow` advance used in the prototype.
- **Live data** — every dataset in `trading-console/data.jsx` is synthetic. In production, fetch from the real backend (see "Data wiring" below).

## State Management

The prototype uses React `useState` for everything. There's no global store, no router library, no fetching. For production:

- **Route state** — use the platform router (React Router, TanStack Router, SvelteKit's filesystem router, etc.). The route → view mapping lives in `app.jsx` `render()`.
- **Tweaks state** — replace with a real user-preferences store. The current `useTweaks` hook just persists to the host via postMessage and is design-time only.
- **Server state** — use TanStack Query (or your framework's equivalent). Every view's data is read-only; cache aggressively. Most data has a natural refresh interval (15–60s for live state; 1–24h for static state). Recommended invalidation:
  - **Account / Holdings / Signals / Activity** — 15s
  - **Engines / Allocator / Risk** — 60s
  - **Health / Escalations / Holds / Cross-table audit** — 60s (read off `data_quality_log` + `application_log`)
  - **Weekly Digest** — 5min (already a weekly artifact)
  - **Lab runs / ECR queue / Providers** — 5min
  - **Validation suite / Self-heal log** — 60s
  - **Daemons** — 30s

## Data wiring (the part the backend actually owns)

Every dataset in `trading-console/data.jsx` corresponds to an existing source-of-truth in the repo. **Do not invent new predicates.** Reuse:

| Frontend dataset       | Backend source                                                                                       |
|------------------------|------------------------------------------------------------------------------------------------------|
| `ACCOUNT`              | `AlpacaPaperBrokerAdapter.get_account()` (already in `dashboard.py:_fetch_account_state`)            |
| `HOLDINGS`             | `scripts.generate_tip_sheet.fetch_engine_holdings(broker, engine)` for each engine, merged          |
| `EQUITY_CURVE`         | `platform.equity_snapshot` table (used by current dashboard equity curve)                            |
| `SIGNALS`              | `scripts.generate_tip_sheet.fetch_today_recommendations` + `fetch_recent_signals`                    |
| `AARS`                 | `tpcore.aar.AARReader` + `scripts.generate_tip_sheet.fetch_recent_trades`                            |
| `ENGINES`              | `tpcore.engine_profile._PROFILE` + `scripts.generate_tip_sheet.fetch_credibility`                    |
| `VALIDATION`           | `platform.data_quality_log` rows with `source LIKE 'validation.%'`                                   |
| `HEAL_LOG`             | `platform.application_log` rows `event_type IN ('ops.stage.complete','SELFHEAL_*')`                  |
| `MOM_TRIALS`           | `backtests/momentum_search_results_t12.csv` (or persist the survey to DB)                            |
| `OHLC`                 | `platform.prices_daily` (already in `dashboard.py:_fetch_ohlc`)                                      |
| `BEAR_TIMELINE`        | `backtests/sentinel_phase_history.csv` or live FRED Bear-Score series                                |
| `RISK_STATE`           | `tpcore.risk.RiskGovernor.state_for()` (use the public accessor, not `_store`)                       |
| `ALLOCATOR`            | `tpcore.allocator` last-rebalance snapshot in `platform.application_log`                             |
| `FORENSICS`            | `tpcore.forensics.service` output + `platform.forensics_triggers`                                    |
| `LADDER`               | `docs/ESCALATION_HARDENING_LADDER.md` rung definitions + counts from the per-rung SoT queries below  |
| `SOURCE_HOLDS`         | `tpcore.datasupervisor.list_open_holds(pool)`                                                        |
| `SOURCE_CLEAR_HISTORY` | `application_log` `DATA_SOURCE_CLEARED` rows (last 24h)                                              |
| `CROSS_TABLE_AUDIT`    | `platform.data_quality_log` rows with `source LIKE 'cross_table_audit.%'`                            |
| `RECENT_ESCALATIONS`   | `application_log` `DATA_REPAIR_ESCALATED` + `AdapterContractDrift INGESTION_FAILED` (7d)             |
| `WEEKLY_DIGEST`        | `ops.weekly_digest.build_weekly_digest(pool)` — **verbatim, do not reparse**                         |
| `LLM_TRIAGE`           | `application_log` `DATA_LLM_TRIAGE_PROPOSAL` + `ENGINE_LLM_TRIAGE_PROPOSAL` (open only)              |
| `DAEMONS`              | `consolidated_daemon_topology --check` probe + `platform.application_log` heartbeat rows             |
| `PROVIDERS`            | `tpcore.providers.PROVIDER_BINDINGS` registry                                                        |
| `LAB_RUNS`             | `application_log` `LAB_RUN_COMPLETE` rows + `tpcore.lab.models.LabResult` sidecars                   |
| `LAB_QUEUE`            | `platform.lab_queue` table or in-memory queue (TBD — design assumed)                                 |
| `LAB_WALK_RESULTS`     | from the selected run's `LabResult.windows` payload                                                  |
| `ECR_QUEUE`            | `ops.engine_sdlc.planner.pending_ecrs(pool)`                                                         |
| `ECR_HISTORY`          | `application_log` `ECR_DECIDED` rows (30d)                                                           |

Where a dataset doesn't exist yet (most notably `LAB_QUEUE`), treat it as a small additional spec — but **the design's purpose is to surface state, not to invent it**. If a SoT doesn't exist, push back on the spec rather than fabricate one in the frontend.

## Design Tokens

All tokens are defined as CSS custom properties on `:root` (and `.theme-*` for alternates) in `Trading Console.html`. Copy these out verbatim; do not approximate.

### Colors — operator-dark (default theme)

```css
--bg:        oklch(13% 0.005 60);    /* page background */
--bg-1:      oklch(15% 0.006 60);    /* topbar / rail */
--bg-2:      oklch(18% 0.006 60);    /* row hover, lighter surface */
--bg-3:      oklch(22% 0.006 60);    /* segmented button, pill bg */
--panel:     oklch(16% 0.005 60);    /* card surface */
--panel-hd:  oklch(19% 0.006 60);    /* card header */
--line:      oklch(28% 0.004 60);    /* hairline border */
--line-2:    oklch(34% 0.004 60);    /* stronger border */
--ink:       oklch(92% 0.005 75);    /* primary text */
--ink-2:     oklch(78% 0.005 75);    /* secondary text */
--ink-3:     oklch(58% 0.005 75);    /* tertiary / muted */
--ink-4:     oklch(40% 0.005 75);    /* faint */
--pos:       oklch(72% 0.16 142);    /* green — gains, pass */
--neg:       oklch(67% 0.20 22);     /* red — losses, fail */
--warn:      oklch(78% 0.15 78);     /* amber — warning, gated */
--accent:    oklch(74% 0.16 60);     /* amber identity */
--row-hov:   oklch(20% 0.006 60);
```

### Engine identity colors

```css
mom: oklch(74% 0.16 60)    /* amber */
rev: oklch(72% 0.14 200)   /* teal */
vec: oklch(74% 0.16 295)   /* violet */
sen: oklch(68% 0.18 22)    /* red-orange */
can: oklch(78% 0.12 130)   /* light yellow-green */
```

Use them as pill foreground (`color`) + 35%-mix border + 12%-mix background. The full pill recipe is in the `.pill-*` rules.

### Type scale

- `--font-base: 13.5px` (comfortable) / `12.5px` (compact)
- ViewHeader title: 24px / 600 / `-0.015em` / line-height 1.1
- ViewHeader subtitle: 12px mono / `--ink-3`
- Panel title (h2): 13px / 600 / `-0.005em`
- Eyebrow: 10.5px mono / `--ink-3` / `letter-spacing 0.06em` / uppercase
- KPI label: 9.5px mono / 600 / `letter-spacing 0.12em` / uppercase
- KPI value: 20px mono / 500 / `-0.01em`
- Table header: 10px mono / 600 / `letter-spacing 0.10em` / uppercase
- Table body: 13px mono (data) / 13.5px sans (labels)
- Pill: 10px mono / 600 / `letter-spacing 0.06em` / uppercase
- Crumb: 10.5px mono / `letter-spacing 0.06em` / uppercase

Font stack:

```css
--mono-font: 'JetBrains Mono', ui-monospace, monospace;
font-family: 'IBM Plex Sans', system-ui, sans-serif;       /* default body */
font-feature-settings: 'cv11', 'ss01';                     /* JetBrains stylistic */
font-variant-numeric: tabular-nums;                        /* on all numeric */
```

Use Google Fonts; preconnect tags are in `Trading Console.html`.

### Spacing

- Density `comfortable` (default): `--pad-x: 16px`, `--pad-y: 11px`, `--pad-panel: 18px`
- Density `compact`: `--pad-x: 12px`, `--pad-y: 7px`, `--pad-panel: 14px`
- Rail width: 208px
- Activity rail width: 300px (collapses below 1280px)
- Topbar height: 44px
- KPI grid gap: 1px hairline
- Engine grid gap: 1px hairline
- Generic view gap: 14–18px

### Border radius / shadow

- Maximum radius: 4px (panels, banners). Pills and bars use 2–3px or none.
- **No box shadows.** Use 1px hairline borders for hierarchy. The `--shadow` token defines an inset border ring instead of a drop shadow.

## Components inventory

The following are the reusable pieces that should become real components in the target framework:

| Component        | File                       | Notes |
|------------------|----------------------------|-------|
| `<Pill>`         | `components.jsx`           | tone={pos/neg/warn/neutral/mom/rev/vec/sen/can}, dim |
| `<StatusDot>`    | `components.jsx`           | 7px filled circle with optional glow |
| `<Num>`          | `components.jsx`           | mono numeric formatter — handles signed/USD/pct/int, auto-tones |
| `<Panel>`        | `components.jsx`           | title + eyebrow + actions header + body |
| `<KPI>`          | `components.jsx`           | label + value + sub + optional hint |
| `<Sparkline>`    | `components.jsx`           | inline mini line chart |
| `<EquityChart>`  | `components.jsx`           | full equity curve with benchmark |
| `<CandleChart>`  | `components.jsx`           | OHLC + entry/exit markers |
| `<Heatmap>`      | `components.jsx`           | parameter search grid |
| `<GateBar>`     | `components.jsx`           | progress bar with threshold marker |
| `<GateRow>`      | `views.jsx`                | full gate row (label + bar + value) |
| `<ActionBtn>`    | `components.jsx`           | kind=default/primary, hot flag |
| `<ViewHeader>`   | `views-system.jsx`         | shared eyebrow/title/meta/actions |
| `<HoldingsTable>`| `views.jsx`                | sortable holdings |
| `<SignalsList>`  | `views.jsx`                | signal feed rows |
| `<AARList>`      | `views.jsx`                | closed-trade feed rows |
| `<BearScoreChart>`| `views.jsx`               | sentinel-specific |
| `<WalkForwardChart>`| `views-lab.jsx`         | Lab walk-forward DSR bars |

## Assets

No images / icons / fonts are bundled. Everything is rendered via CSS, SVG (inline in components), and Google Fonts (JetBrains Mono + IBM Plex Sans + IBM Plex Mono).

## Files

- `Trading Console.html` — root document. Contains all the CSS (themes, layout, all component styles), Google Fonts import, React + Babel script tags, and the `<script type="text/babel">` entries that load the JSX modules.
- `trading-console/data.jsx` — synthetic data. Replace every export with a real query in production.
- `trading-console/components.jsx` — atom-level reusable components.
- `trading-console/views.jsx` — Overview, Engine detail, Ticker, Data Pipeline, Forensics, Allocator.
- `trading-console/views-system.jsx` — Health, Weekly Digest, Providers, ViewHeader.
- `trading-console/views-lab.jsx` — The Lab, Engine SDLC, lifecycle map.
- `trading-console/app.jsx` — shell, topbar, rail, activity rail, routing, keyboard shortcuts.
- `trading-console/tweaks-panel.jsx` — design-time tweaks UI (drop in production).

## Notes for the Claude Code agent picking this up

1. **Do not port Streamlit-isms.** This is a real client-side app. Drop the `st.dataframe`/`st.metric` patterns entirely. Pick React (or your taste) and call into FastAPI/asyncpg JSON endpoints.
2. **Reuse the pure Python classifiers verbatim.** `dashboard_components/escalation.py` + `dashboard_components/health.py` already return `(color, summary, detail)` tuples. Behind a thin FastAPI layer they go straight through to the frontend as JSON — no logic duplication.
3. **Lab dossiers exist as real files in `docs/lab/*.md`.** Render the link, don't reconstruct the dossier.
4. **The two-co-task LLM triage** lives inside a single `llm_triage_service` daemon (per the May-18 Epic E B1 placement). Do not break this into separate daemons in the daemon-topology view — the invariant is "exactly two daemons" + the data-ops cron.
5. **The Canary engine is permanent + non-graduating + has a documented compliance deviation** (it must never call `write_credibility_score`). Render it in the engine grid but never show graduation gates.
6. **The Lab sentinel** (`engine_profile.LifecycleState.LAB`) is a single durable engine entry that proves the LAB state is real. It must not appear in the live engine grid; it must appear in the lifecycle map.
7. **Respect the read-only-renderer principle.** If a backend query for something doesn't exist, ask for one — do not recompute predicates client-side.
8. **The `--check` probes** in `dashboard.py` (19 of them) are still the right place for "is this system healthy" predicates. The Health view consumes them; it doesn't re-implement them.
