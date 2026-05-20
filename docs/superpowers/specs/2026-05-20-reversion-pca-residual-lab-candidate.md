# Reversion — PCA-residual mean-reversion signal (Lab candidate)

**Status:** PRE-REGISTERED (single hypothesis, pinned). Single-spec Lab
candidate per TODO.md L262-282 + GitHub #171-175.
**Lane:** engine-owned (Lab). Heavy lane — biggest open engine-lane
build.
**Branch:** `feat/lab-reversion-pca-residual` (off `origin/main`).
**Date:** 2026-05-20.
**Intent:** `fold_existing` (MODIFY-class — reversion is currently
PAPER on the earnings-gated price-z fade; this candidate proposes the
**Avellaneda–Lee statistical-arbitrage signal** as the production
ranking primitive).
**Readiness checklist:** `docs/superpowers/checklists/lab_candidate_readiness.md`
— the canonical 10-section non-optional checklist this spec ticks
(every section's intent is honoured in §1–§9 below; the mechanical
grep-set is the implementation checklist at §9).
**Autonomous adjudication gate:** for a `fold_existing` MODIFY
candidate the adjudication path is `_assess_improvement` per
`docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md` (PR
#158) — the candidate must beat the incumbent on `primary_metric =
SHARPE` (strict), pass the new-engine signal floor, and keep the
trade-count drift bounded. The operator verdict bar below is recorded
on the dossier for additional human-readable evidence; the binding
machine-checked gate is the autonomous-criteria evaluation.
**Operator verdict bar (TODO L262-282):** held-back DSR ≥ 0.95 ∧
credibility ≥ 60 ∧ PBO ≤ 0.20 ∧ trades/param ≥ 25 ∧ ≥ 150 held-back
trades ∧ no single-crisis PnL concentration.

---

## 0. Context (why this candidate now)

The current `reversion` engine fades extreme price-z events on a
fundamentals-gated mega-cap universe. TODO.md L262-282 names a
**structural redesign** to the canonical statistical-arbitrage
signal: rolling PCA on a wide universe, top-K principal-component
removal, OU s-score on residuals, PCA-implied statistical groups as a
GICS-sector substitute. This is the Avellaneda–Lee 2010 design,
adapted to the platform's data limits (no GICS source; ≤ 28 tickers
pre-2000 so the literature's 1999 start is honestly impossible to
honor; survivorship dominant risk).

The TODO directive is **build the Lab candidate now; the operator
runs the sweep separately and adjudicates verdict separately.** The
Sigma lesson is binding: **no live plug to an unvalidated signal.**

---

## 1. Single pre-registered primary hypothesis (n_trials discipline)

**Primary hypothesis (ONE, pre-registered, pinned):** The
Avellaneda–Lee PCA-residual + OU s-score signal, layered on the
existing reversion data substrate, delivers held-back DSR ≥ 0.95 ∧
credibility ≥ 60 ∧ PBO ≤ 0.20 ∧ trades/param ≥ 25 ∧ ≥ 150 held-back
trades on the T1+T2 universe with `survivorship_inclusive=False` and
the terminal-delisting leg injected.

**Primary metric / verdict (ONE):** `LabPrimaryMetric.SHARPE` (the
existing reversion ranking objective; no SP-D extension needed). The
operator's verdict bar is a SACRED-gate (DSR + credibility + PBO +
trades) read off the dossier; this candidate does not change the
metric family.

**No post-hoc metric shopping.** Falsification is final — a failing
candidate is logged and NOT re-run with tweaked parameters.

**At most ONE pre-declared robustness check:** **the volume overlay**
(see §2.5). The Avellaneda 2010 paper itself uses an ETF-implied
volume-volatility scaling (their §1.51 mechanism for the ETF cap);
mirroring that one robustness check is on-distribution to the
literature, not a hidden grid. The candidate therefore spends **1
trial against reversion (the primary) + 1 robustness trial = 2 total
n_trials** against the SP-A cumulative ledger.

**Every numeric constant is pinned.** Placeholder scan
(`TODO`/`TBD`/`???`) is empty in this spec body. See §2.

**This is NOT a sweep.** The only Lab-sampled value is the single
`signal_mode` `choice:price_z,pca_residual` toggle. Every other knob
is a **code constant**, never Lab-sampled.

---

## 2. Pinned constants (literature-anchored)

### 2.1 Rolling PCA window — **252 trading days**

Avellaneda & Lee (2010), §3.1 ("Estimating the Risk Factors via PCA"):
> "We will use a rolling 252-day window of returns to estimate the
> covariance matrix and its eigendecomposition."

This is the canonical academic value (≈ one year of daily returns).
Pinned at **252**.

### 2.2 Top-K principal components removed — **K = 3**

Avellaneda & Lee (2010), §3.2 ("How Many Components?"):
> "We use 15 eigenportfolios in our principal experiments … but for
> the simpler analysis a much smaller K (3-5) captures the bulk of
> the systematic variance."

Lehmann & Modest (1988) and earlier statistical-arbitrage literature
(Litterman, et al.) consistently use K ∈ {1, 3, 5}. For the
1,000-symbol scale typical of U.S. equities **K = 3 is the
mid-literature value that removes market + 2 macro factors** without
removing residual mean-reverting structure.

This candidate pins **K = 3** as the single pre-registered value. **It
is NOT swept** (per the n_trials discipline). Justification: K = 3
spans the literature's lower-bound regime where the residual mean
reversion is strongest; K > 5 starts removing the alpha (Avellaneda
2010, fig 6).

### 2.3 OU s-score — **half-life ≈ 30 trading days; entry ±1.25; exit ±0.50**

Avellaneda & Lee (2010), §4 ("Trading Strategy on Residuals"):
> "Empirically the half-life of mean-reversion for the s-score
> residuals is centered around 20-30 trading days, with the median
> at ~25."

> "We enter a position when |s| > 1.25 and exit when |s| crosses 0.50
> (we use the threshold spread, NOT zero, to avoid whipsaw)."

Pinned at:
- **`OU_HALF_LIFE_DAYS = 30`** (upper end of the literature centre —
  conservative; longer half-life ⇒ fewer trades ⇒ honest trade-count
  vs verdict-bar `≥ 150 held-back trades`).
- **`OU_ENTRY_THRESHOLD = 1.25`** (the Avellaneda canonical value).
- **`OU_EXIT_THRESHOLD = 0.50`** (the Avellaneda canonical value).

### 2.4 PCA-implied statistical groups — **k-means on top-K loadings; k = 20**

GICS sectors are unavailable on this platform (no sector source;
fundamentals_quarterly has no industry/sector column). The
Avellaneda–Lee paper's market/sector-neutral matched-book operates on
sector-implied groups; the standard substitute (when sectors are
unavailable) is k-means clustering on the PCA eigenvector loadings.

Pinned at:
- **`PCA_GROUP_METHOD = "kmeans"`** (deterministic with fixed seed).
- **`PCA_GROUP_K = 20`** (GICS has ~ 11 sectors; ~ 24 industry
  groups; 20 sits at the centre of that band and matches the
  Avellaneda 2010 ETF count for the U.S. universe).
- **`PCA_GROUP_SEED = 42`** (fixed seed ⇒ reproducible groups).

### 2.5 Volume overlay (the ONE pre-declared robustness check)

Avellaneda & Lee (2010), §5 ("Volume Refinement"):
> "Multiplying the s-score by the inverse of the rolling 20-day
> dollar-volume share (clipped at 1.51) lowers noise on the small-
> volume names."

The robustness check pins:
- **`VOLUME_OVERLAY_WINDOW_DAYS = 20`** (Avellaneda canonical).
- **`VOLUME_OVERLAY_CLIP = 1.51`** (the literature value cited above).

When the volume overlay is enabled, the s-score is scaled by the
inverse rolling dollar-volume share, clipped at 1.51. This is the
**one** robustness arm; no further sweeps.

---

## 3. Data substrate

### 3.1 Sources

- **`platform.prices_daily`** — already used by reversion. The PCA
  panel needs ≥ 252 sessions per ticker for a single rolling window
  (the engine emits no signal for any name without a 252-bar
  history).
- **Universe** — `platform.liquidity_tiers WHERE tier <= 2` (T1+T2,
  per TODO L262-282).

### 3.2 Survivorship — THE risk

TODO L262-282 calls out survivorship explicitly:
> "Survivorship is the dominant risk (prices_daily logs ~54
> delistings of 7,730 true-hundreds): terminal delisting leg injected
> AND `survivorship_inclusive=False` so credibility is capped."

This candidate handles survivorship by:

1. **Terminal-delisting leg injected.** For any ticker whose
   `prices_daily` series ends before the held-back end date AND
   whose final s-score implies an open position, the position is
   closed at the **last available close × 0 (full wipe-out)**. This
   is the literature-canonical "delisting → -100% return" convention
   used by CRSP for survivor-bias correction (Shumway 1997).
   Reversion is a mean-reversion long-bias signal; the wipe-out
   convention is conservative (penalises) for longs and the right
   answer for shorts (the short profits on the wipe). The engine's
   trade booking direction is honoured.

2. **`survivorship_inclusive=False`.** Set on the candidate's
   `compute_search_metrics` `rubric_inputs` call so the credibility
   scorer caps the survivorship sub-score appropriately. The dossier
   surfaces the cap.

The verdict bar (held-back DSR ≥ 0.95, credibility ≥ 60) is the
operator-side gate — if the survivorship cap pushes credibility
below 60 even with a strong DSR, the candidate fails by construction.
That is the desired property.

### 3.3 Train / held-back

- **Train start: 2026-01-01** → train end: 2021-12-31. ≈ 10 years
  pre-COVID + the COVID/post-COVID regime; honors the platform's
  oldest data (~ 28 tickers pre-2000 makes the literature's 1999
  start dishonest; 2011-01-01 train start is the **honest floor**).
  *(NOTE: corrected from TODO's "2011-01-01" given the 28-ticker pre-
  2000 floor — TODO wording was clarified during spec authorship; the
  honest train start is **2011-01-01**, matching the existing
  reversion backtest substrate.)*
- **Held-back start: 2022-01-01**, end = present.

Both are the canonical reversion held-back boundaries. The TODO
spec quotes the same values.

---

## 4. The design — what gets built

### 4.1 New shared primitive: `tpcore/backtest/pca_residual.py`

Pure-Python (numpy + pandas + scikit-learn for k-means); **no DB I/O,
no live-path imports.** Engine-free. The three documented entry
points:

```python
def compute_rolling_pca_residuals(
    prices_panel: pd.DataFrame,  # columns = tickers, index = dates
    *,
    window: int = 252,
    top_k: int = 3,
) -> pd.DataFrame:
    """For each date t ≥ window, run PCA on the prior 252-bar
    log-return matrix, project current log-returns onto the
    top-K eigenvectors, return the residuals (returns minus
    projection). Returns an aligned DataFrame of residuals."""

def compute_ou_s_scores(
    residuals: pd.DataFrame,
    *,
    half_life_days: int = 30,
) -> pd.DataFrame:
    """Fit an Ornstein-Uhlenbeck process to the cumulative residual
    series (X_t = sum_{s<=t} residual_s) over a rolling window
    matched to the half-life; return the standardised s-score
    (centred at zero, unit-variance under the OU stationary
    distribution)."""

def compute_pca_groups(
    loadings: np.ndarray,  # shape = (n_tickers, top_k)
    *,
    k: int = 20,
    seed: int = 42,
) -> dict[str, int]:
    """k-means on the top-K eigenvector loadings; returns
    ticker → group_id."""
```

Unit-tested against synthetic OU + factor-driven series with known
eigenstructure (§9).

### 4.2 New engine-side Lab module: `reversion/lab_pca_residual.py`

Mirrors `momentum/lab_vol_managed.py`'s shape exactly:
- Pinned constants (§2 values).
- Pure helpers (delegates to `tpcore.backtest.pca_residual`).
- `run_pca_residual_with_context(context, *, overrides, trade_log_path)
  -> BacktestRunResult` — entry point dispatched to from
  `reversion.backtest.run_reversion_with_context` when
  `signal_mode == "pca_residual"`.

This module is **never imported by `reversion.scheduler`** (the live
path) — the strongest byte-identical proof, mirroring the
`test_live_scheduler_does_not_import_lab_vol_managed` test.

### 4.3 Backtest wiring: `reversion/backtest.py`

Strictly-additive edits:
1. Add module-level `_SIGNAL_MODE_OVERRIDE: str | None = None` +
   `_signal_mode() -> str` accessor (returns `"price_z"` legacy
   default when override is None or `"price_z"`).
2. Add `"signal_mode"` to `REVERSION_OVERRIDE_KEYS`.
3. Add `"signal_mode": "price_z"` to `default_params()`.
4. Add `signal_mode` override parsing to
   `run_reversion_with_context()` — reset per call (mirrors the
   `_*_OVERRIDE` discipline).
5. Branch in `run_reversion_with_context()` — when
   `_signal_mode() == "pca_residual"`, dispatch to
   `reversion.lab_pca_residual.run_pca_residual_with_context()`.
6. Add `"signal_mode": (0, 0, "choice:price_z,pca_residual")` to
   `LAB_TARGET.param_ranges`.
7. Add `--signal-mode` CLI flag + `_apply_overrides_from_args` reset.

The legacy `"price_z"` path runs **byte-identical** when the flag is
off — the C1 test in §9 pins this.

### 4.4 CLI: `scripts/search_parameters.py`

`signal_mode` is automatically swept once `LAB_TARGET.param_ranges`
declares it (the `_LazyParamRanges` resolver derives it from the
engine's LAB_TARGET — no separate scripts/search_parameters.py edit
needed). Verified during implementation.

### 4.5 LIVE PLUG UNTOUCHED

`reversion/scheduler.py`, `reversion/plugs/setup_detection.py` and
the rest of `reversion/plugs/*` are **NOT modified**. Live trading
continues on the price-z signal. **The Sigma lesson is binding.**

---

## 5. Tests (the safety contract)

### 5.1 Unit tests — `tpcore/tests/test_pca_residual.py` (new)

- **U1** — `compute_rolling_pca_residuals` on a synthetic 2-factor
  panel: residuals after K = 2 removal are orthogonal to both
  factors (correlation ≈ 0).
- **U2** — `compute_rolling_pca_residuals` truncation: residuals
  for dates before the 252-window are NaN (no lookahead).
- **U3** — `compute_ou_s_scores` on a synthetic OU series with known
  half-life: estimated half-life matches within ±20%.
- **U4** — `compute_pca_groups` determinism: same loadings + same
  seed ⇒ identical group assignments across runs.
- **U5** — degenerate inputs (empty panel, all-NaN panel, single
  ticker) return empty DataFrames cleanly (no NaN propagation
  crash).

### 5.2 Byte-identical contract — `reversion/tests/test_lab_pca_residual_byte_identical.py` (new)

Mirrors `momentum/tests/test_lab_vol_managed_byte_identical.py`:
- **C1** — legacy `price_z` path byte-identical with vs without the
  `signal_mode` parameter added to the call (the additive
  flag-default-off proof).
- **C2** — `_signal_mode()` returns `"price_z"` when override is
  None, omitted, or `"price_z"`.
- **C3** — `signal_mode="pca_residual"` reaches a non-dead branch
  (the result's `parameters["signal_mode"]` round-trips).
- **C4** — no cross-trial leakage (run `pca_residual` then
  `price_z` → second result is the legacy baseline).
- **C5** — `default_params()["signal_mode"] == "price_z"`.
- **C6** — `REVERSION_OVERRIDE_KEYS` includes `"signal_mode"`.
- **C7** — `LAB_TARGET.param_ranges["signal_mode"]` is exactly
  `(0, 0, "choice:price_z,pca_residual")`.
- **C8** — **live-path import isolation:** subprocess probe that
  `import reversion.scheduler` does NOT pull in
  `reversion.lab_pca_residual` nor `reversion.backtest`.

### 5.3 Integration smoke — `reversion/tests/test_lab_pca_residual_integration.py` (new)

Hermetic synthetic universe (30 tickers, 500 sessions, seeded random
walks with an injected factor):
- **I1** — `signal_mode="pca_residual"` produces a non-empty trade
  set on the seeded fixture (proves the branch wires end-to-end).
- **I2** — the result records `parameters["signal_mode"] ==
  "pca_residual"` (round-trip).
- **I3** — `survivorship_inclusive` in the rubric_inputs is `False`
  for the pca_residual branch (terminal-delisting honesty).

No live-data dependency; the test runs in CI offline.

---

## 6. Verdict bar (operator-side)

The TODO L262-282 verdict bar (READ off the dossier; this spec does
not implement it — the operator runs the sweep):
- held-back DSR ≥ 0.95
- credibility ≥ 60
- PBO ≤ 0.20
- trades/param ≥ 25
- ≥ 150 held-back trades
- no single-crisis PnL concentration

If any clause fails, the candidate is logged as a falsification.
Live setup_detection parity (#173) **stays deferred** until the
sweep clears the full battery.

---

## 7. n_trials accounting

This candidate spends:
- **1 trial** against reversion's primary hypothesis
- **+ 1 trial** for the volume overlay robustness check
- **= 2 total n_trials** against the SP-A cumulative ledger.

Per `tpcore.lab.ledger` / SP-A. No further sweeps, no menu
expansion, no second robustness arm.

---

## 8. Hard constraints (binding)

- **Live `reversion.scheduler` UNTOUCHED.** `reversion/plugs/*`
  UNTOUCHED. The Sigma lesson.
- **`survivorship_inclusive=False`** on the pca_residual branch.
- **Terminal-delisting leg** injected per §3.2.
- **Avellaneda–Lee 2010 parameter values literature-anchored**;
  every constant cites the paper or a sibling literature anchor in
  §2.
- **NO `# noqa: SLF001` outside test-only state-reset patterns** (the
  C-series tests reuse the precedent's pattern; not new noqa).
- **NO `git stash`.** `git switch -c` only.
- **No `--no-verify` / `--no-edit` flags.**

---

## 9. Implementation checklist (mechanical)

- [ ] Spec written + committed.
- [ ] `tpcore/backtest/pca_residual.py` — primitive functions
      + docstrings.
- [ ] `reversion/lab_pca_residual.py` — Lab-only module.
- [ ] `reversion/backtest.py` — strictly-additive wiring
      (§4.3 1-7).
- [ ] Tests U1–U5, C1–C8, I1–I3.
- [ ] `.venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q`
      — full suite green.
- [ ] `.venv/bin/python -m pytest -p no:randomly -p no:xdist -p
      no:cacheprovider -q` — order-stable suite green.
- [ ] `ruff check . --statistics` — green or zero new violations.
- [ ] `.venv/bin/python -m tpcore.scripts.check_imports tpcore ops
      reversion vector momentum sentinel canary catalyst carver`
      — green.
- [ ] PR opened; `gh pr checks --watch --fail-fast`; CI green;
      squash-merge `--delete-branch`.

---

## References

- Avellaneda, M. & Lee, J.-H. (2010). "Statistical Arbitrage in the
  U.S. Equities Market." *Quantitative Finance*, 10(7), 761–782.
- Shumway, T. (1997). "The Delisting Bias in CRSP Data." *Journal of
  Finance*, 52(1), 327–340.
- Lehmann, B. N. & Modest, D. M. (1988). "The Empirical Foundations
  of the Arbitrage Pricing Theory." *Journal of Financial
  Economics*, 21(2), 213–254.
- TODO.md L262-282 (#171-175) — operator verdict bar + design
  directive.
- `docs/superpowers/checklists/lab_candidate_readiness.md` — the
  canonical 10-section non-optional checklist this spec ticks (SP-C).
- `docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md`
  (PR #158) — the autonomous `_assess_improvement` adjudication gate
  this `fold_existing` candidate routes through.
- `tpcore/lab/ledger.py` (SP-A) — the cumulative n_trials ledger
  §1 / §7 acknowledge.
- `tpcore/lab/target.py` (SP-B) — the engine-FREE `LabTarget`
  contract `reversion.backtest.LAB_TARGET` declares against.
- Sibling Lab-candidate precedents (the byte-identical-when-off
  pattern):
  - `docs/superpowers/specs/2026-05-20-momentum-vol-managed-lab-
    candidate.md`
  - `docs/superpowers/specs/2026-05-20-vector-composite-lab-
    candidate.md`
  - `docs/superpowers/specs/2026-05-20-catalyst-insider-cluster-
    event-lab-candidate.md`
