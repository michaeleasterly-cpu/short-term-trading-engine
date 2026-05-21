---
name: pca-residual-falsified-2026-05-21
description: Reversion PCA-residual (Avellaneda-Lee 2010) lost to the price_z baseline in walk-forward 2026-05-21; no MODIFY shipped; do not propose re-running the same hypothesis. The signal_mode opt-in seam stays in code but defaults to price_z.
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Outcome (2026-05-21): Reversion PCA-residual Lab candidate FALSIFIED in walk-forward.**

**The result.** Operator-run sweep (3 walk-forward windows) on T1+T2 universe × train 2011-01-01 / holdout-start 2022-01-01 / held-back-end 2026-05-15 produced top-5 candidates by mean OOS score:

1. `signal_mode=price_z` (the existing live baseline) — score 0.934, windows=3
2. `signal_mode=pca_residual` — score 0.891, windows=2
3. `signal_mode=price_z` — score 0.817, windows=4
4. `signal_mode=pca_residual` — score 0.694, windows=4
5. `signal_mode=pca_residual` — score 0.670, windows=4

The price_z baseline dominated. PCA-residual placed 2nd / 4th / 5th and did not beat the existing live signal.

**Final held-back replay crashed** on Postgres statement_timeout before the dossier was written — no `VERDICT:` line, no sidecar `.json`. But the walk-forward result alone is sufficient to falsify the hypothesis at this trial count. See [[lab-heavy-probe-needs-chunking]] for the infra side.

**N_trials spent:** ~40 on the SP-A `reversion` ledger from this sweep (cumulative ~68 post-prior probes). The DSR penalty grows with cumulative trials per SP-A — this is the safety mechanism working, not a problem.

**Why this matters / how to apply:**

- **Do NOT propose re-running PCA-residual.** Per operator standing rule (falsification is final; n_trials honest accounting binds), the hypothesis is closed at this trial count. No parameter tweaking, no narrower-universe retry, no "let me try with different PCA components" — that's exactly the multiple-testing inflation that `project_ml_research_track` warned about.
- **`reversion/lab_pca_residual.py` + `tpcore/backtest/pca_residual.py` + the `signal_mode` opt-in seam STAY in code.** They are byte-identical-when-off (default `signal_mode=price_z` unchanged). The Sigma lesson binds: a falsified Lab candidate's code can stay; what changes is the *recommendation* (don't ship it live). The live `reversion/setup_detection` path remains on `price_z` indefinitely; the deferred follow-up #173 is now moot.
- **The price_z baseline is now empirically validated** at this trial count — the existing live signal is the right one for reversion, not a placeholder waiting for a replacement.

**The PR record:**
- PR #187 — Lab candidate build (Avellaneda-Lee 2010, signal_mode opt-in, byte-identical-when-off)
- PR #219 — falsification record (spec status flipped to FALSIFIED; session-log entry with top-5 verbatim)
- separate engineering PR (in flight) — Lab final-holdout chunking to unblock future heavy probes

**Cross-refs:**
- [[lab-heavy-probe-needs-chunking]] — why the dossier didn't write
- [[project-ml-research-track]] — the commissioned-expert verdict that this outcome respects
- [[ref-carver-systematic-trading]] / [[ref-chan-algorithmic-trading]] — the reference set the original spec drew from
