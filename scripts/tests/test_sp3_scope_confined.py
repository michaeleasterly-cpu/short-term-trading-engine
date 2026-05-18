"""T9 — SP3 change-set scope confinement (H-S3-10c). The SP3 diff
against the SP3 base must be confined to the spec §8 net-new surface +
the enumerated in-place extends: NO CLAUDE.md / OPERATIONS.md /
glossary.md (SP4 doc-closure boundary), NO data-lane SoT
(tpcore/providers.py, tpcore/feeds/, tpcore/selfheal/). This test runs
git against a SNAPSHOT of names only (no git mutation, read-only
`git diff --name-only`), never against a synthetic repo — it asserts
the working change set, the canonical T9 scope proof."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _resolve_sp3_base() -> str:
    """Resolve the SP3 merge-base. Prefer ``origin/main`` (the ref CI's
    PR checkout actually has — a bare local ``main`` does NOT exist in a
    `actions/checkout` PR clone, only the PR HEAD + remote-tracking
    refs). Fall back to a local ``main`` for a plain dev clone. Skip
    (not fail) if neither base ref resolves — the scope gate must never
    emit a false RED on a checkout that lacks the base ref entirely."""
    for ref in ("origin/main", "main"):
        rev = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=REPO, capture_output=True, text=True
        )
        if rev.returncode != 0:
            continue
        mb = subprocess.run(  # noqa: S603
            ["git", "merge-base", "HEAD", ref],
            cwd=REPO, capture_output=True, text=True
        )
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    pytest.skip("no SP3 base ref (origin/main / main) in this checkout")

# SP4 / data-lane files SP3 must NEVER touch (spec §1.1, H-S3-10c).
_FORBIDDEN_PREFIXES = (
    "CLAUDE.md",
    "OPERATIONS.md",
    "docs/glossary.md",
    "tpcore/providers.py",
    "tpcore/feeds/",
    "tpcore/selfheal/",
)

# The spec §8 net-new surface + enumerated in-place extends SP3 may add
# /modify (prefix allow-list).
_ALLOWED_PREFIXES = (
    "ops/engine_sdlc/",
    "docs/superpowers/checklists/engine_change_request.md",
    "docs/superpowers/plans/2026-05-18-engine-change-request.md",
    # SP3 design spec — net-new SP3 surface (spec §8 reused-vs-new ledger
    # lists the SP3 spec/plan/checklist docs as net-new SP3 deliverables;
    # the plan allow-list named the plan+checklist but the sibling spec
    # doc is equally net-new SP3 scope, NOT an SP4/data-lane file).
    "docs/superpowers/specs/2026-05-18-engine-change-request-design.md",
    "tpcore/templates/eulogy_template.md",
    "reversion/backtest.py",
    "vector/backtest.py",
    "momentum/backtest.py",
    "ops/lab/run.py",
    "ops/lab/dossier.py",
    "tpcore/lab/context.py",
    "tpcore/tests/test_engine_lifecycle_consistency.py",
    "tpcore/tests/test_ecr_parse.py",
    "tpcore/tests/test_engine_default_params_parity.py",
    "tpcore/tests/test_lab_credibility_pool_threaded.py",
    "tpcore/tests/test_lab_dossier_sidecar.py",
    "tpcore/tests/test_engine_sdlc_planner.py",
    "tpcore/tests/test_engine_sdlc_cli.py",
    "scripts/tests/test_sp3_scope_confined.py",
)


def test_sp3_change_set_confined_to_net_new_surface():
    base = _resolve_sp3_base()
    names = subprocess.run(  # noqa: S603 — read-only name-only diff
        ["git", "diff", "--name-only", base, "HEAD"],
        cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for n in names:
        assert not n.startswith(_FORBIDDEN_PREFIXES), (
            f"SP3 touched a forbidden SP4/data-lane file: {n}")
        assert n.startswith(_ALLOWED_PREFIXES), (
            f"SP3 touched a file outside the §8 net-new surface: {n} "
            f"(if this is intentional, the spec scope is wrong — escalate)")
