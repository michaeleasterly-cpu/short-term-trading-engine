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


# An unambiguously SP3-introduced path that is also inside the SP3
# allow-list (`ops/engine_sdlc/`). If this path is present in the
# AUTHORITATIVE INTEGRATION REF (`origin/main`, fallback local `main`)
# then SP3 (#81) is permanently merged into the integration target, the
# SP3-introduced surface is part of the trunk (not a net-new diff under
# PR review), and the "diff ⊆ SP3 allow-list" assertion is
# historical/N-A — the gate already did its one-shot SP3-PR-review job.
# This deliberately keys off the integration target itself, NOT the
# fork-point merge-base: a non-SP3 branch (e.g. a data/governor branch)
# whose merge-base predates SP3 would lack this path in its merge-base
# tree, so a merge-base-keyed predicate would WRONGLY fall through and
# false-RED the unrelated change set. NOT a blanket skip: on a genuine
# pre-merge SP3 PR the integration ref would NOT yet contain this path,
# the predicate is False, and the non-vacuous assertion below runs
# unchanged (the documented T9 backstop semantics).
_SP3_SIGNATURE_PATH = "ops/engine_sdlc/planner.py"


def _sp3_in_integration_target() -> bool:
    """True iff the SP3-signature path exists in the authoritative
    integration ref — ``origin/main`` (preferred; the ref CI's PR
    checkout actually has), falling back to a local ``main``. When True,
    SP3 (#81) is permanently merged into the trunk and this one-shot
    scope-confinement gate is historical/N-A, so it must skip on EVERY
    post-SP3 branch rather than false-RED unrelated workstreams. When
    NEITHER ref resolves (a bare checkout) return False — do not claim
    "merged" when we cannot tell; ``_resolve_sp3_base`` already
    ``pytest.skip``s the no-ref case downstream (no double-skip)."""
    for ref in ("origin/main", "main"):
        rev = subprocess.run(  # noqa: S603 — read-only ref resolution
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=REPO, capture_output=True, text=True
        )
        if rev.returncode != 0:
            continue
        return subprocess.run(  # noqa: S603 — read-only object existence probe
            ["git", "cat-file", "-e", f"{ref}:{_SP3_SIGNATURE_PATH}"],
            cwd=REPO, capture_output=True, text=True
        ).returncode == 0
    return False


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
    # `base` (the fork-point merge-base) is retained ONLY for the
    # genuine-pre-merge-SP3-PR diff below — the historical T9 backstop.
    # The skip decision keys off the authoritative integration ref
    # DIRECTLY, never the merge-base (that was the false-RED bug: a
    # non-SP3 branch's pre-SP3 merge-base lacks the SP3 signature path).
    base = _resolve_sp3_base()
    if _sp3_in_integration_target():
        pytest.skip(
            "SP3 (#81) is permanently merged into the integration "
            "target (origin/main); this one-shot scope-confinement "
            "gate is historical/N-A — it correctly gated SP3's own PR "
            "pre-merge; it must skip on every post-SP3 branch, not "
            "false-RED unrelated workstreams")
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


# ---------------------------------------------------------------------------
# Regression-pinning tests for the false-RED hotfix.
#
# THE BUG (origin/main 4f7b4f5): the skip predicate was
# ``_sp3_already_merged_into_base(base)`` where ``base`` is the
# fork-point ``git merge-base HEAD origin/main``. SP3 (#81) is
# permanently merged into origin/main, but a non-SP3 branch (e.g. the
# data/governor #251 B2 branch) whose merge-base predates SP3 has NO
# ``ops/engine_sdlc/planner.py`` in that pre-SP3 merge-base tree → the
# old predicate returned False → no skip → it diffed ALL of the
# unrelated governor/risk changes vs the pre-SP3 fork point → not ⊆ the
# SP3 allow-list → false-RED → whole suite red → merge queue blocked.
#
# THE FIX: the skip decision now consults the authoritative integration
# ref (origin/main) DIRECTLY via ``_sp3_in_integration_target()`` — it
# takes NO ``base`` arg, so it is structurally independent of the
# fork-point merge-base and cannot regress to the old false-RED path.
# ---------------------------------------------------------------------------


def test_skip_predicate_keys_on_integration_target_not_merge_base():
    """The new predicate consults the integration ref (origin/main),
    NOT the fork-point merge-base — proving the false-RED bug is fixed
    and the new predicate is non-vacuous on this checkout."""
    # 1. On this checkout origin/main genuinely contains the SP3
    #    signature path (SP3 #81 is permanently merged), so the new
    #    predicate is True → the scope gate correctly SKIPS. This is
    #    the realistic #251-B2-like / post-SP3 scenario.
    assert _sp3_in_integration_target() is True, (
        "origin/main must contain the SP3 signature path post-#81; "
        "the gate must skip on every post-SP3 branch")

    # 2. Structural non-regression: the new predicate takes NO `base`
    #    argument — it is impossible for it to key off the fork-point
    #    merge-base the way the old buggy predicate did. This is the
    #    root-cause guard: the false-RED came from feeding the
    #    merge-base tree (which a pre-SP3 non-SP3 branch lacks the SP3
    #    path in) to the skip decision.
    import inspect

    sig = inspect.signature(_sp3_in_integration_target)
    assert list(sig.parameters) == [], (
        "_sp3_in_integration_target must take no args — it must consult "
        "the integration ref directly, never a (merge-)base parameter")

    # 3. Non-vacuity / the OLD-bug condition is genuinely gone: simulate
    #    the exact scenario that false-RED'd. The old predicate fed a
    #    tree-ish that lacks planner.py (a pre-SP3 merge-base of a
    #    non-SP3 branch). The empty tree is a deterministic, read-only
    #    stand-in for "a tree without the SP3 surface".
    empty_tree = subprocess.run(  # noqa: S603 — read-only object probe
        ["git", "hash-object", "-t", "tree", "--stdin"],
        cwd=REPO, input="", capture_output=True, text=True
    ).stdout.strip()
    old_predicate_on_pre_sp3_tree = subprocess.run(  # noqa: S603 read-only
        ["git", "cat-file", "-e", f"{empty_tree}:{_SP3_SIGNATURE_PATH}"],
        cwd=REPO, capture_output=True, text=True
    ).returncode == 0
    # Old logic: a pre-SP3 (non-SP3-branch) base tree lacks the path →
    # the old `_sp3_already_merged_into_base` would be False → NO skip →
    # false-RED on the unrelated change set. New logic ignores any such
    # tree entirely and stays True (skip). This is the before→after.
    assert old_predicate_on_pre_sp3_tree is False, (
        "a tree without the SP3 surface must NOT satisfy the old "
        "merge-base-keyed probe — this is the false-RED condition the "
        "fix removes")
    assert _sp3_in_integration_target() is True, (
        "the new predicate must remain True (skip) regardless of any "
        "pre-SP3 merge-base tree — it consults origin/main, not the base")


def test_predicate_is_not_a_blanket_always_true_skip(monkeypatch):
    """Preserve the genuine-pre-merge-SP3-PR backstop semantics: the
    predicate returns False iff the resolved integration ref lacks the
    SP3 signature. Monkeypatch the signature path to a definitely-absent
    path → on a hypothetical pre-merge SP3 branch (origin/main WITHOUT
    planner.py) the predicate is False → the §8 scope assertion runs
    (the historical T9 behaviour), proving this is not a blanket skip."""
    import scripts.tests.test_sp3_scope_confined as mod

    monkeypatch.setattr(
        mod, "_SP3_SIGNATURE_PATH",
        "ops/engine_sdlc/__sp3_signature_definitely_absent__.py")
    assert mod._sp3_in_integration_target() is False, (
        "with a signature path absent from origin/main the predicate "
        "MUST be False so the non-vacuous §8 scope assertion runs — "
        "this is the documented genuine-pre-merge-SP3-PR T9 backstop")
