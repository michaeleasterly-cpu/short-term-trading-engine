"""DFCR planner integration tests — verify classify/validate/apply
round-trips work against a copytree-staged repo. Mirrors
``tpcore/tests/test_engine_sdlc_planner.py`` discipline: never touches
the real repo; every test uses ``tmp_path`` and copies the real
``tpcore/providers.py`` + ``tpcore/feeds/profile.py`` into the stage."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ops.data_feed_sdlc.dfcr import parse_dfcr
from ops.data_feed_sdlc.planner import (
    ApprovalClass,
    _read_bindings_snapshot,
    apply,
    classify,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage_repo(tmp_path: Path) -> Path:
    """Copy the real providers.py + feeds/profile.py into a tmp staged
    tpcore tree. Stages just the two SoT files (NOT the whole repo —
    the planner only reads those two for read-only validation and only
    writes those two on apply)."""
    staged = tmp_path / "stage"
    (staged / "tpcore" / "feeds").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "tpcore" / "providers.py",
                staged / "tpcore" / "providers.py")
    shutil.copy(REPO_ROOT / "tpcore" / "feeds" / "profile.py",
                staged / "tpcore" / "feeds" / "profile.py")
    return staged


def test_add_for_new_feed_passes_classify_then_apply(tmp_path: Path) -> None:
    staged = _stage_repo(tmp_path)
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: zzz_test_feed\n"
        "kind: external\n"
        "provider: testvendor\n"
        "adapter: tpcore.testvendor.handle_zzz\n"
        "need: integration-test new feed\n"
        "cadence: daily\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    assert plan.rejection is None
    assert plan.approval_class is ApprovalClass.OPERATOR

    plan = validate(plan, repo_root=staged, dfcr=dfcr)
    assert plan.rejection is None

    apply(plan, repo_root=staged)
    # Re-snapshot — new feed must be present.
    snap = _read_bindings_snapshot(staged)
    assert "zzz_test_feed" in snap
    assert snap["zzz_test_feed"][0]["provider"] == "testvendor"
    assert snap["zzz_test_feed"][0]["status"] == "active"
    # FeedProfile must also be present.
    profile_src = (staged / "tpcore" / "feeds" / "profile.py").read_text()
    assert "'zzz_test_feed'" in profile_src or '"zzz_test_feed"' in profile_src


def test_add_for_existing_feed_rejects(tmp_path: Path) -> None:
    staged = _stage_repo(tmp_path)
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: prices_daily\n"   # already exists in the real registry
        "kind: external\n"
        "provider: dupe_provider\n"
        "adapter: tpcore.dupe\n"
        "need: x\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    assert plan.rejection is not None
    assert "already exists" in plan.rejection


def test_remove_for_missing_feed_rejects(tmp_path: Path) -> None:
    staged = _stage_repo(tmp_path)
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: REMOVE\n"
        "feed: never_existed_feed\n"
        "disposition: delete\n"
        "reason: never existed\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    assert plan.rejection is not None
    assert "nothing to remove" in plan.rejection


def test_modify_cutover_classifies_automated(tmp_path: Path) -> None:
    staged = _stage_repo(tmp_path)
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: MODIFY\n"
        "feed: macro_indicators\n"
        "change: provider:eco_archive\n"
        "reason: parity-cutover\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    assert plan.rejection is None
    assert plan.approval_class is ApprovalClass.AUTOMATED


def test_modify_cadence_applies_atomically(tmp_path: Path) -> None:
    staged = _stage_repo(tmp_path)
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: MODIFY\n"
        "feed: macro_indicators\n"
        "change: cadence:7\n"
        "reason: cadence tune\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    plan = validate(plan, repo_root=staged, dfcr=dfcr)
    apply(plan, repo_root=staged)
    profile_src = (staged / "tpcore" / "feeds" / "profile.py").read_text()
    # The macro_indicators FeedProfile cadence_days field must reflect the new value.
    assert "cadence_days=7" in profile_src


def test_apply_atomic_rollback_on_invalid_change(tmp_path: Path) -> None:
    staged = _stage_repo(tmp_path)
    before_providers = (staged / "tpcore" / "providers.py").read_bytes()
    before_profile = (staged / "tpcore" / "feeds" / "profile.py").read_bytes()
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: MODIFY\n"
        "feed: prices_daily\n"
        "change: bogus_key:nope\n"
        "reason: test rollback\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    plan = validate(plan, repo_root=staged, dfcr=dfcr)
    res = apply(plan, repo_root=staged)
    assert res.rejection is not None
    # Both SoT files must be byte-identical (atomic rollback).
    assert (staged / "tpcore" / "providers.py").read_bytes() == before_providers
    assert (staged / "tpcore" / "feeds" / "profile.py").read_bytes() == before_profile


@pytest.mark.parametrize("change_key,field_name", [
    ("freshness:30", "freshness_max_age_days=30"),
    ("skip_guard:3", "skip_guard_days=3"),
])
def test_modify_cadence_threshold_variants(
    tmp_path: Path, change_key: str, field_name: str,
) -> None:
    staged = _stage_repo(tmp_path)
    dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: MODIFY\n"
        "feed: macro_indicators\n"
        f"change: {change_key}\n"
        "reason: tune\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(dfcr, snapshot)
    plan = validate(plan, repo_root=staged, dfcr=dfcr)
    res = apply(plan, repo_root=staged)
    assert res.rejection is None, res.rejection
    profile_src = (staged / "tpcore" / "feeds" / "profile.py").read_text()
    assert field_name in profile_src


def test_add_full_roundtrip_modifies_both_files(tmp_path: Path) -> None:
    """End-to-end: ADD a derived feed, verify both files mutate, then
    REMOVE the same feed, verify both files return."""
    staged = _stage_repo(tmp_path)

    # ADD a derived feed.
    add_dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: derived_test_feed\n"
        "kind: derived\n"
        "derived_from: [vix]\n"
        "need: end-to-end\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(add_dfcr, snapshot)
    plan = validate(plan, repo_root=staged, dfcr=add_dfcr)
    apply(plan, repo_root=staged)

    snap_after_add = _read_bindings_snapshot(staged)
    assert "derived_test_feed" in snap_after_add

    # REMOVE the same feed.
    rm_dfcr = parse_dfcr(
        "DATA FEED CHANGE REQUEST\n"
        "operation: REMOVE\n"
        "feed: derived_test_feed\n"
        "disposition: delete\n"
        "reason: test cleanup\n"
    )
    snapshot = _read_bindings_snapshot(staged)
    plan = classify(rm_dfcr, snapshot)
    plan = validate(plan, repo_root=staged, dfcr=rm_dfcr)
    apply(plan, repo_root=staged)

    snap_after_remove = _read_bindings_snapshot(staged)
    assert "derived_test_feed" not in snap_after_remove
