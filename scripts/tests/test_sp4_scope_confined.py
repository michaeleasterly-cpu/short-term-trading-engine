"""Tn — SP4 change-set scope confinement (H-S4-12). The SP4 diff
against the SP4 base must be confined to the SP4 allowlist; NO
data-lane file (the 8 owned + the data-SDLC spec/checklist/registry).
Read-only `git diff --name-only` against a SNAPSHOT of names only (no
git mutation), never against a synthetic repo — the canonical scope
proof, mirroring scripts/tests/test_sp3_scope_confined.py."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# NOTE (H-S4-9): this gate imports ONLY stdlib + pytest and never
# ``import ops.*`` (it shells read-only ``git`` exactly like its SP3
# sibling scripts/tests/test_sp3_scope_confined.py). Per H-S4-9 the
# scripts/ops.py-vs-ops/ collision-eviction stanza is carried ONLY by a
# test that actually resolves ``ops.*``; this file does not, so — exactly
# mirroring test_sp3_scope_confined.py's import shape — the stanza is
# (correctly) absent. Adding it here would needlessly mutate global
# ``sys.modules`` at collection time and perturb the locked SP2 oracle
# (the §13.2(d) _FakePool collection-order fragility); omitting it keeps
# the full suite at the canonical 1547+Tn green.
REPO = Path(__file__).resolve().parents[2]


def _resolve_sp4_base() -> str:
    """Prefer origin/main (the ref CI's PR checkout actually has); fall
    back to a local main; skip (not fail) if neither resolves — the
    scope gate must never false-RED on a checkout lacking the base ref
    (the SP3 cef7368 CI-portability lesson)."""
    for ref in ("origin/main", "main"):
        rev = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=REPO, capture_output=True, text=True)
        if rev.returncode != 0:
            continue
        mb = subprocess.run(  # noqa: S603
            ["git", "merge-base", "HEAD", ref],
            cwd=REPO, capture_output=True, text=True)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    pytest.skip("no SP4 base ref (origin/main / main) in this checkout")


# An unambiguously SP4-introduced path that is also inside the SP4
# allowlist (`scripts/gen_engine_manifest.py` — the SP4 shadow-manifest
# generator, net-new in T1, ABSENT from origin/main and from every prior
# sub-project). NOTE the deliberate choice: `ops/engine_sdlc/planner.py`
# would be WRONG here — it is an SP3 deliverable that already exists in
# the base, so it cannot distinguish "SP4 merged" from "SP3 merged". If
# this signature path is present in the RESOLVED BASE TREE then SP4 has
# already merged into the base, the SP4-introduced surface is IN the base
# (not in `git diff base...HEAD`), and the "diff ⊆ SP4 allowlist"
# assertion is moot/historical — the gate already did its one-shot
# PR-review job. This guard is baked in FROM INCEPTION (not a post-merge
# retrofit, the SP3 54cb362 lesson): on a real un-merged SP4 branch this
# path is ABSENT from the base, the predicate is False, and the
# non-vacuous assertion below runs unchanged.
_SP4_SIGNATURE_PATH = "scripts/gen_engine_manifest.py"


def _sp4_already_merged_into_base(base: str) -> bool:
    """True iff the SP4-signature path exists in the resolved base tree —
    i.e. SP4 is already merged into the base, so the scope-confinement
    gate is historical (it correctly passed when SP4 was the
    branch-under-review; it must NOT false-RED on every post-SP4 branch
    whose diff vs the now-SP4-inclusive base is unrelated surface)."""
    return subprocess.run(  # noqa: S603 — read-only object existence probe
        ["git", "cat-file", "-e", f"{base}:{_SP4_SIGNATURE_PATH}"],
        cwd=REPO, capture_output=True, text=True
    ).returncode == 0


# The 8 data-lane-owned files + the data-SDLC spec/checklist/registry
# SP4 must NEVER touch (spec §13.1, H-S4-12).
_FORBIDDEN_PREFIXES = (
    "tpcore/calendar.py",
    "tpcore/risk/",
    "tpcore/risk/governor.py",
    "ops/engine_supervisor.py",
    "ops/engine_service.py",
    "ops/engine_ladder.py",
    "tpcore/supervisor_state.py",
    "tpcore/trade_monitor.py",
    "tpcore/providers.py",
    "tpcore/feeds/",
    "tpcore/selfheal/",
    "docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md",
    "docs/superpowers/checklists/data_feed_change_request.md",
)

# The SP4 net-new surface + the enumerated in-place modifies (allowlist).
# Grounded against the REAL full SP4 surface (`git diff --name-only
# origin/main...HEAD`), reconciled to the spec §13.1 / H-S4-12 intent:
#  - `scripts/tests/test_sp3_scope_confined.py` — legitimately modified
#    by SP4 commit 54cb362 (the SP3-scope-gate skip-when-merged fix); it
#    is SP3-net-new surface that SP4 owns the post-merge correction of,
#    NOT an SP4/data-lane breach (mirrors SP3-T9's spec-doc
#    reconciliation: the real surface is the authority, aligned to spec
#    intent + noted here).
#  - `.gitignore` — Tn carry-forward #2: the SP2 Lab dossier writer
#    emits `docs/lab/{day}-{cand}-{verdict}-seed{n}.{md,json}` run
#    artifacts; gitignoring that pattern is engine-lane-safe run-artifact
#    hygiene, explicit SP4 surface.
_ALLOWED_PREFIXES = (
    "scripts/gen_engine_manifest.py",
    "scripts/tests/test_gen_engine_manifest_render.py",
    "scripts/tests/test_engine_manifest_in_sync.py",
    "scripts/tests/test_sdlc_docs_match_code.py",
    "scripts/tests/test_sp4_scope_confined.py",
    "scripts/tests/test_sp3_scope_confined.py",
    "CLAUDE.md",
    "docs/OPERATIONS.md",
    "docs/superpowers/checklists/engine_readiness.md",
    "docs/glossary.md",
    "docs/superpowers/specs/2026-05-18-engine-sdlc-design.md",
    "docs/superpowers/plans/2026-05-18-engine-sdlc.md",
    "scripts/run_smoke_test.sh",
    "scripts/run_all_engines.sh",
    "ops/platform_pipeline.py",
    "pyproject.toml",
    "tpcore/tests/test_engine_lifecycle_consistency.py",
    "ops/engine_sdlc/planner.py",
    "tpcore/tests/test_engine_sdlc_planner.py",
    ".gitignore",
)


def test_sp4_change_set_confined_to_net_new_surface():
    base = _resolve_sp4_base()
    if _sp4_already_merged_into_base(base):
        pytest.skip(
            "SP4 merged into base; scope-confinement gate is "
            "historical/N-A (one-shot PR-review gate — it correctly "
            "passed on the SP4 branch-under-review; post-merge the "
            "SP4 surface is in the base, not the diff)")
    names = subprocess.run(  # noqa: S603 — read-only name-only diff
        ["git", "diff", "--name-only", base, "HEAD"],
        cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for n in names:
        assert not n.startswith(_FORBIDDEN_PREFIXES), (
            f"SP4 touched a forbidden data-lane / data-SDLC file: {n}")
        assert n.startswith(_ALLOWED_PREFIXES), (
            f"SP4 touched a file outside the allowlist: {n} "
            f"(if intentional, the spec scope is wrong — escalate, do "
            f"not widen the allowlist silently)")
