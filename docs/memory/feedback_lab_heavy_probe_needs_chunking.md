---
name: lab-heavy-probe-needs-chunking
description: Heavy Lab probes (T1+T2 universe × multi-year held-back) hit Postgres statement_timeout on the final-holdout replay; verify the chunking fix has landed before kicking off any heavy probe or you burn n_trials with no dossier.
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Operationally vital: heavy Lab probes need the final-holdout chunking fix before running.**

2026-05-21 incident: operator ran the Reversion PCA-residual sweep canonical command via `/tmp/run_reversion_sweep.sh` (T1+T2 universe ~2500 names × 2022-01-01 → 2026-05-15 final-holdout). Walk-forward completed cleanly (3 windows, top-5 candidates printed) — but the final held-back replay crashed:

```
2026-05-21 05:15:19 [error] lab.run_failed error='canceling statement due to statement timeout'
```

No `VERDICT:` line printed → no dossier written. **40 n_trials burned on the SP-A `reversion` ledger anyway** (the increment fires at trial-emission time, before the final replay). Cumulative `reversion` ledger spend reached ~68 post-prior-probes.

**Why:** Postgres statement_timeout was raised to 30 min on the read-only/Lab pool via PR #166, but the final-holdout replay is a single monolithic SQL operation that exceeds even that on a wide-universe × long-period probe. Per-window panel-load has transient-DB retry (PR #163); the final-holdout phase does not chunk.

**Engineering fix:** chunk the final-holdout replay (per-year preferred — 2022/2023/2024/2025/partial-2026) so no single SQL exceeds ~10 min. The verdict (DSR, credibility, n_trades) is computed on the AGGREGATE of chunks; SP-A ledger spend stays ONE per Lab run. Subagent dispatched 2026-05-21 (re-dispatched after a transport error on first attempt); the PR will close this gap.

**Why this is operationally vital:**
- Future heavy Lab probes (Sentinel Bear Score, Catalyst insider-cluster, Momentum vol-managed all use T1+T2 universe × multi-year period) face the same risk.
- Running a probe before the chunking fix lands burns n_trials with NO dossier — the trial-ledger increment is irreversible. Falsification record has to come from the walk-forward output alone (printed to stdout, captured only if the operator logged the terminal).

**How to apply (future-session checklist before kicking off any heavy Lab probe):**
1. Verify the chunking fix has landed on main: `grep -rn "chunk_final_holdout\|_chunked_holdout" ops/lab/` should return non-empty.
2. If NOT landed: do NOT run a wide-universe × multi-year probe. Either narrow the universe (e.g. `--universe-tier-max 1` for ~T1 only) OR shorten the held-back window OR wait for the fix.
3. Always capture terminal stdout — the walk-forward top-5 printout is the only durable signal if the final-holdout crashes.

**Related:**
- [[project-pca-residual-falsified-2026-05-21]] — the specific case
- [[feedback-no-shortcuts-100-pct]] — "every outcome 100% verified"; a no-dossier crash is NOT a verdict
- `docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-candidate.md` post-sweep status block
- `docs/session-log.md` 2026-05-21 entry
