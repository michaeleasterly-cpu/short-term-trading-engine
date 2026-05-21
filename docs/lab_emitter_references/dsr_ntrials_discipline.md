# DSR + n_trials Discipline — Mandatory Reference (v1.0)

**This file is mandatory-always-include for every Lab-finder run** (per Task #25 spec §3.1 + §5; SP-G spec §3.4). The LLM cannot turn it off via `--reference-bundle`. It encodes the structural fences that prevent the autonomous loop from drifting into multiple-testing fraud.

**Operator pledge embedded:** these rules are NEVER relaxed at runtime, NEVER weakened for a "promising" candidate, NEVER bypassed because the gate is hard. Hard is the point.

---

## 1. The deflated Sharpe ratio (DSR) — what it is, why it's the gate floor

Source: López de Prado (2018) *Advances in Financial Machine Learning*, Ch. 14 §14.4–14.5 (the "Deflated Sharpe Ratio").

The Sharpe ratio of a single backtest is **upward-biased** when you've searched over N hypotheses. Even with no real edge, the **maximum** Sharpe across N independent random strategies will, in expectation, exceed any pre-registered Sharpe threshold as N grows — purely from extremal statistics. The DSR adjusts the observed Sharpe down by a factor that depends on:

1. **N = the cumulative number of trials** spent against the target (NOT this run's N — see §3 below).
2. The **higher moments** of the strategy's return distribution (skew + kurtosis); fat-tailed strategies need higher raw Sharpe to clear DSR.
3. The **track-record length** in independent periods (not calendar time; trade-count + non-overlapping window count).

**Gate floor in this codebase:** `DSR ≥ 0.95` (see `ops/lab/run.py` + the autonomous Lab criteria in PR #158). This is the false-positive ceiling, not the Sharpe ceiling — at 0.95 there's a ~5% chance the observed Sharpe is the maximum-of-N artifact rather than a real edge.

**No-relax pledge:** If a candidate's raw Sharpe is high but DSR is below 0.95, the candidate FAILS. The finder must not propose "but the raw Sharpe is 2.0" — the deflation is the whole point.

---

## 2. The multiple-testing problem in trading research

Source: Harvey, Liu, Zhu (2016) "...and the Cross-Section of Expected Returns" (*Review of Financial Studies* 29(1)), §1 introduction + §3 factor inflation table.

HLZ surveyed 296 published factor "anomalies" claimed to predict equity returns. Their conclusion: with appropriate multiple-testing correction (Bonferroni + Sidak + Holm + Benjamini-Hochberg-Yekutieli applied to factor t-statistics), **most of the published factors are spurious.** They recommend a t-statistic cutoff of ~3.0 (vs the 2.0 typical of single-test journal publication) to keep the false-discovery rate under control across the literature.

**Implication for autonomous edge-finding:** every LLM-proposed hypothesis is a factor candidate being added to the cumulative tested-factor pool. If the cumulative pool is large (autonomous loop = potentially thousands per year), the per-candidate t-statistic / Sharpe / DSR threshold must be COMMENSURATELY harder — not the unadjusted gate.

**Operationalized in this codebase:** `tpcore.lab.ledger.cumulative_n_trials(<target>)` is the cumulative spend; DSR is deflated against `cumulative_n_trials + this_run_trials`, not just this run's `--trials` argument. Every `record_trial_spend` call ratchets the bar strictly harder for every future candidate against the same target.

---

## 3. Cumulative n_trials accounting — the platform-binding rule

Sources: SP-A design (`docs/superpowers/specs/2026-05-19-lab-cumulative-trials-ledger-design.md`), `tpcore/lab/ledger.py`, López de Prado (2018) Ch. 11 (Backtesting Through Cross-Validation).

**Hard rule:** every Lab emission writes ONE `record_trial_spend` row to `platform.data_quality_log` under `lab_trial_ledger.<target>` BEFORE the draft PR is created. The write is UNCONDITIONAL: even rejected emissions count (every analysis turn that COULD have become a hypothesis is part of the multiple-testing N).

Why "before the PR":
- A crash between write-PR and write-ledger would let the ledger under-count the spend → DSR deflation insufficient → false-positive risk leaks past the gate.
- The PR is the SECOND step, the ledger is the FIRST — the order makes the spend honest even on failure.

**Why "rejected emissions also count":** This is the autonomous-scale binding constraint. An LLM that proposes 100 hypotheses, has 95 rejected at the EmittedSpec validator, and 5 reach the PR, has spent 100 trials against the target, not 5. The HLZ multiple-testing math applies to the *tested* count, not the *passed* count. A finder that doesn't account for rejected hypotheses is laundering the multiple-testing problem.

**The autonomous-scale extension (Path B addition):** per-REGIME accounting via `cumulative_n_trials_by_regime(<target>, <regime>)`. Re-firing the same hypothesis under a different MarketRegime counts as a fresh trial against the regime axis — preventing the "I'll re-propose mean-reversion when vol is high" silent re-test pattern.

---

## 4. Backtest overfitting probability (PBO)

Source: Bailey, Borwein, López de Prado, Zhu (2014, 2015, 2016) — the PBO sequence; LdP (2018) Ch. 12 (Backtest Statistics).

PBO measures the **probability that the best in-sample strategy under-performs the median strategy out-of-sample**. PBO > 0.5 ⇒ the optimization process is so overfit that picking the in-sample winner is *worse* than picking the median; the search is anti-informative.

**Gate floor:** `PBO ≤ 0.20` (autonomous Lab criteria via PR #158; reinforced in spec §5 fence #3). Some candidates in the deep-research wave were spec'd with `PBO ≤ 0.20` explicitly (vector_composite, catalyst_insider_drift). The PBO test is computed on the walk-forward held-back set; if it fails, the candidate FAILS regardless of any other metric.

**No-relax pledge:** PBO is never softened to 0.30 / 0.40 "because the candidate is theoretically anchored." The empirical overfit measurement supersedes the theoretical anchor.

---

## 5. Post-publication anomaly decay — the post-2007 effect

Source: McLean, Pontiff (2016) "Does Academic Research Destroy Stock Return Predictability?" (*Journal of Finance* 71(1)), §3 main result table.

McLean-Pontiff studied 97 published equity-return predictors. Mean in-sample return predictability: 0.58% per month. Mean post-sample (pre-publication, out-of-sample): 0.32%. Mean **post-publication**: 0.05%. The decay from in-sample to post-publication is ~91%; from out-of-sample to post-publication is ~84%.

**Operator implication:** the LLM's "trained knowledge" contains every pre-2024 published factor. A finder that proposes "small-firm effect," "low-vol anomaly," "post-earnings drift," "value-momentum interaction" is proposing a POST-PUBLICATION factor — the McLean-Pontiff decay applies. The finder MUST:

1. Treat any well-known anomaly with prior post-publication-decay reasoning (a finder proposing "small-firm effect" must justify why decay doesn't apply HERE, not assume the textbook backtest result).
2. Prefer novel composite hypotheses (factor combinations, regime-conditional applications) over textbook re-runs.
3. Never propose a "pure factor backtest" as the primary hypothesis — those are already arbitraged.

**The reference set is NOT a how-to.** Carver and Chan are doctrinal grounding (how to think about portfolio construction + mean-reversion). They are NOT a hypothesis menu. Re-implementing Carver's vol-targeted multi-forecast or Chan's pairs-cointegration verbatim is BOTH a McLean-Pontiff-decay candidate AND a `n_trials` waste — the literature has already tested them.

---

## 6. HAC robust standard errors — the default for time-series regression

Source: Newey, West (1987) "A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix" (*Econometrica* 55(3)); Andrews (1991) bandwidth selection.

Plain OLS on financial returns publishes **homoskedastic standard errors**, which are systematically too narrow for serially-correlated returns (which is essentially all return series — daily-return autocorrelation persists at lags 1-10 for most equities). A naïve OLS regression of returns on a factor will reject the null at the 95% level much more often than 5% under the true null.

**The fix (textbook):** Newey-West HAC standard errors with automatic bandwidth (Andrews 1991: `bandwidth ≈ floor(0.75 * T^(1/3))` where T is sample size). In `statsmodels.api.OLS`, this is `.fit(cov_type="HAC", cov_kwds={"maxlags": ceil(0.75 * T**(1/3))})`.

**Operationalized in the toolkit (v1):** the whitelist callable `OLS_HAC_NW` (Task #25 spec §6 — Path B revision) wraps OLS-with-HAC as the default; raw OLS is removed. Bandwidth defaults to the Andrews formula. The finder cannot opt out — there is no "raw OLS" callable in the sandbox.

**Why this is in the discipline doc:** an OLS regression with naïve homoskedastic SEs will inflate t-statistics by 1.5-3x on serially-correlated returns. A "significant" factor exposure at p<0.05 under homoskedastic SEs may be p>0.30 under HAC. The discipline is: trust HAC, distrust raw OLS, never claim significance without HAC defaults.

---

## 7. Purged k-fold cross-validation — the right held-out design

Source: López de Prado (2018) Ch. 7 (Cross-Validation in Finance) + Ch. 8 (Feature Importance).

Standard k-fold CV assumes IID samples. **Financial returns are not IID** — they're auto-correlated (clustering of volatility, momentum effects) AND have label leakage when labels are derived from forward-looking windows (e.g., "did the trade hit TP within 5 days" — the label at t depends on returns t+1..t+5).

**Purged k-fold CV** (LdP Ch. 7.3): when validating on fold k, **purge** training samples from a buffer zone around fold k's time span (specifically, drop training samples whose label-derivation window overlaps with fold k). Plus **embargo**: drop training samples from a small window AFTER fold k to prevent serial-correlation leak.

**The Lab implementation:** `ops/lab/run.py` walk-forward CV uses 5-year train / 2-year holdout with non-overlapping windows. The walk-forward design (vs k-fold) handles the purge naturally — but the held-back final 2024+ slice has NO embargo, and the LLM emitting a candidate whose label uses a 60-day forward window will have label-leak into the train window.

**Operator discipline:** the finder's `ProposedSpec` declares `label_window_days` explicitly; the LLM must propose specs whose label window does NOT overlap the walk-forward boundaries. The fail-loud check: if `label_window_days > min(holdout_buffer_days)`, the EmittedSpec validator REJECTS the candidate.

---

## 8. The no-relax pledge — operator-signed, embedded here

**The autonomous loop CANNOT relax any of these rules at runtime.** A future LLM iteration that proposes "let's loosen DSR to 0.85 for this regime" is BY CONSTRUCTION rejected:

- `DSR ≥ 0.95` is hardcoded in `ops/lab/run.py`; the LLM cannot edit it (SP-G `enforce_diff_scope` rejects any PR touching `ops/lab/` paths).
- `record_trial_spend` is unconditional at emission time; the LLM cannot route around it (SP-G `emit_once` step 5 is structurally before the PR creation).
- The toolkit whitelist is hard-coded; the LLM cannot add `arch` / `sklearn` (a CI test reds the build on any new whitelist import).
- The PBO floor (≤ 0.20) is in the autonomous Lab criteria; reaching it requires real signal, not relaxation.

These fences are the **structural defense against the autonomous loop's optimization pressure**. The LLM optimizes against "produce edges that pass the gate." If the gate could be relaxed, the LLM would (correctly, from its objective function) find ways to relax it. The fact that the gate is structurally unbendable is the only thing keeping the system honest at scale.

**The Path B autonomous-loop reversal does NOT relax this.** The reversal removes the OPERATOR-GATE step (the operator no longer hand-merges each draft PR). It does NOT remove the DSR / PBO / n_trials / HAC / cumulative-ledger fences. Those are not operator-judgment surfaces — those are mechanical mathematical defenses.

---

## 9. The operator-judgment surface (Path B, 2026-05-21)

What IS operator-judgment under Path B:
- **Outcome verification.** A finder-emitted PAPER engine that passes every fence above + reaches PAPER is presented to the operator for **outcome judgment** (does it actually make money in PAPER?). The criterion is "I know it when I see it" — operator-discretion. The fences above guarantee the candidate is *not statistically spurious*; the operator's eyes verify it's *actually profitable*.
- **Audit-trail review.** Operator reads the audit trail of autonomous actions (draft / undraft / merge / ECR / retire) DAILY. Course-correction is operator-driven from this audit channel.
- **Bleed-budget review.** Auto-retire fires structurally on bleed-budget exhaustion (capital-safety, not signal-quality). Operator can override the auto-retire if the bleed is regime-driven and the engine is otherwise sound.

What is NOT operator-judgment:
- The fences in §§1-7 above. Those are autonomous-loop fences that the operator does not adjudicate.
- The choice of which hypothesis to propose. The finder picks; the gate rejects/accepts.

---

## 10. Persona-level rules (for the LLM reading this file)

When proposing a hypothesis, the LLM MUST:

1. **State the cumulative n_trials state for the target.** Read `cumulative_n_trials(<target>)`; cite the current N in the rationale; acknowledge the DSR deflation.
2. **State the McLean-Pontiff posture.** Is this a textbook anomaly (decay applies) or a novel composite? If textbook, justify why decay doesn't apply HERE.
3. **Declare `label_window_days`.** Required field on every `ProposedSpec`.
4. **Default to HAC.** All time-series regressions use `OLS_HAC_NW`, not raw `OLS`. No exceptions.
5. **Pre-register the primary metric + threshold.** Single hypothesis, single metric, single threshold. NO "and if that doesn't work, try X" fallback chains — that's a multi-hypothesis grid.
6. **Never propose a relaxation.** If the proposed spec doesn't clear DSR ≥ 0.95 in the LLM's own pre-emission sanity check, REJECT the spec at emit-time. Do not propose "but with PBO 0.30 it'd pass" — that is exactly the failure mode this doc exists to prevent.

The LLM that ignores §10 produces emissions that the EmittedSpec validator / `record_trial_spend` / autonomous-Lab-criteria rejector will reject — wasting trials in the multiple-testing sense without ever reaching a PR. Internalize §10 to spend trials productively.

---

## References (literature, not codebase)

- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley. Ch. 7, 11, 12, 14.
- Harvey, C. R., Liu, Y., Zhu, H. (2016). "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1).
- McLean, R. D., Pontiff, J. (2016). "Does Academic Research Destroy Stock Return Predictability?" *Journal of Finance* 71(1).
- Bailey, D. H., Borwein, J. M., López de Prado, M., Zhu, Q. J. (2014–2016). Probability of Backtest Overfitting (PBO) sequence.
- Newey, W. K., West, K. D. (1987). "A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix." *Econometrica* 55(3).
- Andrews, D. W. K. (1991). "Heteroskedasticity and Autocorrelation Consistent Covariance Matrix Estimation." *Econometrica* 59(3).

## In-codebase pointers

- `ops/lab/run.py` — DSR computation + gate floor.
- `tpcore/lab/ledger.py` — cumulative n_trials accounting.
- `ops/lab/run_lab_core.py` — the deterministic verdict path (SP-A).
- `docs/superpowers/specs/2026-05-19-lab-cumulative-trials-ledger-design.md` — SP-A design.
- `docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md` — PR #158, the autonomous criteria that PBO ≤ 0.20 sits in.
- `docs/memory/project_ml_research_track.md` — the commissioned-expert verdict on what kinds of ML research are defensible.
- `docs/memory/project_research_llm_edge_discovery.md` — the operator-ambition + HARD-CONSTRAINT memory (updated 2026-05-21 for Path B).
