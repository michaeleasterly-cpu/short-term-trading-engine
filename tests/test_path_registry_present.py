"""Sentinel for the canonical path registry at ``.claude/path_registry.yaml``.

H0 path hardening (2026-06-01) — single source of truth for the path
lists previously duplicated across the Claude review workflow filter,
the heavy-lane rule frontmatter/body, ``docs/DEV_PIPELINE_STANDARD.md``
§0, ``.github/pull_request_template.md``, and
``.claude/hooks/session-start.sh``.

This sentinel pins:

  * registry presence + schema_version
  * heavy_lane + claude_system groups, each with non-empty description
    and ``paths:`` list
  * every path entry has a non-empty ``path`` and a non-empty ``why``
  * no duplicate paths within a group
  * groups are disjoint
  * workflow filter equals ``heavy_lane ∪ claude_system`` exactly
  * heavy-lane rule frontmatter equals ``heavy_lane`` exactly
  * DEV_PIPELINE_STANDARD, PR template, and session-start hook each
    contain every heavy_lane path string verbatim

Mirrors the precedent of ``tests/test_claude_review_workflow_present.py``:
presence + load-bearing properties, NOT behavior. The behavior test
is the end-to-end ``scripts/check_manifests.py`` invocation (covered
by ``tests/test_manifest_check_present.py``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO / ".claude" / "path_registry.yaml"
_CHECK_MANIFESTS = _REPO / "scripts" / "check_manifests.py"


def _load_check_manifests_module():
    """Import ``scripts/check_manifests.py`` as a module without
    triggering its ``__main__`` block. We rely on the stdlib parser +
    consumer-sync checks defined there so this sentinel doesn't fork
    its own YAML parser.
    """
    spec = importlib.util.spec_from_file_location(
        "check_manifests_h0", _CHECK_MANIFESTS,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_manifests_h0"] = mod
    spec.loader.exec_module(mod)
    return mod


def _registry_data() -> dict:
    mod = _load_check_manifests_module()
    data, failures = mod.load_registry()
    assert not failures, f"registry load failures: {failures}"
    assert data is not None, "registry could not be loaded"
    return data


def test_registry_file_present() -> None:
    assert _REGISTRY.is_file(), f"missing path registry: {_REGISTRY}"
    text = _REGISTRY.read_text(encoding="utf-8")
    assert text.strip(), "path registry is empty"


def test_registry_schema_version_pinned() -> None:
    data = _registry_data()
    assert data.get("schema_version") == 1, (
        f"schema_version must be 1, got {data.get('schema_version')!r}; "
        f"bumping the schema requires a coordinated update to "
        f"scripts/check_manifests.py and this sentinel"
    )


def test_registry_has_heavy_lane_and_claude_system_groups() -> None:
    data = _registry_data()
    groups = data.get("groups", {})
    assert "heavy_lane" in groups, "registry missing 'heavy_lane' group"
    assert "claude_system" in groups, "registry missing 'claude_system' group"
    for name in ("heavy_lane", "claude_system"):
        group = groups[name]
        assert group.get("description", "").strip(), (
            f"group {name!r} must have a non-empty description"
        )
        paths = group.get("paths", [])
        assert paths, f"group {name!r} must have at least one path entry"


def test_every_path_has_a_why() -> None:
    data = _registry_data()
    for name, group in data.get("groups", {}).items():
        for item in group.get("paths", []):
            path_value = item.get("path", "")
            why_value = item.get("why", "")
            assert path_value.strip(), (
                f"{name}: entry missing non-empty 'path': {item!r}"
            )
            assert why_value.strip(), (
                f"{name}: entry {path_value!r} missing non-empty 'why' — "
                f"every path must document why it earns its group's discipline"
            )


def test_no_duplicate_paths_within_groups() -> None:
    data = _registry_data()
    for name, group in data.get("groups", {}).items():
        paths = [item.get("path", "") for item in group.get("paths", [])]
        seen: dict[str, int] = {}
        for p in paths:
            seen[p] = seen.get(p, 0) + 1
        duplicates = sorted([p for p, n in seen.items() if n > 1])
        assert not duplicates, (
            f"group {name!r} has duplicate path entries: {duplicates}"
        )


def test_no_duplicate_paths_across_groups() -> None:
    """heavy_lane and claude_system must be disjoint. A path that
    needs both disciplines is a sign the groups are mis-modeled."""
    mod = _load_check_manifests_module()
    data = _registry_data()
    heavy = set(mod.registry_paths(data, "heavy_lane"))
    claude = set(mod.registry_paths(data, "claude_system"))
    overlap = heavy & claude
    assert not overlap, (
        f"paths in BOTH heavy_lane and claude_system: {sorted(overlap)}"
    )


def test_workflow_filter_equals_registry_union() -> None:
    """``.github/workflows/claude-review-heavy-lane.yml`` ``paths:``
    filter must equal exactly ``heavy_lane ∪ claude_system``. No
    missing entries (drift toward under-review), no extras (drift
    toward over-review or stale entries)."""
    mod = _load_check_manifests_module()
    failures = mod.check_workflow_filter_equals_registry_union()
    assert not failures, (
        "workflow filter drift from registry:\n  "
        + "\n  ".join(failures)
    )


def test_heavy_lane_rule_frontmatter_equals_registry_heavy_lane() -> None:
    """``.claude/rules/heavy-lane.md`` frontmatter ``paths:`` must
    equal exactly the registry's ``heavy_lane`` group."""
    mod = _load_check_manifests_module()
    failures = mod.check_heavy_lane_rule_frontmatter_equals_registry()
    assert not failures, (
        "heavy-lane rule frontmatter drift from registry:\n  "
        + "\n  ".join(failures)
    )


def test_doc_pipeline_standard_lists_heavy_lane_paths() -> None:
    """Every registry ``heavy_lane`` path must appear verbatim in
    ``docs/DEV_PIPELINE_STANDARD.md`` so operator-facing lane docs
    stay accurate. String-presence is intentionally permissive on
    surrounding markdown — the check protects against missed
    enumeration, not formatting drift."""
    mod = _load_check_manifests_module()
    failures = mod.check_doc_pipeline_standard_lists_heavy_lane()
    assert not failures, (
        "DEV_PIPELINE_STANDARD.md drift from registry:\n  "
        + "\n  ".join(failures)
    )


def test_pr_template_lists_heavy_lane_paths() -> None:
    """Every registry ``heavy_lane`` path must appear verbatim in the
    PR template's risk-path checklist."""
    mod = _load_check_manifests_module()
    failures = mod.check_pr_template_lists_heavy_lane()
    assert not failures, (
        "pull_request_template.md drift from registry:\n  "
        + "\n  ".join(failures)
    )


def test_session_start_lists_heavy_lane_paths() -> None:
    """Every registry ``heavy_lane`` path must appear verbatim in the
    session-start hook's summary heredoc."""
    mod = _load_check_manifests_module()
    failures = mod.check_session_start_hook_lists_heavy_lane()
    assert not failures, (
        "session-start.sh drift from registry:\n  "
        + "\n  ".join(failures)
    )
