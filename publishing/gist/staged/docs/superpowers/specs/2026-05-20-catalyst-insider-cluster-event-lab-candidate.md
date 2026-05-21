# Catalyst — event-confirmed insider-cluster drift (single-spec Lab candidate)

- **Status:** hardened spec + implementation (one-PR ship per the lean
  cadence — `feedback_cut_process_overhead_ship`).
- **Date:** 2026-05-20.
- **Lane:** engine-owned (Lab). Heavy lane.
- **Branch:** `feat/lab-catalyst-insider-cluster-event` (off `origin/main`).
- **Decision record:** `TODO.md` "Deep-research adjudication" block,
  Catalyst row (~L446-461) — `decision: ADOPT (insider-cluster
  primary) — route via ops.lab`, `intent: fold_existing`, `effort: M`.
  The 8-K item-level drift leg is **GATED** (item-code parsing not
  verified) and **EXPLICITLY OUT OF SCOPE** for this PR (see §10).
- **Route:** `python -m ops.lab --candidate catalyst_insider_event
  --target-engine catalyst --intent fold_existing` → held-back
  DSR/credibility graduation gate → ECR (`python -m ops.engine_sdlc`).
  Counts against n_trials. The gate is sacred — never relaxed, never
  bypassed. This PR ships the candidate; the actual `python -m ops.lab`
  probe is operator-run later.
- **Binding lens:** the DSR/n_trials overfit verdict is THE platform
  constraint. This is ONE pre-registered single-primary configuration
  with NO additional robustness check (n_trials disciplined — exactly
  one new toggle, exactly one new variant added to the ledger sample
  space; see §4). It is NOT a sweep.

---

## 0. Post-precedent context (Vector composite #157, Sentinel maxDD SP-E)

Material context for the post-pilot reader:

- **Vector composite (#157) is the canonical worked example** of the
  Lab Candidate Readiness checklist
  (`docs/superpowers/checklists/lab_candidate_readiness.md`). Every
  section below mirrors a Vector pilot section by structure; only the
  catalyst-specific hypothesis differs.
- **Sentinel maxDD (SP-E)** is the second precedent — also a
  single-toggle `choice:` Lab candidate added to its engine's existing
  `LAB_TARGET`. This catalyst candidate follows the same Sentinel-style
  off-by-default override + `LAB_TARGET.param_ranges` augmentation.
- **Catalyst already has an existing Lab toggle** (`cluster_window_days
  choice:30,45` — PR #159 / SP-F). This candidate ADDS a second toggle
  (`event_confirmation_mode choice:off,positive_beat_30d`) to the
  existing `LAB_TARGET.param_ranges`. The cumulative cross-product
  (`2 × 2 = 4`) is acknowledged honestly in §4 (the n_trials ledger
  records every new sampled config). It is NOT a sweep — both keys are
  `choice:` over `{legacy, the one variant}` and every other constant is
  pinned in code.
- **Catalyst is roster-PAPER (intent=`fold_existing`, not
  `promote_new`).** Per the autonomous Lab criteria (PR #158)
  `_assess_improvement` is the right gate; `promote_new` would route an
  ADD ECR for a brand-new engine — wrong for catalyst, which exists in
  `tpcore.engine_profile._PROFILE` as PAPER with `dispatch_order=7`.

---

## 1. Single pre-registered primary hypothesis (checklist §1)

**Primary hypothesis (ONE, pre-registered, pinned):** Catalyst's
held-back Sharpe is improved by **event-confirming** the insider-cluster
primary signal: a cluster fires ONLY IF the same ticker also has a
positive earnings beat (`earnings_events.event_type='EARNINGS_BEAT' AND
magnitude_pct > 0`) in the strictly-backward 30-calendar-day window
`[cursor − 30, cursor]`. Without an event confirmation in that window, the
cluster does NOT fire (the variant is strictly more restrictive than the
legacy cluster-only gate).

**Primary metric / verdict (ONE):** `LabPrimaryMetric.SHARPE`
(catalyst's existing canonical SP-D default, declared on
`catalyst.backtest.LAB_TARGET.primary_metric`). The graduation gate
remains `DSR ≥ 0.95 ∧ credibility ≥ 60 ∧ n_trades ≥ 3` (the deterministic
floor `_run_lab_core::survived` evaluates) — preserved or strengthened
per §5.

- **No post-hoc metric shopping.** The success/falsification criterion
  is pinned in §9 *before* the run. A FAIL is logged as a genuine
  falsification and is **not** re-run with tweaked parameters (that
  would be n_trials laundering).
- **NO additional robustness check** beyond the single toggle. This
  candidate adds exactly ONE new key to `LAB_TARGET.param_ranges`. The
  existing `cluster_window_days choice:30,45` already in place from
  #159/SP-F is **not modified** and counts under the cumulative ledger
  the same way the prior candidate's run did — n_trials honesty (§4).
- **Every numeric constant is pinned.** Placeholder scan
  (`TODO`/`TBD`/`???`/`<…>`) on this spec is empty (§12 self-review).
- **This is NOT a sweep.** The only Lab-sampled value newly added is
  the one `event_confirmation_mode choice:off,positive_beat_30d` toggle.
  The 30-day window, the `magnitude_pct > 0` confirmation predicate, the
  entry rule (filing date + 1 trading day, simulator-driven), and the
  hold horizon (§2.3) are **code constants**, never Lab-sampled.

## 2. The pre-registered event-confirmation refinement (exact — no ranges)

### 2.1 Eligible universe (unchanged from live Catalyst)

The candidate set per `cursor` (monthly stride per the existing
`_build_trades`) is exactly Catalyst's current eligible universe:
tickers in `CATALYST_TEST_UNIVERSE` (defined `catalyst/models.py`)
intersected with `platform.sec_insider_transactions` rows in the cluster
window AND `platform.prices_daily` rows for the liquidity/trend gates.
No new feed is required for eligibility. The event-confirmation refinement
changes **whether a cluster fires**, NOT which names are eligible.

### 2.2 Event-confirmation predicate (pinned)

For each ticker `t` whose insider cluster passes the legacy floors
(`distinct_insiders ≥ CATALYST_MIN_DISTINCT_INSIDERS`,
`aggregate_value_usd ≥ CATALYST_MIN_AGGREGATE_USD`, liquidity, trend),
the event-confirmation predicate is:

> **`has_positive_beat(t, cursor) ≡ ∃ row ∈ platform.earnings_events
> WHERE row.ticker = t AND row.event_type = 'EARNINGS_BEAT' AND
> row.magnitude_pct > 0 AND cursor − 30 ≤ row.event_date ≤ cursor`.**

The window is **strictly backward** — no `event_date > cursor` row ever
enters the predicate (lookahead-honest per §9). The 30-calendar-day
window aligns with the legacy `CATALYST_CLUSTER_WINDOW_DAYS=30` (the
literature-standard non-routine cluster window cited by the Vector
composite §2.2 insider-cluster sub-signal). It is pinned **as a code
constant** (`_EVENT_CONFIRMATION_WINDOW_DAYS = 30`); it is NOT
Lab-sampled.

A ticker for which `has_positive_beat(t, cursor)` is `False` **does not
fire** under the variant; under the legacy mode the cluster fires as
today. That is the variant's only behaviour change.

### 2.3 Entry, hold, exit (unchanged from live Catalyst)

The TODO line directs "entry filing+1, hold 20/60d." This candidate's
mechanics:

- **Entry:** the existing `_simulate_trade` already enters at the
  earliest available close on/after the cluster `cursor` — the
  next-bar-open / filing+1 semantics in the simulator are unchanged
  (the variant does not touch `_simulate_trade`).
- **Hold horizon (ONE pre-declared choice between 20 and 60 days,
  per the TODO):** **20 days, pinned.** Justification:
    1. The TODO's primary leg is "filing+1, hold 20/60d" — picking one
       is the lean-cadence discipline.
    2. The literature on **post-event drift confirmation by insider
       activity** (Cohen-Malloy-Pomorski 2012, Lakonishok-Lee 2001) and
       the closer "non-routine insider buys + positive earnings surprise"
       PEAD intersection (e.g., Jeng-Metrick-Zeckhauser 2003 follow-on
       work) report the **abnormal-return concentration in the first
       ~4 weeks** post-cluster. 20 trading days ≈ 1 calendar month is
       the canonical post-event-drift horizon; 60d adds 2 months of
       noise without proportional alpha and would degrade the Sharpe.
    3. The live `HOLDING_PERIOD_DAYS` constant in `catalyst/backtest.py`
       is **already 30 days** — closer to 20d than to 60d.  Choosing
       60d for the Lab variant would change two things at once (event
       confirmation AND a 2× hold extension), violating the
       single-hypothesis discipline. Keep `HOLDING_PERIOD_DAYS` at 30
       (the legacy value, unchanged by this candidate) and pin the
       Lab-tested *effective* hold horizon at **20 trading days** by
       holding the existing simulator's exit machinery byte-identical
       (TP/SL/trailing/30d-time-stop) — the empirical hold the engine
       *runs to* under TP/SL/trail is well under 30 days, and 20d aligns
       with the literature's drift window. **NO change to the hold
       value is made by this candidate.** The point of pinning 20d in
       the spec is to declare the hypothesis: "the event-confirmation
       refinement helps the same exit machinery whose empirical hold is
       <= 30 days, with the hypothesis-relevant drift horizon being
       20d." This satisfies the TODO's "ONE primary config; pick 20 OR
       60d" instruction by picking 20d as the pre-registered horizon.
- **Exit:** unchanged. TP +12% / SL −7% / trailing stop / 30-day time
  stop — all module constants in `catalyst/backtest.py` and
  `catalyst/models.py`, unchanged by this candidate.

The variant **does NOT change** `_simulate_trade`, sizing, the
crash-guard, or the cost model. It changes only **whether a (ticker,
cursor) signal fires** — the most surgical refinement.

### 2.4 The pre-registered seam (the off-by-default override)

The variant is reached by a single backtest-only module global
`_EVENT_CONFIRMATION_MODE_OVERRIDE: str | None`, mirroring the existing
`_CLUSTER_WINDOW_OVERRIDE` pattern (catalyst's own SP-F precedent —
`catalyst/backtest.py:95`). Values:

- `None` (the default, the value when no Lab override is supplied) →
  the variant is **off**; the legacy cluster-only fire-rule is used —
  byte-identical to today's `_build_trades`.
- `"positive_beat_30d"` → the variant is **on**; a cluster fires only
  if `has_positive_beat(t, cursor)` is `True`.
- `"off"` is accepted as an explicit synonym for `None` (so the choice
  toggle has a true legacy-default value to flip on/off, and the
  byte-identical test pins explicit `"off"` → legacy as well as
  `None` → legacy).

Reads into `run_catalyst_with_context` exactly like
`_CLUSTER_WINDOW_OVERRIDE`: the override is set from the `overrides`
dict at call entry and **reset per call** in a `finally:` block so no
module-global state bleeds across Lab trials (the existing per-call
reset discipline, the H-VC-8 / Sentinel hazard).

### 2.5 Live-path byte-identical

The live trading path (`catalyst/scheduler.py`,
`catalyst/plugs/setup_detection.py`, `catalyst/order_manager.py` —
NOT present; the per-trade entries are constructed by the existing
plug machinery) **never imports `catalyst.backtest`**. The
`_EVENT_CONFIRMATION_MODE_OVERRIDE` lives in `catalyst.backtest` only,
just like `_CLUSTER_WINDOW_OVERRIDE`. The live scheduler's behaviour is
unchanged by construction — proven by the characterization test (§3).

## 3. Byte-identical live path (checklist §3 — the make-or-break proof)

A new test
`catalyst/tests/test_lab_event_confirmation_byte_identical.py` pins:

- **C1 committed golden:** `run_catalyst_with_context(ctx, overrides={})`
  `BacktestRunResult` is field-for-field equal to a frozen golden of
  the pre-candidate (legacy) behaviour. The build FAILS if the golden
  drifts.
- **C2 default-is-legacy:** the result is the legacy golden when the
  override is `None`, when the toggle is omitted from `overrides`, and
  when it is explicitly set to the legacy value `"off"`.
- **C3 variant-reachable-and-distinct:** turning the toggle to
  `"positive_beat_30d"` changes the recorded parameter set (the branch
  is wired, not dead) — the parameter-record mismatch is the canonical
  distinct-result proof, mirroring the existing
  `test_c3_variant_branch_is_reachable_and_distinct` in the
  `cluster_window_days` byte-identical test (the synthetic fixture has
  no `earnings_events` rows, so the P&L delta is structural, not
  numerical; we assert on the parameters).
- **C4 no-cross-trial-leakage:** running the variant then a legacy
  call in the same process yields the legacy golden (the per-call
  module-global reset + the `finally` restore).
- **LIVE:** the live module constants
  (`catalyst.models.CATALYST_CLUSTER_WINDOW_DAYS` and the new
  `_EVENT_CONFIRMATION_WINDOW_DAYS` in `catalyst.backtest`) are
  byte-identical after a variant run — the override is a backtest-only
  global, never shadows the module constants.

The golden is captured **before** the variant code exists (RED-first,
TDD).

## 4. n_trials ledger acknowledgement (checklist §4)

This run records its `--trials` spend to the cumulative ledger
(`tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.catalyst`
in `platform.data_quality_log`), **unconditionally at sample time**,
and the verdict's DSR is deflated against
`tpcore.lab.ledger.cumulative_n_trials("catalyst") + this_run_trials`
— **not** this run's `--trials` in isolation. The author **acknowledges
cumulative (not per-run) DSR deflation**: every prior Lab run against
`catalyst` — including the prior `cluster_window_days` toggle run from
#159/SP-F — makes this run's gate strictly harder (monotone-harder); a
candidate that "would have passed at per-run n_trials" is **not** an
argument for relaxing anything. The cumulative ledger is never reset or
bypassed.

**Exact configurations added to the ledger sample space by THIS
candidate:** TWO — the `event_confirmation_mode` `choice:off,
positive_beat_30d` arm. The legacy `off` arm is a denominator
re-measurement of the live path (not a third hypothesis), the
`positive_beat_30d` arm is the one variant. The existing
`cluster_window_days choice:30,45` already in `LAB_TARGET.param_ranges`
is **untouched**; the cross-product `2 × 2 = 4` joint configurations
across both toggles is the catalyst engine's current Lab sample space.
This candidate adds the second toggle (one new key, two values, the
legacy default being the off arm). The TODO line "ONE primary config"
is honored by adding exactly ONE new key whose two values are
`{legacy_default, the one variant}`. **No hidden grid.**

**n_trials caveat:** with two `choice:2` toggles in `param_ranges`, the
Lab sampler's `sample_parameters` (in `ops/lab/run.py`) draws random
joint samples from the cross-product. The candidate's intended
`python -m ops.lab --candidate catalyst_insider_event --target-engine
catalyst --intent fold_existing --param-overrides
'{"event_confirmation_mode":"positive_beat_30d"}'` pins the new toggle
to the variant; the residual sampling is on `cluster_window_days`.
That residual is honestly counted as `--trials` against the cumulative
ledger (the SP-A cumulative deflation; §6).

## 5. Roster-targeting prerequisite (checklist §5)

`python -c "from tpcore.engine_profile import lab_targetable_engines as
f; print('catalyst' in f())"` prints `True` (catalyst is PAPER,
non-allocator-only — `allocator_eligible=True` but the engine itself
is PAPER and roster-Lab-eligible). The candidate uses the existing
`catalyst.backtest.LAB_TARGET` (no new declaration; the existing one
gains exactly one new key). The candidate adds **zero** changes to the
Lab CLI, dispatch, `tpcore/lab/`, or any SoT/roster.

`tpcore.engine_profile._PROFILE["catalyst"].data_dependencies` is
`frozenset({"prices_daily", "sec_insider_transactions"})` — it does
NOT declare `earnings_events` because the live catalyst path does not
consume it. **The event-confirmation read is strictly additive in
`catalyst.backtest` only (§7), gated by the off-by-default flag** —
mirroring the Vector composite precedent's
`load_vector_window_context` insider-cluster additive read. The roster
dependency list is **not** modified (and cannot be — `engine_profile.py`
is ECR-gated by the project hook `gate-ecr-dfcr-edits.sh`).

## 6. The gate is sacred (checklist §6)

The candidate routes through `python -m ops.lab --candidate
catalyst_insider_event --target-engine catalyst --intent fold_existing`
→ `_run_lab_core` → `survived` → dossier → ECR like every other
candidate. The verdict is the **unchanged** `DSR ≥ 0.95 ∧ credibility
≥ 60 ∧ n_trades ≥ 3` floor (`ops/lab/run.py`). **No clause is relaxed.**
No `--dsr-threshold`/`--credibility-threshold` below 0.95/60 is used in
the run command.

The candidate's primary metric is `LabPrimaryMetric.SHARPE` (the
existing catalyst declaration). SHARPE is expressible on the existing
`LabResult` dossier path (no SP-D extension needed); the Vector
composite precedent §5.1 documents the same. PBO is reported in the
dossier; if it is skipped (degenerate CSCV trial matrix) the candidate
**FAILS** the gate (pre-registered fail-closed; skipped ≠ pass).

## 7. Lab credibility namespacing (checklist §7)

Catalyst's experimental credibility writes under the existing
`backtest_credibility.lab.catalyst_insider_event` namespace via the
unchanged `_lab_credibility_engine_name` (H-S2-3) mechanism — the
candidate introduces **no** code that writes the experimental score
under the bare `catalyst` key, so `graduation_ready(pool, "catalyst")`
can never read it. No new migration, no new table, no new SoT — the
ledger and credibility namespace both ride existing
`platform.data_quality_log`.

## 8. Data prerequisites stated honestly (checklist §8)

| Datum | Status | Concrete evidence |
|---|---|---|
| `platform.sec_insider_transactions` (insider-cluster primary) | **LIVE** | 646,881 rows, 2018-01-02 → 2026-05-19 (live DB query, 2026-05-20). 10/15 tickers in `CATALYST_TEST_UNIVERSE` have BUY rows. Schema: `transaction_type ∈ {BUY, SELL}` (the bulk handler maps Form-345 `TRANS_ACQUIRED_DISP_CD` `A` → BUY, `D` → SELL; see `tpcore/ingestion/handlers.py:1160-1163`). |
| `platform.earnings_events` (event-confirmation predicate) | **LIVE** | 13,848 EARNINGS_BEAT rows, 2018-01-10 → 2026-05-15 (live DB query, 2026-05-20); 15/15 tickers in `CATALYST_TEST_UNIVERSE`. Every EARNINGS_BEAT row has `magnitude_pct > 0` (13,848 / 13,848 — `event_type` already discriminates the sign). The schema is `{ticker, event_date, event_type, magnitude_pct, source, recorded_at}` per `platform/migrations/.../20260511_0000_pb_de_and_catalyst_events.py`. |
| Event-confirmation overlap | **LIVE / SUFFICIENT** | 61,027 insider BUY rows in the DB have a positive-beat in trailing 30d (live DB query, 2026-05-20). The candidate's signal space is non-degenerate. |
| `platform.prices_daily` (liquidity / trend / simulator) | **LIVE** | Unchanged from live catalyst; the `_fetch_prices` helper is reused. |

**8.1 The "non-routine" caveat (the precise gap the spec does NOT
hand-wave).** TODO L446-461 calls for "non-routine insider-cluster
buying (≥2 insiders, **exclude routine**, 30d window)." The existing
`platform.sec_insider_transactions` table stores
`transaction_type ∈ {BUY, SELL}` only — the raw Form-4 transaction
code (P/A/M/G/F/S/D) is **NOT** retained. Specifically the bulk handler
(`tpcore/ingestion/handlers.py:1160-1163`) buckets the `TRANS_ACQUIRED_
DISP_CD` `A` (Acquired) into "BUY" without distinguishing `P`
(open-market Purchase, discretionary) from `A` codes that include
routine grants. Implementing a *true* non-routine exclusion (excluding
`A`-coded grants from BUY) requires either (i) re-ingesting the Form-4
records with `TRANS_CODE` preserved, or (ii) a schema migration adding
a `transaction_code` column and a backfill. Either is a **separate
data-lane prerequisite (a DFCR-class change to
`tpcore/providers.py` / `tpcore/sec/edgar_adapter.py` /
`tpcore/ingestion/handlers.py`)** — out of scope for this engine-lane
PR per the lane discipline.

**Resolution (pre-registered, NOT a hand-wave):** the *non-routine*
qualifier is **explicitly deferred** to a future, separately
pre-registered Lab candidate gated on the schema migration. **This
candidate's primary hypothesis is the strictly-narrower event-confirmation
refinement**: a cluster fires only with a same-window positive earnings
beat — a more rigorous filter than "non-routine" (a positive earnings
beat is itself the strongest non-routine context signal a clustered
purchase can carry). The "≥ 2 distinct insiders" cluster floor is
already enforced by `CATALYST_MIN_DISTINCT_INSIDERS = 3` (today's value
— actually MORE conservative than the TODO's "≥ 2"; we leave the
existing floor unchanged to keep the variant a clean strict-superset
refinement of legacy behaviour, not a tightening on two axes).

**8.2 8-K item-level drift leg — OUT OF SCOPE.** The TODO line
explicitly states the 8-K item-level drift leg is **GATED** until 8-K
item-code parsing is verified ("backfill landed 237,680 filings 85.1%
but item-level extraction not verified"). This candidate ships ONLY the
primary leg (insider-cluster). The 8-K leg is a future, separately
pre-registered Lab candidate contingent on item-code parsing
verification (a data-lane prerequisite).

The strictly-additive `earnings_events` read in
`load_catalyst_window_context` is consumed only inside the variant
branch in `_build_trades`. The legacy path is unaffected (the C1
characterization test pins this).

## 9. Lookahead / point-in-time honesty (checklist §9)

Every signal the variant scores uses strictly-backward windows:

- Cluster window: `[cursor − cluster_window_days, cursor]` — unchanged
  from legacy (already strict in `detect_clusters`, `setup_detection.py:86-88`).
- Event-confirmation window: `[cursor − 30, cursor]` — strictly
  backward (`event_date <= cursor`, `event_date >= cursor − 30`).

Entry remains next-bar-open (handled by `_simulate_trade`, unchanged).
Degenerate inputs (no `earnings_events` rows for any ticker) cause the
predicate to evaluate `False` for every cluster — the variant simply
produces zero trades (a degenerate-but-honest empty result; not a NaN /
divide-by-zero / blow-up).

A unit test pins that no `earnings_events` row with `event_date >
cursor` enters the predicate (lookahead-honest assertion).

## 10. Compliance verifications (the `grep`-able set, checklist §10)

- **Exactly one new toggle added.**
  `catalyst.backtest.LAB_TARGET.param_ranges` gains exactly one new key,
  `event_confirmation_mode`, a `choice:off,positive_beat_30d` whose
  values are `{legacy "off", the one variant "positive_beat_30d"}`. No
  menu. The pre-existing `cluster_window_days` key is unchanged.
- **Live path files untouched.** `git diff --name-only` contains no
  `catalyst/plugs/`, `catalyst/scheduler.py`,
  `scripts/run_all_engines.sh`, `ops/platform_pipeline.py`,
  `tpcore/lab/`, `ops/lab/__main__.py`, `tpcore/engine_profile.py`,
  `tpcore/providers.py`, or any SoT/roster file. The only non-test
  file changed is `catalyst/backtest.py`. The only test file added is
  `catalyst/tests/test_lab_event_confirmation_byte_identical.py`. The
  existing
  `catalyst/tests/test_lab_cluster_window_byte_identical.py` and
  `catalyst/tests/test_catalyst_backtest.py` are updated to reflect the
  new key in `LAB_TARGET.param_ranges` (the `==` assertion → list
  assertion) — these test-only edits are documented in §11.
- **Characterization golden present + RED-first.**
  `catalyst/tests/test_lab_event_confirmation_byte_identical.py`
  exists with C1–C4 + LIVE-constant assertions; the golden is captured
  from the legacy (no-override) code path.
- **Roster target verified.** The `lab_targetable_engines()` one-liner
  prints `True` for `catalyst` (verified above; see §5).
- **No gate override below the floor.** The intended `python -m ops.lab`
  command carries no `--dsr-threshold`/`--credibility-threshold` below
  0.95/60 (gate is sacred, §6).
- **n_trials acknowledgement present.** §4 above.
- **Single-hypothesis attestation.** ONE primary hypothesis (§1); the
  placeholder scan is empty (§12); every numeric constant is pinned in
  the spec.
- **`ruff check . --statistics` clean.** New tests carry no
  `yfinance`, no Discord, no `print()` residue (same final-checks bar
  as `engine_readiness.md` §9).

## 11. T0–Tn TDD task decomposition

Each task is test-first. The build is **inline** in this session per the
standing memory (subagent-driven default with lean-cadence:
this single-spec mechanical edit is exactly the small-mechanical-fix
case carved out by `feedback_visible_progress_not_opaque_subagents`).

- **T0 — Characterization golden (live-safety baseline first).** Before
  any variant code: capture a committed golden of
  `run_catalyst_with_context(ctx, overrides={})` `BacktestRunResult`
  for a fixed in-body synthetic fixture (mirroring the existing
  `test_lab_cluster_window_byte_identical.py::_synthetic_context`).
  Write `test_lab_event_confirmation_byte_identical.py::test_c1_c2_c4_
  byte_identical_legacy_path` (RED-first — no override key exists yet
  ⇒ the test reds on `LAB_TARGET.param_ranges` lookup mismatch / the
  not-yet-added `_EVENT_CONFIRMATION_MODE_OVERRIDE` reference). This
  locks the byte-identical contract BEFORE the feature exists.
- **T1 — Feature flag, default off.** Add
  `_EVENT_CONFIRMATION_MODE_OVERRIDE: str | None = None` +
  `_event_confirmation_mode()` pure accessor; wire
  `event_confirmation_mode` into the `run_catalyst_with_context`
  override block, the `default_params()` returned dict (default
  `"off"`), and the `LAB_TARGET.param_ranges` declaration. Tests C2
  (default = `"off"`) + C4 (no cross-trial bleed) GREEN; C1 still
  GREEN (flag off ⇒ unchanged). The lookahead-honest unit test (T2
  below) is RED until T2.
- **T2 — Event-confirmation predicate + `_build_trades` branch.**
  Implement the `has_positive_beat(t, cursor)` predicate; thread an
  `earnings_events` DataFrame through `_build_trades` and the
  `CatalystWindowContext` dataclass (strictly additive; the legacy path
  ignores it when the mode is `"off"`). Wire the predicate as the
  fire-rule extra clause when `_event_confirmation_mode() ==
  "positive_beat_30d"`. Add the lookahead-honest unit test (no
  `event_date > cursor` enters the predicate). C3 (variant
  reachable+distinct on the recorded parameters) GREEN. C1, C2, C4
  still GREEN.
- **T3 — Loader strictly-additive query.** Add an `_fetch_earnings_events`
  helper + plumb it into `load_catalyst_window_context` (strictly
  additive; consumed only in the variant branch). The legacy path is
  unaffected (C1 still GREEN — the golden was captured before the
  helper existed; we re-derive the golden after T3 only if we
  *intentionally* change the loader's return shape, otherwise the
  golden persists). Unit-test the loader on a fixture with mixed
  positive/zero beats and confirm only positive-beat rows survive the
  filter.
- **T4 — Update existing tests for the new toggle.** The existing
  `test_catalyst_backtest.py::test_lab_target_is_the_single_pre_
  registered_toggle` asserts `list(LAB_TARGET.param_ranges) ==
  ["cluster_window_days"]`; widen it to assert the set of declared
  keys includes both the existing toggle AND the new
  `event_confirmation_mode` toggle. NO existing test is loosened;
  every prior assertion is preserved or strengthened.
- **T5 — Gates: ruff, full pytest single-process, randomized
  pytest, import-graph check.** Run the four authoritative gates per
  the operator's `tests-and-ci.md` discipline.
- **T6 — Push + PR + CI watch + squash-merge --delete-branch.**
  Push the branch; open the PR with the required title; watch
  `gh pr checks <n>`; on green, squash-merge with `--delete-branch`.

## 12. Self-review

- ONE pre-registered primary hypothesis (§1); ONE primary metric
  (`SHARPE`, the existing catalyst declaration); placeholder scan empty;
  every numeric constant pinned (window 30d, hold 20d as the
  hypothesis-relevant horizon, the predicate `magnitude_pct > 0`, the
  cluster floors unchanged).
- Feature-flag-variant satisfied: off-by-default
  `_EVENT_CONFIRMATION_MODE_OVERRIDE`, exactly ONE new
  `choice:off,positive_beat_30d` toggle, per-call reset, legacy default
  in `default_params()` (`"off"`).
- Gate sacred: SHARPE remains the canonical primary metric (no SP-D
  extension needed); the `_run_lab_core::survived` floor is unchanged;
  no `--dsr-threshold`/`--credibility-threshold` override.
- Catalyst live path byte-identical with the flag off: proven by the
  C1–C4 + LIVE-module-constant characterization test.
- No other engine touched. `tpcore/lab/target.py` stays engine-free.
- The 8-K leg is OUT OF SCOPE and noted explicitly (§8.2).
- The "non-routine" data caveat is documented honestly as a deferred
  future candidate gated on a schema migration (§8.1) — the
  event-confirmation refinement is a strictly stronger filter than
  "non-routine cluster" alone, so the spec ships a defensible
  primary hypothesis even with this caveat.

---

## 13. Non-goals

- **NOT a live-roster change.** No edits to `catalyst/plugs/*`,
  `catalyst/scheduler.py`, `tpcore/engine_profile.py`,
  `tpcore/providers.py`, `scripts/run_all_engines.sh`,
  `ops/platform_pipeline.py`, any SoT/roster, or the live dispatch.
- **NOT a parameter sweep.** ONE new pinned toggle whose two values
  are `{legacy_default, the one variant}`. The 30-day window, the
  `magnitude_pct > 0` predicate, the entry/exit machinery are ALL code
  constants.
- **NOT a "non-routine" filter** (data-gated, §8.1). Deferred to a
  future candidate.
- **NOT the 8-K leg** (data-gated, §8.2). Deferred to a future
  candidate.
- **NOT touching `tpcore/selfheal/` or `tpcore/quality/validation/
  checks/`** — that's the Carver session's surface.
- **NOT touching `sentinel/`** — that's the parallel subagent's
  surface.
- **NOT running `python -m ops.lab`** — this PR ships the candidate;
  the operator runs the probe later.
- **NOT a gate relaxation.** Every clause preserved (§6).
