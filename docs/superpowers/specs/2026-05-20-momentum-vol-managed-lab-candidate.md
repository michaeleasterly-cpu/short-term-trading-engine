# Momentum — vol-managed 12-1 + earnings-beat overlay (Lab candidate)

**Status:** PRE-REGISTERED (single hypothesis, pinned). Single-spec Lab
candidate per TODO.md L463-470 ("Deep-research adjudication block").
**Lane:** engine-owned (Lab). Heavy lane.
**Branch:** `feat/lab-momentum-vol-managed` (off `origin/main` @ `e1f26e6`).
**Date:** 2026-05-20.
**Intent:** `fold_existing` (MODIFY-class re-tune of an existing PAPER
engine — momentum is currently paper-trading and self-gated on its own
credibility row).
**Readiness checklist:** `docs/superpowers/checklists/lab_candidate_readiness.md`
(every section ticked below; §10 mechanical set at the end).
**Autonomous adjudication gate:** for a `fold_existing` MODIFY candidate
the adjudication path is `_assess_improvement` per
`docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md` (PR #158)
— the candidate dossier must beat the incumbent on `primary_metric =
SHARPE` (strict), pass the new-engine signal floor as a safety floor,
and keep the trade-count drift bounded (no "better Sharpe by trading
90% less"). The absolute `DSR ≥ 0.95 ∧ credibility ≥ 60` clauses in §5
below are still recorded but are no longer the binding gate.
**Operator decision (TODO.md L463-470):** `[lane: engine-owned] [gate:
held-back DSR≥0.95 + lower crash DD than current paper spec] [decision:
DEFER — paper-research lane] [effort: M]`. DEFER was about
operator-attention prioritization; the operator's "find another task and
keep going" authorization (2026-05-20) covers building the candidate now
so it is **ready to run** when monthly cadence accrues enough trade
history. **Building the candidate** ≠ **running the probe** — the probe
itself is operator-triggered later.

---

## 0. Context (the structural direction picked by the deep-research
adjudication)

TODO.md L463-470 describes "**Momentum — vol-managed 12-1 + earnings/
revenue overlay**" as the highest-evidence structural fix for the
Momentum engine's two known weaknesses (per the TODO adjudication):

1. **Crash drawdown.** Classical 12-1 cross-sectional momentum is
   vulnerable to **momentum crashes** — sharp drawdowns when high-beta
   recent winners reverse hard (Daniel & Moskowitz 2016, "Momentum
   Crashes"). The current engine has no vol-scaling; a high-vol decile
   in a turning regime is sized identically to a low-vol decile in a
   trending regime, magnifying the crash.
2. **Selection noise.** A pure 12-1 sort lets any high-past-return name
   into the decile regardless of whether the **fundamental story
   confirms the price move**. The TODO direction names "earnings/revenue
   overlay" as the canonical fundamental filter — entry restricted to
   tickers whose recent fundamentals support the rally.

This candidate tests **ONE pre-registered combined fix**:
**vol-managed sizing + earnings-beat overlay**, against the sacred
held-back DSR ≥ 0.95 ∧ lower-crash-DD-than-current-paper-spec gate.

---

## 1. Single pre-registered primary hypothesis (checklist §1)

**Primary hypothesis (ONE, pre-registered, pinned):** The combined
**vol-managed sizing (annualized target σ = 0.40) + recent-EARNINGS_BEAT
overlay (≤ 90 calendar days backward)** delivers a held-back DSR ≥ 0.95
AND a strictly **less-negative held-back max drawdown** than the legacy
12-1 baseline measured on the **same held-back window**.

**Primary metric / verdict (ONE):** `LabPrimaryMetric.SHARPE` (the
default ranking objective; the gate is the DSR/credibility floor +
TODO.md's "lower crash DD than current paper spec" clause expressible on
the existing `LabResult.held_metrics.max_drawdown` field — no SP-D
extension required). Sharpe is the appropriate ranking objective for a
return-generating overlay; the maxDD clause is **additive gate evidence
read off the dossier**, not a SP-D metric-family swap.

- **No post-hoc metric shopping.** Success/falsification is pinned in §9
  *before* the run. A FAIL is logged as a genuine falsification and the
  candidate is **NOT** re-run with tweaked parameters (that would be a
  sweep / n_trials laundering).
- **At most ONE pre-declared robustness check:** **NONE.** This
  candidate carries the primary hypothesis only. No sub-ablation, no
  alternate-overlay swap, no second toggle. (The checklist allows
  *at most* one; this candidate uses *zero* — the cleanest possible
  Lab footprint.)
- **Every numeric constant is pinned.** Placeholder scan
  (`TODO`/`TBD`/`???`/`<…>`) is empty in this spec body. See §2.
- **This is NOT a sweep.** The only Lab-sampled value is the single
  `vol_managed_mode` `choice:legacy,vol_managed` toggle. Every other
  knob (target vol = 0.40, vol window = 60 trading days, vol clip =
  [0.5, 2.0], earnings-beat window = 90 calendar days backward,
  earnings-beat magnitude floor > 0) is a **code constant**, never
  Lab-sampled.

### 1.1 Picked overlay choice (the one pre-registered fundamental
overlay)

The TODO names "**earnings/revenue overlay**" — the operator's
instruction is to **pre-register ONE** of `{earnings-beat,
revenue-growth}`, not both. This spec pins:

> **PINNED CHOICE: earnings-beat overlay (recent EARNINGS_BEAT in
> [sim_date − 90d, sim_date], magnitude_pct > 0).**

Justification (not adjudication optimism — concrete data + design
constraints):

1. **Data fit.** `platform.earnings_events` has **13,848 EARNINGS_BEAT
   rows / 1,104 distinct tickers** covering 2018-01-10..2026-05-15
   (live DB query, 2026-05-20). `magnitude_pct` is **fully populated**
   (13,848 / 13,848, all > 0 — the row exists iff a positive beat
   occurred). This matches the momentum T1+T2 universe size; no
   widening, no synthetic NULL handling.
2. **PIT cleanliness.** `earnings_events.event_date` is a single PIT
   timestamp — no joining-by-period-label, no filing_date-vs-period-end
   skew. The strictly-backward `[sim_date − 90d, sim_date]` window is
   trivially lookahead-honest (a unit test pins this; see §9).
3. **Discriminatory power.** PEAD (post-earnings announcement drift) is
   the most-cited cross-sectional anomaly with the strongest tie to
   12-month momentum (Chan/Jegadeesh/Lakonishok 1996; Lewellen 2010);
   the earnings-beat overlay is the literature-canonical confirmation
   signal for momentum.
4. **Avoids the YoY revenue join.** Revenue-YoY needs 4-quarter
   lookback per name + period-label matching + handling of
   irregular filing cadence; that is **incremental complexity** with
   no clear edge over event-date earnings, and would itself become a
   research degree of freedom (rolling window length, period-label
   semantics) — exactly the kind of hidden grid the binding constraint
   forbids.
5. **Vector precedent.** Vector's existing live engine + Vector's
   composite Lab candidate (PR #157) both use `earnings_events
   EARNINGS_BEAT` with strictly-backward windows; momentum reusing the
   same SQL idiom keeps the codebase consistent and the test fixtures
   transferable.

The revenue-growth overlay is **explicitly NOT** in this candidate. A
separate future candidate may test revenue-YoY against momentum —
that's not this one.

### 1.2 Picked vol target value (the one pre-registered annualized σ)

The deep-research vol-managed momentum literature converges on
**σ_target = 0.40** (annualized) as the canonical target for monthly
rebalanced 12-1 momentum:

- Daniel & Moskowitz (2016) "Momentum Crashes" — vol-managed momentum
  with σ_target ≈ 12% monthly ≈ **41% annualized** (page 232, Table 5).
- Barroso & Santa-Clara (2015) "Momentum Has Its Moments" — analogous
  monthly vol target ≈ 12% monthly.
- Moreira & Muir (2017) "Volatility-Managed Portfolios" — generalizes
  the same scaling rule across factors.

PINNED: `TARGET_ANNUAL_VOL = 0.40`. Vol window: `VOL_WINDOW_TRADING_DAYS
= 60` (≈ 3 months, the standard short-window realized vol used in the
academic vol-managed literature for a 12-1 strategy). Vol-scale clip:
`[0.5, 2.0]` — bounds the per-name leverage to prevent a degenerate
near-zero realized-vol stub from blowing the size up unbounded. None
of these are Lab-sampled.

---

## 2. The single pre-registered spec (exact — no ranges)

### 2.1 Universe + scoring (unchanged from legacy)

The candidate universe is **exactly** the legacy momentum universe per
`sim_date`: T1+T2 tickers from `platform.liquidity_tiers` with `skip +
lookback` bars of continuous prior history that pass
`is_tradeable_common_stock` at the entry-bar close. The 12-1 raw score
`(p_{t−skip} / p_{t−skip−lookback}) − 1` is unchanged.

### 2.2 Vol-managed sizing (the variant's first lever)

For each name `n` selected into the top decile at rebalance date `t`:

1. Compute trailing **realized annualized vol** over the
   `VOL_WINDOW_TRADING_DAYS = 60` bars **strictly prior to** the
   entry-bar (`[t_entry − 60, t_entry − 1]`):
   `σ_n = std(daily_log_returns) × sqrt(252)`.
2. Compute the **vol-scale**:
   `s_n = clip(TARGET_ANNUAL_VOL / σ_n, 0.5, 2.0) = clip(0.40 / σ_n,
   0.5, 2.0)`.
3. The trade's **realized return** is scaled:
   `pnl_pct_scaled = s_n × pnl_pct_raw` (legacy slippage-adjusted raw
   PnL ×  the vol-scale).

This is the strict vol-managed-momentum scaling rule from the
literature: a name with abnormally high realized vol is downsized; an
abnormally low-vol name is upsized — both clipped at the bounds. The
cost model is **unchanged** (tier round-trip costs apply to the
notional, which is itself proportional; vol-scaling commutes with the
multiplicative slippage adjustment).

**Degenerate guard (pinned):** if `σ_n ≤ 1e-6` (effectively zero realized
vol — possible for an unusually quiet bar window or a malformed
sub-window), `s_n := 1.0` (no scaling, the most conservative neutral).
Unit-tested.

### 2.3 Earnings-beat overlay (the variant's second lever)

For each name `n` in the top decile candidate set at rebalance date
`t`, look up `platform.earnings_events` with
`event_type = 'EARNINGS_BEAT'`:

```
∃ row: ticker = n AND
       event_date IN [t − EARNINGS_LOOKBACK_DAYS, t] AND
       magnitude_pct > 0
```

where `EARNINGS_LOOKBACK_DAYS = 90` (calendar days, strictly backward).
A name with no such row is **excluded from the trade set for this
rebalance**; one with at least one such row trades with the vol-managed
sizing of §2.2.

This is a **filter**, not a score weight — a name either has the
confirming beat or it does not. Filtering preserves the literature's
PEAD-confirmation semantic; it is NOT a "soft" overlay that re-weights
by magnitude (that would be a sweep dimension).

### 2.4 Cadence + entry/exit (unchanged from legacy)

Monthly rebalance on the last NYSE session of each calendar month;
entry at the next session's open (× 1+slippage); exit at the close of
the `HOLD_DAYS = 21` trading-day-later session (× 1−slippage). The
`_compute_one_rebalance` machinery — date selection, top-decile fraction
0.10, slippage tier model, `is_tradeable_common_stock` filter — is **all
unchanged**. The candidate adds two **post-decile** transformations
(filter + size); it does NOT change which raw 12-1 scores rank where.

### 2.5 The ONE Lab-sampled toggle (the canonical shape)

Exactly ONE new key is added to `momentum.backtest.LAB_TARGET.
param_ranges`:

```python
"vol_managed_mode": (0, 0, "choice:legacy,vol_managed"),
```

The `choice:` kind's two members are `{legacy_default 'legacy', the one
variant 'vol_managed'}`. The legacy `'legacy'` value re-measures the
existing 12-1 path verbatim (no vol scaling, no earnings overlay). The
variant `'vol_managed'` reaches the §2.2 + §2.3 combined branch.

**Why combined and not two toggles:** the TODO names ONE structural
direction ("vol-managed 12-1 + earnings/revenue overlay") — a SINGLE
hypothesis that **both** levers together produce the gate-clearing
combination. Splitting into two toggles would create a 4-cell sweep
(none/vol-only/overlay-only/both) and **inflate n_trials by 4×** for the
same evidence. The pre-registration is the **combined** spec.

---

## 3. Live-safety design (feature-flag-OFF ⇒ byte-identical live path)

This is the make-or-break invariant.

### 3.1 Off-by-default feature flag in `momentum/backtest.py`

A new module-level override `_VOL_MANAGED_OVERRIDE: str | None = None`
mirrors the existing `_*_OVERRIDE` pattern (`_LOOKBACK_OVERRIDE`,
`_SKIP_OVERRIDE`, `_HOLD_OVERRIDE`, `_TOP_DECILE_OVERRIDE`). A pure
accessor `_vol_managed_mode() -> str` returns `"vol_managed"` iff the
override is exactly the string `"vol_managed"`, else `"legacy"` (the
legacy default when the override is `None`).

- When `_vol_managed_mode() == "legacy"` (the default, and the value when
  no Lab override is supplied), `_run_backtest` / `run_momentum_with_
  context` / `run_for_search` execute the **existing 12-1 code path
  verbatim**. The new module's vol-scaling + overlay code is in a branch
  that is never entered.
- When `_vol_managed_mode() == "vol_managed"` (set ONLY by an explicit
  Lab `overrides={"vol_managed_mode": "vol_managed"}`), `_run_backtest`
  dispatches to `momentum.lab_vol_managed.run_vol_managed_backtest(...)`
  which applies the §2.2 + §2.3 transformations.

The flag is read the same way the existing overrides are read in
`run_momentum_with_context` (the `overrides` dict → module global,
reset each call). No environment variable, no config file, no
default-on path anywhere.

### 3.2 The live roster path is untouched

`momentum/scheduler.py`, `momentum/plugs/*`, `momentum/models.py` are
**NOT modified** by this candidate. The vol-managed logic lives ONLY in
a new module `momentum/lab_vol_managed.py` (Lab-only — never imported
by the scheduler) + the `_VOL_MANAGED_OVERRIDE` seam in
`momentum/backtest.py`. **`momentum/scheduler.py` does not import
`momentum.backtest` either** (verified: the scheduler's imports are
`momentum.models`, `momentum.plugs.*`, `tpcore.*` only). Therefore the
live path never enters the new module — byte-identical by construction.

### 3.3 The characterization test that pins byte-identical (T-C)

A new test `momentum/tests/test_lab_vol_managed_byte_identical.py`
asserts:

- **C1 (default path unchanged):** for a fixed `MomentumWindowContext`
  fixture, `run_momentum_with_context(ctx, overrides={...legacy keys
  only...})` returns a `BacktestRunResult` whose load-bearing fields
  match a pinned baseline. No legacy override key changes behaviour.
- **C2 (flag default is legacy):** `_vol_managed_mode()` returns
  `"legacy"` when `_VOL_MANAGED_OVERRIDE is None` AND when `overrides`
  omits the toggle AND when `overrides={"vol_managed_mode": "legacy"}`.
- **C3 (vol_managed is reachable & distinct):** with
  `overrides={"vol_managed_mode": "vol_managed"}` the resulting
  `parameters["vol_managed_mode"]` round-trips into the dossier (proves
  the branch is wired, not dead); on a fixture with both earnings and
  diverse vol the trade-set output is distinct from legacy.
- **C4 (no cross-trial leakage):** running C3 then C1 in the same
  process yields C1's pinned baseline (per-call override reset, no
  cross-trial state bleed).
- **Live-path import-isolation:** a `grep`/`importlib.util.find_spec`
  assertion proves `momentum.scheduler` does NOT import
  `momentum.backtest` or `momentum.lab_vol_managed` (the strongest
  byte-identical proof — the live path cannot transitively reach the
  Lab branch).

### 3.4 Lab credibility namespacing (H-S2-3, reused as-is)

`ops/lab/run.py::_lab_credibility_engine_name` already persists Lab
credibility under `lab.<candidate>` (here:
`backtest_credibility.lab.momentum_vol_managed`) whenever
`candidate is not None`. `graduation_ready(pool, "momentum")` reads
`backtest_credibility.momentum` and can **never** read the experimental
score. **No change to this mechanism is required or made** — the
candidate introduces no code that writes the experimental score under
the bare `momentum` key.

---

## 4. Lab integration (zero CLI/dispatch change)

### 4.1 The ONE new toggle (engine-OWNED)

Add exactly one key to `momentum.backtest.LAB_TARGET.param_ranges`:

```python
"vol_managed_mode": (0, 0, "choice:legacy,vol_managed"),
```

`_sample_value`'s existing `choice:` branch supports this with **no
change to the sampler**. `(0, 0, ...)` is the established placeholder
for choice specs. Per SP-B (`tpcore.engine_profile` roster + the
`_LazyParamRanges` mapping in `ops/lab/run.py`), `PARAM_RANGES` is
**roster-driven and engine-OWNED** — adding the key here automatically
exposes it to the Lab sampler with **zero edits to `ops/lab/run.py` or
`ops/lab/__main__.py`**.

### 4.2 How `run_for_search` / `run_momentum_with_context` honor it

`run_momentum_with_context` already reads each known override key into a
module global and resets it per call. Add `vol_managed_mode` to that
block exactly like the existing entries:

```python
global _VOL_MANAGED_OVERRIDE
_VOL_MANAGED_OVERRIDE = (
    str(overrides["vol_managed_mode"])
    if "vol_managed_mode" in overrides
    else None
)
```

`run_for_search` delegates to `run_momentum_with_context`, so it
inherits the behaviour with no change. `default_params()` and
`MOMENTUM_OVERRIDE_KEYS` gain `"vol_managed_mode"` (default `"legacy"`)
so the SP3 O1 `default_params` seam reports the live default and
`param_diff` carries the real `legacy → vol_managed` delta in the
dossier.

### 4.3 No CLI / dispatch / contract change

`momentum` is already a valid `--target-engine` choice (it is in
`lab_targetable_engines()` post-SP-B; verified by the one-liner in §10).
The only Lab-side change is the ONE new `LAB_TARGET.param_ranges` key
+ `MOMENTUM_OVERRIDE_KEYS` + `default_params()` — all engine-owned.

---

## 5. The held-back gate + n_trials discipline (preserved/strengthened;
sacred)

The graduation gate for this candidate is **exactly** the TODO.md
adjudication bar, restated and never relaxed:

| Clause | Threshold | Source / how expressed |
| --- | --- | --- |
| Held-back DSR | **≥ 0.95** | `compute_dsr_for_verdict(held_period_returns, n_trials=args.trials + cumulative_n_trials("momentum"))` — the existing Lab DSR, cumulatively deflated (SP-A). |
| Credibility | **≥ 60** | `final_result.credibility_score` (the `CredibilityScore.score`); `survived` already ANDs `>= args.credibility_threshold` (default 60). |
| Held-back trades | **≥ 3** | `core.held_metrics.n_trades` (`SliceMetrics.n_trades`). Structural floor (already in `_run_lab_core`'s `survived`); not strengthened because monthly rebalance is a slow-evidence cadence (TODO.md L463-470 acknowledges this — the gate is what it is). |
| **Lower crash DD than current paper spec** | **strictly less-negative held-back `max_drawdown`** than the legacy arm on the SAME held-back window | `LabResult.held_metrics.max_drawdown` for the `vol_managed` candidate compared to the legacy arm's `max_drawdown` on the same window. Read off the existing dossier — no SP-D extension, no bespoke metric path. This is the TODO L463 "lower crash DD than current paper spec" clause expressed as a dossier-derivable scalar comparison. |

**The gate is preserved-or-strengthened on every clause. No clause is
relaxed. The gate is never bypassed — the candidate routes through
`python -m ops.lab` → `_run_lab_core` → `survived` → dossier → ECR
exactly like every other candidate.**

### 5.1 The maxDD clause is dossier-expressible (the Sentinel contrast)

Unlike Sentinel's adjudication bar (which required SP-D's pluggable
metric family because the maxDD-reduction objective is not Sharpe-
expressible), Momentum's maxDD clause is an **additive gate-evidence
read** on top of the existing Sharpe-ranked verdict — both the legacy
and the vol_managed arms run through the standard `_run_lab_core` path,
both produce a `LabResult.held_metrics.max_drawdown`, and the candidate's
SURVIVED status requires `max_drawdown_vol_managed > max_drawdown_legacy`
(less-negative). This is **NOT** a metric-family swap; it is a
direct field comparison on the dossier. No SP-D involvement.

Concretely: the `vol_managed` arm's dossier dose `held_metrics.
max_drawdown` is compared post-hoc by the operator/probe against the
legacy arm's same field. The dossier carries both. A `--vol-managed`
SURVIVED that did NOT achieve the strictly less-negative maxDD is
**logged as a FAIL** under the TODO L463 "lower crash DD" clause (red
is red — see §9).

### 5.2 n_trials accounting (honest, pinned — SP-A acknowledgement)

This run records its `--trials` spend to the cumulative ledger
(`tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.momentum` in
`platform.data_quality_log`), **unconditionally at sample time**, and
the verdict's DSR is deflated against
`tpcore.lab.ledger.cumulative_n_trials("momentum") + this_run_trials` —
**not** this run's `--trials` in isolation. The author **acknowledges
cumulative (not per-run) DSR deflation**: every prior Lab run against
`momentum` makes this run's gate strictly harder (monotone-harder); a
candidate that "would have passed at per-run n_trials" is **not** an
argument for relaxing anything. The cumulative ledger is never reset
or bypassed.

This candidate adds **exactly TWO configurations** to the ledger
sample space (the `{legacy, vol_managed}` `choice:` arm), and the
legacy `legacy` arm is a denominator re-measurement of the live path,
claiming no edge of its own (it IS the live behavior). There is **no
hidden grid** — the only Lab-sampled value is the one toggle. Target
vol, vol window, vol clip, earnings window, and the magnitude floor
are ALL code constants (§1.2, §2.2, §2.3), never sampled.

---

## 6. Data prerequisites stated honestly (checklist §8)

| Datum | Status | Concrete evidence |
| --- | --- | --- |
| `platform.prices_daily` (12-1 momentum signal + realized vol) | **READY** | Already the live momentum dependency; CRITICAL_TICKERS-gated by `tpcore/quality/validation/checks/prices_daily_freshness.py`. |
| `platform.liquidity_tiers` (T1+T2 universe) | **READY** | Already the live momentum dependency. |
| `platform.earnings_events` (`event_type='EARNINGS_BEAT'`, `magnitude_pct`) | **READY** | **13,848 rows / 1,104 distinct tickers / 2018-01-10..2026-05-15 / `magnitude_pct` populated on 13,848 / 13,848 rows, all > 0** (live DB query, 2026-05-20). Already in catalyst engine's `data_dependencies` (PR #171). The freshness check `tpcore/quality/validation/checks/earnings_events_freshness.py` is part of the data-layer acceptance gate. |
| Cost model (`tpcore.backtest.cost_model.load_tier_costs`) | **READY** | Already reused by the legacy backtest; the vol-scaling commutes with the multiplicative slippage adjustment (cost direction unaffected). |

The candidate adds **no new feed**. It DOES add a single
strictly-additive read to `load_momentum_window_context` (an
`earnings_events` query, parallel to the existing `prices_daily`
+ tier-cost loads) that lands in a NEW optional field
`MomentumWindowContext.earnings_by_ticker: Mapping[str, list[tuple[date,
float]]] | None = None`. This mirrors the Vector composite spec's
treatment of `sec_insider_transactions` (the same loader-additive
idiom, vector spec §7). The legacy branch in `run_momentum_with_context`
**never reads** `earnings_by_ticker`, so the legacy
`BacktestRunResult` output is byte-identical regardless of whether the
earnings rows were loaded (the C1 characterization test pins this).
The load is invoked once per walk-forward window (the Lab pattern),
not per trial.

### 6.1 No BLOCKER

There is no analogue of the Vector spec's sector-source BLOCKER. Every
feed this candidate consumes is **READY** with concrete row counts +
data ranges + a passing live freshness check. The candidate is
deployable today against the existing data layer.

---

## 7. Lookahead / point-in-time honesty (checklist §9)

- **12-1 momentum score window:** unchanged from legacy
  (`[t − skip − lookback, t − skip]`, strictly backward). Confirmed by
  inspection of `_compute_one_rebalance` (`momentum/backtest.py:229-302`).
- **Realized-vol window:** `[t_entry − 60, t_entry − 1]` — strictly
  backward, ends one trading day BEFORE entry. Test: a unit test pins
  that no bar dated ≥ `t_entry` ever enters the σ computation.
- **Earnings-beat window:** `[t − 90, t]` calendar days — strictly
  backward. Test: a unit test pins that no `earnings_events` row with
  `event_date > t` ever clears the overlay.
- **Cost model:** unchanged tier round-trip slippage from
  `tpcore.backtest.cost_model.load_tier_costs`. Vol-scaling multiplies
  the slippage-adjusted PnL by the scalar `s_n` AFTER the slippage is
  applied — the cost direction is preserved. (A long-only fold; no
  borrow-rate model needed.)
- **Degenerate inputs:**
  - `σ_n ≤ 1e-6` → `s_n := 1.0` (no scaling neutral; pinned in §2.2).
  - No EARNINGS_BEAT row in window → name **excluded** (the overlay's
    semantic).
  - Both unit-tested.

---

## 8. Failure modes + Hardening register (H-MVM-*)

| ID | Risk | Hardening |
| --- | --- | --- |
| **H-MVM-1** | Vol-managed / overlay code subtly changes the live/legacy backtest path (the make-or-break invariant). | Off-by-default flag (§3.1); the C1/C4 characterization test (§3.3) pins the legacy `BacktestRunResult` field-for-field equal to a committed pre-candidate baseline for ALL legacy-key calls; build FAILS if the baseline drifts. `momentum/plugs/*` + `momentum/scheduler.py` untouched (§3.2). The live scheduler does NOT import `momentum.backtest` or `momentum.lab_vol_managed` — proven by an import-isolation test in the characterization file. |
| **H-MVM-2** | n_trials inflation via a hidden grid (the platform constraint). | `vol_managed_mode` is the ONLY added Lab-sampled key; target_vol (0.40), vol_window (60), vol_clip (0.5,2.0), earnings_lookback_days (90), magnitude_floor (>0) are CODE CONSTANTS (§1.2, §2.2, §2.3), never sampled. Exactly 2 configurations recorded against n_trials (§5.2). No `--family-weights` menu. A test asserts `LAB_TARGET.param_ranges` gained exactly one new key and it is the choice toggle. |
| **H-MVM-3** | The combined-toggle decision (vol-managed + overlay together, not split) hides a sweep. | §2.5 justifies the combined toggle: the TODO's ONE structural direction is the **combined** spec; splitting into two toggles would 4× n_trials. The combined choice is itself the pre-registration discipline; the spec is binary `{legacy, vol_managed}` — no partial-on intermediate state. |
| **H-MVM-4** | The graduation gate relaxed or bypassed. | §5 restates every clause preserved (DSR≥0.95, cred≥60, n_trades≥3) + adds the TODO L463 dossier-derivable "lower crash DD than legacy" clause (§5.1); routes through `python -m ops.lab` → `_run_lab_core` → `survived` → dossier → ECR; experimental credibility namespaced `lab.momentum_vol_managed` (H-S2-3, reused) so `graduation_ready(pool,"momentum")` can never read it. No `--credibility-threshold`/`--dsr-threshold` override below the gate in the run command. |
| **H-MVM-5** | The maxDD clause needs a bespoke non-Sharpe metric path. | §5.1 verifies by inspection: both arms run through the standard `_run_lab_core` Sharpe-ranked path, both produce `held_metrics.max_drawdown` on the same held-back window — the comparison is a direct field read on the dossier, NOT a SP-D metric-family swap. The Sentinel contrast (where SP-D was unavoidable) does not apply here. |
| **H-MVM-6** | Lookahead via the vol window or the earnings overlay window. | All windows are strictly backward (§7). Unit tests pin that no bar dated ≥ `t_entry` enters σ; no `earnings_events.event_date > t` clears the overlay. The held-back DSR is therefore lookahead-honest. |
| **H-MVM-7** | Vol-scaling blows up on a near-zero σ stub. | Degenerate guard `σ ≤ 1e-6 → s := 1.0` pinned in §2.2; unit-tested. The clip `[0.5, 2.0]` also bounds the scale for any non-degenerate but extreme low-vol case. |
| **H-MVM-8** | Module-global override bleeds across Lab trials (the existing `_*_OVERRIDE` hazard). | `_VOL_MANAGED_OVERRIDE` reset per `run_momentum_with_context` call exactly like the existing overrides; C4 characterization test pins no cross-trial bleed. |
| **H-MVM-9** | Candidate accidentally treated as a live-roster change. | §3.2 + §10 non-goals; no edits to dispatch/roster/SoT/scheduler/plugs; spec + backtest-only code behind the flag. The live `momentum.scheduler` does not import `momentum.backtest` (verified). |
| **H-MVM-10** | `_build_lab_result` `default_params` seam misses `vol_managed_mode` ⇒ wrong `param_diff`. | `vol_managed_mode` added to `MOMENTUM_OVERRIDE_KEYS` + `default_params()` (default `"legacy"`) so `param_diff` carries the true `legacy → vol_managed` delta (§4.2); unit-tested via the SP3 O1 `default_params(args.engine)` path. |
| **H-MVM-11** | Lane/collision: touching a forbidden surface. | This candidate touches ONLY `momentum/backtest.py`, NEW `momentum/lab_vol_managed.py`, NEW `momentum/tests/test_lab_vol_managed_byte_identical.py` + NEW unit tests, and this spec doc. It does NOT touch `sentinel/`, `catalyst/`, `tpcore/selfheal/`, `tpcore/quality/validation/checks/`, `scripts/audit_data_pipeline.py`, `ops/lab/run.py`, `ops/lab/__main__.py`, the roster SoT, or any data-SDLC spec/checklist. |

---

## 9. Success / falsification criteria (red is red)

- **SURVIVED** iff ALL of: held-back DSR ≥ 0.95 **AND** credibility ≥ 60
  **AND** held-back n_trades ≥ 3 **AND** held-back max_drawdown
  strictly less-negative than the legacy arm's held-back max_drawdown
  on the same window. The dossier records every clause; ECR proceeds
  only on SURVIVED + `recommended_exit = fold_existing`.
- **FAILED** if ANY clause misses. A FAIL is a genuine, recorded
  falsification of the most cross-spike-evidenced Momentum structural
  fix (vol-managed + earnings-beat overlay). It is **NOT** re-run with
  tweaked vol-target / vol-window / earnings-window values (that would
  be a sweep / n_trials laundering). The honest outcome is logged; the
  next Momentum direction is a separate adjudication.

---

## 10. Compliance verifications (the `grep`-able set, checklist §10)

- **Exactly one new `LAB_TARGET.param_ranges` toggle.**
  `git diff` on `momentum/backtest.py` shows exactly one new key
  (`"vol_managed_mode"`) inside `LAB_TARGET.param_ranges`, and it is a
  `choice:` spec whose values are `{"legacy", "vol_managed"}`. No
  `--family-weights` menu, no second toggle.
- **Live path files untouched.** `git diff --name-only` contains **no**
  `momentum/plugs/`, `momentum/scheduler.py`, `momentum/models.py`,
  `scripts/run_all_engines.sh`, `ops/platform_pipeline.py`,
  `tpcore/lab/`, `ops/lab/__main__.py`, `ops/lab/run.py`, or any
  SoT/roster file.
- **Characterization golden present + RED-first.** The byte-identical
  test file `momentum/tests/test_lab_vol_managed_byte_identical.py`
  exists with the C1-C4 assertions; the baseline pin is captured before
  the vol-managed branch code is exercised. The live-path import-
  isolation assertion is in the same file.
- **Roster target verified.** `python -c "from tpcore.engine_profile
  import lab_targetable_engines as f; print('momentum' in f())"` prints
  `True` (momentum is LIVE/PAPER per the roster; this is also implied
  by Vector's spec §13 sibling cross-reference where the same roster is
  used).
- **No gate override below the floor.** The intended `python -m
  ops.lab` command (operator-triggered later) will carry **no**
  `--dsr-threshold` / `--credibility-threshold` below 0.95 / 60.
- **n_trials acknowledgement present.** §5.2 above.
- **Single-hypothesis attestation.** ONE primary hypothesis (§1); ONE
  primary metric (Sharpe; §1); the placeholder scan is empty; every
  constant is pinned (§1.2, §2.2, §2.3, §2.4).
- **`ruff check` clean** on the added code + tests; no `print()`
  residue.

---

## 11. Non-goals

- **NOT a live-roster change.** No edits to `momentum/plugs/*`,
  `momentum/scheduler.py`, `scripts/run_all_engines.sh`,
  `ops/platform_pipeline.py`, any SoT/roster, or the live dispatch.
  The live momentum path is byte-identical with the flag off (the C1
  characterization test is the proof obligation).
- **NOT a parameter sweep.** No vol-target grid, no vol-window grid,
  no earnings-window grid, no overlay-choice menu. ONE pinned spec.
- **NOT a revenue-growth overlay.** Earnings-beat is the pinned choice
  (§1.1). Revenue-growth is a separate future candidate if anyone
  wants to test it; not this one.
- **NOT a robustness check.** This candidate carries the primary
  hypothesis only — no sub-ablation.
- **NOT a Sentinel / Catalyst / Carver change.** Those are separate
  surfaces being built by parallel sessions; lane-clean per H-MVM-11.
- **NOT touching `ops/lab/run.py`, `ops/lab/__main__.py`, or any
  `tpcore/lab/` file.** The PARAM_RANGES is roster-driven by the
  engine-OWNED `LAB_TARGET` declaration; the Lab side has zero new
  knowledge.
- **NOT a gate relaxation.** Every clause preserved (§5) + one new
  dossier-derivable maxDD clause added (§5.1).
- **NOT a probe run.** This PR ships the candidate; the operator runs
  `python -m ops.lab --candidate momentum_vol_managed --target-engine
  momentum --intent fold_existing` separately when monthly cadence has
  produced sufficient trade history (per TODO.md L463-470's "slow DSR
  evidence accrual" note).

---

## 12. Self-review

- ONE pre-registered primary hypothesis (§1); ONE primary metric
  (Sharpe; §1); placeholder scan empty; every constant pinned (target
  vol 0.40, vol window 60, vol clip [0.5,2.0], earnings window 90d,
  magnitude floor >0, schedule unchanged, top-decile 0.10 unchanged).
- Feature-flag-variant satisfied: off-by-default `_VOL_MANAGED_
  OVERRIDE`, exactly one `choice:legacy,vol_managed` toggle, per-call
  reset, legacy default in `default_params()`.
- Gate sacred: maxDD clause is a dossier-derivable field comparison,
  NOT a SP-D metric-family swap (H-MVM-5).
- Momentum live path byte-identical with the flag off: proven by the
  C1-C4 + live-path import-isolation characterization test.
- No other engine touched; `tpcore/lab/target.py` stays engine-free.
- Data prereqs all READY with concrete live row counts (§6); no
  BLOCKER (§6.1).
- Build is one PR + one operator-triggered probe later; this PR ships
  the candidate, NOT the run (the operator decides the probe).

---

## 13. Lab Candidate Readiness checklist walk-through

`docs/superpowers/checklists/lab_candidate_readiness.md` 10 non-optional
sections:

**§1 Single pre-registered primary hypothesis.** Met — §1 above.

**§2 Feature-flag-variant pattern.** Met — `_VOL_MANAGED_OVERRIDE`
mirrors existing `_*_OVERRIDE` pattern; ONE new `LAB_TARGET.
param_ranges` `choice:` key; reset per call; `MOMENTUM_OVERRIDE_KEYS` +
`default_params()` updated with legacy default.

**§3 Byte-identical live path.** Met —
`test_lab_vol_managed_byte_identical.py` C1-C4 + the live-path
import-isolation assertion (§3.3).

**§4 n_trials ledger acknowledgement.** Met — §5.2 explicitly cites
`tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.momentum`
and the cumulative deflation rule. Exactly TWO configurations.

**§5 Roster-targeting prerequisite.** Met — `momentum ∈
lab_targetable_engines()` (one-liner in §10). No edits to Lab CLI /
dispatch / `tpcore/lab/` / any SoT/roster.

**§6 Gate is sacred — preserved or strengthened.** Met — §5 truth
table: DSR ≥ 0.95 (preserved), cred ≥ 60 (preserved), n_trades ≥ 3
(preserved), maxDD ≤ legacy (added). No `--dsr-threshold` /
`--credibility-threshold` override below the gate in the run command.

**§7 Lab credibility namespacing.** Met — writes under
`backtest_credibility.lab.momentum_vol_managed` via the existing
`_lab_credibility_engine_name` H-S2-3 mechanism. No new migration.

**§8 Data prerequisites stated honestly.** Met — §6 prereq table with
live row counts (13,848 EARNINGS_BEAT rows / 1,104 tickers,
2018-01-10..2026-05-15, magnitude_pct fully populated). No BLOCKER.

**§9 Lookahead / point-in-time honesty.** Met — §7: vol window strictly
backward (ends `t_entry − 1`); earnings window strictly backward
(`[t − 90, t]`); degenerate σ neutral; degenerate empty-overlay
excludes the name.

**§10 Compliance verifications.** Met — §10 above.
