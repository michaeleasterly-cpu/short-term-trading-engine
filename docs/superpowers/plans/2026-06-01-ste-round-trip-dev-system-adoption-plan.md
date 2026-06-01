# STE round-trip adoption plan — packetvoid-dev-system (2026-06-01)

> **Phase: PLAN ONLY.** This document is the *only* artifact created or modified by this PR. No STE runtime code, configuration, rules, skills, hooks, agents, workflows, scripts, tests, or generated artifacts are changed. All adoption work is deferred to follow-up PRs sequenced in §S0–S7 below.

## Executive verdict

The Packet Void dev system (`michaeleasterly-cpu/packetvoid-dev-system` at `882e852`, public + branch-protected) is **portability-validated** but not yet **STE-adoption-ready by overwrite**. STE's existing `.claude/`, `.github/workflows/`, and `docs/` surfaces are the *source of truth* from which the portable templates were originally extracted, and STE has accumulated domain-specific extensions (engine SDLC, data-feed lifecycle, risk path, ops daemons, lab/weekly-digest skills) that the portable seeds do not — and should not — replace.

Adoption is therefore **additive-only and incremental**, never bulk-regenerate. The portable surface joins STE as a baseline-comparison gate (S2) and a profile declaration (S1); STE-specific overrides retain priority everywhere they exist.

The plan below classifies every STE artifact, identifies the conflicts that would arise from a naive `bootstrap_project.py --target-dir /Users/michael/short-term-trading-engine --force` (don't do this), and proposes a 7-stage adoption path that ends in a *controlled regenerate-on-demand* posture rather than a one-shot overwrite.

## Evidence from D1 / D1b / D2 / D2b

| Stage | Consumer | Profile | PR outcome | Fixes triggered in dev-system |
|---|---|---|---|---|
| D1 bootstrap | `packetvoid-d1-consumer-smoke` | `generic-python` | merged after 3 fix cycles | (PR #7) drop `cache: pip`; (PR #8) wrap pytest to swallow exit-5; (PR #9) ship portable `.gitleaks.toml` + Claude-review secret gate |
| D1b real-edit | `packetvoid-d1-consumer-smoke` | `generic-python` | merged first try | none |
| D2 bootstrap | `packetvoid-d2-railway-consumer-smoke` | `python-railway` | merged first try | none |
| D2b real-edit | `packetvoid-d2-railway-consumer-smoke` | `python-railway` | merged first try | none |

The dev system has therefore:
- proven its bootstrap pipeline against 2 profile shapes;
- proven the heavy-lane path filter fires correctly when a Railway-protected path is touched;
- proven the Claude-review secret gate no-ops gracefully without `ANTHROPIC_API_KEY`;
- proven the gitleaks workflow + `.gitleaks.toml` ship + scan correctly;
- proven the dogfooded `secret-scan.yml` runs on the dev-system repo itself under its own protect-main ruleset.

What remains untested by D1/D2 is **adoption onto a repo that already has a richer dev-system surface than the portable baseline** — exactly STE's shape. Hence the plan below.

## STE surface inventory

| Surface | STE count | Portable (fintech-research seed) count | Gap |
|---|---|---|---|
| `.claude/rules/*.md` | **12** | 2 | 10 STE-specific rules to preserve |
| `.claude/skills/*/SKILL.md` | **11** | 1 (`security-review`) | 10 STE-specific skills to preserve |
| `.claude/hooks/*.sh` | **5** | 3 | 2 STE-specific hooks + 1 hook rename divergence |
| `.claude/agents/*.md` | **6** | 2 | 4 STE-specific agents to preserve |
| `.github/workflows/*.yml` | **4** | 3 | 1 STE-specific workflow (`deploy-window.yml`) |
| `docs/*.md` (portable-shape) | 5 + **3 STE-only** | 5 | STE-specific: `glossary.md`, `MASTER_PLAN.md`, `STYLE_GUIDE.md` |
| `.gitleaks.toml` | **present** + `.gitleaksignore` | minimal portable seed | STE has historical-baseline allowlist; richer than portable |
| `PROJECT_PROFILE.yaml` | **absent** | required | S1 deliverable |
| `.claude/path_registry.yaml` | **present** (H0) | present (rendered from profile) | STE's is canonical; portable would have to mirror it |

**STE-only rules** (each is STE_EXTENSION, must remain): `daemons.md`, `dashboard.md`, `data-adapter.md`, `data-feed-roster.md`, `engine-build.md`, `engine-roster.md`, `migrations.md`, `risk-path.md`, `selfheal-auditheal.md`, `tests-and-ci.md`.

**STE-only skills** (STE_EXTENSION): `adapter-readiness/`, `audit-data-pipeline/`, `defect-register/`, `dfcr/`, `ecr/`, `engine-readiness/`, `lab-target-run/`, `run-data-ops/`, `supabase-postgres-best-practices/`, `weekly-digest/`.

**STE-only hooks**: `gate-ecr-dfcr-edits.sh`, `risk-path-reminder.sh` (both STE_EXTENSION, must remain).

**Hook rename divergence**: STE has `block-pytest-subset-when-ops.sh`; portable has `block-pytest-subset-when-critical.sh`. Same role, different scope-detection — STE's is hardcoded to `ops/`; portable's reads `critical_paths` from `PROJECT_PROFILE.yaml`. **DEFER**: rename + reparameterize is a separate operator decision; the OPS package-shadow lesson STE encodes is non-portable in shape, even if the role generalizes.

**STE-only agents** (STE_EXTENSION): `adapter-implementer.md`, `db-architect.md`, `engine-implementer.md`, `lab-target-runner.md`.

**STE-only workflows**: `deploy-window.yml` (STE_EXTENSION — Railway "Wait for CI" gate; emergency-disabled per operator directive 2026-05-26 but kept always-green for Railway compatibility).

## Generated fintech-research comparison tree summary

Rendered into `mktemp -d` with `--profile fintech-research`, 20 artifacts. Audit + check_manifests pass.

| Artifact | Status vs STE | Classification |
|---|---|---|
| `.claude/path_registry.yaml` | shape matches; STE registry is canonical SoT | **STE_OVERRIDE** — STE registry never gets overwritten; portable mirrors STE |
| `.claude/rules/heavy-lane.md` | STE 74 vs portable 62 lines | **STE_OVERRIDE** — STE adds ECR/DFCR / spec/audit pointers |
| `.claude/rules/security-guidance.md` | STE 58 vs portable 56 | **STE_OVERRIDE** — STE wraps C0.4 cascade with STE-specific incident refs |
| `.claude/skills/security-review/SKILL.md` | STE 146 vs portable 141 | **STE_OVERRIDE** — close to portable; STE has small extensions |
| `.claude/hooks/block-git-checkout.sh` | bytes differ | **STE_OVERRIDE** — STE's predates portable extraction (portable is a derivative); functional behavior matches |
| `.claude/hooks/session-start.sh` | structural parity, project-name divergence | **STE_OVERRIDE** — STE version is canonical; portable templated from it |
| `.claude/hooks/block-pytest-subset-when-*.sh` | name + scope divergence | **DEFER** — rename + reparameterize is operator decision (see §Conflicts) |
| `.claude/settings.json` | STE has worktree block + 2 additional hook matchers (Edit/Write/MultiEdit + PostToolUse) | **CONFLICT** — naive replacement would silently drop STE's ECR/DFCR + risk-path enforcement |
| `.github/workflows/secret-scan.yml` | STE missing `actions: read` + `continue-on-error: true` on SARIF upload (the D0g fixes) | **PARTIAL_MATCH** — adopt the D0g fixes into STE's version-specific copy |
| `.github/workflows/claude-review-heavy-lane.yml` | STE missing the D0g-era ANTHROPIC_API_KEY gate step | **PARTIAL_MATCH** — adopt the secret gate into STE's STE-pathed copy |
| `.github/workflows/ci.yml` | STE has full Postgres-service shape + STE-specific test paths | **STE_OVERRIDE** — STE CI is far more discriminating than portable; portable does not match STE's needs |
| `.github/workflows/deploy-window.yml` | STE-only | **STE_EXTENSION** |
| `.github/pull_request_template.md` | divergent path checklist (STE uses `tpcore/risk/**` etc; portable uses fintech `src/risk/**`) | **STE_OVERRIDE** — STE checklist matches STE registry |
| `.gitleaks.toml` | STE richer (historical baseline allowlist + `.gitleaksignore`) | **STE_OVERRIDE** — portable is minimal-starter; STE is full posture |
| `docs/DEV_PIPELINE_STANDARD.md` | STE 95 vs portable 82 lines | **STE_OVERRIDE** |
| `docs/MEMSTORE_HANDOFF.md` | STE 133 vs portable 85 lines | **STE_OVERRIDE** — STE has real memstore IDs + handoff history |
| `docs/MEMORY_MAINTENANCE.md` | STE 176 vs portable 71 lines | **STE_OVERRIDE** |
| `docs/SECURITY_GUIDANCE.md` | STE 218 vs portable 124 lines | **STE_OVERRIDE** |
| `docs/CLAUDE_SESSION_OBSERVABILITY.md` | STE 226 vs portable 117 lines | **STE_OVERRIDE** |
| `PROJECT_PROFILE.yaml` | absent in STE | **PORTABLE_MATCH** — S1 adds it, hand-authored to match STE registry |

## Conflicts and non-overwrite rules

The following would be **destroyed by `bootstrap_project --target-dir /Users/michael/short-term-trading-engine --force`**. None of these may be replaced as a side effect of adopting the dev system:

1. **`.claude/settings.json`** — STE wires 4 hooks across 3 matchers (`PreToolUse(Bash)`, `PreToolUse(Edit|Write|MultiEdit)`, `PostToolUse(Edit|Write|MultiEdit)`, `SessionStart`). The portable template wires 2 hooks across 2 matchers. Overwriting would silently *remove* `gate-ecr-dfcr-edits.sh` (the ECR/DFCR mutator block) and `risk-path-reminder.sh`, both of which guard live-money paths.
2. **`.claude/hooks/gate-ecr-dfcr-edits.sh`** — STE-specific block on edits to `tpcore/engine_profile.py` and `tpcore/providers.py` without an ECR/DFCR checklist. No portable counterpart; deletion would let any hand-edit slip past the SDLC gate.
3. **`.claude/hooks/risk-path-reminder.sh`** — STE-specific PostToolUse hook reminding the operator when a risk-path edit just landed. No portable counterpart.
4. **All 10 STE-specific rules** under `.claude/rules/` — deleting any would silently strip its path-scoped enforcement next session.
5. **All 10 STE-specific skills** — particularly `ecr/`, `dfcr/`, `audit-data-pipeline/`, `weekly-digest/`, `defect-register/`, `lab-target-run/` whose slash-commands are wired into operator workflows.
6. **All 4 STE-specific agents** — `engine-implementer`, `adapter-implementer`, `db-architect`, `lab-target-runner` are profile entry points the operator dispatches by name.
7. **`.github/workflows/deploy-window.yml`** — Railway "Wait for CI" gate; emergency-disabled but kept always-green to unblock Railway's CI dependency. Removing it would re-block Railway deploys.
8. **`.github/workflows/ci.yml`** — STE has full Postgres-service shape (`lab-isolation-db`), STE-specific test paths, alembic migration to `platform` schema. Portable `ci.yml` is generic Python and would silently downgrade STE's CI gate.
9. **`.claude/path_registry.yaml`** — STE registry is *canonical*; portable registry is *generated from a profile*. STE's must remain SoT.
10. **`.gitleaks.toml` + `.gitleaksignore`** — STE has the historical-baseline allowlist (3 confirmed-clean test fixtures from the 2026-05-21 public-repo audit). Portable is a minimal starter; overwriting would either red the next CI run (allowlist gone) or invite a future operator to add the wrong allowlist back.

**Therefore: there will never be a `bootstrap_project --target-dir <STE> --force` call. Period.** Any future adoption code path must be *selective copy from temp tree* with explicit allowlist of which artifacts to update.

## Memory boundary handling

STE's `MEMSTORE_HANDOFF.md` is the canonical record of:
- Two Anthropic API beta memstores in use (`memstore_01P5Di…` dev, `memstore_01MzLu…` finder).
- Anthropic-beta header pinning.
- Local memory ceiling (24 400 bytes) enforced by `tests/test_memory_index_size.py`.
- 4-tier boundary (`CLAUDE.md` → local `MEMORY.md` → API memstores → repo docs/tests/hooks).

The portable template renders an **empty-by-default** memstore section (`api_memstores_enabled: false`, blank IDs) appropriate for new consumers. **STE's actual state is `api_memstores_enabled: true`** with real IDs.

**Round-trip rules:**
- STE's S1 `PROJECT_PROFILE.yaml` will declare `api_memstores_enabled: true` and reference the existing memstore IDs by name (matching what's already in `docs/MEMSTORE_HANDOFF.md`). No new exposure; existing canonical location stays authoritative.
- STE memstore IDs **must not move** out of `docs/MEMSTORE_HANDOFF.md` and `.claude/memory/` into any other location. If S1 references them, it cites the canonical doc, never inlines the values.
- The dev-system audit (S2) must **not** write to any memstore, **not** call the Anthropic API, **not** read STE's API key. Read-only file comparison only.
- The C0.1 memory-boundary sentinel (`tests/test_memory_boundary_present.py`) and size sentinel (`tests/test_memory_index_size.py`) are STE-canonical and stay STE-owned. The portable template's `MEMSTORE_HANDOFF.md` and `MEMORY_MAINTENANCE.md` are derivatives of these; never the other way round.

## Cloud memstore handling

Three rules:
1. **Never overwrite STE's MEMSTORE_HANDOFF.md from the portable template.** STE's 133-line version contains real handoff history; portable's 85-line version is the empty-template shape.
2. **Never inline a memstore ID anywhere it doesn't already live.** S1 profile references by *purpose* (`dev_memstore_id` field), not raw value, unless operator explicitly authorizes inlining.
3. **Never call the Anthropic API or write to a memstore as part of adoption tooling.** `bootstrap_project.py` and `audit_project.py` are pure file-rendering / pure file-comparison. Same for whatever S2 wrapper STE adds.

## Workflow and branch-protection handling

STE workflows that must not change:
- **`ci.yml`** — full Postgres-service shape, `lab-isolation-db`, STE-specific paths, alembic migration to `platform` schema. The portable `ci.yml.template` is a generic-Python skeleton (no DB service, no migration step); replacing STE's CI with it would be a massive downgrade.
- **`deploy-window.yml`** — kept always-green for Railway "Wait for CI" compatibility per the 2026-05-26 emergency-operations directive. STE_EXTENSION; no portable counterpart.
- **`secret-scan.yml` STE-specific comments** (public-repo audit history, baseline reference). The D0g portability fixes (`actions: read`, `continue-on-error: true` on SARIF upload) **should** be adopted into STE's version, but the surrounding documentation comments stay STE-specific.
- **`claude-review-heavy-lane.yml` STE-specific path filter** — STE filter lists `tpcore/risk/**`, `tpcore/selfheal/**`, etc. The portable filter lists fintech-research-shape paths (`src/risk/**`, …). STE's must remain STE-pathed. The D2-era ANTHROPIC_API_KEY gate step **should** be back-ported.

STE branch protection: not changed by this plan. STE is currently public and the protect-main ruleset is the operator's existing settings. Adoption introduces no new required check.

## Proposed staged adoption PR sequence

> Each stage below is a *future* PR. This plan does not create any of them.

### S0 — Doc-only plan (this PR)

- Scope: this document only.
- Lane: default (docs-only path).
- No code/config changes.
- Verifies the existing STE check_manifests, ruff, gitleaks gates remain green with the doc added.

### S1 — `PROJECT_PROFILE.yaml` only

- **Single file:** `PROJECT_PROFILE.yaml` at repo root, hand-authored to match the existing STE state:
  - `project_name: short-term-trading-engine`
  - `language: python`
  - `deployment: railway`
  - `database: postgres`
  - `critical_paths:` mirror `.claude/path_registry.yaml` groups.heavy_lane (verbatim)
  - `claude_system_paths:` mirror groups.claude_system
  - `security_sensitive_paths:` mirror existing security-guidance surface
  - `memory_policy.api_memstores_enabled: true`
  - `memory_policy.local_memory_limit_bytes: 24400`
  - `memory_policy.dev_memstore_id` and `agent_memstore_id` reference `docs/MEMSTORE_HANDOFF.md` § for canonical values (no inlining)
  - `review_mode: claude-review-only`
- No regeneration. The profile is *declarative*; STE renders nothing from it in S1.
- Lane: default.
- Sentinel: a new STE test asserts the profile parses with the dev-system's `parse_yaml` and round-trips all 4 memstore-policy keys.

### S2 — Read-only audit integration

- New script `scripts/run_dev_system_audit.sh` wraps `python3 /Users/michael/packetvoid-dev-system/devsystem/scripts/audit_project.py --target-dir /Users/michael/short-term-trading-engine` (or a worktree path).
- Output: report-only drift list. Exit code never reds STE CI; the operator reads the report.
- No artifact overwrite. No write. Pure file comparison.
- Optional: extend with a `--report-mode` flag on the dev system that suppresses exit-code-1-on-drift and just prints findings.
- Lane: default.
- Acceptance: drift report enumerates every divergence between STE and the rendered `fintech-research` baseline, classified using §"Artifact-by-artifact classification" above (which the operator can stably check against).

### S3 — Adopt portable docs *where strictly additive*

- Only consider docs that already match the portable shape and where the portable version has content STE lacks:
  - None today. STE's 5 portable-shape docs are all longer/richer than portable versions.
- Net result: **S3 may be a no-op.** The plan deliberately schedules it so the audit can re-evaluate if a future portable change adds content worth adopting.
- Lane: default; doc-only.

### S4 — Adopt portable sentinel/test improvements

- Candidate: dev-system's `test_no_anthropic_api_surface.py` pattern (verify no API call surface in code-shaped files). STE already has `tests/test_no_anthropic_api_surface_in_*.py`; would need to confirm coverage parity.
- Candidate: dev-system's lockstep-alignment sentinel between rendered and source workflows (only relevant if STE adopts both copies — likely not).
- Net result: **most STE sentinels exceed portable baseline.** S4 may be additive-only for one or two specific gaps.
- Lane: default; tests-only.

### S5 — Reconcile workflows (most likely value)

- **Adopt into STE's `secret-scan.yml`:**
  - `permissions.actions: read` (D0g fix #1)
  - `continue-on-error: true` on the SARIF upload step (D0g fix #2)
  - Preserve all STE-specific comments and history references.
- **Adopt into STE's `claude-review-heavy-lane.yml`:**
  - The `Gate on ANTHROPIC_API_KEY presence` step (D2 fix). Avoids future crashes if the operator rotates / revokes the secret.
  - Preserve STE-specific paths, prompt text, and review wording.
- **Do not touch:**
  - `ci.yml` — STE-specific shape stays.
  - `deploy-window.yml` — STE_EXTENSION stays.
- Lane: default; workflow paths are heavy_lane / claude_system, so the §1 pipeline applies even though the change is small.
- Acceptance: STE's existing CI continues to pass; the existing secret-scan run continues to upload SARIF successfully; the existing Claude-review continues to fire on heavy-lane paths.

### S6 — Reconcile `.claude` settings/hooks/rules

- **No bulk overwrite of `.claude/settings.json`.** STE's 4-hook / 3-matcher config is canonical.
- **No removal of `gate-ecr-dfcr-edits.sh` or `risk-path-reminder.sh`.** STE_EXTENSION.
- **No removal of any of STE's 10 STE-specific rules or 10 STE-specific skills.** STE_EXTENSION.
- **Hook rename decision:** `block-pytest-subset-when-ops.sh` → `block-pytest-subset-when-critical.sh` is **deferred** to a separate operator decision. The portable version reads `critical_paths` from `PROJECT_PROFILE.yaml`; STE's hardcodes `ops/`. Until the operator confirms STE wants to switch from the OPS-shadow scope to a registry-driven scope, both names co-exist and the rename does not happen.
- **Possible additive change:** if S5 introduces D0g-style portability into STE's workflows, the corresponding sentinel test `tests/test_repo_hardening_present.py` may be ported to assert the same invariants on STE's copies.
- Lane: default for additive changes; heavy if any STE-specific hook is touched.

### S7 — Optional regenerate-on-demand (NOT a regenerate-and-overwrite)

- After S1–S6 prove drift is small and well-understood, evaluate adding a script:
  - `python3 /Users/michael/packetvoid-dev-system/devsystem/scripts/bootstrap_project.py --profile-file PROJECT_PROFILE.yaml --target-dir <tmp>` into a *temp directory*.
  - Then a **selective copy** step that only updates artifacts whose drift was classified `PORTABLE_MATCH` in the S2 audit report.
  - The selective-copy step has a per-file allowlist; STE_OVERRIDE / STE_EXTENSION / CONFLICT artifacts are never in the allowlist.
- This is the *only* mechanism by which STE files would ever be regenerated from the dev system. Even then, never against `/Users/michael/short-term-trading-engine` directly — always into a worktree, with a diff PR for operator review.
- Lane: heavy (any rendering script touches `claude_system` paths).

## Rollback plan

- **S0 (this PR):** revert by `git revert <commit>` — single file deletion, no functional impact.
- **S1:** revert the PROJECT_PROFILE.yaml addition. No STE runtime references it yet.
- **S2:** revert the wrapper script and any sentinel test. The dev-system repo is untouched.
- **S3:** revert the targeted doc updates. STE docs were pre-S3 source of truth.
- **S4:** revert the sentinel additions. STE existing sentinels were not modified.
- **S5:** revert the two-line `secret-scan.yml` change and the gate-step addition to `claude-review-heavy-lane.yml`. STE workflows return to their pre-D0g state. (Note: pre-D0g state has the SARIF upload failure mode if code-scanning becomes disabled; the operator accepts that risk on rollback.)
- **S6:** revert the additive `tests/test_repo_hardening_present.py` port if any. STE's existing settings.json / hooks / rules are untouched and never were modified.
- **S7:** revert the temp-render + selective-copy script. Nothing in STE's tree was overwritten because the selective copy is allowlist-gated by definition.

At no stage does STE depend on the dev system being present on disk. The dev system is a *comparison anchor* and *baseline source*, not a runtime dependency.

## Acceptance criteria (this PR)

- [x] Plan doc created at `docs/superpowers/plans/2026-06-01-ste-round-trip-dev-system-adoption-plan.md`.
- [x] No other STE file modified.
- [x] Plan classifies every STE artifact in the portable-shape surface.
- [x] Plan identifies conflicts before adoption (§Conflicts and non-overwrite rules).
- [x] Plan preserves STE-specific rules/skills/hooks/agents/workflows.
- [x] Plan preserves cloud-memory boundary (§Memory boundary handling, §Cloud memstore handling).
- [x] Plan explicitly forbids `bootstrap_project --target-dir <STE> --force` (§Conflicts).
- [ ] STE local gates pass: `python scripts/check_manifests.py` exit 0; `python -m ruff check .` clean; `gitleaks detect --config .gitleaks.toml --no-banner --redact --source .` clean.

## Explicitly out of scope

- Any code or configuration change other than this plan doc.
- Any modification to `.claude/`, `.github/`, `tpcore/`, `ops/`, `platform/`, engine packages, `scripts/`, `tests/` (other than docs/superpowers/plans/).
- Any memstore write, Anthropic API call, MCP addition, Docker invocation, Railway deployment, or admin merge.
- Any decision on whether to adopt the portable hook-rename (`when-ops` → `when-critical`) — explicitly DEFER.
- Any decision on whether STE switches its `.claude/path_registry.yaml` shape — the STE registry stays canonical.
- Any decision on whether to add branch protection on the STE repo if not already present — out of scope here.
- Any decision on STE Claude-review GitHub action ANTHROPIC_API_KEY rotation policy — separate from this plan.

## NEEDS_OPERATOR_DECISION (for follow-up PRs)

1. **Hook rename:** keep `block-pytest-subset-when-ops.sh` (OPS-specific scope, encoding the package-shadow lesson) vs. rename to `block-pytest-subset-when-critical.sh` (registry-driven scope, matches portable). Default in this plan: **DEFER, keep current name**.
2. **S3 doc adoption scope:** which portable doc additions, if any, are net-additive to STE's richer existing versions? Default: **likely no-op, evaluate during S2 audit run**.
3. **S5 secret-scan + claude-review back-port scope:** adopt all 3 D0g/D2 fixes verbatim? Or selectively? Default: **adopt all 3, preserve STE-specific comments**.
4. **S7 selective-copy allowlist:** which artifact classes are eligible for ever being regenerated from the dev system? Default in this plan: **none until S2 audit produces evidence**.

---

## S2 audit override acceptance — 2026-06-01

> **Status: ACCEPTED.** This addendum closes the S2 audit loop. The S2 wrapper is operating correctly. `check_manifests.py --target-dir` is **CLEAN**. `audit_project.py --target-dir` reports intentional **STE_OVERRIDE** drift because STE has richer / domain-specific artifacts than the portable baseline. These findings are advisory and **do not authorize `bootstrap_project.py --force`, blind overwrite, or regeneration over the STE working tree** — the §"Conflicts and non-overwrite rules" section still governs.

S2 work completed since the plan first landed:

| Stage | Status | Reference |
|---|---|---|
| S0 plan doc | merged | this file (PR #416) |
| S1 PROJECT_PROFILE.yaml + alignment sentinel | merged | PR #417 |
| S2 read-only audit wrapper | merged | PR #418 |
| S5 workflow portability back-ports | merged | PR #419 |
| PR-template `ops/engine_sdlc/**` checkbox fix | merged | PR #420 |
| Dev-system pointer-only memstore semantics | merged | `packetvoid-dev-system` PR #10 |

After the dev-system PR #10 fix, `audit_project` no longer hard-errors on STE's pointer-only `PROJECT_PROFILE.yaml` and now successfully proceeds to file comparison. The drift it reports is precisely the §"Conflicts and non-overwrite rules" surface enumerated in this plan; it is not a defect.

### Current audit_project findings (20)

Each row is a finding from `scripts/run_dev_system_audit.sh` stage 1 (`audit_project.py --target-dir <STE>`), captured 2026-06-01 against `main` at `9cb7ed1`. Classification re-confirms the §"Artifact-by-artifact classification" table above.

| # | Audit finding | Classification | Disposition |
|---|---|---|---|
| 1 | `docs/CLAUDE_SESSION_OBSERVABILITY.md` differs from re-rendered template | **STE_OVERRIDE** | STE 226 lines vs portable 117; STE adds session-cost + redaction history. Keep STE. |
| 2 | `docs/DEV_PIPELINE_STANDARD.md` differs from re-rendered template | **STE_OVERRIDE** | STE 95 lines vs portable 82; STE encodes whole-suite + order-flip discipline + ops-package-shadow lesson. Keep STE. |
| 3 | `docs/MEMORY_MAINTENANCE.md` differs from re-rendered template | **STE_OVERRIDE** | STE 176 lines vs portable 71; STE encodes the operator-side audit procedure. Keep STE. |
| 4 | `docs/MEMSTORE_HANDOFF.md` differs from re-rendered template | **STE_OVERRIDE** | STE is the canonical memstore-ID location for the cloud-memory boundary. Per §"Cloud memstore handling" rule 1: never overwritten. Keep STE. |
| 5 | `docs/SECURITY_GUIDANCE.md` differs from re-rendered template | **STE_OVERRIDE** | STE 218 lines vs portable 124; STE wraps the C0.4 cascade with STE-specific incident refs. Keep STE. |
| 6 | `.claude/path_registry.yaml` differs from re-rendered template | **STE_OVERRIDE** | STE registry is canonical SoT; portable would have to mirror STE, not the other way round. Keep STE. |
| 7 | `.claude/settings.json` differs from re-rendered template | **CONFLICT → resolved STE_EXTENSION wins** | STE wires 4 hooks across 3 matchers (`PreToolUse(Bash)`, `PreToolUse(Edit│Write│MultiEdit)`, `PostToolUse(Edit│Write│MultiEdit)`, `SessionStart`). Portable wires 2 hooks across 2 matchers. Overwriting would drop ECR/DFCR + risk-path enforcement. Keep STE. |
| 8 | `.claude/rules/heavy-lane.md` differs | **STE_OVERRIDE** | STE 74 lines vs portable 62; STE adds ECR/DFCR + spec/audit pointers. Keep STE. |
| 9 | `.claude/rules/security-guidance.md` differs | **STE_OVERRIDE** | STE 58 vs portable 56; STE wraps the C0.4 cascade with STE-specific spec/audit refs. Keep STE. |
| 10 | `.claude/skills/security-review/SKILL.md` differs | **STE_OVERRIDE** | STE 146 vs portable 141; STE has small extensions tied to STE-specific path globs. Keep STE. |
| 11 | `.claude/hooks/block-git-checkout.sh` differs | **STE_OVERRIDE** | STE is the original from which the portable hook was derived; STE has the canonical implementation. Keep STE. |
| 12 | missing `.claude/hooks/block-pytest-subset-when-critical.sh` | **DEFER (hook rename)** | STE has `block-pytest-subset-when-ops.sh`; portable has `…-when-critical.sh`. The OPS-shadow lesson STE encodes is non-portable in shape. Per §S6 + §"NEEDS_OPERATOR_DECISION #1": keep STE name. |
| 13 | `.claude/hooks/session-start.sh` differs | **STE_OVERRIDE** | STE version is canonical; portable was templated from STE. Keep STE. |
| 14 | `.claude/agents/code-quality-reviewer.md` differs | **STE_OVERRIDE** | STE includes STE-specific quality-check checklist items (tpcore-private access, classify_exit_reason, FilterDiagnostics). Keep STE. |
| 15 | `.claude/agents/spec-reviewer.md` differs | **STE_OVERRIDE** | STE includes STE-specific spec-review checklist items (engine SDLC, ECR/DFCR). Keep STE. |
| 16 | `.github/workflows/secret-scan.yml` differs | **STE_OVERRIDE** (S5 back-port + STE comments) | STE has the D0g `actions: read` + `continue-on-error` fixes (PR #419) plus STE's public-repo audit history comments. Portable lacks the comments; STE keeps them. Keep STE. |
| 17 | `.github/workflows/ci.yml` differs | **STE_OVERRIDE** | STE has full Postgres-service shape (`lab-isolation-db`), alembic migration to `platform` schema, STE-specific test paths. Portable `ci.yml` is generic Python — replacing STE's CI with it would be a massive downgrade. Keep STE. |
| 18 | `.github/workflows/claude-review-heavy-lane.yml` differs | **STE_OVERRIDE** (S5 back-port + STE path filter) | STE has the D2 `ANTHROPIC_API_KEY` gate step (PR #419) plus STE-specific path filter (`tpcore/risk/**`, …) and STE-specific review prompt wording. Keep STE. |
| 19 | `.github/pull_request_template.md` differs | **STE_OVERRIDE** | STE's checklist mirrors STE's `path_registry.yaml` (`tpcore/risk/**`, `ops/engine_service.py`, etc.) — portable uses fintech-shape paths (`src/risk/**`). The PR-template `ops/engine_sdlc/**` checkbox fix (PR #420) closed the only check_manifests-side gap; the remaining diff is STE's canonical path-list shape. Keep STE. |
| 20 | `.gitleaks.toml` differs | **STE_OVERRIDE** | STE is the full posture (historical-baseline allowlist + `.gitleaksignore` for the 3 confirmed-clean test fixtures from the 2026-05-21 public-repo audit). Portable is a minimal starter. Keep STE. |

### S3 disposition

§"Proposed staged adoption PR sequence" S3 (adopt portable docs where strictly additive) is **NO-OP per current audit evidence.** All 5 portable-shape STE docs are richer than their portable counterparts (findings #1–#5); there is no additive doc improvement to port back. S3 stays scheduled in the plan as a future re-check trigger but does not require a PR today.

### S6 disposition

§"Proposed staged adoption PR sequence" S6 (`.claude` reconciliation) is **scoped to additive-only** going forward. The 10 `.claude/`-surface drift findings above (#6–#15) are all STE_OVERRIDE or DEFER. None warrants a bulk patch.

### S7 disposition

§"Optional regenerate-on-demand" S7 selective-copy allowlist remains **empty.** S2's evidence shows that every PORTABLE_MATCH-shaped artifact actually drifts as STE_OVERRIDE in practice. There is no allowlist to write yet.

### Operating expectation going forward

- `scripts/run_dev_system_audit.sh` is expected to return `REPORT_ONLY: DRIFT_DETECTED` for stage 1 (`audit_project`) and `exit 0` overall, indefinitely. That is the correct steady state given STE's canonical / richer / domain-specific posture.
- `check_manifests --target-dir` is expected to return **CLEAN** going forward. Any future stage-2 red is a real defect (registry / PR-template / settings-json drift, etc.) worth a fix PR.
- A new audit_project finding beyond the 20 above is the signal worth investigating — re-triage that single delta against the §"Artifact-by-artifact classification" table.

### Forbidden actions (re-asserted)

- No `bootstrap_project.py --target-dir /Users/michael/short-term-trading-engine --force`.
- No blind overwrite of any `.claude/` file, any `.github/workflows/*.yml`, any STE doc.
- No memstore write; no Anthropic API call as part of adoption tooling.
- No `--admin` to bypass branch protection.

---

> Status: PLAN ONLY. Adoption is staged across S1–S7 in follow-up PRs, with operator authorization at each stage. Cross-references: packetvoid-dev-system PRs #1–#10; consumer validation PRs at `packetvoid-d1-consumer-smoke` #1 #2 and `packetvoid-d2-railway-consumer-smoke` #1 #2; STE PRs #416 #417 #418 #419 #420.
