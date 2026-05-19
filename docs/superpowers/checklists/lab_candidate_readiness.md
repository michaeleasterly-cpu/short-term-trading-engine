# Lab Candidate Readiness Checklist

Pre-flight checklist for **any** Lab candidate (a single pre-registered
edge experiment run via `python -m ops.lab --candidate <name>
--target-engine <engine> --intent {fold_existing|promote_new}`) before
the candidate is run against the sacred held-back DSR/credibility gate.
Every box must be checked **before the Lab run is launched** — not after
the dossier comes back.

This is the Lab-lane sibling of
`docs/superpowers/checklists/engine_readiness.md`. Where
`engine_readiness.md` gates an **engine ADD** (the Engine SDLC ADD-path
build gate), this checklist gates a **Lab candidate** before it can
spend a cumulative n_trials increment against a target engine and
produce a graduation dossier. The two are deliberately the same shape:
numbered non-optional sections, a `grep`-able compliance set, and a
named place in the flow where it is enforced.

> **Reference worked example (the pilot).** The Vector composite spec —
> `docs/superpowers/specs/2026-05-19-vector-composite-lab-candidate.md`
> — is the hand-authored pilot this checklist generalizes. Every section
> below cites the Vector pilot section that demonstrates the item. When
> in doubt about the *shape* of an item, read that section of the pilot:
> it is the canonical instance, this checklist is the rule.
>
> **Pilot location caveat (in-flight).** The pilot is not yet on `main`
> / this branch — it currently lives only on the in-flight branch
> `lab-candidates-rollthrough` (commit `0a94414`). To read it without
> switching branches:
> `git show 0a94414:docs/superpowers/specs/2026-05-19-vector-composite-lab-candidate.md`.
> **Post-merge follow-up:** once the Vector composite lands on `main`,
> repoint every `(Vector pilot §X)` reference to the on-`main` path and
> **delete this branch/commit caveat** (here and the Sibling
> cross-reference entry) — the dangling `0a94414` ref must not outlive
> the merge.

> **These ten sections are non-optional.** A candidate that cannot tick
> every box is not Lab-ready — it is either an unscoped sweep, an
> n_trials-laundering hazard, or a live-path risk, and must be reworked
> into a single pre-registered spec before it is run. There is no
> "skip a section" path; a candidate that genuinely cannot satisfy a
> section is rejected with the exact reason (mirroring the ECR's
> reject-don't-force discipline), never run anyway.

---

## 1. Single pre-registered primary hypothesis

- [ ] The candidate has a written spec under
      `docs/superpowers/specs/` (the Vector-pilot precedent;
      **not** `docs/lab/`, which is the machine-generated Lab dossier
      output dir) with **exactly ONE
      pre-registered primary hypothesis** and **ONE primary metric/verdict**
      stated *before* the run. (Vector pilot §1–§2: "tests that
      structural fix once, honestly".)
- [ ] **No post-hoc metric shopping.** The success/falsification
      criterion is pinned in the spec (the §9 "red is red" truth table
      in the pilot). A FAIL is logged as a genuine falsification and the
      candidate is **not** re-run with tweaked parameters — that is a
      sweep / n_trials laundering. (Vector pilot §9.)
- [ ] **At most ONE pre-declared robustness check**, and it is itself
      counted as a trial in the n_trials accounting (Section 4). It is a
      single named run, not a parameter the Lab samples. (Vector pilot
      §2.6.)
- [ ] Every numeric constant in the spec is **pinned** (no grid, no
      `--family-weights` menu, no range). A placeholder scan
      (`TODO`/`TBD`/`???`/`<…>`) comes back empty. (Vector pilot §12
      placeholder scan; §2 "every constant pinned".)
- [ ] The spec explicitly states this is **NOT a sweep** and enumerates
      which knobs are code constants vs the (one) Lab-sampled toggle.
      (Vector pilot §5.2, §10 "NOT a parameter sweep".)

## 2. The feature-flag-variant pattern (the canonical Lab-candidate shape)

The canonical shape of a `fold_existing` Lab candidate is an
**off-by-default backtest code path reached by exactly ONE Lab param
toggle**, with the **live trading path byte-identical when the flag is
off**. (Vector pilot §3 — "the make-or-break invariant".)

- [ ] The new code path lives **only in the target engine's
      `backtest.py`** (or a backtest-only module), behind a module-level
      override that **defaults to `None`/off** and mirrors the engine's
      existing `_*_OVERRIDE` pattern. The legacy default is the value
      when no override is supplied. (Vector pilot §3.1
      `_COMPOSITE_MODE_OVERRIDE`.)
- [ ] The variant is reached by **exactly ONE** new
      `PARAM_RANGES["<engine>"]` key (a `choice:` toggle whose values
      are `{legacy_default, the_one_new_spec}`). No environment
      variable, no config file, no second toggle, no default-on path
      anywhere. (Vector pilot §4.1.)
- [ ] The override is read into a module global and **reset per call**
      (the existing `run_<engine>_with_context` override-reset
      discipline) so no module-global state bleeds across Lab trials.
      (Vector pilot §3.1, §4.2; H-VC-8.)
- [ ] `default_params()` / the engine's `*_OVERRIDE_KEYS` set gains the
      new key with the **legacy default** so the dossier `param_diff`
      carries the true `legacy → variant` delta. (Vector pilot §4.2;
      H-VC-10.)
- [ ] **`grep` proof:** the only non-test files changed are the target
      engine's `backtest.py` and the one `PARAM_RANGES` key in
      `ops/lab/run.py`. `vector/plugs/*`, `<engine>/scheduler.py`,
      `<engine>/order_manager.py`, `scripts/run_all_engines.sh`,
      `ops/platform_pipeline.py`, any SoT/roster, and the live dispatch
      are **NOT** in the diff. (Vector pilot §3.2, §7, §10.)

## 3. Byte-identical live path (the make-or-break proof)

A candidate author must **prove** the live trading path is unchanged
when the flag is off — not assert it. (Vector pilot §3.3, the C1–C4
characterization test; §11 T0.)

- [ ] A characterization test (`<engine>/tests/test_<feature>_byte_
      identical.py` or equivalent) pins a **committed golden** of
      `run_<engine>_with_context(ctx, overrides={legacy keys only})`
      `BacktestRunResult` **field-for-field equal** to the
      pre-candidate behaviour. Build FAILS if the golden drifts.
      (Vector pilot §3.3 C1.)
- [ ] The test asserts the flag **default is the legacy path** when the
      override is `None`, when the toggle is omitted from `overrides`,
      and when it is explicitly set to the legacy value. (Vector pilot
      §3.3 C2.)
- [ ] The test asserts the variant **is reachable and distinct** (turning
      the toggle on changes the result) — proving the branch is wired,
      not dead. (Vector pilot §3.3 C3.)
- [ ] The test asserts **no cross-trial leakage**: running the variant
      then a legacy call in the same process yields the legacy golden
      (the per-call module-global reset). (Vector pilot §3.3 C4.)
- [ ] The golden is captured **before** the variant code exists (TDD
      RED first): the byte-identical contract is locked before the
      feature is written. (Vector pilot §11 T0.) This mirrors how
      SP-B/SP4's roster consistency clockwork proves "no live-path
      change" — the live path is fenced by a test that reds on drift,
      not by reviewer goodwill.

## 4. n_trials ledger acknowledgement (the cumulative DSR-deflation rule)

The platform's binding constraint is the multiple-testing / overfit
verdict. **One Lab run = one cumulative trial-spend increment against
the target engine.** The candidate author must acknowledge this in the
spec — the DSR is deflated for the **cumulative** trial count ever spent
in pursuit of an edge for that target, not just this run's `--trials`.
(SP-A, MERGED; Vector pilot §5.2.)

- [ ] The spec explicitly states that this run records its `--trials`
      spend to the cumulative ledger
      (`tpcore.lab.ledger.record_trial_spend` →
      `lab_trial_ledger.<target>` in `platform.data_quality_log`),
      **unconditionally at sample time**, and that the verdict's DSR is
      deflated against
      `tpcore.lab.ledger.cumulative_n_trials(<target>) + this_run_trials`
      — **not** the single run's `--trials` in isolation.
- [ ] The author **acknowledges cumulative (not per-run) DSR
      deflation**: every prior Lab run against this target makes this
      run's gate strictly harder (monotone-harder). A candidate that
      "would have passed at per-run n_trials" is **not** an argument for
      relaxing anything — the cumulative ledger is the structural defense
      and is never reset or bypassed.
- [ ] The spec states the **exact number of configurations** this
      candidate adds to the ledger (the primary + at most one
      pre-declared robustness check; a baseline/denominator
      re-measurement of the legacy path is **not** a third hypothesis
      and claims no edge of its own). (Vector pilot §5.2 — "exactly TWO
      configurations".)
- [ ] There is **no hidden grid**: weights, windows, fractions,
      long-only/long-short, the sector method, etc. are all code
      constants, never Lab-sampled — so the recorded trial count is the
      honest deflation N. (Vector pilot §5.2, H-VC-2.)

## 5. Roster-targeting prerequisite (post-SP-B)

The Lab targets engines by the roster SoT, not a hardwired list. The
candidate's `--target-engine` must be a legitimate, roster-derived Lab
target. (SP-B; `tpcore.engine_profile`.)

- [ ] The target engine appears in
      `tpcore.engine_profile.lab_targetable_engines()` — i.e. it is in
      `LifecycleState.{LAB,PAPER,LIVE}`, non-allocator, and is **not**
      the `lab` sentinel or `canary` (non-graduating by construction; a
      Lab graduation verdict against it is a category error that would
      still spend ledger budget).
      `python -c "from tpcore.engine_profile import lab_targetable_engines as f; print('<engine>' in f())"`
      prints `True`.
- [ ] If the target requires a per-engine Lab declaration that is not
      yet wired (e.g. Sentinel's non-Sharpe objective / its `LAB_TARGET`
      declaration is an SP-E deliverable), the candidate **STOPS here
      with the SP-pointing reason** — it does not hand-edit the roster
      or the Lab dispatch to force a target in. (Roster change is a SoT
      edit via the ECR, never Lab surgery — the SP-B / Sigma
      22-site-drift discipline.)
- [ ] The candidate adds **zero** changes to the Lab CLI, dispatch,
      `tpcore/lab/`, or any SoT/roster. The only Lab-side edit is the
      single `PARAM_RANGES` toggle (Section 2). (Vector pilot §4.3, §7
      "No new … CLI flags, no dispatch/daemon/SoT/roster changes".)

## 6. The gate is sacred — preserved or strengthened, never bypassed

- [ ] The candidate routes through `python -m ops.lab` →
      `_run_lab_core` → `survived` → dossier → ECR like **every** other
      candidate. The verdict is the deterministic
      `DSR ≥ 0.95 ∧ credibility ≥ 60 ∧ n_trades ≥ 3` floor
      (`ops/lab/run.py`). (Vector pilot §5.)
- [ ] **No clause is relaxed.** Every gate clause the spec states is
      **preserved or strengthened** (the pilot strengthens PBO
      0.50→0.20 and held-back trades 3→150). No
      `--credibility-threshold` / `--dsr-threshold` override **below**
      the gate is permitted in the run command. (Vector pilot §5,
      H-VC-4.)
- [ ] Any candidate-specific extra clause (PBO, family-variance,
      trade-count multiple, …) is **expressible on the existing
      `LabResult` dossier path** — verified by code inspection, with a
      pre-registered **fail-closed** rule for the degenerate case
      (skipped ≠ pass; red is red). If a clause needs a bespoke
      non-Sharpe metric path the Lab does not yet have, that is a SP-D
      pluggable-scoring prerequisite — STOP and route through SP-D, do
      not silently waive the clause. (Vector pilot §5.1 — the Sentinel
      contrast.)

## 7. Lab credibility namespacing (no live-gate poisoning)

- [ ] The candidate writes its experimental credibility under the
      `backtest_credibility.lab.<candidate>` namespace (the
      `_lab_credibility_engine_name` H-S2-3 mechanism — already correct,
      reused as-is). The spec's only obligation is to **NOT** introduce
      any code that writes the experimental score under the bare
      `<target>` key, so `graduation_ready(pool, "<target>")` can never
      read it. (Vector pilot §3.4, §7.)
- [ ] No new migration, no new table, no new SoT. The cumulative ledger
      and the credibility namespace both ride existing
      `platform.data_quality_log` substrate. (SP-A `ledger.py` module
      docstring; Vector pilot §7.)

## 8. Data prerequisites stated honestly

- [ ] Every data dependency the candidate consumes is listed with its
      **status + concrete evidence** (a live row/ticker count, not "it
      should be there"). (Vector pilot §6 prereq table.)
- [ ] Any genuine **BLOCKER** is stated precisely (the exact missing
      table/column, the `information_schema` query result) and resolved
      by a **single pre-registered conservative fallback** — not
      hand-waved, not a runtime choice, not a sweep. A controller note
      flags the blocker explicitly. (Vector pilot §6.1, H-VC-3 — the
      no-GICS-source resolution.)
- [ ] If the candidate adds a strictly-additive read (e.g. a new query
      in `load_<engine>_window_context`), it is **consumed only in the
      variant branch** so the legacy path is unaffected (Section 3 C1
      still green). (Vector pilot §3.2, §7, §11 T3.)

## 9. Lookahead / point-in-time honesty

- [ ] Every signal the variant scores uses **strictly point-in-time /
      backward** data windows. No row dated after `sim_date` ever enters
      a score. A unit test pins this. (Vector pilot §2.2, §6 H-VC-6 —
      the strictly-backward catalyst/insider windows; "the held-back DSR
      is therefore lookahead-honest".)
- [ ] Degenerate cross-sectional inputs (zero-variance columns, empty
      windows) have a pinned, unit-tested neutral guard — they cannot
      blow up a z-score or silently bias the verdict. (Vector pilot
      §2.3, H-VC-7.)
- [ ] Entry/exit mechanics, sizing, crash-guard, and the cost model are
      **unchanged** — the variant changes *which names are
      selected/scored*, not the trade machinery. The cost model is
      validated for the candidate's trade direction (e.g. no borrow
      model bolted on for a long-only fold). (Vector pilot §2.4, §2.5,
      §7.)

## 10. Compliance verifications (the `grep`-able set)

Mirrors `engine_readiness.md` §10: each item closes with a one-line
mechanical check, run **before launching the Lab run**.

- [ ] **Exactly one `PARAM_RANGES` toggle added.**
      `git diff` on `ops/lab/run.py` shows exactly one new key under
      `PARAM_RANGES["<engine>"]` and it is a `choice:` spec whose values
      are `{legacy_default, the_one_variant}`. No `--family-weights`
      menu. (Vector pilot §4.1, H-VC-2.)
- [ ] **Live path files untouched.**
      `git diff --name-only` contains **no**
      `<engine>/plugs/`, `<engine>/scheduler.py`,
      `<engine>/order_manager.py`, `scripts/run_all_engines.sh`,
      `ops/platform_pipeline.py`, `tpcore/lab/`, `ops/lab/__main__.py`,
      or any SoT/roster file. (Vector pilot §3.2, §10, §11 Tn.)
- [ ] **Characterization golden present + RED-first.** The
      byte-identical test file exists and its golden was committed
      before the variant code (Section 3). `grep` the test for the C1–C4
      assertions.
- [ ] **Roster target verified.** The
      `lab_targetable_engines()` one-liner (Section 5) prints `True` for
      `--target-engine`.
- [ ] **No gate override below the floor.** The intended
      `python -m ops.lab …` command carries **no**
      `--dsr-threshold`/`--credibility-threshold` below 0.95/60.
      (Vector pilot §5, H-VC-4.)
- [ ] **n_trials acknowledgement present.** The spec contains an
      explicit paragraph acknowledging cumulative (not per-run) DSR
      deflation and naming `tpcore.lab.ledger` (Section 4).
- [ ] **Single-hypothesis attestation.** The spec's self-review section
      states one primary hypothesis, the placeholder scan is empty, and
      every constant is pinned (Section 1). Mirrors the Vector pilot §12
      self-review block.
- [ ] **`ruff check .` clean** on any test the candidate adds; no
      `yfinance`, no Discord, no `print()` residue (same final-checks
      bar as `engine_readiness.md` §9).

---

## Where this checklist is enforced (the place in the flow)

`engine_readiness.md` is the **Engine SDLC ADD-path build gate**. This
checklist is its Lab-lane sibling: it gates a **candidate before the Lab
run**, upstream of the dossier and the ECR.

- A `fold_existing` candidate routes:
  **this checklist** → `python -m ops.lab --candidate … --intent
  fold_existing` → held-back DSR/credibility gate → dossier → ECR
  (`python -m ops.engine_sdlc`, `action: MODIFY`).
- A `promote_new` candidate routes:
  **this checklist** → `python -m ops.lab … --intent promote_new` →
  gate → dossier → ECR (`action: ADD`, `source: lab_candidate`) → **then
  `engine_readiness.md`** for the scaffolded engine. The Lab dossier's
  "Next step" block (`ops/lab/dossier.py::_next_step`) already names
  `engine_readiness` for `promote_new`; this checklist is the symmetric
  **pre-run** gate, so a candidate cannot bypass it on the way in the
  same way an engine cannot bypass `engine_readiness` on an ADD.
- Every future Lab candidate — including any #242 research-LLM-emitted
  `LabCandidate` — must pass this checklist before its run. The LLM
  proposes; the deterministic gate (cumulatively deflated, SP-A) and
  this readiness checklist dispose. The LLM cannot under-declare its
  trial spend (SP-A counts it) and cannot skip a readiness section.

## Sibling cross-reference

- `docs/superpowers/checklists/engine_readiness.md` — the Engine SDLC
  ADD-path build gate. This checklist is its Lab-lane sibling
  (candidate-before-run vs engine-before-merge).
- `docs/superpowers/checklists/engine_change_request.md` — the ECR; a
  SURVIVED candidate's dossier feeds an ECR `MODIFY` (fold_existing) or
  `ADD` (promote_new). Numeric gate evidence is re-verified by the
  planner from the dossier JSON sidecar — never trusted from text.
- `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md` §SP-C —
  the epic that mandates this checklist (after SP-A, independent of
  SP-B).
- `docs/superpowers/specs/2026-05-19-vector-composite-lab-candidate.md`
  — the reference worked example every section above cites. **In-flight:**
  on branch `lab-candidates-rollthrough` (commit `0a94414`) until it
  lands on `main`; see the "Pilot location caveat" callout near the top
  (same post-merge repoint follow-up applies).
- `tpcore/lab/ledger.py` — SP-A, the cumulative n_trials ledger this
  checklist's Section 4 mandates the author acknowledge.
- `docs/DEV_PIPELINE_STANDARD.md` / `docs/STYLE_GUIDE.md` — the
  gate-is-sacred, single-pre-registered-spec, never-relax-the-gate
  doctrine this checklist encodes as readiness items.
