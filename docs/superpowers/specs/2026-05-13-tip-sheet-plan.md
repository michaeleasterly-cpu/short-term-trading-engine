# Tip Sheet Implementation Plan

- **Path**: `docs/superpowers/specs/2026-05-13-tip-sheet-plan.md`
- **Version**: 1.0
- **Date**: 2026-05-13
- **Status**: approved
- **Referenced from**: `docs/MASTER_PLAN.md` (Research Tools subsection)

---

## What this is

A research tool — `scripts/generate_tip_sheet.py` — that renders, per engine, the current credibility score, recent signals, and recent trade outcomes in a human-readable terminal report. It is **not** a publication, **not** a public feed, and **not** a product. Build phasing reflects that:

1. **Phase 1 — Private operator review tool.** Build now. Local-terminal output only.
2. **Phase 2 — Gated publication.** Build when an engine earns it. Adds `--publish` to write a shareable file; the credibility gate becomes non-overrideable.
3. **Phase 3 — Multi-engine roll-up.** Build when two-plus engines are published. Adds `--rollup`.

---

## Engine descriptions (layman-readable; printed in each tip-sheet header)

- **Sigma** — Looks for stocks stuck in a sideways channel, bouncing between a price floor and ceiling without a clear trend. Enters when the stock touches the channel floor and shows signs of turning back up, with a tight stop-loss. Takes half off at mid-channel and the rest at the ceiling.
- **Reversion** — Hunts for stocks that have fallen too far, too fast, that are statistically likely to snap back. Waits for fundamentals to confirm the company is still healthy (not a falling knife), then buys the panic and waits for the price to return to its average.
- **Vector** — Rides stocks moving with strong directional force, backed by a real reason — an earnings beat, a new contract, an improving business. Only enters when the stock is fundamentally cheap, a catalyst is present, and the technicals confirm the trend is accelerating.
- **S2** — Detects stocks that are heavily shorted and ripe for a squeeze. Triggers when social chatter spikes and borrow rates surge. A rare-event hunter — might fire only a handful of times a year, but when it does, the move can be explosive.
- **Catalyst** — Trades the aftermath of corporate events: earnings surprises, big contract wins, regulatory approvals. Waits for the news to break, lets the market digest it, then enters after the dust settles to capture the drift as the rest of the market catches up.
- **Sentinel** — The platform's insurance policy. Monitors recession indicators (unemployment claims, manufacturing data, the yield curve). When warning signs flash red, it shifts a portion of capital into defensive ETFs (inverse equity, bonds, gold) to protect the portfolio until the storm passes.

---

## Phase 1 — Private operator review tool (build now)

**Description:** The tip-sheet script exists and functions, but only outputs to the operator's local terminal. No public access. No web endpoint. No distribution. This is a research tool, not a publication. No regulatory exposure.

### What gets built

- Four async helpers:
  - `fetch_recent_trades(pool, engine, since) -> list[AfterActionReport]` — reads `platform.aar_events`
  - `fetch_recent_signals(pool, engine, since) -> list[dict]` — reads `platform.application_log` filtered by `event_type='SIGNAL'`
  - `fetch_engine_holdings(broker, engine) -> list[dict]` — live broker positions filtered to the engine's order-history prefix
  - `fetch_today_recommendations(pool, engine, as_of) -> list[dict]` — what the engine WOULD trade today (engine-specific dispatch; Momentum-only in Phase 1)
- Tip-sheet formatting using existing `render()` and `render_rubric()` functions from `tpcore.backtest.statistical_validation`.
- Credibility gate (≥ 60) enforced by default, with a `--force` flag for private operator review of unproven engines.
- `--no-broker` flag for offline review (skips the live Alpaca query).
- **Mandatory disclaimer printed on every output.** Not removable.
- Engine layman description (above) printed in the header for context.

### Section order in the rendered report

1. Header (engine name, generation timestamp, layman description)
2. Credibility — 10-item rubric breakdown + PASS/BLOCKED gate verdict
3. **Currently holding** — live broker positions: ticker, qty, entry/current price, market value, unrealized $/% P&L, totals
4. **Today's recommendations** — top-decile candidates as the engine would rank them right now
5. Recent signals — `SIGNAL` events from `application_log`
6. Recent completed trades — AARs from `aar_events`
7. Disclaimer

### Gates

| Field | Value |
|---|---|
| credibility_threshold | 60 |
| force_override_available | true |
| force_override_scope | private review only |
| public_distribution | false |

### Documentation updates (applied alongside this spec)

- `docs/OPERATIONS.md` — section describing tip sheet as private operator research tool. No mention of public distribution.
- `docs/MASTER_PLAN.md` — Research Tools subsection added with Phase 1 description, gates, disclaimer requirements, and a back-reference to this spec.
- `docs/EDGE_VALIDATION_PLAN.md` — Phase 4 entry for tip-sheet publication gate, blocked on credibility and attorney review.

---

## Phase 1.5 — Historical-replay view (deferred; needs a real signal→trade FK)

**Status:** scoped, not built.

A competing Phase-1 spec (DeepSeek 2026-05-13) proposed a `--past` flag that joins SIGNAL events to AAR outcomes on `ticker` within ±5 trading days, rendering a single table of [Date, Ticker, Score, Direction, Outcome, P&L%] + a `N signals | M acted on | W win | L loss | P pending` summary.

**Why it's not in Phase 1**: the ±5-trading-day join is structurally wrong for our engines. Momentum holds 21+ trading days, so a 5-day window systematically *misses* the engine currently trading; worse, it can silently produce wrong matches (same ticker, different trade). For Sigma/Reversion/Vector the window happens to fit, but those engines aren't paper-trading and have no AAR rows to match.

**Build path when it's worth doing**:

1. Add a `signal_id UUID` column to `platform.aar_events` (or co-locate via `client_order_id`).
2. At order-submission time in each engine's scheduler, stamp the originating signal's UUID into the order's `client_order_id` and copy it onto the AAR when the position closes.
3. Then the join is deterministic — no time window heuristic.
4. *Then* build the `--past` view as a separate command (or flag) that renders the joined table.

Until step 1 lands, the current sectioned format (Recent signals + Recent trades as independent panels) is the honest one — the operator correlates them mentally, but no false-positive matches.

## Phase 2 — Gated publication (build when an engine earns it)

**Description:** The same script, but output can be shared. The credibility gate is active and non-overrideable for any shared output.

### Prerequisites

- At least one engine has credibility ≥ 60 from held-back validation
- That engine has completed ≥ 30 paper trades with documented outcomes
- Disclaimer language reviewed by a securities attorney

### What changes

- Script gains a `--publish` flag that writes formatted report to a static file
- `--force` override removed for `--publish` mode
- `--past` flag enabled to show historical signals with outcomes

### Gates

| Field | Value |
|---|---|
| credibility_threshold | 60 (hard) |
| force_override_available | false |
| minimum_paper_trades | 30 |
| attorney_review_required | true |

### Documentation updates

- `docs/TIP_SHEET_POLICY.md` — new document: publication criteria, disclaimer requirements, legal review status.

---

## Phase 3 — Multi-engine roll-up (build when two-plus engines published)

**Description:** Cross-engine summary view showing all published engines' recent signals and aggregate performance.

### Prerequisites

- At least two engines have passed Phase 2 gates

### What changes

- New `--rollup` flag that queries all published engines and produces a combined report.

---

## Expert analysis

**Summary:** The tip sheet is a product feature. The platform is still a research tool. Publishing signals, even with a disclaimer, implies those signals have value. Right now, every engine fails the credibility gate. Publishing tips from unproven strategies is misleading regardless of the disclaimer. Wait until an engine passes the gate. Then publish only that engine's signals. Have the disclaimer reviewed by a securities attorney before anything goes public.

**Key risks:**

- Disclaimer alone may not provide legal cover if tip sheet is shared publicly
- Past tips showing outcomes could be construed as performance advertising under SEC Marketing Rule
- Publishing before strategies are validated damages platform credibility with no recovery path

**Recommendation:** Build Phase 1 now as a private research tool. Defer Phases 2 and 3 until an engine earns publication rights through the credibility gate.

---

## Momentum Phase 2.5 — sector concentration cap (deferred)

**Status:** deferred — not implemented in the 2026-05-13 Phase 2.5 batch.

**Why deferred:** A sector cap requires a per-ticker sector classification source that the platform doesn't yet have. The two viable options both involve new data infrastructure:

1. **FMP `/api/v3/profile/{symbol}`** — exposes `sector` (Technology, Financials, Consumer Cyclical, etc.) and `industry`. ~1,281 calls to backfill T1+T2; refresh quarterly. Needs a new `platform.ticker_classifications` table + an ingestion handler under `tpcore/ingestion/handlers/`. Estimated effort: ~1 day.
2. **SIC code → sector mapping** — hand-curated. Brittle, doesn't track FMP's classification, but no ongoing data dependency. Estimated effort: ~half day for the mapping + lookup logic.

**What the cap would do once built:** at `MomentumExecutionRisk.build_decision` time, after the top-decile cut, enforce a max-sector-weight (e.g., 30%). If too many top-decile names are in the same sector (typical during regime concentrations — e.g., AI bubble pulled tech to >40% of momentum's top decile in 2024), the excess names get dropped to the next-best non-overrepresented candidate.

**When to build:** after sufficient paper-trading data exists to confirm sector concentration is materially hurting risk-adjusted returns. The 54-name portfolio is well-diversified by construction; the cap is insurance against a regime where it stops being so.

## Audit findings (reusable components)

| Component | Source | Reusable as |
|---|---|---|
| `graduation_ready(pool, engine_name)` | `tpcore.backtest.credibility` | per-engine credibility pass/fail |
| `render_rubric(score)` | `tpcore.backtest.statistical_validation` | format the 10-item checklist |
| `render(report)` | `tpcore.backtest.statistical_validation` | format DSR/PSR/MinBTL block |
| `build_asyncpg_pool(database_url)` | `tpcore.db` | DB connection |
| `AfterActionReport`, `ExitReason` | `tpcore.aar.models` | trade-outcome shapes |
| `CredibilityScore` | `tpcore.backtest.credibility` | score + 10-item checklist + `passes_gate` property |
| Engine `SetupCandidate` / `PhaseAssessment` / `ExecutionDecision` | `{sigma,reversion,vector,momentum}.models` | current opportunity shapes |

**Gaps (new code in Phase 1):**

- No public function reads `platform.aar_events` — need `async fetch_recent_trades(pool, engine, since) -> list[AfterActionReport]`
- No reader for `SIGNAL` events in `platform.application_log` — need `async fetch_recent_signals(pool, engine, since) -> list[dict]`

**Total estimated LOC:** ~150-200, mostly composition of existing tested pieces.
