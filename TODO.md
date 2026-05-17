# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

## WEEK GOAL (2026-05-16): Data layer finalization + hardening

Single focus until further notice — no engine/Sigma-redesign work. Sequence:

1. ✅ **SEC backfill — DONE 2026-05-16.** Per-ticker crawl root-caused
   as wrong tool; built two-phase bulk Form-345 ETL (insider 646,107
   rows / 84.1% T1-T2) + full-history-shard 8-K API backfill (237,680
   rows / 85.1%), 2018→2026, DB-verified, CI-green. `sec_filings_freshness`
   GREEN. **Still owed:** the catalyst/SEC 180d coverage *verdict vs
   thresholds* (our-defect-until-proven-per-ticker; no vendor-blame).
   3 suite checks red for **structural** reasons, not pull-staleness —
   `short_interest_freshness` (FINRA bi-monthly cadence > 35d
   threshold), `social_sentiment_freshness` (ApeWisdom ~23% < 30%
   floor), `prices_daily_freshness` (needs investigation). Belongs in
   threshold calibration, NOT a re-pull.
2. ✅ **Self-heal rollout — DONE 2026-05-16.** Honest end state:
   **14/20 checks genuinely self-heal** (all named to real bounded ops
   stages; zero fake specs — verified), **6/20 honest permanent
   escalate-for-investigation** (row/fundamentals/corporate_actions
   integrity = corruption class; delistings/constituent/splits =
   source-of-truth reconciliation — these can NEVER honestly
   auto-heal; healable=False is correct, not pending). The expert-
   flagged "11/11 self-heal" target was rejected as fake-green. Root
   causes fixed not masked: FINRA adapter missing offset-pagination
   (only 1 stale period ever ingested — our defect, not cadence;
   commit 16840f7); ApeWisdom 30% floor structurally unreachable →
   evidence-derived 15% (proven 23% source ceiling, full-overlap
   ingest verified; a58304c); per-class honest unhealable reasons
   (69e84b2); 3 + 2 healable flips (556cc9e, 51fb643). Force param
   added to tier_refresh/classify_tickers.
3. ✅ Validation/self-heal honest-green path proven (macro + classify
   force-repull live-verified).
3a. ✅ **Per-feed cadence profile (#163) — cadence facet DONE
   2026-05-16.** `tpcore/feeds/` is the single source of truth: one
   evidence-backed `FeedProfile` per feed (13 feeds), frozen, with an
   `evidence` string (no-vendor-blame). The 9 single-MAX_AGE freshness
   checks now READ `freshness_max_age_days` from the profile instead
   of scattered guessed constants — this also fixed the live
   short_interest docstring/constant lie (said 42, constant was still
   35 → now 42 from the profile). Clockwork drift test: every healable
   HealSpec source must declare a profile (can't ship a self-healing
   feed without an evidence-backed cadence). The other 3 facets are
   **declared as profile fields with per-feed values but enforcement
   is honestly phased, NOT dropped**: TRIGGER (scheduler re-arch off
   the blanket daily sweep — launchd-level), TARGETING (demand-driven
   set for constrained feeds — crosses the engine boundary),
   PUBLICATION-AVAILABILITY GATE (per-adapter "source has newer?"
   probe so vendor-late ≠ red). Those three are the remaining #163
   work, each a deliberate phase.
3b. ✅ **TRIGGER facet (#165) — DONE 2026-05-16.** `tpcore/feeds/
   dispatcher.py` (pure, tested) + `python -m tpcore.feeds`: reads
   the canonical per-stage last-success from `application_log` + the
   XNYS close gate, returns only feeds whose trigger/cadence is due
   per FeedProfile. The EXISTING data-ops daemon (no new daemon)
   calls it → `ops.py --update --only <due>`; absent `--only` =
   today's full sweep (preserved/reversible); empty-due = infra +
   Step-4 self-heal only (NONE_DUE sentinel — green-gate unaffected);
   launchd timing untouched. Live-proven; 879 tests.
3c. ✅ **TARGETING + PUBLICATION facets (#165) — DONE 2026-05-16.**
   TARGETING: `tpcore/feeds/targeting.py` — `demand_targets` (DB-
   derived active interest: open_orders ∪ recent aar_events ∪ recent
   universe_candidates; NO engine code — engine *output* in shared
   tables) + `prioritise`; CONSTRAINED_DEMAND_DRIVEN feeds spend their
   bounded budget on demand tickers first, WHOLE_UNIVERSE never
   narrowed; empty demand → unchanged. Wired exemplar:
   IBorrowDesk handler. PUBLICATION: `tpcore/feeds/publication.py` —
   freshness is now VENDOR-ANCHORED (UTC, the vendor's calendar, NOT
   today−N): `FeedProfile.publish_weekday` (AAII=Thu) +
   `expected_latest_publish` (pure, offline — last scheduled publish
   minus dissemination lag) wired into the AAII check, so a red means
   "vendor published, we're behind" (genuine our-gap) and normal
   vendor lag never false-fires. Live HEAD `Last-Modified` probe
   (`AAIIAdapter.latest_published` + `source_has_newer`) built +
   registered + tested as the mechanism. 891 tests; ruff/imports
   clean; no engine code modified.
   **Honest remaining (incremental adoption, not unbuilt design):**
   per-constrained-feed targeting rollout beyond IBorrowDesk;
   per-adapter probes beyond AAII (FINRA has no cheap latest-probe);
   self-heal-orchestrator probe consult for the vendor-MISSED-a-
   scheduled-publish-beyond-lag edge (schedule anchoring already
   covers the normal case). Each a one-entry/­one-wire increment.
4. **Hardening pass** (some items NOT blocked on the verdict — run in
   parallel while SEC backfills):
   - `prices_daily_gaps` audit check: close the 14-day-recency blind spot
     (old un-backfilled liquid holes invisible).
   - sporadic `row_velocity`: tighten (currently only fires on total
     silence; misses sustained severe partial degradation).
   - FMP handler-path CSV archive: verify end-to-end (presence unproven).
   - ✅ **HY-spread recovery — DONE 2026-05-16.** ALFRED/Nasdaq ruled
     out empirically; full history recovered (eco-archive 1996-2021 +
     Scribd FRED-graph gap, validated 772/772 exact). `hy_spread`
     contiguous 1996→present, re-activated as a maintained
     `INDICATOR_SERIES` member (FRED rolling window keeps tail fresh).
     BAA10Y also still maintained. Research spike RESOLVED.
     **Deferred (held by operator):** the HY→Sentinel Bear-Score
     scoring switch — original was binary HY>5%; current is graduated
     BAA10Y. Requires backtest-derived HY-OAS graduated thresholds
     before going live. NOT done; awaiting explicit go + validation.
   - then the tracked `catalyst→earnings` rename (below).

## ✅ Per-engine data gates — DONE 2026-05-16 (operator-authorised)

Shipped: `tpcore/quality/validation/capital_gate.py` gains
`ENGINE_TABLES` (EVIDENCE-derived engine→table map, every pair a real
`platform.<table>` read with file:line documented in the module) +
`assert_passed_for_engine(pool, engine, *, require_all_green=False)`.
Table→checks is derived from the selfheal registry `source` field
(existing SoT; registry-coverage test makes it drift-proof). An
engine is blocked ONLY if a source IT reads is missing/stale/failed
at the latest run; an unrelated red no longer blocks it. Refinement,
not weakening — each engine still needs 100% of ITS data; run-
staleness still blocks everyone (safety). `assert_passed` (global
all-green) unchanged and retained as the operator override
(`CAPITAL_GATE_REQUIRE_ALL_GREEN` env, read by each plug). The
suite-gated engine plugs (reversion/vector/momentum; sigma archived
2026-05-16) call the
per-engine variant; sentinel has no suite gate (satellite model) —
untouched. NO engine strategy logic modified (gating call only). 897
tests (12 capital_gate incl. unrelated-red-doesn't-block, own-red-
blocks, override-restores-global, unknown-engine-fails-safe); ruff/
check_imports clean.

--- superseded design notes below (kept for rationale) ---

**Merit: real and growing.** Today the gate is global all-or-nothing —
`DATA_OPERATIONS_COMPLETE` / `run_all_engines.sh` / `capital_gate`
require ALL validation checks green or NO engine trades. But each
engine consumes a different data subset: Sentinel→macro/credit
(hy_spread, credit_spread, macro_indicators); Vector→earnings_events +
fundamentals; Momentum→prices_daily only; etc. `options_max_pain` and
`insider_sentiment` (added today) are consumed by **no current engine**
— yet under the global gate a red `insider_sentiment_freshness` would
block even Momentum. Over-blocks now; worse with every new adapter.

**Design:** a per-engine data-dependency map → engine X trades iff the
checks for the data X actually reads are green. Synergizes with the
selfheal orchestrator (per-engine escalation scoping).

**Constraints / why not now:**
- This is a **production trade-gating change** (touches capital_gate /
  emit / run_all_engines). NO PRODUCTION CHANGE without validation +
  explicit operator go (SCOPE DISCIPLINE).
- Stays consistent with the "100% data or don't trade" mandate — it's
  a *refinement* (each engine still needs 100% of ITS data), not a
  weakening. State that explicitly when built.
- The per-engine dependency map must be **derived from each engine's
  actual data reads** (grep the plugs), NOT invented. That derivation
  is the first sub-task.
- Sits behind the WEEK GOAL data-layer work + #132 in priority.

## ✅ Rename: `catalyst_* → earnings_*` — FEED DONE 2026-05-16

The data **feed** was renamed in lockstep (operator picked
`earnings_events`): `platform.catalyst_events → platform.earnings_events`
(idempotent migration `20260516_0800`, table + index + PK constraint,
no data dropped — 13,848 rows / 1,104 tickers verified intact); stage
`catalyst_refresh → earnings_refresh` (+ `_stage_*`/`handle_*` fns);
validation check + module file (`checks/earnings_events_freshness.py`,
`CHECK_NAME="earnings_events_freshness"`) + `KNOWN_CHECK_NAMES` +
suite vars + conftest routing + test_suite/e2e source sets; selfheal
`HealSpec(source="earnings_events")`; `audit_data_pipeline.py`; the two
one-off backfill scripts (git-mv'd); Vector's data-loading queries;
docs. `earnings_events_freshness` verified GREEN on the live renamed
table via the validation suite.

**OPEN — deliberate scope boundary, operator decision pending:** the
Vector engine's **internal scoring vocabulary** is *not* renamed —
the `VectorScore.catalyst` Pydantic field (0–35 component), the
`catalyst_magnitude` backtest CSV column header, `_has_catalyst` /
`_catalyst_window_days`, and "Catalyst-Driven Swing" branding. That is
the engine's model concept, not the feed; renaming it touches a
serialized model field + CSV schema + dashboard reads (artifact-
breaking) and was out of scope for "rename the feed." Flagged to
operator: decide whether to also purge Vector's internal "Catalyst"
vocabulary or leave the engine concept as-is.

Note: the misnomer's threshold-reasoning confusion (the
`earnings_events_freshness` "≥X% of T1/T2 in 180d" floor reasons as a
broad catalyst stream but it is quarterly earnings *beats*) remains
relevant to the still-owed SEC/earnings coverage verdict (WEEK GOAL #1).

## Autonomous self-heal — EVERY data source (P0, 2026-05-15)

> **STATUS 2026-05-16 — substantially DELIVERED (see WEEK GOAL #2/3a-c
> above).** Honest end state: 14/20 checks genuinely self-heal (real
> bounded canonical repair, verified no fake specs); 6/20 are honest
> permanent escalate-for-investigation (corruption + source-of-truth
> classes — re-pull cannot fix them; healable=False is correct, not
> pending). Per-feed cadence profile is the SoT; feed-driven dispatch
> + vendor-anchored freshness shipped. Root causes fixed not masked
> (FINRA pagination, ApeWisdom ceiling). The mandate's spirit ("runs
> on its own, no fake-green") is met; the section below is the
> original design intent, kept for rationale — remaining work is
> incremental per-feed breadth (targeting/probes), not unbuilt
> architecture.

> **🔴 OPEN INCIDENT — prices_daily coverage collapse (logged 2026-05-17).**
> `validation.prices_daily_freshness` red (ran 2026-05-16 21:30 UTC):
> `stale=True confidence=0.889`, reason `coverage_collapse` — the
> 2026-05-15 (Fri) NYSE session has only **506 tickers = 7%** of the
> ~7,634 trailing-20-session avg (MAX(date) is current so the recency
> check passes; coverage cratered underneath it — same failure class as
> the prior 91% collapse). Core ETFs SPY/GLD/IWM/SH/PSQ stop at
> 2026-05-14. Canonical fix is the existing bounded heal
> (`prices_daily_freshness` → `daily_bars --param repair_gaps=true`).
> **Decision (operator, 2026-05-17): report-only — no manual repair;
> left for the next `run_data_operations.sh` self-heal cycle to clear.**
> Re-check this entry after the next cycle; if still red, the bounded
> heal is not converging and needs root-cause (why did 2026-05-15 ingest
> only 506/7,634?). Not caused by the concurrent reversion/backtest
> session (backtests read prices_daily, they don't write daily_bars).

**Mandate (operator, verbatim intent):** "100% data, no gaps, no
bullshit, runs on its own — I cannot babysit this." This applies to the
WHOLE data layer, not just daily bars. The 2026-05-15 build delivered
true end-to-end auto-heal for `prices_daily` ONLY (zero-tolerance
completeness invariant + Step-4 auto-heal loop in
`run_data_operations.sh`). Every other source is currently
*detected + hard-gated* (red blocks the emit / engine sweep) but
*escalates to the operator* instead of self-healing. That residual
babysitting is unacceptable per the mandate — close it.

**Scope — bring each source to the same bar as `prices_daily`:**
1. **`fundamentals_quarterly`** (FMP) — define the ungameable
   completeness/correctness invariant (every addressable T1/T2 stock has
   the expected filed quarters within its active range, no missing
   period), then an auto-heal path via the canonical
   `ops.py --stage fundamentals_refresh --param …` (no one-off script).
2. **`corporate_actions`** (Alpaca) — invariant + auto-heal via the
   canonical corp-actions stage; shrinkage detector already exists,
   wire it into the heal loop.
3. **`earnings_events`** (FMP) — completeness invariant + auto-heal via
   `earnings_refresh`.
4. **`sec_insider_transactions` / SEC filings** (EDGAR) — invariant +
   auto-heal via `ops.py --stage sec_filings --backfill`.
5. **`macro_indicators`** (FRED) — invariant + auto-heal (re-pull); the
   BAMLH0A0HYM2 truncation class must self-recover.
6. **`liquidity_tiers`, `ticker_classifications`** — invariant +
   auto-heal/recompute.

**ARCHITECTURE MANDATE (binding — the shape, not negotiable):**
Self-heal is a GENERIC `tpcore` capability, NOT per-source bash.
1. **One self-heal orchestrator in `tpcore`**, beside the validation
   suite (detector + healer in the same layer). Input: the suite
   result. Per red check → dispatch to the registered healer for that
   source → bounded retry → re-validate → escalate if exhausted or
   unhealable. Pure Python, unit-testable with fake healers.
2. **Each data feed contributes only a declarative `HealSpec`**:
   {invariant = the existing validation check; canonical repair =
   which `ops.py --stage X --param …`; is-auto-healable; bounded
   retry/backoff policy}. Adding a source = registering a spec —
   ZERO bash edits, zero new branches.
3. **Heal executes ONLY via the canonical `ops.py --stage` infra.**
   The orchestrator INVOKES it; it never reimplements ingestion. No
   one-off scripts. (Standard: data_adapter_pipeline.md.)
4. **Every HealSpec is BOUNDED/targeted.** Proven 2026-05-15: a
   whole-universe `force_refresh` exceeds the 3600s stage timeout and
   can never self-heal. Targeted repair only (the `repair_gaps`
   pattern: re-pull just the invariant-flagged tickers/window).
5. **Detector/healer symmetry.** The healer's target set is computed
   from the SAME code as the check (cf. `_evaluate` shared by
   `check_prices_daily_completeness` + `compute_gap_repair_targets`)
   so they can never disagree.
6. **Process concerns stay in the bash wrapper, thin:** never emit
   `DATA_OPERATIONS_COMPLETE` unless 100% green; self-exclusion lock;
   post-close/`tpcore.calendar` gating. `run_data_operations.sh`
   becomes a thin caller of the tpcore orchestrator.
7. **`prices_daily` is the reference implementation, migrated INTO
   the orchestrator** — not a bash special case. One canonical
   mechanism, no N variants (operating-identity: symmetry/standard).

**Per-source design constraints (within the architecture above):**
- Each invariant is ungameable: physical-truth, zero-tolerance, no
  recency window, no percentage knob. Scoped to exactly the data the
  engines depend on.
- Honest heal only: a source's HealSpec must actually be able to fix
  that source's failure class. No dishonest cross-source "heal";
  not-bars-fixable → escalate, never fake-green.
- **No lazy vendor-blame.** A shortfall on authoritative data (SEC
  EDGAR especially) is OUR ingestion defect until proven per-ticker
  against the source. Threshold recalibration only after the our-gap
  hypothesis is empirically killed.
- Each source's required tickers registered where the freshness check
  can see them; add/retire the matching `audit_data_pipeline.py` check in
  the same change.

This is the path to the operator never touching data again. Until every
item above is done, the "runs on its own" mandate is only partially met
and that must be stated plainly, not glossed.

## Engine structural redesign (post-2026-05-15 sweep)

The 2026-05-15 parameter sweeps validated the targeted fixes (Sigma SPY-
regime filter, Reversion Z-relaxation + T3 expansion) at the metric level
but DSR/credibility gates remain structurally blocked.

- **Sigma — ⚰️ ARCHIVED 2026-05-16 (CLOSED).** The "final test before
  permanent retirement" was run this session — not the queued HMM path
  but the operator-approved **failed-expansion redesign** (#168:
  volatility-compression → attempted-then-failed breakout entry →
  VWAP/value-mid exit, with VIX>25 + Fear&Greed Extreme-Fear
  suppressors). Run end-to-end through the **canonical**
  `scripts/search_parameters.py` pipeline (no one-off script).
  **Verdict — decisive FAILED:** 50/50 trials negative held-back
  Sharpe (best −0.1185, mean −2.55), credibility pinned at 45 (gate is
  60), DSR 0.0000. Smoke confirmed real trades with VIX/F&G series
  loaded → true negative, not a dead-signal artifact. Operator
  confirmed archival. Engine moved `sigma/`→`archive/sigma/`; removed
  from `pyproject.toml`, `run_all_engines.sh`, `run_smoke_test.sh`,
  `run_all_searches.sh`, `ENGINE_TABLES`, tip-sheet registry, and all
  real importers (tpcore tests now duck-type `ExecutionDecision`).
  Canonical record + raw sweep CSV: `archive/sigma/EULOGY.md` /
  `archive/sigma/sigma_failed_expansion_search.csv`.
  **Scoping caveat (carried into the eulogy):** this adjudicates only
  the *directional* failed-expansion form. The **sector-neutral
  residual idea** is pursued (operator decision 2026-05-17) as a
  **Reversion enhancement** — the PCA-residual signal switch (Avellaneda
  & Lee), NOT a new standalone engine and NOT a Sigma un-archiving.
  "Sigma failed" ≠ "compression/residual mean-reversion is dead." (The
  previously-queued HMM-regime path and the rejected 2026-05-15 OU gate
  are now moot — Sigma is closed.)

- **Reversion PCA-residual switch (2026-05-17, IN PROGRESS #171-175).**
  Switch Reversion's primary signal from earnings-gated price-z fades to
  daily PCA-residual mean reversion (rolling 252d PCA on T1+T2, top-K PC
  removal, OU s-score, PCA-implied statistical groups for
  market/sector-neutral matched book, volume overlays). Shared primitive
  `tpcore/backtest/pca_residual.py`; sweep via canonical
  `search_parameters.py --engine reversion` (signal_mode adjudicates
  pca_residual vs retained price_z baseline). Train 2011-01-01,
  held-back 2022-01-01 (data can't honor the literature's 1999 start —
  28 tickers pre-2000; sector-neutral has no GICS source so PCA-implied
  groups substitute). Survivorship is the dominant risk (prices_daily
  logs ~54 delistings of 7,730 true-hundreds): terminal delisting leg
  injected AND `survivorship_inclusive=False` so credibility is capped.
  Verdict bar (operator): held-back DSR≥0.95, credibility≥60, PBO≤0.20,
  trades/param≥25, ≥150 held-back trades, no single-crisis PnL
  concentration. Live setup_detection parity (#173) is deferred until
  the sweep clears the battery — do not wire a live plug to an
  unvalidated signal (the Sigma lesson).

- **Reversion — reclassified as satellite 2026-05-15 (closed).** The
  signal-class-redesign decision was resolved by reclassifying Reversion
  as a satellite engine alongside S2: permanent 5–10% capital cap,
  per-trade graduation criteria, DSR gate retired. The combined filter
  (Z ≥ 3.0 + HIGH earnings quality) produces 19 trades / Sharpe +0.312
  / PF 1.755 / max DD −11.5% on 2018-2025 — strong per-trade metrics at
  a structurally bounded firing rate. See `docs/MASTER_PLAN.md` §4.2 and
  `backtests/reversion_satellite_backtest.json`.

## Data archival — CSV-first retrofit (DONE 2026-05-15)

**Closed.** The 2026-05-15 BAMLH0A0HYM2 incident exposed that the
CSV-first sub-protocol was implemented for only one handler. Rather
than patch FRED alone, all five ingest handlers were retrofitted to a
shared archive layer.

**Shipped:**
- `tpcore/ingestion/csv_archive.py` — shared write + gzip + shrinkage
  detection. 8 unit tests including the BAMLH0A0HYM2 truncation
  scenario (7,500 → 785 rows → `over_threshold=True`).
- All 5 handlers write a gzipped CSV archive before/after the DB
  upsert: `handle_macro_indicators`, `handle_daily_bars`,
  `handle_corporate_actions`, `handle_fundamentals_refresh`,
  `_stage_earnings_refresh`.
- **Shrinkage detection** (the vendor-truncation alarm) is wired into
  the two *full-snapshot* sources only — `fred_macro` and
  `alpaca_corporate_actions` — which re-pull their entire history every
  run, so a row-count drop unambiguously means truncation. The three
  *incremental* sources (`alpaca_daily_bars`, `fmp_fundamentals`,
  `fmp_earnings_events`) pull a variable window each run, so row-count
  shrinkage there is noise — they get the audit-trail archive but no
  alarm (a full-table baseline would false-flag their next incremental
  run; this was caught and corrected during the build).
- `scripts/dump_baseline_archives.py` — seeds baseline snapshots for
  the two full-snapshot sources so shrinkage detection has a real
  predecessor from run 1. Run once 2026-05-15.

**Compliance-matrix re-grade — DONE 2026-05-15.** The `fred` row in
`docs/superpowers/pipelines/data_adapter_pipeline.md` now rests on the
real CSV-first implementation (the ✅ previously sat on the
"trivial first pull" carve-out, which the BAA10Y backfill invalidated).
Matrix audit note + FRED row + cross-cutting summary updated. Section
fully closed — no remaining items.

## Publishing

- **Publish a GitHub gist of the entire project.** Scope: everything —
  architecture (`docs/MASTER_PLAN.md`), database + dataflow
  (`docs/DATABASE_AND_DATAFLOW.md`), operations (`docs/OPERATIONS.md`),
  style guide, engine specs (Sigma, Reversion, Vector, Momentum) with
  credibility scorecards, parameter-search methodology + walk-forward +
  held-back DSR, 5-plug architecture, FilterDiagnostics + baseline-
  equivalence framework, dashboard, the Railway/Supabase ops story.
  Public-facing — review for any embedded keys, paths, or PII before
  publishing.
- **Publish to PyPI.** Open scope — decide what gets packaged. Most likely
  candidate: `tpcore/` as a standalone library (RiskGovernor, AAR,
  parity, backtest harness, filter diagnostics, baseline-equivalence) —
  the parts that are genuinely reusable outside this repo. Engines
  (`sigma/`, `reversion/`, `vector/`, `momentum/`) and `platform/`
  schema stay private. Prereqs: pick a name (likely not `tpcore` —
  reserved/generic), pin a license, add `pyproject.toml` package
  metadata, set up `python -m build` + `twine upload`, decide on
  versioning scheme. Same key/PII review as the gist.

## Discovered follow-ups — RiskGovernor work + architecture review (2026-05-17)

Surfaced while making the RiskGovernor real + uniform (branch
`worktree-risk-governor-fix`). Recorded here so they are not lost.

**Architecture epics (operator directives 2026-05-17 — see memory
`project_three_service_architecture`):**
- **Event-driven engine services (P1 epic).** Entire engine service
  event-driven: an engine fires the moment its preconditions are met
  (data ready + market closed + setup ready), never on a clock. Time is
  a GATE/precondition, never a TRIGGER. Engine service is already
  event-driven (`DATA_OPERATIONS_COMPLETE`); the allocator is the
  time-driven outlier to convert.
- **Two-daemon consolidation.** Collapse to exactly two daemons: data
  daemon (emits readiness event) + engine daemon. AAR, forensics, and
  the allocator all move INTO the engine daemon (no separate launchd
  jobs).
- **Declarative `engine_profile` (the vehicle).** Per-engine cadence +
  precondition SoT, same proven pattern as `tpcore.feeds` /
  `tpcore.risk.limits_profile`. MUST extend the existing per-engine
  data gate ("Per-engine data gates — DONE 2026-05-16"), NOT a parallel
  mechanism. First step: inventory the existing per-engine gate.
- **Allocator → event-driven.** Fire on the readiness event + an
  idempotent "first-trading-day-of-week / already-ran-this-cycle"
  guard (it is weekly, not daily — today it has NO such guard, only the
  `(engine, allocation_date)` unique constraint prevents corruption).

**Pre-existing bugs discovered (NOT introduced by this work; out of
scope here, flagged honestly):**
- **Allocator `_engines` default is stale.** `AllocatorService.__init__`
  default `("sigma","reversion","vector","momentum")` includes ARCHIVED
  sigma and OMITS live sentinel. Production constructs it without
  `engines=`, so: the allocator never allocates capital to sentinel,
  and it re-upserts a `sigma` risk_state row every run. Needs a design
  decision (is sentinel in the inverse-vol capital set? it is a
  defensive overlay — may be intentionally excluded) then fix the
  default / unify with the canonical engine roster used by
  `run_all_engines.sh` as a single shared SoT. (T9's prune was made
  fail-safe via an explicit `_ARCHIVED_ENGINES` allowlist so this stale
  default can no longer cause live-engine data loss.)
- **`audit_pipeline.shrinkage_detector` is vacuous.** It counts
  `csv_archive.shrinkage_detected` in `application_log.message`, but
  that is a pure structlog event never written to `application_log`
  (no structlog→DB bridge in the repo). Same false premise the new
  `governor_enforcement` check was redesigned away from. Re-key it off
  persisted evidence or it is audit theatre.

**Governor follow-ups:**
- **Batch-engine slot accounting.** `open_positions` for momentum/
  sentinel is a conservative proxy (gate records +1 per gated order,
  −1 per submitted close; stale prior-holding slots not reconciled).
  Errs tight/never fails open. Follow-up: reconcile against broker
  positions / AAR for an exact concurrent count.
- **`ALLOCATOR_PRUNED_RISK_STATE` audit payload** `live_engines` field
  is informational-only and slightly misleading (lists `self._engines`
  incl. stale sigma) — cosmetic cleanup.
- **Verify real-state substrate end-to-end once an engine graduates**
  (allocator feeds `engine_equity`; trade_monitor/AAR feed pnl/
  positions). The `tpcore.risk.equity_unallocated` WARNING surfaces a
  still-placeholder equity — watch for it post-graduation.
