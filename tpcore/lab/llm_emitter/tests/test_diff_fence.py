"""SP-G — diff-scope allow-list (the §4.4 build-time fence).

This is the structural enforcement of spec §2.6: the agent's draft PR
may touch ONLY the rendered spec + the JSON sidecar + a single engine
test stub. Any other path reds the build.

The deliberately-over-broad-diff trip test is the load-bearing safety
proof: a hand-crafted diff trying to slip ``tpcore/`` / ``ops/`` /
``.claude/`` / ``_PROFILE`` / ``providers.py`` / etc. into the PR is
rejected.
"""
from __future__ import annotations

import pytest

from tpcore.lab.llm_emitter.diff_fence import (
    FORBIDDEN_PATH_PREFIXES,
    DiffScopeViolation,
    enforce_diff_scope,
)


def _allowed(date: str = "2026-05-20") -> list[str]:
    """The exact three-slot allow-list for a sample emission."""
    return [
        f"docs/superpowers/specs/{date}-sample_candidate-lab-candidate.md",
        f"docs/lab/{date}-sample_candidate-emitted-spec.json",
        "sentinel/tests/test_lab_sample_candidate_byte_identical.py",
    ]


# ─── ALLOWED PATHS PASS ────────────────────────────────────────────────


def test_allowed_three_slots_pass() -> None:
    enforce_diff_scope(
        _allowed(),
        candidate="sample_candidate",
        target_engine="sentinel",
    )


def test_empty_diff_is_no_op() -> None:
    enforce_diff_scope(
        [], candidate="sample_candidate", target_engine="sentinel"
    )


def test_subset_of_allowed_paths_passes() -> None:
    """The fence doesn't require all three slots to be present — the
    agent might only write the spec + the sidecar if the test stub is
    deferred to operator hardening (§3 OPERATOR-DRAFT)."""
    enforce_diff_scope(
        _allowed()[:2],
        candidate="sample_candidate",
        target_engine="sentinel",
    )


def test_candidate_slug_with_hyphens_maps_to_underscores_for_test_stub() -> None:
    """Hyphenated candidate name renders to an underscore test file."""
    enforce_diff_scope(
        [
            "docs/superpowers/specs/2026-05-20-my-candidate-lab-candidate.md",
            "sentinel/tests/test_lab_my_candidate_byte_identical.py",
        ],
        candidate="my-candidate",
        target_engine="sentinel",
    )


# ─── STRUCTURAL FORBIDDEN PREFIXES RED THE BUILD ──────────────────────


@pytest.mark.parametrize("forbidden_prefix", FORBIDDEN_PATH_PREFIXES)
def test_every_forbidden_prefix_trips_the_fence(forbidden_prefix: str) -> None:
    """Each of the structural-fence prefixes (``tpcore/`` / ``ops/`` /
    ``platform/`` / ``.claude/`` / ``.github/`` / ``scripts/`` / etc.)
    is rejected outright."""
    paths = [
        *_allowed(),
        f"{forbidden_prefix}foo.py" if forbidden_prefix.endswith("/") else forbidden_prefix,
    ]
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            paths,
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_tpcore_engine_profile_edit_rejected() -> None:
    """The roster-mutation attack vector (spec §2.6 + the .claude/hooks/
    ECR gate)."""
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "tpcore/engine_profile.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_tpcore_providers_edit_rejected() -> None:
    """The data-feed roster (DFCR) attack vector."""
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "tpcore/providers.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_engine_backtest_edit_rejected() -> None:
    """An attempt to edit the target engine's backtest.py is rejected
    (only the test stub is allowed under the engine package)."""
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "sentinel/backtest.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_engine_scheduler_edit_rejected() -> None:
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "sentinel/scheduler.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_engine_plug_edit_rejected() -> None:
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "sentinel/plugs/lifecycle_analysis.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_other_engine_package_edit_rejected() -> None:
    """The agent targets ONE engine; touching a sibling engine is wrong."""
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "vector/tests/test_lab_sample_candidate_byte_identical.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_pyproject_edit_rejected() -> None:
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "pyproject.toml",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_claude_hooks_edit_rejected() -> None:
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                ".claude/hooks/gate-ecr-dfcr-edits.sh",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_alembic_migration_rejected() -> None:
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "platform/migrations/versions/0001_add_thing.py",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


def test_dotfile_at_root_rejected() -> None:
    """Spec §4.4 — touching root-level files outside the allow-list reds."""
    with pytest.raises(DiffScopeViolation):
        enforce_diff_scope(
            [
                *_allowed(),
                "README.md",
            ],
            candidate="sample_candidate",
            target_engine="sentinel",
        )


# ─── DELIBERATELY OVER-BROAD DIFF — the load-bearing safety proof ─────


def test_over_broad_diff_with_multiple_forbidden_paths_lists_all_violations() -> None:
    """The fence reports EVERY violating path (not just the first) so
    the operator audit log shows the full scope of the attempted
    breach."""
    over_broad = [
        *_allowed(),
        "tpcore/engine_profile.py",
        "tpcore/providers.py",
        "ops/lab/run.py",
        ".claude/hooks/gate-ecr-dfcr-edits.sh",
        "sentinel/backtest.py",
    ]
    with pytest.raises(DiffScopeViolation) as ei:
        enforce_diff_scope(
            over_broad,
            candidate="sample_candidate",
            target_engine="sentinel",
        )
    msg = str(ei.value)
    assert "tpcore/engine_profile.py" in msg
    assert "tpcore/providers.py" in msg
    assert "ops/lab/run.py" in msg
    assert ".claude/hooks/gate-ecr-dfcr-edits.sh" in msg
    assert "sentinel/backtest.py" in msg


def test_violating_paths_attribute_populated() -> None:
    """``DiffScopeViolation.violating_paths`` carries the structured
    tuple — useful for programmatic operator logging."""
    with pytest.raises(DiffScopeViolation) as ei:
        enforce_diff_scope(
            [*_allowed(), "tpcore/engine_profile.py"],
            candidate="sample_candidate",
            target_engine="sentinel",
        )
    assert ei.value.violating_paths == ("tpcore/engine_profile.py",)


def test_strips_whitespace_around_paths() -> None:
    """``git diff --name-only`` produces newlines; the splitter may leave
    whitespace. The fence trims and ignores empty entries."""
    enforce_diff_scope(
        ["  " + _allowed()[0] + "  ", "", "\t"],
        candidate="sample_candidate",
        target_engine="sentinel",
    )
