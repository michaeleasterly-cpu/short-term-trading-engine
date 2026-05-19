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
   - ✅ **`prices_daily_gaps` 14-day-recency blind spot — CLOSED (DONE-
     stale).** Superseded by the ungameable zero-tolerance invariant
     `tpcore/quality/validation/checks/prices_daily_completeness.py`
     (its module docstring L1-9 names this exact blind spot; no recency
     window, no >7d-run minimum — ANY missing (ticker, session) in the
     30-session liquid window fails). The widening of the heuristic
     `prices_daily_gaps` audit check is moot — the invariant gate is the
     correct mechanism (registered in `KNOWN_CHECK_NAMES`, healable via
     `daily_bars --param repair_gaps=true`).
   - sporadic `row_velocity`: tighten (currently only fires on total
     silence; misses sustained severe partial degradation).
     `[lane: data-lane-mine] [gate: none] [needs operator decision: no]
     [effort: S]` — VERIFIED still open: `scripts/audit_data_pipeline.py`
     L1136-1144, sporadic branch WARNs only on `recent == 0 and prior > 0`.
   - FMP handler-path CSV archive: verify end-to-end (presence unproven).
     `[lane: data-lane-mine] [gate: none] [needs operator decision: no]
     [effort: S]` — the `csv_archive_presence` audit check now covers
     `fmp_fundamentals` (`scripts/audit_data_pipeline.py` L584-607,
     `ARCHIVE_SOURCES` L178-181); remaining work is the runtime end-to-end
     proof that `handle_fundamentals_refresh` actually writes the archive
     on a real pull (a verification task, not a missing-code gap).
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

**Scope — bring each source to the same bar as `prices_daily`** —
`[lane: data-lane-mine] [gate: none] [needs operator decision: no]
[effort: L]` **VERIFIED GENUINELY OPEN 2026-05-18:** only
`prices_daily_completeness.py` is an ungameable completeness invariant.
The other 6 sources have `*_freshness` checks + `healable=True` re-pull
HealSpecs (`tpcore/selfheal/registry.py` L114-177) but NO completeness
invariant module — `ls tpcore/quality/validation/checks/` shows no
`fundamentals/corporate_actions/earnings/sec/macro/liquidity/classif`
`_completeness.py`. Auto-heal-via-re-pull exists; the *zero-tolerance
physical-truth invariant* per source does not. This is the binding
residual of the "runs on its own" mandate:
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
   BAMLH0A0HYM2 truncation class must self-recover. **Partial:** the
   auto-heal-re-pull half is DONE (`tpcore/selfheal/registry.py` L124-126,
   `macro_indicators_freshness` → `healable=True` stage
   `macro_indicators`); the *ungameable completeness invariant* half is
   still open (no `macro_indicators_completeness.py`).
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

## #186 — Remaining deterministic data agents

- ✅ **candidate (5): audit-driven referential remediation — DONE
  2026-05-17.** `tpcore/auditheal/` — structured cross-table audit
  (`tpcore/audit/cross_table.py`, persisted to `data_quality_log` as
  `cross_table_audit.*` rows) + bounded `cross_ref_cleanup` remediation
  loop + ENFORCED Step-3 gate (previously theatre: `audit_all_tables.py`
  always exited 0, a 🔴 printed and the cycle continued). Launch scope
  strictly the two `tradier_options_chains` checks (expired / orphan);
  all other cross-table checks are escalate-only. PRs #26 (P1 structured
  audit + persistence), #28 (P2 `tpcore/auditheal` loop, dark), #29
  (P3 wire Step 3 + enforce gate).
- **candidates (3)/(4): largely realized by #165** (per-feed cadence
  profile, TRIGGER facet, TARGETING, PUBLICATION — see WEEK GOAL §3a-c
  above). Remaining: incremental per-adapter targeting/probe rollout
  (each a one-entry increment, not unbuilt architecture).
- ✅ **candidate (6): schema/contract-drift sentinel — DONE 2026-05-17.** `tpcore/ingestion/adapter_contract.py` — declared `ADAPTER_CONTRACTS` SoT (all 12 CSV-first feeds; clockwork drift test == CSV-first feed set); `assert_contract_populated` raises before load when a required adapter-output field is systematically empty across a non-empty pull (producer hard-stop; symptom-level detection; escalate-only, no auto-heal); 4 high-risk feeds enforced (fred_macro/iborrowdesk_borrow_rates/finra_short_interest/apewisdom_social_sentiment), rest `guard_pending`; thin Step-4c `adapter_contract` known_knowns check adds coverage/visibility + 24h-escalation FAIL. PRs #32 (P1 registry+helper dark) / #33 (P2 enforce 4 high-risk handlers) / #35 (P3 thin Step-4c check). (3)/(4) realized by #165; (5) auditheal done; **(6) done** ⇒ remaining deterministic-agents work = the Data Supervisor (Escalation & Hardening Ladder rung 2) + #187 LLM triage (rung 3).

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

- **Reversion PCA-residual switch (2026-05-17, #171-175).** `[lane:
  engine-owned] [gate: operator verdict bar — held-back DSR≥0.95 etc.]
  [needs operator decision: yes — adjudication on sweep results]
  [effort: L]` **VERIFIED NOT STARTED IN CODE 2026-05-18:**
  `tpcore/backtest/pca_residual.py` does not exist; no `signal_mode` /
  `pca_residual` symbol anywhere under `reversion/`. Status line below
  said "IN PROGRESS" — that is a plan, not shipped code. Engine-lane
  work; do not action from the data lane.
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

## Lab-isolation DB proofs not CI-enforced (follow-up, 2026-05-19)

INHERITED from SP2 (predates SP-A; SP-A only added tpcore/lab/ledger.py).
`.github/workflows/ci.yml` `test` job has no `services: postgres` / no
`DATABASE_URL`, so 5 DB-gated suites skip in CI: test_lab_isolation,
test_db_read_only, test_aar_writer, test_fundamentals_cache,
test_persistent_store. The make-or-break SP-A proofs
(test_cumulative_n_trials_real_db_integer_correctness [H-LL-9],
test_lab_ledger_disjoint_from_live_graduation [T-LIVE/H-LL-4]) are merge-time
proven by the mandatory operator-run compensating control, NOT by CI.

- **Dedicated `lab-isolation-db` CI job (scoped, zero repo-wide blast radius).**
  `[lane: shared-infra] [decision: build separately, NOT in SP-A] [effort: M]`
  Add a NEW ci.yml job (NOT a repo-wide pytest-with-DB): `services: postgres:16`,
  `env: DATABASE_URL=postgres://...localhost`, that (1) runs Alembic upgrade head
  (schema bootstrap), (2) seeds a minimal `platform.prices_daily` fixture for the
  reversion walk-forward, (3) runs ONLY `pytest tpcore/tests/test_lab_isolation.py
  tpcore/tests/test_lab_credibility_pool_threaded.py -q`. Pin to those node IDs —
  do NOT un-skip the other 4 dormant DB suites (data-lane-owned; separate
  adjudication). KNOWN: test_read_pool_rejects_write_and_guards_fire fails
  against pooled Supabase but should pass against a direct ephemeral Postgres
  (no pgbouncer) — verify when building. Cross-ref SP-A, #242.

## ✅ Code-sweep findings — engine-lane-owned — ALL SHIPPED 2026-05-19 (CONSOLIDATED v2)

Data-lane sweep handoff v2 (superseded the prior 4-item). Engine lane owned
these. **All 5 findings SHIPPED to origin/main 2026-05-19** (operator
decision: "findings only — stop after #3"; the Lab front-half epic remainder
is paused — see the Lab-front-half epic block below). Data lane tracks/fixes
its own side in project_code_sweep_findings_2026_05_19.md (will not
duplicate). Closed record kept legible for cross-session history:

- ✅ **SHIPPED PR #104 (096ff68) — #1 [HIGH] DSR null-variance estimator too
  lenient (= SP-A2, task #147).** tpcore/backtest/overfitting.py
  _expected_max_sharpe_under_null used single-estimator variance
  sr_variance=1/(n_obs-1); corrected to the Bailey & López de Prado (SSRN
  2460551) selection-bias dispersion V[SR̂ₙ] across the N searched trials —
  the live DSR gate genuinely tightens (was too lenient, not yet exploited:
  all engines fail DSR). Pre-existing & orthogonal to SP-A. Spec
  `docs/superpowers/specs/2026-05-19-dsr-null-variance-fix.md`, plan
  `docs/superpowers/plans/2026-05-19-dsr-null-variance-fix.md`.
- ✅ **SHIPPED PR #106 (bdd8736) — #2 [MED] Credibility rubric mislabelled
  the gate threshold (+ latent VALUE bug).** tpcore/backtest/credibility.py
  flag/description said "> 0.90" but the real gate is DSR_PASS_THRESHOLD=0.95;
  renamed to key off the constant. **Worse than first stated:** the sweep
  also surfaced a latent VALUE bug in statistical_validation.py (hardcoded
  0.90 → DSR_PASS_THRESHOLD) — not just a label, an actual wrong threshold
  fixed in the same PR.
- ✅ **SHIPPED PR #107 (edadf12) — #3 [MED] Inverse-vol allocator volatility
  estimator.** tpcore/allocator/service.py realized_vol used
  statistics.pstdev (population ÷N); **two coupled defects fixed:** (a)
  pstdev → statistics.stdev sample (ddof=1), consistent with
  overfitting.py _per_trade_sharpe; (b) the estimator ran over raw
  absolute per-session P&L — normalized to returns before inverse-weighting
  (the worse-than-stated half: the input, not just ddof, was wrong).
- ✅ **SHIPPED PR #105 (680cb44) — #4 [HIGH] Blocking sync Anthropic client
  in async engine-triage daemon (CROSS-LANE #244).**
  ops/engine_llm_triage.py synchronous Anthropic() awaited inside the
  llm_triage_service loop → migrated to anthropic.AsyncAnthropic + await
  client.messages.create(...). Engine twin / symmetric mirror of the
  data-lane fix #97 (twin ops/llm_data_triage.py); AsyncAnthropic chosen
  (async-native) per the #244 cross-lane alignment.
- ✅ **SHIPPED PR #96 (40bd8a5) — #5 [HIGH] asyncpg pooler-safety missing in
  3 direct create_pool sites.** ops/engine_sdlc/planner.py + ops/lab/run.py
  (×2). planner._emit_audit + lab.run._load_universe_by_tier routed through
  the canonical tpcore.db.build_asyncpg_pool (post data-lane FIX-2 #95); the
  legacy credibility-write site kept a raw asyncpg.create_pool to preserve
  the SP-A H-S3-8 byte-identical-legacy isolation invariant
  (test_lab_credibility_pool_threaded.py) with the pooler-safety kwargs
  (statement_cache_size=0 + server_settings jit:off) mirrored inline + a
  "keep in sync with tpcore.db.build_asyncpg_pool" comment.

## Lab front-half epic — PAUSED at a clean milestone (2026-05-19)

Operator-approved epic (memory `project_lab_front_half_epic.md` is the
**authoritative full state** — cross-ref it before resuming). The Lab is
half-built (back-half correct); the front-half builds the anti-overfit
safety floor + roster-driven plug-and-play targeting + readiness checklist
+ pluggable scoring; absorbs Sentinel/Catalyst; prerequisite for #242 (now
engine-lane). `[lane: engine-owned] [needs operator decision: YES — explicit
go to resume]`

**Safety floor SHIPPED (the only part operator authorised this session):**
- ✅ **SP-A — cross-candidate n_trials ledger — SHIPPED PR #93 (96e6ce6).**
  The anti-overfit-laundering safety floor: every Lab candidate honestly
  counted against a persistent cumulative n_trials so a tuned spec can't be
  laundered past the DSR gate by splitting trials across runs.
- ✅ **SP-A2 — DSR null-variance estimator correction — SHIPPED PR #104
  (096ff68)** (= code-sweep Finding #1, task #147; see the shipped
  code-sweep block above). With SP-A, this completes the safety floor: the
  gate now counts trials honestly AND deflates correctly.

**EPIC PAUSED — operator decision 2026-05-19: "findings only — stop after
#3".** SP-B..SP-G are **DEFERRED to an explicit future go — do NOT
auto-start SP-B.** This is a clean milestone: the safety floor (SP-A +
SP-A2) is the only piece that needed to land before pausing; nothing is
half-built.

**RESUME POINT (when the operator gives the explicit go):**
- **SP-B = roster-driven plug-and-play Lab targeting.** Replace the
  hardcoded 3-tuple in `ops/lab/run.py` (`_runner_for` / `PARAM_RANGES`)
  and the `ops.lab` CLI `--target-engine` choices — drive all of it from
  `tpcore.engine_profile` (the roster SoT) so a new/retuned engine is
  Lab-targetable without editing the Lab. This is the first step on
  resume.
- Then SP-C — Lab Candidate Readiness checklist (seeded by the Vector
  pilot spec, commit `0a94414` on branch `lab-candidates-rollthrough`) →
  SP-D → SP-E → SP-F → SP-G (= #242, research-LLM edge-discovery,
  engine-lane-owned per `project_research_llm_edge_discovery.md`).

## Deep-research spike adjudication — Lab-candidate backlog (2026-05-19)

Decision record from the two commissioned edge-research spikes
(`deep-research-report.md` / `deep-research-report2.md`, expert-reviewed
2026-05-19). Binding lens: the DSR/n_trials overfit verdict is THE
constraint. Every accepted edge is ONE pre-registered single-primary-spec
Lab candidate routed `python -m ops.lab` → DSR/credibility graduation gate
→ ECR (`python -m ops.engine_sdlc`); honestly counted against n_trials; at
most ONE pre-declared robustness check (counted as a trial, NOT a sweep);
the reports' own success bars preserved/strengthened, never relaxed. NEVER
bypass the gate. Meta-track cross-ref: #242. The reports' multi-value
grids (`--pca-components 8,10,12,15`, `--family-weights` menus) ARE the
n_trials hazard and are explicitly rejected — single config only.

- **Reversion PCA-residual — CORROBORATED, folds into #171-175 (no new
  item).** `[lane: engine-owned] [gate: operator verdict bar — held-back
  DSR≥0.95/cred≥60/PBO≤0.20/trades-param≥25/≥150 held-back trades/no
  single-crisis PnL] [decision: fold] [effort: L]` Both spikes' flagship
  rec (Avellaneda–Lee daily PCA residuals) IS #171-175 — do NOT create a
  duplicate. Literature Sharpe (1.44, 1997–2007) is NOT evidence it
  survives THIS data/period/costs. Genuinely-new nuance captured as
  sub-notes under #171-175 ONLY, each at most ONE pre-declared robustness
  check (NOT sweep dimensions): (a) volume / "trading-time" overlay
  (Avellaneda ETF 1.51); (b) ETF-residual crisis fallback when systematic
  correlation dominates PCA. Cross-ref #171-175, #242.

- **Vector — sector-relative composite score (single-spec Lab
  candidate).** `[lane: engine-owned] [gate: held-back DSR≥0.95 + cred≥60
  + PBO≤0.20 + ≥150 held-back trades + ≥3× current gate-model candidate
  count + no family >70% score variance] [decision: ADOPT — route via
  ops.lab] [effort: M]` Replace the AND-gate with ONE fixed-weight
  sector-relative composite: target-engine vector; ONE primary config —
  value/catalyst/technical weights = 0.35/0.40/0.25, sector-relative
  standardization, top-decile selection, ONE pre-registered
  sector-neutralization choice (reports disagree long-only vs long-short;
  pick one, do NOT test both). Catalyst family = earnings_events +
  insider-cluster (data live). Data prereq: none beyond live feeds. Via
  `python -m ops.lab --candidate vector_composite --target-engine vector
  --intent fold_existing` → graduation gate → ECR; counts against
  n_trials; NEVER bypass the gate.

- **Sentinel — graduated Bear Score (single-spec Lab candidate).**
  `[lane: engine-owned] [gate: maxDD reduction ≥30% vs base + ulcer
  improvement + median inverse-ETF hold <20d + no single-recession PnL
  concentration] [decision: ADOPT — route via ops.lab] [effort: M]`
  Graduated (scaled-defense) vs binary flip. ONE pre-registered config,
  literature-anchored thresholds (Sahm ≥0.50, CFNAI-MA3 ≤−0.70,
  SOS ≥0.20 — external, not fitted: the anti-overfit anchor): weights
  0.30/0.15/0.20/0.15/0.20 (Sahm/SOS/curve/CFNAI/HY-OAS), bands
  0.45/0.60/0.80, inverse-ETF cap 25% of defensive capital, Treasuries/
  gold-first. n_trials caveat: weight×band surface is large — ONE spec
  only, ONE pre-declared robustness check max. Data prereq: confirm
  credit-spread (hy_spread/credit_spread) series wired into live FRED
  ingestion BEFORE the Lab run. Via `python -m ops.lab --candidate
  sentinel_bear_score --target-engine sentinel --intent fold_existing` →
  graduation gate → ECR; counts against n_trials; NEVER bypass the gate.

- **Catalyst — event-confirmed insider-cluster drift (single-spec Lab
  candidate; 8-K leg data-gated).** `[lane: engine-owned] [gate:
  held-back DSR≥0.95 + cred≥60 + PBO≤0.20 + ≥150 held-back trades +
  positive post-2020 held-back alpha + better hit-rate than pure
  post-beat drift] [decision: ADOPT (insider-cluster primary) — route via
  ops.lab] [effort: M]` Plain large-cap PEAD discarded (both spikes;
  too arbitraged). Primary leg = non-routine insider-cluster buying
  (≥2 insiders, exclude routine, 30d window) confirming a positive
  corporate event/earnings beat — DATA READY (WEEK-GOAL SEC backfill:
  646,107 Form-345 rows 84.1% T1-T2). 8-K item-level drift leg is GATED:
  do NOT run until 8-K item-code parsing is confirmed (backfill landed
  237,680 filings 85.1% but item-level extraction not verified). ONE
  primary config, entry filing+1, hold 20/60d. Via `python -m ops.lab
  --candidate catalyst_insider_drift --target-engine catalyst --intent
  promote_new` → graduation gate → ECR; counts against n_trials; NEVER
  bypass the gate.

- **Momentum — vol-managed 12-1 + earnings/revenue overlay.** `[lane:
  engine-owned] [gate: held-back DSR≥0.95 + lower crash DD than current
  paper spec] [decision: DEFER — paper-research lane] [effort: M]` Real
  structural direction (vol-targeting + fundamental overlay) but lowest
  (impact×prob)/effort vs the binding constraint; monthly rebalance ⇒
  slow DSR evidence accrual; engine already paper-trading + self-gated.
  Deferred to the paper-research lane; promote to a single-spec Lab
  candidate only if a top-three slot frees and capacity exists.

- **REJECTED: Sigma sector-neutral failed-break / compression+
  failed-expansion residual fade.** Sigma ARCHIVED 2026-05-16 (two honest
  FAILED gate attempts; `archive/sigma/EULOGY.md`). The sector-neutral
  residual idea is already the Reversion enhancement #171-175 per the
  EULOGY scoping caveat — NOT a Sigma revival, NOT a new item. Durable
  decision; do not re-litigate.

- **REJECTED: S2 systematic short-squeeze engine.** Data-parked
  (point-in-time securities-lending + options-positioning history absent;
  FINRA short-interest structurally bi-monthly). Both spikes independently
  say archive/manual-only; matches the existing platform decision. Not
  backtestable now — a DATA limitation, not modeling. Reopen ONLY if
  point-in-time securities-lending + options-positioning history is
  acquired; then route as a single-spec Lab candidate. Do not re-litigate.

## ⚠ PRE-RAILWAY MIGRATION BLOCKER — archive substrate (LOCKED design 2026-05-18)

**Do NOT let a Railway cutover silently ship the broken substrate.**
The vendor-truncation `shrinkage_detector` + the whole CSV-first
archive are hardwired to a persistent **local FS**
(`csv_archive.repo_data_dir()` = `Path(__file__).parents[2]/"data"`;
no env/volume override; `railway.json` has no volume). On Railway's
**ephemeral container FS**: detection silently always-passes (empty
`data/` → emits OK = "checked nothing" — worst class for live money),
`csv_archive_presence` flaps, recovery substrate evaporates. Expert
verdict (2026-05-18): snapshot-vs-single-prior-CSV is the wrong
substrate even on the Mac (poisoned baseline; gradual <20%/snapshot
erosion invisible; only 5 full-snapshot sources).

**LOCKED design (operator-approved 2026-05-18; built AT migration,
not now — Railway paused, re-enable deferred until an engine proves
edge):** `[lane: data-mine][gate: Railway-re-enable][decision: made][effort: L]`
- **Detection → D2:** persist per-source row-count / min-max-date /
  coverage to **Postgres** each ingest; shrinkage = deviation vs
  rolling-median of durable history (host-agnostic; reuses the
  `prices_daily_completeness`/freshness pattern; fixes the local
  flaws too). [D3 = fold full-snapshot sources into a completeness
  physical invariant — stronger/larger; D2 is primary.]
- **Recovery → R3:** CSV-first archive → an **S3-compatible
  object-storage bucket attached to the service** (Railway-attached /
  Supabase Storage / R2 / S3) via S3 API + env-injected creds. Keeps
  the CSV-first canonical workflow; host-agnostic. [R2 Volume =
  weaker fallback; R4 Postgres-BYTEA rejected — 8GB Supabase budget.]
- A bucket alone is necessary-for-recovery, NOT sufficient: detection
  must become DB-derived regardless. Exact Railway bucket wiring is a
  migration-time detail to verify vs current Railway docs.
- **Zero-risk preps done now (separate PR, no Railway infra):**
  (1) `repo_data_dir()` honors `TP_DATA_DIR` env (default unchanged)
  — the R2/R3 seam; (2) empty-archive shrinkage path → WARN/UNKNOWN,
  never silent OK — a "no fake-green" latent-bug fix.
- Memory: `project_railway_archive_substrate_migration`. Sequencing:
  re-base detection onto Postgres BEFORE Railway re-enable.

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

## Review-found defects — the durable surface (#254 register)

A review-found defect (found by verify-before-acting / a failing test /
a code review — NOT a deterministic-agent escalation) no longer lives
ONLY as an ad-hoc TODO line. ✅ **Consolidated Defect Register — BUILT
2026-05-19 (#254: DR1 #90, DR2 #91, DR3 this PR).** The durable home is
`python -m ops.defect_register log --ref <#NNN|slug> --summary "…"`
(retention-exempt `REVIEW_DEFECT_LOGGED`; resolve with `… resolve --ref
<r> --pr <#NNN|sha>`). It composes BOTH Escalation & Hardening Ladders
verbatim + the review class, joined by `defect_ref`; surfaced read-only
on the dashboard Health tab and via `python -m ops.defect_register
list`. **Convention:** a TODO line for a still-open review-found defect
carries a `[defect_ref: X]` tag and MUST have a matching open
`REVIEW_DEFECT_LOGGED` (CI forcing-test — a review defect cannot live
only in TODO.md and be forgotten). `[lane: ops] [gate: none] [needs
operator decision: no] [effort: done]`

- **OPEN — `test_lab_ntrials_ledger.py` collection-time `del sys.modules`
  eviction defect.** `[lane: engine] [defect_ref: #148] [gate: none]
  [needs operator decision: no] [effort: S]` Pre-existing engine-lane
  defect (NOT a code-sweep finding — its own tracked task #148, surfaced
  alongside the SP-A n_trials ledger work): `tpcore/tests/
  test_lab_ntrials_ledger.py` does a collection-time `del sys.modules[...]`
  that evicts a shared module — **subset-collection-order-only**; the full
  single-process suite is GREEN (no production / CI-gate impact).
  Canonical fix = scope the eviction per-test (not at collection time).
  Do **NOT** fix opportunistically — it is its own task.

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
- ✅ **Allocator `_engines` stale default — FIXED (DONE-stale).** The
  design decision was made and the default unified to a canonical SoT:
  `AllocatorService.__init__` now defaults to `_DEFAULT_ENGINES =
  allocator_eligible_engines()` (`tpcore/allocator/service.py` L44,
  L85-87, L151) — derived from `tpcore.engine_profile`, NOT the
  hardcoded `("sigma","reversion","vector","momentum")`. Decision
  recorded inline (service.py L141-150): **sigma removed** (archived),
  **sentinel intentionally excluded** (defensive macro overlay budgeted
  by `SentinelCapitalGate` 10–20% cap, not the inverse-vol pool),
  **canary excluded by omission** (spec §5a). `_ARCHIVED_ENGINES =
  archived_engines()` (L85) keeps the prune fail-safe. This was a
  pre-existing bug, now closed.
- ✅ **`audit_pipeline.shrinkage_detector` re-keyed — FIXED (DONE-
  stale).** No longer keyed off the never-written `application_log`
  structlog event. `scripts/audit_data_pipeline.py` `_detect_archive_
  shrinkage()` (L184-214) is now **pool-free and disk-only**: it
  compares each `ARCHIVE_SOURCES` source's latest on-disk `.csv.gz`
  archive to its predecessor via `tpcore.ingestion.csv_archive.
  detect_shrinkage` — real persisted evidence, not theatre. Finding
  rendered at L217-260.

**Governor follow-ups:**
- ✅ **Batch-engine slot accounting — RESOLVED 2026-05-19 (B1#82 + B2#87 + A1#88).** Root fixed, not deferred: B1 introduced the idempotent `record_close`/`risk_close_ledger` arbiter (never-fail-open hardening + reusable primitive); B2 fixed the REAL dual-decrement (reversion/vector `order_manager.reconcile()` `−1` now routes through `record_close`, keyed by the shared bare `open_orders.trade_id`); A1 added the `max(proxy, broker_floor)` never-fail-open last-line raise (opt-in `reconcile_open_floor=True` for momentum/sentinel). **Remaining deferred:** per-engine broker attribution (needs `client_order_id` engine tagging; cross-engine over-count is strictly tighter/safe meanwhile). `[lane: platform-overlay (RiskGovernor)] [gate: none] [needs operator decision: no] [effort: S]`
- ✅ **`ALLOCATOR_PRUNED_RISK_STATE` `live_engines` payload — MOOT
  (resolved as a side-effect).** `self._engines` no longer includes
  stale sigma (now `allocator_eligible_engines()` — see the fixed
  allocator default above), so the payload at
  `tpcore/allocator/service.py` L242 is now accurate. No separate
  cosmetic cleanup needed.
- **Verify real-state substrate end-to-end once an engine graduates**
  (allocator feeds `engine_equity`; trade_monitor/AAR feed pnl/
  positions). The `tpcore.risk.equity_unallocated` WARNING surfaces a
  still-placeholder equity — watch for it post-graduation. `[lane:
  platform-overlay] [gate: blocked — no engine has graduated (all 4
  fail DSR)] [needs operator decision: no] [effort: M]` — VERIFIED
  genuinely open AND gated; cannot be actioned until a graduation
  event exists. Park until then.
