# Canary — Pipeline-Exercise Test Engine — Design

**Status:** approved 2026-05-17 (operator; mandate delegated to expert
→ committed hybrid C+; design approved). Precondition sub-project for
honestly trusting the Deterministic Agents epic (DA-1 #27, DA-2 #34
shipped). Sequenced before DA-3.

## 1. Problem

All alpha engines (reversion/vector/momentum/sentinel) FAIL the
DSR≥0.95 / credibility≥60 graduation gate → none is live → none
produces daily trades. Consequence: the whole pipeline
(should_fire→dispatch→RiskGovernor→trade_monitor→AAR→forensics→
DA-1/DA-2→allocator→weekly_digest), and especially DA-1's
`missed_cycle`/`crashed_startup` and ALL of DA-2, are **unexercised
in production**. We shipped two deterministic control agents with no
authentic data flowing through them.

## 2. Mandate (expert-committed hybrid C+)

ONE sub-project, two coupled deliverables:
- **(a) `canary` heartbeat engine** — its SOLE purpose: trade a tiny
  fixed paper position every trading day through every real path so
  DA-1's liveness detectors + the dispatch/AAR/forensics-detection/
  allocator-skip/digest chain get authentic daily data. It is NOT an
  alpha engine (not expected to make money — expected to make TRADES).
- **(b) DA-2 injection harness** — a deterministic, canonical
  `ops.py --stage` that writes one well-formed `forensics_triggers`
  row for `engine='canary'` only, so DA-2's full HOLD/ESCALATE branch
  table is provably exercised end-to-end through the real
  `should_fire` gate.

**Rejected alternative (expert): engineered-loss real trades (B).**
Forcing a 5-loss streak via real paper trades is nondeterministic
(fills/variance; the −3σ `outlier_loss` math might fire instead of
`loss_cluster≥5`), has a larger blast radius (poisons AAR/allocator/
DSR via one missed `WHERE engine != 'canary'`), and conflates the
already-tested forensics scanner with DA-2 (the unit we need to
cover). Honest controller testing = drive a known step input at a
documented seam and verify the agent's REAL response; do NOT fabricate
a P&L the platform then trusts as signal.

## 3. The canary engine

Built from `tpcore/templates/engine_template/`; closest live model is
**sentinel** (batch day-market, allocator-excluded). Engine name
`canary` (reserved; NOT an alpha-roster member). Cadence `DAILY` with
`tpcore.calendar.is_trading_day()` early-return (exercises the
non-trading-day path too). 5 plugs subclass `BaseEnginePlug` with
real `validate_dependencies`/`healthcheck`; `setup_detection`
populates `tpcore.backtest.filter_diagnostics.FilterDiagnostics` so
SIGNAL events carry per-gate counters like every engine. **Trivial,
deterministic "signal":** every trading day, the canary first SELLS
any share held from the prior cadence (realizing exactly ONE AAR
round-trip with authentic `pnl_net`/`exit_ts`/`ticker`) and then BUYS
1 share SPY to re-enter. Net: exactly one realized AAR per trading
day (steady-state) — guaranteed, deterministic per-cadence AAR flow
for forensics (≥5 AARs reached in ~5 trading days). Day 1 (flat,
nothing to sell) produces only the entry buy, no AAR — expected.
Paper-only, fixed 1 share. Every trade
goes through `RiskGovernor.check_trade()` + `record_fill()` via the
real batch submit path (`tpcore.risk.batch_gate.gate_batch_order` →
`broker.place_order`, like sentinel — day-market, NO OCO bracket, so
canary does NOT belong in `scripts/pipeline_smoke_test.py`).
Scheduler emits `db_log.startup()` after the `try:` and
`db_log.shutdown(duration_ms, exit_code)` in `finally:` — the
unforgeable substrate DA-1's `missed_cycle`/`crashed_startup`/
`scheduler_crash` read.

## 4. should_fire wiring — canary MUST fire daily

`should_fire` is the gate; canary firing daily is the entire point,
so canary must PASS it every trading day:
- `tpcore/engine_profile.py` `_PROFILE["canary"] =
  EngineProfile(engine="canary", cadence=Cadence.DAILY)` → profiled +
  cadence-boundary every trading day.
- `tpcore/quality/validation/capital_gate.py` `ENGINE_TABLES["canary"]
  = frozenset({"prices_daily"})` → `should_fire`'s `data_ready`
  (`assert_passed_for_engine`) passes on fresh SPY data. EXACT C-T5
  pattern (the allocator entry). SPY is already in `CRITICAL_TICKERS`
  (`prices_daily_freshness`) so its data is guaranteed — no change
  needed there.
- DA-1 `supervisor_held` and DA-2 behavioral `ENGINE_HELD` apply to
  canary uniformly and for free via the existing gate (no DA-1/DA-2
  change).

`should_fire`/graduation note: should_fire's `data_ready` is the
capital_gate DATA freshness check, NOT the DSR/credibility graduation
gate. Canary passes data_ready (so it trades daily) AND is
structurally non-graduating (§5b) — these are different gates;
correct by construction.

## 5. Non-pollution / non-graduation (safe by construction)

- **(a) Allocator:** the allocator's managed set is the default tuple
  `("reversion","vector","momentum")` in `tpcore/allocator/service.py`
  `__init__` (sentinel already excluded by omission). Canary is
  likewise excluded **by omission** — it must hit the allocator-*skip*
  path, never the inverse-vol pool. Add a test asserting `"canary"`
  is not in the allocator's managed engines.
- **(b) Graduation/credibility:** canary's `backtest.py` does NOT call
  `write_credibility_score` → no rubric row → it can structurally
  never pass the live-graduation gate → permanent paper canary by
  construction (not by a flag). This is the ONE intentional,
  documented deviation from the engine-build compliance shortlist.
- **(c) P&L segregation:** **Plan-time determination (no vacuous
  test):** identify every site that aggregates AAR P&L *across
  engines as alpha* (allocator inverse-vol input, weekly_digest alpha
  rollup, any AAR-reader alpha summary). At EACH such existing site,
  add an `engine != 'canary'` filter + a real test asserting a
  canary AAR row does NOT enter that aggregate. If NO such
  cross-engine alpha-aggregation site exists today (the allocator
  exclusion §5a already covers the inverse-vol pool, and per-engine
  AAR reads are not cross-engine alpha sums), then this requirement
  is satisfied by §5a + the structural facts — do NOT write a vacuous
  always-pass test; instead record a CLAUDE.md/glossary note that any
  FUTURE cross-engine alpha aggregate must exclude `canary`. The plan
  states which case holds, with evidence.
- **(d) Paper-only + tiny:** fixed 1-share SPY; `ALPACA_PAPER` path;
  `tpcore/risk/limits_profile.py` `_PROFILE["canary"]` capping it
  tiny (e.g. `max_open_positions=1`).
Each exclusion has a test asserting the guard exists so a future
consumer cannot silently re-include canary.

## 6. Wiring touch-points (all hit at build, not after)

`ROSTER` (`ops/engine_dispatch.py`); `_PROFILE`
(`tpcore/engine_profile.py`); `ENGINE_TABLES`
(`tpcore/quality/validation/capital_gate.py`); `_PROFILE`/`limits_for`
(`tpcore/risk/limits_profile.py`); `scripts/run_all_engines.sh`
for-engine loop + docstring listing; `ops/platform_pipeline.py`
docstring engine listing; `scripts/run_smoke_test.sh` step-3 loop;
allocator exclusion (§5a, by omission + a guard test). **NOT**
`scripts/pipeline_smoke_test.py` (Tier-2 OCO bracket engines only;
canary is batch day-market like momentum/sentinel). SPY already in
`CRITICAL_TICKERS` — unchanged. CLAUDE.md engine roster/conventions +
`docs/glossary.md` updated to list canary as the infra canary engine.

## 7. The DA-2 injection harness

Canonical stage: `python scripts/ops.py --stage canary_inject_trigger
--param kind=loss_cluster --param streak=5` (Session Rules: a
registered `ops.py --stage` handler, NEVER a one-off `scripts/foo.py`;
repeatable; coerces params). It writes EXACTLY ONE well-formed
`platform.forensics_triggers` row for `engine='canary'` ONLY (payload
matches the forensics producer's shape for that `kind`, incl.
`fingerprint`, `streak_length` for loss_cluster, etc.) with a
`source='canary_injection'` payload marker for audit + teardown. It
does NOT touch real engines and NEVER writes for any engine other than
`canary` (hard-guarded). `--param kind` ∈
{`outlier_loss`,`loss_cluster`,`drawdown_period`}; `--param
streak`/severity params shape the payload. A teardown mode
(`--param teardown=true`) deletes rows with the injection marker.

## 8. Testing

**Engine:** each plug unit-tested (`validate_dependencies`/
`healthcheck`/the trivial 1-share-SPY logic + `FilterDiagnostics`
populated); scheduler test (`is_trading_day` early-return;
`db_log.startup`/`shutdown`; batch submit through `gate_batch_order` +
`record_fill`; an AAR row written with forensics-compatible fields
`engine`/`trade_id`/`pnl_net`/`exit_ts`/`ticker`); `should_fire(
"canary")` fires on a trading day with fresh `prices_daily`; the 4
exclusion guard tests (§5 a–d).
**Harness (the DA-2 end-to-end proof):** for each kind — inject →
`aar_autotune.autotune("canary")` → assert the correct outcome
(`loss_cluster≥5`/`drawdown_period` → `ENGINE_HELD` behavioral +
`ENGINE_ESCALATED`; `outlier_loss`/short cluster → `ENGINE_ESCALATED`
only, no hold) → `should_fire("canary")` returns held (HOLD cases) →
`_dispatch_engine` records the skip → operator resolves (`resolved_at`)
→ `_maybe_clear_behavioral` emits `ENGINE_CLEARED` → next cycle
`should_fire("canary")` fires again. Teardown deletes injected rows;
a test asserts teardown leaves `forensics_triggers` clean and never
touched a non-canary row.
**Integration:** full suite + CI-exact `ruff`
(`reversion/ vector/ momentum/ sentinel/ tpcore/ scripts/ ops/` —
canary package added to the lint set) + `check_imports` (canary is an
`ENGINE_PACKAGE`: must NOT import another engine; tpcore must NOT
import canary). B/C/DA-1/DA-2 dispatch/supervisor/autotune suites
reconciled for the new ROSTER member (faithful — the established
pattern; per-actor count/order assertions get the canary entry).

## 9. Error handling / lane discipline

Canary scheduler is crash-isolated by the existing `_safe_invoke`
ROSTER wrap (a broken canary must not abort the sweep — same invariant
as every engine). The injection stage is bounded + idempotent (one
row per invocation; fingerprint-deduped like the forensics producer;
teardown by the `canary_injection` marker; hard-guarded to
`engine='canary'`). DA-2/DA-1/forensics logic is consumed AS-IS — NOT
modified. Does NOT touch the data lane (`tpcore/selfheal`,
`tpcore/feeds`, `tpcore/ingestion`, `ops/data_repair_service.py`,
`ops/cutover_agent.py`, `ops/weekly_digest.py` internals beyond a
read-side alpha-aggregate exclusion if one exists), nor alpha-engine
code.

## 10. Scope boundary

DA-canary delivers: the `canary/` engine package (5 plugs +
scheduler + `backtest.py` WITHOUT `write_credibility_score` + models),
all §6 wiring touch-points, the 4 §5 exclusion guards + tests, the
`canary_inject_trigger` ops stage + teardown, the §8 DA-2 end-to-end
chain test, and CLAUDE.md/glossary registration. It does **NOT**:
engineer real losses (B rejected); give canary alpha/graduation/
allocator participation; modify DA-1/DA-2/forensics logic; touch the
data lane; or do DA-3 (two-daemon consolidation — sequenced after,
per operator). Acceptance: canary fires + paper-trades every trading
day through the real path producing forensics-compatible AARs; DA-1
liveness detectors see authentic STARTUP/SHUTDOWN; the harness proves
DA-2's full branch table end-to-end; all 4 non-pollution guards
test-asserted; full suite + ruff + check_imports green; no data-lane
file touched; engine_readiness.md satisfied except the documented
no-credibility deviation.

## 11. Decisions log

- **D-CAN-1** Expert-committed **hybrid C+**: heartbeat engine for
  DA-1 + a separate deterministic injection harness for DA-2;
  engineered-loss real trades (B) REJECTED (nondeterministic, larger
  blast radius, conflates forensics-scanner with DA-2, fabricate-then-
  trust anti-pattern).
- **D-CAN-2** Instrument = 1 share SPY, paper-only, DAILY, round-trip
  per cadence; real batch path (sentinel-shaped, no OCO).
- **D-CAN-3** `ENGINE_TABLES["canary"]={prices_daily}` so should_fire
  data-gate passes daily (C-T5 pattern); SPY already in
  CRITICAL_TICKERS.
- **D-CAN-4** Non-graduating BY CONSTRUCTION: no
  `write_credibility_score` (the one documented compliance-shortlist
  deviation); excluded from allocator by omission + guard test;
  paper-only + tiny limits; alpha-aggregate exclusion.
- **D-CAN-5** Injection = canonical `ops.py --stage
  canary_inject_trigger` (never a one-off script), `engine='canary'`
  hard-guarded, `source='canary_injection'` marker, teardown mode.
- **D-CAN-6** Permanent canary — never promoted, never DA-3-folded
  away as alpha; it is infrastructure. DA-3 sequenced after.
