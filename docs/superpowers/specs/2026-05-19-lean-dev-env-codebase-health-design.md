# Lean Development Environment + Codebase Health — Design **v1 (expert-hardened, operator-approved 2026-05-19)**

**Status:** design **v1** 2026-05-19. Brainstorm → expert-harden →
operator-approved (this doc) → operator spec read → implementation plan
→ phased subagent build. **Spec 1 of 2** (the agents / cross-session
coordination design is the deferred spec 2, "design-now-adopt-deferred"
per the 2026-05-19 cross-session-coordination decision —
`[[project_cross_session_coordination]]`).

## 0. Problem — stated vs measured

Operator symptom: "development slows down because we have huge logs or
shit like that" + a codebase-health worry ("redundancies, one-off
scripts no longer used, python best-practice consistency, and whether
`tpcore`-as-canonical-shared-core has stayed consistent across the two
parallel Claude sessions").

**Measured reality (the stated symptom is not the bottleneck — say so
plainly):**

- `logs/` = **1.4 MB** — a rounding error. Log volume is NOT the
  problem.
- The full pytest suite = **1765 tests, collect 1.27 s, runs
  single-process serial ≈ 100–108 s** — no `pytest-xdist` installed;
  ~10 CPUs idle. The standing correctness gate also requires the
  order-flip runs (≈ 3 × 100 s per change). **This is the dominant
  dev-loop tax.**
- `data/` working tree = **1.4 GB** (`tradier_export` 1.0 G,
  `sec_backfill` 371 M, `*_archive/`), gitignored (only 4 data files
  tracked; `.git` 40 M) — does NOT bloat git/PRs, but every
  `grep`/`ruff`/IDE-index/`find`/agent tree-walk traverses it.
- 13 local / 11 remote branches, 3 worktrees (the worktrees create
  duplicate `conftest.py`/`check_imports.py` copies that pollute every
  grep).
- **Critical finding:** `tpcore/scripts/check_imports.py`
  (`FORBIDDEN_MODULES` / `ENGINE_PACKAGES`, run in CI `ci.yml`) ALREADY
  AST-enforces the tpcore layering invariant: engine→tpcore one-way, no
  engine→engine. The operator's worst fear is *already mechanically
  guarded at the import boundary*. The unsolved gap is **logic
  duplication that does not surface as an import** (copy-pasted /
  divergently-reimplemented helpers across sessions) — that needs a
  duplication/dead-code tool, NOT a redundant import-linter.

## 1. Verdict — speed the loop, hide the tree, add duplication/dead-code detection on top of the existing import guard

A "lean environment" here is **not** log pruning. It is: (1) a fast,
parallel-safe inner/CI test loop; (2) keeping the 1.4 GB `data/`
invisible to tool walks without moving it; (3) standing CI enforcement
for dead code, orphan one-off scripts, and Python-best-practice
consistency *layered on top of* the existing import guard; (4) a small,
bounded retention fix; and (5) a deferred, coordinated de-dup refactor.

Do **not** rebuild import-layering enforcement (already solved). Do
**not** relocate `data/` (live runtime path; the `TP_DATA_DIR` seam in
`tpcore/ingestion/csv_archive.py` is archive-root-only, not a full
relocation — out of scope, too invasive for a live path).

## 2. Invariants every phase must preserve

- **Live-money safety:** no phase alters runtime, determinism, or the
  live trade/data paths. All Phase 1–4 work is config / new CI checks /
  new test files only.
- **The serial whole-suite + order-flip is the AUTHORITATIVE gate.**
  Parallelism is an *accelerator*, never the sole gate (the `ops/`
  package-shadow `sys.modules` poisoning is by definition a
  single-process property; the streamlit "no `dashboard.py` import in
  CI" hazard also stands). See `[[feedback_ops_package_shadow_full_suite_gate]]`.
- **Cross-session non-stomp:** Phases 1–4 are read-only/additive
  (config + new CI checks + new test files in existing testpaths) →
  zero cross-session code-collision; safe while the engine session is
  live. Phase 5 is code-mutating/high-collision → deferred.
- **Allowlist-baselined gates:** every new health gate (dead code,
  orphan scripts) fails on *new* violations only; the existing tree
  passes day one.

## 3. Phased architecture

### Phase 1 — Fast test loop (additive config; safe now; ~75–85 % of the win)

- Add `pytest-xdist` to `[project.optional-dependencies].dev`
  (`pyproject.toml`) and the `ci.yml` install step.
- Run the common signal with `pytest -n auto --dist loadgroup -q`.
  Apply `pytestmark = pytest.mark.xdist_group("ops_shadow")` to every
  test module that loads `scripts/ops.py` via
  `importlib.util.spec_from_file_location` OR mutates
  `sys.modules["ops"]` (the ~6 known modules incl.
  `tests/test_llm_triage_service.py`, `tests/test_defect_register.py`,
  `tpcore/tests/test_handle_daily_bars_multi.py`,
  `tests/test_lab_isolation.py` — the implementation plan enumerates
  the full set by grep). Same group ⇒ same worker ⇒ deterministic
  intra-group order ⇒ the single-process ordering guarantees hold
  inside that worker; cross-module poisoning is per-worker-isolated
  (separate processes) so xdist is *safer* than serial here, not
  riskier.
- **The serial + order-flip pair is RETAINED as a separate,
  single-process CI step** (`pytest -p no:xdist -q` + the reversed
  variant) — the authoritative correctness gate. The parallel run is
  the fast accelerator for the common path.
- **Sentinel test** (manifest-discipline, mirrors
  `gen_engine_manifest.py`): assert the `xdist_group("ops_shadow")`
  membership set == the set of test modules referencing
  `sys.modules["ops"]` / `scripts/ops.py`, so a future poisoning test
  added without the group reds CI rather than going flaky.
- **Reject `pytest-testmon`**: its dependency graph is fooled by the
  dynamic `spec_from_file_location` import of `ops.py` → false-clean
  risk, unacceptable on a live-money repo.
- Expected: ~100 s → ~15–25 s wall for the common dev/CI run on ~10
  cores; order-flip pair unchanged. **Rollback:** drop `-n` (zero code
  change).

### Phase 2 — Hide the 1.4 GB tree (additive config; safe now)

- `ruff`: `extend-exclude = ["data", "logs", "*_archive",
  ".claude/worktrees"]`.
- `pytest`: `norecursedirs = ["data", "logs", ".claude",
  "*_archive"]`.
- Tracked repo-root `.ignore` (ripgrep/fd/editors honor it) listing
  `data/ logs/ *_archive/ .claude/worktrees/`.
- `data/` is NOT moved. Pure additive config to already-gitignored /
  excluded paths; cannot alter runtime. **Rollback:** revert config
  lines.

### Phase 3 — Standing codebase-health enforcement (additive CI; safe now)

- **(a) Dead code — `vulture`** (add to dev deps). Complements ruff
  F401/F841 (which only catch unused imports/locals) by catching
  unused functions/methods/classes/attributes. CI:
  `vulture <pkgs> --min-confidence 80 vulture_allowlist.py`. Seed the
  allowlist once (read-only) with today's intentional dark /
  feature-flagged code (canary non-graduation, LLM-triage fenced
  code). **Gate: fail on NEW dead code only** (allowlist = baseline).
- **(b) Orphan one-off scripts.** New
  `scripts/tests/test_no_orphan_scripts.py`: each `scripts/*.py` must
  be referenced by an `ops.py` stage, a `*.sh` wrapper, a daemon,
  `ci.yml`, or another test, else fail with the offender. Allowlist
  for deliberate standalone tools. Already-found candidate:
  `scripts/ingest_tradier_csv.py` (0 references) — the plan confirms
  before allowlisting/flagging. Structurally prevents re-accretion
  (CLAUDE.md bans one-off scripts).
- **(c) tpcore-canonical consistency.** Import-direction is ALREADY
  enforced (`check_imports.py`) — do NOT add a redundant import-linter
  layers contract (YAGNI). Deliverable: a **one-time READ-ONLY audit**
  — an AST-hash / `pylint --enable=duplicate-code` (report-only) pass
  across `reversion/ vector/ momentum/ sentinel/ canary/` vs `tpcore/`
  → a findings markdown, **no code mutation** (cross-session-safe). The
  actual de-dup refactor is Phase 5 (deferred).
- **(d) ruff ruleset (staged).** Current
  `select = ["E","F","I","B","UP"]`; 18 inert `# noqa` exist for
  *unselected* rules. Staged additions, each rolled out alone with an
  `--add-noqa` baseline so existing code does not explode:
  **DTZ first** (flake8-datetimez — mechanically enforces the
  prose-only CLAUDE.md "all timestamps UTC" mandate; live-money
  relevant), **SLF next** (mechanically enforces the prose-only
  "never access `._store`/`._pool`" mandate). `SIM`/`RUF`/`PTH` later;
  `PLR`/`ARG` deferred/rejected (noisy, low ROI here).

### Phase 4 — Retention hygiene (small; ~2 % of value, but in scope)

- `logs/` size cap: not urgent (1.4 MB); add only a guard if it ever
  grows.
- Real item: the new **retention-exempt `REVIEW_DEFECT_LOGGED` /
  `REVIEW_DEFECT_RESOLVED`** `application_log` rows are deliberately
  exempt from the 7-day `DBLogHandler` prune and can grow unbounded.
  Add a **bounded secondary cap** so "exempt" ≠ "infinite".
  **Decision D4 (operator-approved default):** retain a `REVIEW_DEFECT_*`
  row iff it is younger than **180 days** OR among the **most recent
  2000** such rows (the more-retentive union, not the intersection) —
  AND unconditionally retain it if it is an *open* (unresolved) defect
  the register's anti-join open-predicate depends on (never prune an
  open defect; mirror the DR2 retention-exemption invariant). Pruned by
  the same `DBLogHandler` path, not a new mechanism.

### Phase 5 — Actual de-dup / tpcore reconsolidation (HIGH collision — DEFERRED, then scheduled)

Code-mutating; touches the exact files both Claude sessions edit.
**Not started now.** Phase 3(c) produces the findings report; the
refactor is a separately-scheduled, single-session-owned task.
**Unblock trigger:** the operator will end the engine session when its
current work finishes (operator, 2026-05-19); at that point the data
lane is the sole session and Phase 5 — together with the long-gated
**#189** (dashboard refactor) and **#252** (full docs-to-reality
reconciliation) — becomes single-session-safe and schedulable. Phase 5
gets its own plan at that time.

## 4. Decisions (expert-recommended; operator-approved 2026-05-19)

- **D1** — parallel run = accelerator; serial + order-flip retained as
  the authoritative gate. **APPROVED (yes).**
- **D2** — add a redundant import-linter `independence` contract?
  **APPROVED (no)** — `check_imports.py` already covers direction;
  revisit only if Phase 3(c) finds engine→engine logic bleed.
- **D3** — ruff `DTZ` + `SLF` first, rest staged. **APPROVED (yes).**
- **D4** — `REVIEW_DEFECT_*` bounded cap. **APPROVED:** 180 days or
  last 2000 rows (whichever larger); never prune an open defect.

## 5. Scope boundary / fatal-objection self-check

**OUT:** moving `data/` out of the tree; rebuilding import-layering
enforcement; the Phase 5 refactor itself; any runtime/determinism/live-
path change; log infrastructure beyond a size guard; the agents /
cross-session-coordination design (spec 2).

**Fatal-objection check:**
- *Could xdist regress the CI gate?* Only if a `sys.modules["ops"]`
  test escapes the `xdist_group`. Mitigated by the membership sentinel
  test + retaining the serial order-flip pair as the real gate.
  Fallback ladder: `--dist loadscope` (coarser, safe) →
  xdist-local-only (still a large dev win, zero CI risk).
- *Could excludes hide a real test?* Audited: no test recurses an
  excluded dir; all fixtures live under `tests/` / `tmp_path`. The
  plan re-verifies.
- *Could vulture/orphan gates false-block the operator?* No —
  allowlist-baselined (fail on *new* only); existing tree passes day
  one; live path untouched.
- *Biggest residual risk:* Phase 5 (real value AND real danger);
  keeping it explicitly deferred + single-session-scheduled is the
  safety design, not a punt.

## 6. Phasing (gated PR per phase; subagent-driven; Phases 1–4 proceed now, Phase 5 deferred)

| Phase | Deliverable | Cross-session | When |
|---|---|---|---|
| **P1** | `pytest-xdist` + `loadgroup`/`xdist_group("ops_shadow")` + retained serial+order-flip authoritative gate + group-membership sentinel test. One gated PR. | additive — safe | now |
| **P2** | `ruff extend-exclude` + pytest `norecursedirs` + tracked `.ignore` (data/logs/_archive/worktrees). One gated PR. | additive — safe | now |
| **P3** | `vulture` dead-code CI gate (allowlist-baselined) + orphan-one-off-script test + read-only tpcore-duplication audit report + staged `ruff` DTZ then SLF (each its own gated PR with `--add-noqa` baseline). | additive — safe | now |
| **P4** | Bounded `REVIEW_DEFECT_*` retention cap (D4) preserving the open-defect anti-join invariant. One gated PR. | additive — safe | now |
| **P5** | Actual de-dup / tpcore reconsolidation from the P3(c) findings. Own plan. | code-mutating — HIGH | when engine session ends |

Every PR: fresh implementer → split spec/intent then code-quality
review → gated PR → CI authoritative via `gh pr checks` → whole-suite
single-process gate (the ops-shadow + streamlit hazards) → squash-merge
→ sync. Standard subagent-driven discipline.

**Design ready for the operator spec read.** It speeds the dev loop
~4–7×, hides the 1.4 GB tree from tool walks, adds standing
dead-code/orphan/duplication + best-practice enforcement layered on the
already-solved import guard, bounds the one unbounded retention class,
and explicitly defers the only dangerous (code-mutating) part to a
single-session window — preserving every live-money and cross-session
invariant.
