# Short-Term Trading Engine — Unified Platform Master Plan

**Version:** 1.0
**Date:** 2026-05-13
**Status:** Phase 0 Complete — Ready for Engine Builds

---

## 1. Constitution

### 1.1 Product Identity

The Short-Term Trading Engine is a personal, fully automated, multi-strategy trading platform for US equities. It operates on daily timeframes, executes all orders via the Alpaca API, and explicitly does not provide financial advice. The operator oversees; the platform executes.

### 1.2 Core Principles

- **Risk control IS the product.** Position sizing, kill switches, exposure caps, and refusal-to-trade are first-class features — not safety wrappers around a "real" engine.
- **Suspicion is the default.** The platform becomes MORE skeptical in crowded, volatile conditions.
- **AI is auditor, never execution authority.** Deterministic rules govern all orders. AI modules explain, classify, and refuse — they never place, size, or exit positions.
- **Personal-use scope.** Commercial use is blocked. The platform protects ONE operator.
- **Provenance discipline is non-negotiable.** Every data point is timestamped with source and observed-at. Raw text is never persisted unless needed for audit.
- **Engines stay isolated; summary data does not.** Engines share `platform.*` tables with an engine discriminator column. No engine calls another engine directly.
- **Core-first principle.** Every engine consumes shared functionality through `tpcore` modules. Direct vendor SDK imports are prohibited.

### 1.3 Architectural Constraints

- **Time:** All timestamps in UTC. Market calendar provided by `exchange_calendars` (NYSE).
- **Data:** No `yfinance`. Paid sources only where free tiers are insufficient. Survivorship-free backtesting mandatory.
- **Execution:** Fully automated via Alpaca API. Bracket orders wherever possible. Manual execution only for emergency overrides.
- **Build order:** Exactly one engine is built and paper-traded before the next begins.

---

## 2. Shared Core (`tpcore`)

`tpcore` is the stable foundation. All engines depend on it.

### 2.1 Interfaces

- `BaseEnginePlug` ABC — every engine plug inherits from this.
- `BrokerExecutionInterface` — Alpaca adapter implements this.
- `DataProviderInterface` — FMP, Alpaca, SEC EDGAR adapters implement this.

### 2.2 Risk Governor (`tpcore.risk`)

- Per-engine daily loss limit (5%), weekly loss limit (10%), max concurrent positions (8).
- Platform-wide net long exposure cap (60% of total capital).
- `check_trade(engine_id, size, direction) → bool`
- `emergency_kill()` — cancels all orders, flattens positions.
- State persisted in `platform.risk_state`.

### 2.3 After-Action Reports (`tpcore.aar`)

- `AfterActionReport` Pydantic model — engine, trade_id, entry/exit details, P&L, regime tags, rule-compliance flag.
- `AARWriter` writes idempotently to `platform.aar_events`.

### 2.4 Quality & Parity (`tpcore.quality`, `tpcore.parity`)

- `DataQualityScore` + writer → `platform.data_quality_log`
- `ExecutionQualityScore` + writer → `platform.execution_quality_log`
- `LivePaperParityHarness` — compares paper/live fills → `platform.parity_drift_log`

### 2.5 Backtest Integrity (`tpcore.backtest`)

- Provider-agnostic harness.
- `BacktestCredibilityRubric` (0–100) — lookahead, survivorship, PIT fundamentals, regime coverage, out-of-sample.
- Score < 60 → engine cannot trade live.
- Transaction cost model: 0.05% slippage per side for liquid stocks, configurable.

### 2.6 Fundamentals & Valuation (`tpcore.fundamentals`, `tpcore.valuation`)

- Earnings quality, FCF trend, insider analysis, comps analysis, moat scorecard.
- DCF, Owner Earnings, Buy Bands — for later engines.
- `ThesisTemplate` — every setup must articulate mispricing, catalyst, and thesis killer.

### 2.7 Tax Overlay (`tpcore.tax`)

- `TaxLotTracker` — FIFO lot assignment, cost basis tracking.
- `WashSaleTracker` — 61-day window, cross-engine wash sale detection, basis adjustment.
- `TaxLossHarvester` — daily scan for "probably failing" positions. Auto-harvest during Q4 (capped at $3,000 net loss). Manual mode otherwise. Forces 31-day re-entry block.

### 2.8 Market Calendar & Scripts

- `tpcore.calendar` — NYSE calendar wrapper.
- `tpcore.scripts.check_imports` — blocks forbidden vendor imports.

---

## 3. Platform Database Schema

All tables live in the `platform` schema. The `engine` column discriminates engine-specific rows.

| Table | Purpose |
| --- | --- |
| `platform.aar_events` | Unified after-action reports |
| `platform.execution_quality_log` | Fill quality per order |
| `platform.data_quality_log` | Data source staleness / integrity |
| `platform.parity_drift_log` | Paper vs live fill drift |
| `platform.risk_state` | Risk Governor state per engine |
| `platform.allocations` | (Stub) Future Allocator capital assignments |
| `platform.forensics_triggers` | (Stub) Future Forensics sprint triggers |
| `platform.tax_lots` | Tax lot FIFO records |

---

## 4. Engine Specifications

All engines share the **5-Plug model:** Setup Detection → Lifecycle Analysis → Execution & Risk Scaling → AAR Logging → Capital Gate.

### 4.1 Sigma — Range Scalping Engine (First Build)

**Mission:** Capture mean-reversion within well-defined, low-volatility price channels on a daily timeframe.

**Setup Detection:**
- Universe: Price > $10, avg vol > 1M, ADX(14) < 20, Bollinger Band width < 30th percentile.
- Score: Channel Quality (0–40), Entry Precision (0–35), Market Context (0–25).
- Thresholds: ≥ 70 strong, 50–69 weak, < 50 no trade.

**Lifecycle Analysis:**
- Phases: Setup, Approaching, Active, Exhaustion.
- Band-ride detector: price closes outside band 2 days → exit.
- Max hold: 3 days without reaching mid-band.

**Execution & Risk:**
- Entries: Market at next open after signal.
- Exits: Sell 50% at mid-band, remaining at opposite band. Hard stop −3%.
- Sizing: Pre-grad cap $1,500, max 4 concurrent positions.
- Bracket orders: take-profit limit + stop-loss.

**AAR Logging:** via `tpcore.aar.AARWriter`.

**Capital Gate:** Hard cap enforcement, graduation (50 trades, 65% win rate, avg return ≥ 1.5%).

### 4.2 Reversion — Mean Reversion Engine (Second Build)

**Mission:** Fade statistically extreme price deviations on a multi-day horizon.

**Setup Detection:**
- Z-score vs 20-MA < −2 (oversold) or > +2 (overbought), RSI extremes, volume climax.
- Fade Score: Statistical Extremity (0–45), Exhaustion Confirmation (0–30), Market Context (0–25).
- Thresholds matched to Sigma.

**Lifecycle Analysis:**
- Monitors reversion progress toward 20-MA.
- Time stop: 5 days without reversion.
- ADX gate: if ADX > 25, engine disabled (trending market).

**Execution & Risk:**
- Entries at market open after signal.
- Targets: Close 75% at 20-MA, remaining at 50-MA.
- Hard stop: −8%.
- Must pass `tpcore.fundamentals.EarningsQualityCheck` — if LOW, trade suppressed.

**Graduation:** 30 trades, 60% win rate, avg return ≥ 2%.

### 4.3 Vector — Momentum Swing Engine (Third Build)

**Mission:** Capture multi-day momentum in fundamentally-backed, catalyst-driven stocks.

**Setup Detection:**
Three-gate model:
1. Value & Quality pre-screen (P/B, D/E, revenue).
2. Catalyst NLP (EDGAR, contracts).
3. Technical trigger (pullback or breakout).

Swing Score: Technical (0–40), Catalyst (0–35), Sentiment (0–25). Thresholds ≥ 65.

**Crash Guard (mandatory):**
- Volatility-scaled sizing: VIX > 25 → 50% size, VIX > 30 → 25% size.
- Post-drawdown cooldown: SPY −10% in 20 days & rebounding → no new entries for 10 days.
- Engine-level circuit breaker: −10% rolling 20-day P&L → freeze for 10 days.

**Execution & Risk:**
- Entries at market open. Hard stop −7%. Profit target +15% or trailing stop after +10%.
- Sizing pre-grad $2,000. Max 5 concurrent positions.

### 4.4 S2 — Short Squeeze Engine (Fourth Build, Satellite)

**Mission:** Detect conditions conducive to short squeezes. Satellite only — permanent 5% capital cap.

**Setup Detection:**
- Layer 0: Short interest > 20% (FINRA, release-date matched), days-to-cover > 5, borrow rate acceleration.
- Layer 1: Social volume spike (ApeWisdom).
- Squeeze Score ≥ 50 (pre-chat), ≥ 60 (social alert).

**Data Limitations:**
- FINRA data is bi-monthly with 2-week lag. Used for regime detection only — live entry triggered by real-time borrow rates and social signals.

**Lifecycle Analysis:**
- Phases: Accumulation → Breakout → Spike → Exhaustion.
- Exit trail: Tier 1 (close 10% below 5-day high, sell 50%), Tier 2 (close 20% below, sell remaining).
- Instant profit trail at +100% unrealized gain.

**Execution & Risk:**
- Hard stop −7%. Max hold 15 trading days.
- 30-day ticker re-entry lock.

**Graduation:** 5 trades / 6+ months, win rate ≥ 60%, avg return ≥ 30%.

### 4.5 Catalyst — Event-Driven Engine (Fifth Build)

**Mission:** Capture post-event drift from earnings surprises and contract awards. No binary events, no options.

**Setup Detection:**
- Event Score: Event Quality (0–45), Asymmetry (0–30), Market Context (0–25).
- Only scheduled events: earnings beats with raised guidance, material government contract awards.
- Pre-event anticipation removed — post-event reaction only.

**Entry Gate:**
- Stock must be above 200-day SMA.
- Event ≤ 2 trading days old (staleness gate).

**Execution & Risk:**
- Entry at market open after breakout from pre-event range.
- Hard stop −10%. Profit target event-specific.
- Max 10% of total platform capital allocated to Catalyst.

### 4.6 Sentinel — Macro Defense Engine (Sixth Build)

**Mission:** Protect the platform during recessions. Reformed basket with minimal decay.

**Composition (non-leveraged dominant):**

| Symbol | Weight | Notes |
| --- | --- | --- |
| SH | 35% | |
| PSQ | 25% | |
| TLT | 20% | |
| GLD | 10% | |
| SQQQ | 10% | Tactical, 5-day max hold |

**Activation:**
- Bear Score ≥ 60 for 3 consecutive days with no counter-trend rally > 5%.
- Bear Score 60–79 → allocate up to 10% of platform capital.
- Bear Score 80+ → allocate up to 20%.
- Permanent maximum: 20% of platform capital.

**Safety Overrides:**
- Shallow recession override (Bear Score < 80): reduce SH/PSQ by 50%, increase TLT/GLD.
- VIX > 40: reduce inverse ETFs by 50% (compounding drag spikes).
- SH/PSQ re-evaluated every 30 calendar days.

---

## 5. Platform Services (Deferred)

These are built only after at least two engines are live.

- **Allocator:** Equal-risk-weighted capital distribution. Performance-chasing explicitly rejected. Primary value is the floor (freezing engines in persistent drawdown).
- **Forensics:** Monitors AARs. Generates Sprint Dossiers on drawdown, loss cluster, or outlier loss.
- **Settlement:** Annual distribution (75% to operator, 25% retained). Produces Schedule D-ready tax CSV.

---

## 6. Data Architecture

### 6.1 Live / Production Stack

| Source | Purpose | Cost |
| --- | --- | --- |
| Alpaca (IEX free) | Daily bars, quotes, execution, delisted stock data | $0 |
| FMP (Starter $22/mo or Premium $59/mo) | Fundamentals, insider, earnings | $22–59 |
| SEC EDGAR | Point-in-time filings, fundamentals backup | $0 |
| ApeWisdom | Social sentiment | $0 |
| FRED | Macro indicators | $0 |
| FINRA / NASDAQ | Short interest (release-date matched) | $0 |
| IBorrowDesk | Borrow rates (scraped, fragile) | $0 |

### 6.2 Historical / Backtesting Database (Self-Built)

- Alpaca free tier → survivorship-free daily bars (delisted stocks included).
- SEC EDGAR XBRL filings → point-in-time quarterly fundamentals.
- Historical SPY constituent proxy (month-end market-cap ranking).
- Built in Phase 0–1, operational by July 2026.

### 6.3 Data Quality Gates

- `DataValidationSuite` (10 delisting spot checks, S&P 500 constituent comparison, split verification).
- Passed before any engine graduates to live.

---

## 7. Tax Overlay

- `TaxLotTracker` records every purchase.
- `WashSaleTracker` prevents cross-engine wash sales.
- `TaxLossHarvester` daily scans "probably failing" positions. Auto-harvests within $3,000 net loss cap during Q4.
- Settlement module generates annual Schedule D CSV.

---

## 8. Platform Operations & Safety

- **Kill Switch:** Emergency button → `RiskGovernor.emergency_kill()` → cancels all orders, flattens positions.
- **Cumulative Exposure Cap:** Net long ≤ 60% of platform capital.
- **Vacation Mode:** Pauses new entries; exits remain active.
- **Broker Outage Protocol:** Backup manual login path. No secondary automated broker.
- **Performance Benchmark:** SPY total return (Sharpe ratio). Failure = underperformance for 24 consecutive months.
- **Trade Discipline Log:** Daily checklist before first trade.
- **Tradier account:** CLOSED — $500 moved to Alpaca. No inactivity fees.

---

## 9. Build Order

| Phase | Deliverable | Dependencies | Status |
| --- | --- | --- | --- |
| Phase 0 | `tpcore` + platform schema + ingestion script | — | Complete |
| Phase 1 | Sigma engine — full plug implementation | Phase 0 | Next (Tue) |
| Phase 1b | Sigma paper trading (3+ months), Parity Harness active | Phase 1 | Jul–Oct 2026 |
| Phase 2 | Reversion engine | Phase 1b success | Late 2026 |
| Phase 3 | Allocator + Forensics (basic) | 2 engines live | 2027 |
| Phase 4 | Vector engine | Phase 3 | 2027 |
| Phase 5 | S2 (satellite) | Platform proven | 2027–2028 |
| Phase 6 | Catalyst | Platform proven | 2027–2028 |
| Phase 7 | Sentinel | At least one recession signal needed | When triggered |

---

## 10. Governance

This master plan is the binding specification. Any deviation must be ratified by a new decision entry in `docs/decisions/` following the naming convention `YYYY-MM-DD-topic.md`. All engine code must reference the relevant section of this plan. The `docs/session-log.md` records each build session. The `docs/glossary.md` defines every term.
