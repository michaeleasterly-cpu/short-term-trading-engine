---
name: master-remaining-program
description: "THE authoritative full remaining program + sequence (operator scope-correction 2026-05-19; refined 2026-05-20). Lean P5 + Agent-Teams decision + Lab front-half SP-A/B/C/D/E/F all SHIPPED. Remaining: SP-G (#242 research-LLM, re-surface autonomous-quant ambition at design point) → engine follow-ups (#148, momentum AAR, orphan scripts, DBLogHandler.run_id) → engine improvements (Lab candidates; s2/catalyst new engines incl. carver) → #189 dashboard refactor → #252 docs-to-reality (DEAD LAST). Autonomous, no checkpoints, lean cadence ([[cut-process-overhead-ship]])."
metadata:
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

Operator 2026-05-19: *"your list is light ... the new setup for the
development environment ... setup to develop with agents ... make
everything faster ... the lab needs to be finished and everything else
the other session was working on"* + *"dashboard refactor and docs to
reality can be dead last."* Single session owns BOTH lanes
([[cross-session-coordination]]). Mode: **fully autonomous, no further
checkpoints** (operator: "Everything"), validated pipeline
([[always-subagent-driven]], gated PR + split review), authoritative
docs/best-practice > CLAUDE.md ([[authoritative-docs-override-claudemd]]).

**MASTER SEQUENCE (in order):**
1. **Finish P5 de-dup** ([[lean-dev-env-state]]): P5.5a (built, in
   review) → P5.5b vector cutover → P5.5c momentum assert_can_graduate
   + delete `_legacy_*`. ~3 cycles. Then P5 = 7/7 clusters done.
2. **Spec-2 Pillar A — Agent Teams ADOPTION** (the "new dev
   environment / develop with agents"; design done in
   [[spec2-agents-dev-env]], was deferred — operator now green-lights).
   Experimental `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, lead+teammates,
   canary-one-task-first, zero-cost human-relay fallback. Brainstorm
   the *adoption specifics* → spec/plan → set up. Riskiest INFRA change
   — care; it makes the Lab epic faster so it precedes it.
3. **Lab front-half epic** ([[lab-front-half-epic]]) — "the lab needs
   to be finished": SP-B roster-driven plug-and-play targeting (replace
   the hardcoded reversion/vector/momentum 3-tuple in
   `ops/lab/run.py` `_runner_for`/`PARAM_RANGES` + `ops.lab`
   `--target-engine` → `tpcore.engine_profile`-driven) → SP-C Lab
   Candidate Readiness checklist (Vector pilot, spec `0a94414` on
   `lab-candidates-rollthrough`) → SP-D/E/F pluggable per-engine
   scoring + richer dossier (Sentinel/Catalyst absorbed as validation
   cases) → SP-G #242 research-LLM edge-discovery
   ([[research-llm-edge-discovery]], gated on the front-half + the
   n_trials ledger which SP-A/SP-A2 already shipped). Each sub-project
   = brainstorm→expert→spec→plan→subagent build. LARGEST chunk.
4. **Engine-lane tracked follow-ups** (now in-scope — "everything else
   the other session was working on"; fold near the related Lab work):
   #148 `test_lab_ntrials_ledger.py` collection-time `del sys.modules`
   eviction defect (per-test eviction scoping; the earlier
   "don't-fix-opportunistically" is SUPERSEDED — it's now scoped work);
   the `lab-isolation-db` postgres CI job (5 DB-gated suites skip);
   [[momentum-aar-plug-finding]]; the 13 orphan scripts (TODO(P5) in
   `test_no_orphan_scripts._ALLOWLIST`); the `DBLogHandler.run_id`
   public accessor + repoint `scripts/ops.py:1939/2108/3350`
   ([[lean-dev-env-state]]).
4b. **Engine improvements** (operator 2026-05-19: "we also had the
   engine improvements to be done"). Repo evidence: NOT one crisp item
   — it is (i) the **edge-research decision-record engine configs**
   (TODO.md ~524-593: graduated Sentinel Bear Score; Vector composite
   value/catalyst/technical 0.35/0.40/0.25 weights; etc.) which are
   explicitly single-pre-registered-spec **Lab candidates routed
   `python -m ops.lab` → DSR/credibility graduation gate → ECR, never
   bypassing the gate, counting against n_trials** → these are
   **SUBSUMED by the Lab front-half epic** (build SP-B/C/D/E/F first;
   then each improvement is a Lab candidate, not a hand-tuned engine
   edit) — do NOT double-count or hand-apply them outside the Lab flow;
   AND (ii) **future engines `s2/`, `catalyst/`** (CLAUDE.md:25) =
   net-new engine builds via the engine scaffold/SDLC. Sequence: runs
   WITH/AFTER the Lab front-half epic (3) — the edge configs flow
   through the Lab the front-half builds; `s2/`/`catalyst/` are
   net-new builds after the Lab can target them roster-driven (SP-B).
   If, on reaching this, the operator's intent is genuinely broader/
   other than (i)+(ii), THEN ask a precise scope question (not before).

5. **#189 dashboard refactor** — DEAD LAST. Input =
   `design_handoff_trading_console/` (Claude-design handoff, untracked).
   Full brainstorm→spec→plan→build.
6. **#252 docs-to-reality reconciliation** — DEAD LAST / final.
   Reconcile CLAUDE.md/specs/docs/TODO/memory vs everything shipped.

**▶ RESUME POINT (updated 2026-05-19 — P5 COMPLETE 7/7):**
- main = `477b380` (P5.5c PR #127 squash-merged, branch deleted),
  origin-synced, working tree clean, stash 0, NO open PRs. CI
  `pytest+ruff+check_imports` green 3m29s (authoritative whole-suite
  gate). Untracked-only: `design_handoff_trading_console/`,
  `docs/MEMORY_MAINTENANCE.md`.
- DONE & merged: code-sweep fixes; Lean P1,P2,P3(c/a/b/d/e),P4;
  Spec-2 Pillar B (#109/#110/#111); P5.1 #120, P5.2 #121, P5.3 #122,
  P5.4a #123, P5.4b #124, P5.5a #125, P5.5b #126, **P5.5c #127**.
  **→ Lean P5 de-dup epic = 7/7 clusters CLOSED.** Split spec+quality
  reviews both passed; momentum `assert_can_graduate` now delegates to
  the shared free fn (stays `BaseEnginePlug`, D2 honoured); `_legacy_*`
  scaffolding + diff tests + pyproject SLF ignore deleted; one
  documented `row`→`run` message normalization.
- **Step (2) Agent-Teams adoption = CLOSED: SKIP single-session**
  (PR #128 merged `a9af35c`; Decision Record = §7 of
  `docs/superpowers/specs/2026-05-19-agents-dev-environment-design.md`).
  claude-code-guide expert verdict (current official Teams docs): the
  only single-session delta over the existing subagent-driven +
  parallel-agents + per-feature-worktree stack is inter-agent
  messaging, bought at a shared-tree same-file race — net risk, no
  benefit on a live-money platform; operator's "develop with agents /
  faster" intent already realized by shipped Pillar B. Premise
  (replace 2-session relay) moot single-session. §3 Phase B design
  retained ONLY if a future multi-session topology re-creates the
  premise. Ancillary "tighten the flow" suggestions explicitly OUT
  (scope discipline). [[spec2-agents-dev-env]] updated.
- **Step (3) Lab front-half epic — SP-B COMPLETE & MERGED** (PR #131
  `bcbd98a`). Roster-driven plug-and-play Lab targeting: spec #129,
  plan #130, impl T0–T8 each split-reviewed (spec then code-quality)
  via subagent-driven-development. CI authoritative gate SUCCESS; full
  single-process suite 1945 passed/0 failed; SP-A spine byte-untouched.
  3 live-money fence holes found+closed mid-flight (planner-path
  ImportError/SyntaxError; malformed-LAB_TARGET AttributeError; a T5
  oracle-drift footgun) + a hollow plan red-proof corrected; plan/spec
  kept truthful via `> Plan correction` notes. The pre-existing #148/
  ops-package-shadow subset artifact did NOT manifest under the proper
  single-process gate (only hand-picked subsets) — tracked in master
  step 4 / task #15.
- **SP-C COMPLETE & MERGED** (PR #132 `1724dbf`): `docs/superpowers/
  checklists/lab_candidate_readiness.md` + present-sentinel test +
  dossier Lab→ECR cross-link; spec+doc-quality reviews passed.
- **NEW BACKLOG (operator 2026-05-20): task #24 build `carver`
  engine** (Carver Systematic Trading; canonical key/pkg `carver` —
  operator renamed from systematic_carver) via SDLC ADD/new_scaffold —
  sequenced at master step 4b (new engines), after Lab front-half,
  unless operator says "prioritize carver". Design captured in task
  #24; refs [[ref-carver-systematic-trading]] +
  [[ref-chan-algorithmic-trading]] (standing cross-engine improvement
  toolkit too).
- **⚠ CADENCE CHANGE 2026-05-20 ([[cut-process-overhead-ship]]):**
  operator emphatic the review spiral burned tokens + still shipped
  buggy artifacts. STANDING: implement directly, ONE review/task max
  (no split spec+code-quality THEN re-review-each-fix), tests+CI are
  the proof not ceremony, fold/skip reviewer Minors don't round-trip,
  bias to DONE+merged. CI gate stays (it caught the real SP-D
  hermeticity + ops-shadow bugs local runs missed — verify CI-exact
  locally before push).
- **SP-D COMPLETE & MERGED** (PR #135 `0cb64b0`): pluggable per-engine
  ranking metric (`LabTarget.primary_metric`/`LabPrimaryMetric`,
  `_RANKING_METRICS`, pre-spend resolve fence, objective dossier);
  gate byte-identical by construction; make-or-break GREEN
  non-vacuously. Reviews caught real issues (MAXDD sign, unsatisfiable
  construction, dead-return); CI caught non-hermetic test + ops-shadow
  cascade (fixed: defer `import ops.lab.run` in-body).
- **SP-E COMPLETE & MERGED** (PR #136 `64975f7`): Sentinel as
  roster-driven Lab target w/ `primary_metric=MAXDD_REDUCTION`,
  feature-flag-variant (live path byte-identical), gate sacred,
  hermetic tests; FRED `hy_spread`/`credit_spread` prereq confirmed
  LIVE. Lean cadence worked one-shot through fix-loop after
  consolidated review caught a tautological test (now invokes real
  `_run_lab_core`→gate with non-vacuity proven).
- **Lab front-half now 5/7** shipped (SP-A pre-existing; SP-B/C/D/E
  this session). NEXT = SP-F (Catalyst new engine via
  engine_template+ECR-ADD, scoped to the data-ready insider-cluster
  leg). Then SP-G (#242 — re-surface autonomous-quant ambition at
  design point per [[research-llm-edge-discovery]]).
- **(prior SP-D detail, historical):** pluggable per-engine
  scoring → SP-E Sentinel validation case (confirm hy_spread/
  credit_spread FRED prereq) → SP-F Catalyst new-engine via
  engine_template+ECR → SP-G #242 thin advisory LLM spec-emitter
  (gated on all prior). Each sub-project = brainstorm→expert-harden→
  spec(gated PR)→plan(gated PR)→subagent-driven T0..Tn w/ split
  spec-then-code-quality reviews→CI(gh pr checks, gate on the pytest
  check CONCLUSION not mergeStateStatus — [[ci-gate-on-check-conclusion]])
  →squash-merge→sync.
- MASTER SEQUENCE remaining in order: (3) Lab front-half SP-B..SP-G →
  (4) engine follow-ups → (4b) engine improvements → (5) #189 →
  (6) #252. Autonomous, no checkpoints, validated pipeline (gated PR +
  split spec/intent then code-quality review), authoritative-docs>CLAUDE.md.
- Update THIS Status block as each track lands (compaction-safe).

**2026-05-21 OPERATOR DECISION — local-LLM-bridge + run-everything-to-find-bugs.**
Post-gate-pilot (PR #254 dossier — Task #25 §10.6.b PASS): operator
rejected Anthropic API credit top-up. ALL 4 LLM lanes (edge finder
T9, SP-G emitter, data triage, engine triage) must route through the
operator's local Claude Max Pro session via `ops/llm_local_bridge.py`
(NEW MODULE — single-source the bridge contract). Hosting split:
edge finder + 3 LLM lanes = LOCAL-ONLY on operator's Mac; rest of
platform (data ops + engines + daemons) = Railway per the
[[project_railway_archive_substrate_migration]] roadmap. Why: API
billing compounds to real $$ when the Max subscription already pays
for the same model access. Backlog captured in TODO.md L499 (above
the Task #25 epic block).

Parallel directive: **run EVERY component end-to-end** (not just
mocked tests) to surface design-vs-real-data drift bugs (today's
gate pilot exposed 7 column-name mismatches + LLM-shape gap — bugs
no FakePool test could catch). Cadence: actually-run → capture error
→ classify (real-bug / design-drift / missing self-heal coverage) →
add HealSpec if missing → re-run green-as-cat-piss → commit + defect-
register-log if applicable.

**SIDE-EPIC 2026-05-20: AUTONOMOUS SELF-HEAL P0 = 5/5 SOURCES SHIPPED.**
Distinct workstream from SP-A..SP-G (operator-prompted in-session, not
on the master sequence). Each P0 source got an ungameable
physical-truth completeness invariant + HealSpec routed to canonical
`ops.py --stage X` infrastructure, detector/healer symmetry via shared
`_evaluate()`. PRs: macro_indicators (#168), fundamentals_quarterly
(#172), corporate_actions (#174), sec_insider_transactions (#179),
earnings_events (#181). KNOWN_CHECK_NAMES grew 22→25 across the round.
Mid-round fix-forward PR #175 (function-scoped autouse fixture race
under pytest-xdist; switched to session scope + dropped delete).
**P1 follow-ons surfaced:** (a) `liquidity_tiers` +
`ticker_classifications` completeness shape (different — derived/
recomputed, not append-only); (b) earnings_events NO_BEAT sentinel
ingestion (Path B in the design — emit non-beat rows so per-quarter
completeness becomes auditable; today's invariant catches truncation
only, KNOWN GAP). Also built the DFCR planner mid-round (PR #170 —
ADD/REMOVE/CUTOVER/cadence) to unblock fundamentals provider
binding; symmetric to ECR.

**SIDE-EPIC 2026-05-21: DEEP-RESEARCH LAB-CANDIDATE SWEEP — 4/5
HONEST FAILURES.** All four spec'd Lab candidates from the
2026-05-20 deep-research adjudication have now probed:
- Vector composite (`vector_composite`): **FAILED 2026-05-20**
  (DSR/credibility gate; dossier in `docs/lab/2026-05-20-...`).
- Reversion PCA-residual (`reversion_pca_residual`): **FALSIFIED
  in walk-forward 2026-05-21** (PR #219; concurrent-session record).
- Sentinel graduated Bear Score (`sentinel_bear_score`):
  **FAILED 2026-05-21** with n_trades=0 OOS — defect filed
  `[defect_ref: SENTINEL-ACTIVATION-DORMANT-2026-05-21]`. Offline
  activation-score distribution probe (PR #220 + hotfix #223 to
  migrate the probe to `ops.py --stage probe_sentinel_activation`)
  showed **OOS p95=0.237, 100% DORMANT across 872 holdout days**.
  The composite never reaches the 0.45 LIGHT floor in the
  2024-2025 window. **Insight (NOT a defect in the composite):**
  Sentinel is a defensive engine; literature-anchored thresholds
  (Sahm/CFNAI) are correctly rare-firing. In a non-recessionary
  holdout, `n_trades=0` is the CORRECT engine behaviour — but
  the sacred Lab gate (`DSR≥0.95 ∧ n_trades≥3`) is **structurally
  inappropriate** for defensive engines.
- Catalyst event-confirmed insider-cluster drift
  (`catalyst_insider_drift`, `event_confirmation_mode=positive_beat_30d`):
  **probe IN-FLIGHT 2026-05-21**, last shot before the wave is
  fully exhausted.
- Momentum vol-managed 12-1 + earnings overlay: DEFER per TODO
  (slowest DSR-accrual cadence; lowest impact×prob/effort).

**Strategic open question:** Lab-evaluability for defensive engines
needs a re-frame — three honest paths (1) declare defensive engines
validation-by-construction (no n_trials spend); (2) SP-D
pluggable-metric extension where the gate becomes
`MAX(DSR_with_trades, equity_protection_score)`; (3) run against a
recessionary backtest window (2007-2010 or 2020 Q1-Q2 explicit
`--train-start` override). Pending operator decision.

`lab_trial_ledger.sentinel` cumulative = 40 (every subsequent
Sentinel probe strictly harder). `lab_trial_ledger.catalyst` will
update when in-flight probe completes.

Concurrent session work landing today (other authors): PR #221 (test
isolation for `test_search_parameters` reverse-order — mirror PR #165
pattern), PR #222 (Lab final-holdout replay chunking to avoid
Supabase `statement_timeout`), PR #213 (#242 SP-G hardened design
spec), PR #214 (publishing gist staging + stelib PyPI carve), PR
#215 (vector internal `catalyst→earnings` vocab rename — resolve
catalyst/engine-name collision).

**Public-repo flip 2026-05-21:** GitHub Actions free-tier minutes
exhausted (2000/2000) → operator made repo public → unlimited free
minutes restored. The merge-window outage produced 2-3 PRs with
1-second-empty-steps "failures" (#211, #212 originally) — all
verified locally-green; reruns post-public-flip confirmed real CI
SUCCESS. Future watch: 1-second-empty-step jobs are the diagnostic
signature of a quota wall, not a code regression.
