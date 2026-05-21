# Lab Front-Half Epic — Ordered Decomposition (SP-A … SP-G)

**Status:** APPROVED (operator 2026-05-19). Decomposition + roadmap.
**Memory ADR:** `project_lab_front_half_epic`, `project_research_llm_edge_discovery`, `project_ml_research_track`.
**Lane:** engine lane only. Data-SDLC files are read-only symmetry references, never edited here.
**Discipline:** one sub-project at a time; isolated worktree; brainstorm→expert-decompose→spec→expert-harden→writing-plans→subagent-driven-exec (split spec + code-quality review)→PR→CI-green merge. Don't stomp the data lane.

---

## 0. Context — what already exists (verified in code, 2026-05-19)

The Lab's **back half is built and correct** (SDLC SP2, `2026-05-18-engine-lab-design.md`):

- Enforced isolation: `tpcore/lab/context.py` `LabContext` flips `_LAB_ACTIVE`; `tpcore/db.build_asyncpg_pool` auto-engages `default_transaction_read_only=on`; 5 reentrancy guards (`assert_not_in_lab`) at the side-effect boundaries. A Lab bug cannot corrupt live capital.
- The sacred graduation gate: `compute_dsr_for_verdict` + `final_result.credibility_score` → `survived = dsr ≥ 0.95 ∧ cred ≥ 60 ∧ n_trades ≥ 3` (`ops/lab/run.py:787`).
- `lab.<candidate>` no-poison namespacing (H-S2-3): Lab credibility persists under `source=backtest_credibility.lab.<candidate>`, so `graduation_ready(pool, <live_engine>)` (`tpcore/backtest/credibility.py:230`) can never read an experimental score.
- The ECR promotion rail + the frozen `LabResult` SP2→SP3 contract (`tpcore/lab/models.py`).
- n_trials honesty **inside a single run's verdict** (`compute_dsr_for_verdict(returns, n_trials=args.trials)`, `ops/lab/run.py:726`).

The **front half is missing** — four interlocking pieces. The Lab today:

- Deflates DSR **per-run only** (`n_trials=args.trials` — no cross-run/cross-candidate memory). **← the safety hole SP-A closes.**
- Hardwires a stale 3-tuple (`reversion/vector/momentum`) in `PARAM_RANGES`, `_runner_for`, `_context_loader_for`, `_context_runner_for`, and the `ops.lab` CLI `--target-engine choices` — contradicting the SP1 roster SoT (`tpcore.engine_profile`); already stale (Sigma archived, canary added).
- Has no formal Lab Candidate Readiness checklist (the feature-flag-variant pattern is hand-authored per the Vector pilot).
- Ranks Sharpe-only (`_score_for_ranking`, `ops/lab/run.py:386`).

---

## 1. The ordered SP chain (goal · dependency+why · delivers · CI-green boundary · scale)

### SP-A — Cross-candidate n_trials ledger  **[FIRST · safety-critical]**

- **Goal:** DSR deflation for any Lab verdict against a target engine uses the **cumulative** trial count ever spent in pursuit of an edge for that target, not the single run's `--trials`.
- **Dependency:** none — it is the safety floor. **Why first:** every later piece (SP-B roster targeting, SP-D pluggable scoring) and the entire #242 vision *adds Lab activity*. Without a cumulative ledger, more activity = more independent small DSR penalties = launder multiple-testing inflation past the one ungameable gate. Building anything that increases Lab throughput before the ledger exists is the `project_ml_research_track` failure mode at scale. Non-negotiable; literal precondition for #242.
- **Delivers:** an event-sourced cumulative-trial read; `_run_lab_core` switches DSR deflation to `cumulative_n_trials(target) + this_run_trials`; the gate threshold (0.95 / 60 / 3) is **unchanged**.
- **CI-green boundary:** full suite green incl. the make-or-break tests (monotone-harder, no-reset, cumulative-fails-where-per-run-survived, gate-threshold-unchanged, live-graduation-untouched); ruff/check_imports exact; lane assertion. Mergeable standalone — it strictly tightens an internal accounting number, touches no live engine.
- **Scale:** genuine SP-scale (safety-critical accounting + persistence design + ungameable proof), but a *contained* one — see this doc's §2 (the full hardened SP-A spec).

### SP-B — Roster-driven plug-and-play Lab targeting

- **Goal:** replace the hardwired 3-tuple. `PARAM_RANGES` / `_runner_for` / `_context_loader_for` / `_context_runner_for` / CLI `--target-engine choices` become driven by `tpcore.engine_profile` (any engine in `LifecycleState.{LAB,PAPER,LIVE}`). Engine add/remove is a SoT edit, never Lab surgery.
- **Dependency:** **after SP-A.** Why: SP-B widens *which* engines the Lab can fish against (and the convenient set of targets). Widening the attack surface before the cumulative ledger exists re-opens exactly the hole SP-A closes for the newly-targetable engines. The ledger must be the floor every new target stands on from its first run.
- **Delivers:** a roster-SoT dispatch indirection (the per-engine `run_for_search` / `load_*_window_context` / `run_*_with_context` / param-range registration declared by/derived from the engine, resolved via `engine_profile`); CLI choices generated from the roster; the stale-shadow contradiction with SP1/SP4 consistency clockwork removed.
- **CI-green boundary:** full suite green; a consistency test asserting the Lab target set == roster SoT (mirrors SP4 clockwork — a new/removed engine fails the build until the Lab follows). Mergeable standalone.
- **Scale:** genuine SP-scale (touches the dispatch core + a new per-engine declaration contract + a clockwork test), but mechanical relative to SP-A.

### SP-C — Lab Candidate Readiness checklist

- **Goal:** formalize the canonical **feature-flag-variant pattern** (off-by-default backtest code path reached by ONE Lab param toggle; live path byte-identical; single pre-registered primary hypothesis) as a first-class checklist sibling to `docs/superpowers/checklists/engine_readiness.md` / the ECR.
- **Dependency:** after SP-A; independent of SP-B (a doc/checklist artifact, not a code-path change). Sequenced after SP-A so the checklist can *mandate* "one Lab run = one cumulative ledger increment against the target" as a readiness item (the ledger must exist to reference it).
- **Vector-pilot feed:** the Vector composite spec (`docs/superpowers/specs/2026-05-19-vector-composite-lab-candidate.md`, commit `0a94414` on `lab-candidates-rollthrough`) is the **reference pilot** — its hand-authored feature-flag-variant pattern is the worked example SP-C generalizes. Vector composite proceeds standalone (NOT a sub-project; not blocked by the epic).
- **Delivers:** `docs/superpowers/checklists/lab_candidate_readiness.md` + the single-pre-registered-primary-hypothesis rule + the byte-identical-live-path rule + the n_trials-ledger-acknowledgement item.
- **CI-green boundary:** doc-only; the only "test" is a cross-link/consistency assertion if one is added (e.g. the checklist referenced from the ECR). Mergeable standalone.
- **Scale:** **thin** — a checklist + cross-links. Not SP-scale code; SP-scale only in that it is a gating artifact every future candidate (incl. #242 output) must pass.

### SP-D — Pluggable per-engine success scoring + richer dossier

- **Goal:** generalize the Sharpe-only ranking (`_score_for_ranking`) to a per-engine **declared primary metric** + an objective-appropriate dossier block. **The graduation GATE stays sacred/unchanged** — the pluggable metric only changes *which candidate WINS the ranking*, never *whether it may graduate* (DSR≥0.95 ∧ cred≥60 ∧ n_trades≥3 is untouched).
- **Dependency:** after SP-A (ledger first, always); independent of SP-B/SP-C. Must precede SP-E (Sentinel's success bar is not Sharpe/DSR-expressible — it needs the pluggable metric to even be rankable).
- **Delivers:** a per-engine primary-metric declaration (objective: Sharpe / maxDD-reduction / ulcer / inverse-ETF-hold / …) consumed by `_score_for_ranking` + `rank_candidates`; a dossier block keyed to the declared objective.
- **CI-green boundary:** full suite green; a test pinning that the gate verdict is byte-identical regardless of the chosen ranking metric (the make-or-break: ranking-metric pluggability never reaches `survived`). Mergeable standalone.
- **Scale:** genuine SP-scale (a new per-engine declaration + ranking generalization + the gate-invariance proof).

### SP-E — Sentinel validation case  *(proves SP-D)*

- **Goal:** run Sentinel through the front-half Lab as the validation case proving SP-D — its success bar is **maxDD-reduction / ulcer / inverse-ETF-hold**, NOT Sharpe/DSR-expressible (the contrast the Vector spec confirmed).
- **Dependency:** after SP-D (needs pluggable scoring) and SP-B (Sentinel must be a roster-driven Lab target — it is not in the hardwired 3-tuple). **Data prereq (must confirm before the validation run):** `hy_spread` / `credit_spread` are wired into live FRED ingestion.
- **Delivers:** a Sentinel Lab candidate + a passing front-half run demonstrating a non-Sharpe primary metric ranks correctly while the gate stays sacred.
- **CI-green boundary:** the Sentinel candidate + its readiness checklist pass; full suite green. Validation, not new core machinery.
- **Scale:** **thin-to-medium** — mostly a candidate definition + a data-prereq confirmation; it is a *proof case*, not new infrastructure.

### SP-F — Catalyst validation case  *(proves SP-B + SP-C)*

- **Goal:** stand up a brand-new engine (Catalyst) via `tpcore/templates/engine_template/` + ECR-ADD `promote_new`, targeted by the **roster-driven** Lab, passing the **Lab Candidate Readiness** checklist.
- **Dependency:** after SP-B (brand-new engine ⇒ must be a roster-SoT-driven Lab target — the literal proof SP-B works) and SP-C (must pass the readiness checklist). After SP-A by transitivity. **Data state:** insider-cluster leg data-ready (646k Form-4 rows); 8-K leg data-gated on item-level parsing (scope the candidate to the ready leg).
- **Delivers:** Catalyst engine scaffold + a roster-driven Lab run + a readiness-checklist pass — the end-to-end proof of SP-B+SP-C.
- **CI-green boundary:** Catalyst passes engine_readiness + lab_candidate_readiness; roster consistency clockwork stays green with the new engine; full suite green.
- **Scale:** genuine SP-scale (a new engine), but its *front-half* novelty is just "first roster-driven new target" — the engine build itself follows the established engine_readiness path.

### SP-G — #242 research-LLM edge-discovery  **[LAST · gated on all prior]**

- **Goal:** a **thin, advisory, human-gated LLM spec-emitter** (engine-lane) that drives the front-half pipeline to propose Lab candidates.
- **Dependency:** gated on **all** of SP-A…SP-F. Why last: an LLM that proposes N hypotheses is precisely the `project_ml_research_track` failure mode — it is only safe atop the cumulative ledger (SP-A), roster-driven targeting (SP-B), the readiness checklist (SP-C), and pluggable honest scoring (SP-D), all proven by SP-E/SP-F. It NEVER bypasses the gate, NEVER auto-applies to live capital; the LLM proposes, the deterministic gate (now cumulatively-deflated) disposes.
- **Delivers:** a thin advisory spec-emitter that produces `LabCandidate`s a human reviews before any Lab run; every LLM-proposed config is a cumulative trial against the target (SP-A counts it — the LLM cannot under-declare).
- **CI-green boundary:** out of this epic's first-pass scope beyond the spec; built as a thin LATER phase after the front-half ships + is gate-proven (per the memory ADR).
- **Scale:** genuine SP-scale, but explicitly a thin emitter — the heavy safety lifting is SP-A.

### Decomposition risk notes

- **No merges recommended.** SP-A and SP-B both touch `_run_lab_core`/`ops/lab/run.py` but on disjoint concerns (DSR n_trials input vs dispatch resolution); merging them would entangle a safety-critical change with a mechanical refactor — keep them separate, SP-A first, so SP-A's make-or-break tests gate on a minimal diff.
- **SP-C / SP-E are thin** and could *look* mergeable into their proof partners, but keeping SP-C (the checklist) separate from SP-E/SP-F (the proofs) preserves "the checklist exists and is gating *before* the first new candidate uses it."
- **Vector composite is intentionally not a sub-project** — it is the in-flight reference pilot that *seeds* SP-C; it proceeds standalone off `lab-candidates-rollthrough` and is not blocked by, nor blocks, the epic chain.
- **Hard ordering invariant:** SP-A precedes everything. SP-B precedes SP-F. SP-D precedes SP-E. SP-G is last. SP-C after SP-A, before SP-F.

---

# SP-A — Cross-candidate n_trials Ledger (Hardened Spec)

> The spec body for SP-A lives in the sibling file
> `docs/superpowers/specs/2026-05-19-lab-ntrials-ledger.md`.
> This epic doc is the roadmap; that file is the build contract for the FIRST sub-project.
