# 2026-05-20 — Lab candidate spec consistency audit

**Scope.** Audit of the 5 Lab candidate specs (4 target-engine candidates +
1 meta-emitter design) authored / refreshed 2026-05-20 in this session.
Same shape as the discipline established by PRs #131 / #157 / #178 / #180
/ #187 (the SP-A / SP-B / Vector pilot / per-engine candidate cadence).

**Method.** Six per-spec consistency checks, with file:line evidence
captured for every YES/NO answer. No spec rewrites; in-PR fixes limited
to missing cross-links and missing literature citations on already-pinned
parameter values.

**Specs in scope.**

| # | Spec | Target engine | Intent | PR |
| - | ---- | ------------- | ------ | -- |
| 1 | `docs/superpowers/specs/2026-05-20-vector-composite-lab-candidate.md` | `vector` | `fold_existing` — composite-vs-AND-gate | #157 |
| 2 | `docs/superpowers/specs/2026-05-20-catalyst-insider-cluster-event-lab-candidate.md` | `catalyst` | `fold_existing` — event-confirmed insider cluster | #178 |
| 3 | `docs/superpowers/specs/2026-05-20-momentum-vol-managed-lab-candidate.md` | `momentum` | `fold_existing` — vol-managed + earnings overlay | #180 |
| 4 | `docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-candidate.md` | `reversion` | `fold_existing` — Avellaneda–Lee PCA residual | #187 |
| 5 | `docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md` | meta (no target engine) | spec-emitter design | #152 |

---

## §1. Six consistency checks (per spec)

For each spec, every check is answered with a file:line citation
(spec-relative). N/A is used **only** where the field structurally does
not apply (the spec-emitter is not itself a candidate; the four checks
keyed on a target engine — declared `LAB_TARGET`, byte-identical test
file, literature anchor on pinned numerics — are N/A for it).

### 1.1 Vector composite (`2026-05-20-vector-composite-lab-candidate.md`)

| Check | Verdict | Evidence |
| ----- | ------- | -------- |
| a. Cites Lab Candidate Readiness checklist | **PASS** | spec §0 (L7), §13 walkthrough (L671-702) — every checklist section ticked. |
| b. Declares `LAB_TARGET` shape with target/metric/intent | **PASS** | `vector/backtest.py:1306` (`LAB_TARGET = LabTarget(...)`); intent `fold_existing` declared spec L8 (Route line); `primary_metric=SHARPE` is the SP-D default for vector (no explicit override → byte-identical Sharpe). |
| c. References the SP-A cumulative n_trials ledger | **PASS** | spec §5.2 (L412-431), §13 §4 (L681) — both name `tpcore.lab.ledger.record_trial_spend` / `lab_trial_ledger.vector` and the cumulative-deflation acknowledgement. |
| d. Declares ONE pre-registered robustness check max | **PASS** | spec §2.6 (L212-229) — the catalyst-family ablation, exactly ONE, counted as a trial. §10 (L562) restates "ONE pinned spec + ONE pre-declared ablation". |
| e. Has byte-identical-when-off seam test | **PASS** | `vector/tests/test_composite_flag_byte_identical.py` exists (verified via filesystem search); declared spec §3.3 C1–C4 (L266-285) + §7 (L507). |
| f. Literature anchor for parameter values | **FAIL → fixed in-PR** | spec §2.2 (L103) calls the 30-day insider window "literature-standard non-routine cluster window" without citing a paper. Composite weights `0.35/0.40/0.25` are "pinned by the adjudication" with no literature ground. **Fix:** add Cohen-Malloy-Pomorski 2012 / Lakonishok-Lee 2001 citation to §2.2 mirroring catalyst spec §1.1 (the same window the two specs share). Composite-weights are kept as adjudication-pinned (acknowledged structural choice; no single paper specifies these weights). |

### 1.2 Catalyst insider-cluster event (`2026-05-20-catalyst-insider-cluster-event-lab-candidate.md`)

| Check | Verdict | Evidence |
| ----- | ------- | -------- |
| a. Cites Lab Candidate Readiness checklist | **PASS** | spec §0 (L33) names the checklist; every section header §1–§10 (L56, L92, L205, L237, L275, L295, L312, L323, L376, L394) annotated `(checklist §N)`. |
| b. Declares `LAB_TARGET` shape with target/metric/intent | **PASS** | `catalyst/backtest.py:791` (`LAB_TARGET = LabTarget(...)`); spec L67-69 declares `LabPrimaryMetric.SHARPE` on `catalyst.backtest.LAB_TARGET.primary_metric`; intent `fold_existing` L10. |
| c. References the SP-A cumulative n_trials ledger | **PASS** | spec §4 (L237-273) — names `tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.catalyst`; the "SP-A cumulative deflation" call-out at L273. |
| d. Declares ONE pre-registered robustness check max | **PASS** | spec §1 (L78-82) — explicitly **NO** additional robustness check (uses zero of the allowed one); only the single new toggle. |
| e. Has byte-identical-when-off seam test | **PASS** | `catalyst/tests/test_lab_event_confirmation_byte_identical.py` exists; declared spec §3 (L205-235) C1–C4 + LIVE. |
| f. Literature anchor for parameter values | **PASS** | spec §1.1 implicit via the literature cited at §2.3 of the momentum spec sibling (Chan/Jegadeesh/Lakonishok 1996), and §2.2 (L116-118) names the 30-day window as the "literature-standard non-routine cluster window cited by the Vector composite §2.2". Cohen-Malloy-Pomorski 2012 / Lakonishok-Lee 2001 implicit via that cross-reference. **Cross-link fix:** the catalyst spec cites the Vector spec which itself was missing the explicit citation; fixing Vector (above) closes both. Hold horizon 20d justified explicitly via Chan/Jegadeesh/Lakonishok 1996 (citation already present at spec §2.3 L139-147). |

### 1.3 Momentum vol-managed (`2026-05-20-momentum-vol-managed-lab-candidate.md`)

| Check | Verdict | Evidence |
| ----- | ------- | -------- |
| a. Cites Lab Candidate Readiness checklist | **PASS** | spec L11 (Readiness checklist front-matter), §13 walkthrough (L605-647). |
| b. Declares `LAB_TARGET` shape with target/metric/intent | **PASS** | `momentum/backtest.py:657` declares `LAB_TARGET = LabTarget(...)`; spec L57 declares `LabPrimaryMetric.SHARPE`; intent `fold_existing` L9. |
| c. References the SP-A cumulative n_trials ledger | **PASS** | spec §5.2 (L406-426) explicitly names `tpcore.lab.ledger.record_trial_spend` → `lab_trial_ledger.momentum` and the cumulative-deflation rule; §13 §4 (L621-623) re-cites. |
| d. Declares ONE pre-registered robustness check max | **PASS** | spec §1 (L69-73) — explicitly **NONE**; the cleanest possible Lab footprint. |
| e. Has byte-identical-when-off seam test | **PASS** | `momentum/tests/test_lab_vol_managed_byte_identical.py` exists; declared spec §3.3 (L281-305) C1–C4 + live-path import-isolation. |
| f. Literature anchor for parameter values | **PASS** | spec §1.2 (L128-147) — Daniel & Moskowitz 2016 ("Momentum Crashes", σ_target ≈ 41% annualised); Barroso & Santa-Clara 2015; Moreira & Muir 2017; §1.1 (L107-109) — Chan/Jegadeesh/Lakonishok 1996 + Lewellen 2010 for PEAD-confirmation overlay. |

### 1.4 Reversion PCA residual (`2026-05-20-reversion-pca-residual-lab-candidate.md`)

| Check | Verdict | Evidence |
| ----- | ------- | -------- |
| a. Cites Lab Candidate Readiness checklist | **FAIL → fixed in-PR** | The spec body does not mention `lab_candidate_readiness.md`. Every other 2026-05-20 candidate cites it explicitly (Vector §0/§13, Catalyst §0, Momentum L11/§13). **Fix:** add front-matter "Readiness checklist" line + a §10 (or References) entry citing the checklist by path. |
| b. Declares `LAB_TARGET` shape with target/metric/intent | **PASS** | `reversion/backtest.py:1162` declares `LAB_TARGET = LabTarget(...)`; spec L46 declares `LabPrimaryMetric.SHARPE`; intent `fold_existing` L9-12. |
| c. References the SP-A cumulative n_trials ledger | **PASS** | spec §1 (L61) + §7 (L373-379) — names `tpcore.lab.ledger` and the SP-A cumulative ledger; "2 total n_trials against the SP-A cumulative ledger". |
| d. Declares ONE pre-registered robustness check max | **PASS** | spec §1 (L55-61) + §2.5 (L134-148) — the volume overlay, exactly ONE, on-distribution to Avellaneda 2010 §5. |
| e. Has byte-identical-when-off seam test | **PASS** | `reversion/tests/test_lab_pca_residual_byte_identical.py` exists; declared spec §5.2 (L320-338) C1–C8 incl. live-path import isolation. |
| f. Literature anchor for parameter values | **PASS** | spec §2.1 (L74-82) Avellaneda & Lee 2010 §3.1 (252-day window); §2.2 (L85-101) Avellaneda 2010 §3.2 + Lehmann & Modest 1988 + Litterman (K=3); §2.3 (L103-117) Avellaneda 2010 §4 (half-life 30d, ±1.25 / ±0.50); §2.5 (L134-148) Avellaneda 2010 §5 (volume overlay 1.51); §3.2 (L162-180) Shumway 1997 (delisting bias / -100% convention). The most literature-dense of the four engine candidates. |

### 1.5 SP-G LLM spec-emitter (`2026-05-20-lab-sp-g-llm-spec-emitter-design.md`)

This is a meta-emitter design, not a target-engine candidate. Checks
keyed on a target engine are **N/A** by construction.

| Check | Verdict | Evidence |
| ----- | ------- | -------- |
| a. Cites Lab Candidate Readiness checklist | **PASS** | spec §1 (L11), §2.2 (L84), §2.3 (L94), §3.2 (L175), §3.5 (L252-275), §5 (L362-368), §11 (L602) — the most heavily-cited spec, because the emitter renders into that checklist's shape. |
| b. Declares `LAB_TARGET` shape with target/metric/intent | **N/A — meta-emitter not a candidate** | The emitter READS `LAB_TARGET` from the roster (§3.2 L184-185) and writes `EmittedSpec` whose validator enforces the candidate target's declared metric (§3.3 L207); the emitter itself declares no `LAB_TARGET`. |
| c. References the SP-A cumulative n_trials ledger | **PASS** | spec §2.1 (L70-79), §3.4 (L221-250), §8.1 (L439-443) — `record_trial_spend` is the load-bearing pre-emission step. |
| d. Declares ONE pre-registered robustness check max | **N/A — meta-emitter not a candidate** | The emitter ENFORCES the single-hypothesis-per-emission rule on EmittedSpec via pydantic contract (§3.3 L203, §2.2 L83); it does not itself carry a robustness check. |
| e. Has byte-identical-when-off seam test | **N/A — meta-emitter not a candidate** | The emitter renders a byte-identical-test STUB into every candidate it emits (§4.4 L323) but it does not itself carry a seam test in a target engine's `tests/` tree. |
| f. Literature anchor for parameter values | **N/A — meta-emitter not a candidate** | The emitter has no engine-side pinned numerics. It references Carver / Chan as operator-staged reference excerpts the LLM may read (§3.2 L189-194, §11 L611) — the appropriate analogue for a meta-emitter. |

---

## §2. Cross-link consistency matrix

| Spec | refs Vector pilot | refs SP-A ledger | refs lab_candidate_readiness | refs autonomous-lab-criteria (#158) | refs SP-D pluggable scoring | refs Sentinel SP-E |
| ---- | :---: | :---: | :---: | :---: | :---: | :---: |
| Vector | self | YES | YES | YES (L707 — by branch name) | YES (§0 L16) | YES (§0 L16, §5.1) |
| Catalyst | YES (§0 L28-30) | YES | YES | **YES** (L49 — "PR #158 / `_assess_improvement`") | implicit via SHARPE default | YES (§0 L36-39) |
| Momentum | YES (§1.1 L118-122) | YES | YES | **MISSING → fixed in-PR** | implicit via SHARPE default | implicit via "Sentinel contrast" (§5.1) |
| Reversion | YES (References block L433-440) | YES | **MISSING → fixed in-PR** | **MISSING → fixed in-PR** | implicit (no SP-D extension) | not referenced (different engine lane) |
| SP-G | YES (renders into the same shape) | YES | YES (most-cited) | not explicitly (SP-G stops at draft-PR; ECR adjudication is downstream) | YES (§3.2 L184-185, §11 L599) | YES (§11 L601) |

**Cross-link gaps to fix in-PR:** Reversion missing checklist + autonomous-lab-criteria cross-link; Momentum missing autonomous-lab-criteria cross-link. Both are small additive edits to existing References / front-matter sections — no spec rewrites.

---

## §3. In-PR fixes shipped

Minimal additive cross-link + citation fixes only. No structural spec
edits. No code changes (this is a docs-only audit).

| # | Spec | Fix |
| - | ---- | --- |
| F1 | Vector composite (§2.2) | Add Cohen-Malloy-Pomorski 2012 + Lakonishok-Lee 2001 citation behind the "literature-standard non-routine cluster window" phrase, mirroring the Catalyst spec (which the Vector spec is the upstream source of via §0 cross-link). |
| F2 | Momentum vol-managed (front-matter + §5) | Add cross-link to `docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md` — for a `fold_existing` MODIFY candidate, `_assess_improvement` is the right autonomous gate per the autonomous-criteria spec. |
| F3 | Reversion PCA residual (front-matter + References) | Add `docs/superpowers/checklists/lab_candidate_readiness.md` Readiness-checklist line in the front-matter + a References entry. Add `2026-05-20-autonomous-lab-criteria.md` cross-link (same rationale as F2). |

---

## §4. Gaps surfaced for follow-up (NOT fixed in this PR — scope)

These are structural / cross-spec consistency items that surfaced
during the audit but exceed the in-PR fix scope (minimal cross-link +
literature citation only). Each is queued for separate work.

| # | Surfaced | Why deferred |
| - | -------- | ------------ |
| G1 | Vector composite weights `0.35 / 0.40 / 0.25` are pinned "by the adjudication" without a literature anchor. | This is an honest adjudication-pinned design choice (no single paper specifies these weights); fixing it would require either (a) finding a literature anchor that does pin these weights, or (b) acknowledging adjudication-pinning explicitly as the meta-justification. Either is a spec edit beyond cross-link scope. **Tracked-followup:** consider whether the candidate's eventual ECR-MODIFY adjudication should ask for an equal-weight ablation as the once-pre-declared robustness check rather than the catalyst-family ablation, since the weights are the spec's least-anchored choice. <br/>✅ RESOLVED 2026-05-21 — Vector spec §2.6 pre-declared robustness check swapped from catalyst-family ablation to equal-weight ablation (`(1/3, 1/3, 1/3)` vs adjudicated `(0.35, 0.40, 0.25)`); §5 family-variance evidence-source updated to derive from the primary composite's per-family contributions, with the equal-weight ablation supplying companion weight-robustness evidence; T6 + §13 walkthrough + H-VC-5 row + §5.2 ledger paragraph all updated; swap note added to §2.6. |
| G2 | The byte-identical test files exist by name in all five engines (verified `find`), but the audit did not run the tests to verify they actually assert byte-identity. | Out of scope per task: "If you find a code issue (e.g. byte-identical test exists in name but doesn't actually assert byte-identity), surface in the audit doc as a follow-up; don't fix in this PR." Tracked here. <br/>✅ Verified 2026-05-21 — all 6 byte-identical tests (sentinel/activation_threshold, catalyst/cluster_window, catalyst/event_confirmation, reversion/pca_residual, vector/composite_flag, momentum/vol_managed) assert real byte-identity on the load-bearing `BacktestRunResult` surface (tuple-equality on `(engine, credibility_score, passed_gate, sharpe, profit_factor, max_drawdown, trades, dsr, …, parameters)` for sentinel + both catalyst tests; snapshot-dict equality for vector; per-field equality + parameters-block equality + subprocess import-isolation for momentum and reversion). All 38 tests pass (`pytest -p no:xdist` against the project venv). No fixes needed. |
| G3 | The Vector composite spec §13.11 lists "ops/engine_sdlc/ — parallel session is shipping the autonomous-criteria-set gate (`feat/autonomous-lab-criteria`)" — that work has since landed (PR #158); the Vector spec's "out-of-scope paths" reads as if it were still in-flight. | Refresh post-merge follow-up; not an audit failure (the spec was written before #158 merged and the lane-clean discipline still held). <br/>✅ RESOLVED 2026-05-21 — Vector spec §13.11 line updated to reflect post-merge status ("shipped via PR #158 — referenced here for the lane-clean discipline that held when this spec was written"). |
| G4 | The reversion spec L195-201 contains a "(NOTE: corrected from TODO's '2011-01-01' …)" inline that mixes a typo correction with the substantive train-start choice; the surrounding text says "Train start: 2026-01-01 → train end: 2021-12-31" which is internally inconsistent (the corrected line should read "Train start: 2011-01-01"). | Pre-existing spec defect, not a cross-spec consistency issue. The substantive choice (`2011-01-01`) is unambiguous from the note. Surface for the reversion author's follow-up. <br/>✅ RESOLVED 2026-05-21 — Reversion spec §3.3 bullet rewritten to read "Train start: 2011-01-01 → train end: 2021-12-31"; self-contradicting "(NOTE: corrected from TODO's '2011-01-01' …)" inline removed; the unambiguous substantive choice is now stated directly without the typo-correction parenthetical. |

---

## §5. Closing verdict

After the three in-PR fixes (F1 / F2 / F3), the **30-cell verdict
matrix** (5 specs × 6 checks) reads:

- **Engine candidates (4 specs × 6 checks = 24 cells):** 24 / 24 pass.
- **Spec-emitter meta (1 spec × 6 checks = 6 cells):** 2 pass, 4 N/A.
- **Overall (30 cells):** **26 pass, 4 N/A, 0 fail.**

The five 2026-05-20 specs ship as a consistent set against the SP-A
cumulative ledger + SP-B roster + SP-C readiness checklist + SP-D
pluggable scoring + SP-E Sentinel precedent + PR #158 autonomous
criteria. Confidence-audit closed at full pass after the additive
fixes; the four deferred gaps (§4) are docs-only follow-ups that do
not block the candidates' Lab probes.
