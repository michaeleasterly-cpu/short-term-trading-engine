"""S1 — PROJECT_PROFILE.yaml sentinel.

Asserts that STE's hand-authored PROJECT_PROFILE.yaml is present,
parses with a narrow stdlib reader, and stays aligned with
``.claude/path_registry.yaml``. Catches drift in either direction:

  * profile diverges from registry → red.
  * registry diverges from profile → red.

Also asserts the cloud-memstore posture is declared correctly
(``api_memstores_enabled: true`` + a pointer to the canonical
handoff doc; no raw memstore IDs inlined).

Stdlib only. Does NOT call the dev-system parser, does NOT call the
Anthropic API, does NOT touch any memstore.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PROFILE = _REPO / "PROJECT_PROFILE.yaml"
_REGISTRY = _REPO / ".claude" / "path_registry.yaml"
_MEMSTORE_HANDOFF = _REPO / "docs" / "MEMSTORE_HANDOFF.md"


# ─────────────────────────────────────────────────────────────────────
# Narrow YAML reader — covers the subset PROJECT_PROFILE.yaml uses.
# ─────────────────────────────────────────────────────────────────────


def _extract_top_scalar(text: str, key: str) -> str | None:
    """Return the quoted-or-unquoted string value of a top-level
    ``key: value`` line, or None if absent. Strips quotes."""
    for raw in text.splitlines():
        if raw.startswith(key + ":"):
            value = raw[len(key) + 1:].strip()
            if value and (
                (value.startswith('"') and value.endswith('"'))
                or (value.startswith("'") and value.endswith("'"))
            ):
                value = value[1:-1]
            return value
    return None


def _extract_nested_scalar(text: str, parent: str, key: str) -> str | None:
    """Return the value of ``parent: \\n  key: value`` for nested
    one-level objects."""
    in_parent = False
    for raw in text.splitlines():
        if raw.startswith(parent + ":") and not raw.lstrip().startswith("-"):
            in_parent = True
            continue
        if in_parent:
            if raw and not raw[0].isspace() and not raw.startswith("#"):
                # Left the parent block.
                in_parent = False
                continue
            stripped = raw.strip()
            if stripped.startswith(key + ":"):
                value = stripped[len(key) + 1:].strip()
                if value and (
                    (value.startswith('"') and value.endswith('"'))
                    or (value.startswith("'") and value.endswith("'"))
                ):
                    value = value[1:-1]
                return value
    return None


def _extract_path_list(
    text: str, key: str, kind: str,
) -> list[str]:
    """Extract a top-level list of either plain strings or
    ``- path: "..."`` mapping items. ``kind`` is ``"string"`` or
    ``"path_mapping"``."""
    out: list[str] = []
    in_block = False
    for raw in text.splitlines():
        if raw.startswith(key + ":") and not raw.lstrip().startswith("-"):
            in_block = True
            continue
        if in_block:
            if raw and not raw[0].isspace() and not raw.startswith("#"):
                break
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if kind == "string" and stripped.startswith("- "):
                value = stripped[2:].strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                out.append(value)
            elif kind == "path_mapping" and stripped.startswith("- path:"):
                value = stripped[len("- path:"):].strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                out.append(value)
    return out


def _registry_group_paths(text: str, group: str) -> list[str]:
    """Walk the registry, find ``groups.<group>.paths`` and return
    the ordered list of ``- path: "..."`` values."""
    in_groups = False
    in_group = False
    in_paths = False
    out: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("groups:"):
            in_groups = True
            continue
        if in_groups:
            indent = len(raw) - len(raw.lstrip())
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Top-level key after groups → done.
            if indent == 0 and stripped.endswith(":"):
                break
            # ``  heavy_lane:``  at indent 2.
            if indent == 2 and stripped == f"{group}:":
                in_group = True
                in_paths = False
                continue
            if in_group:
                if indent <= 2 and stripped.endswith(":"):
                    # Hit another sibling group.
                    break
                if indent == 4 and stripped == "paths:":
                    in_paths = True
                    continue
                if in_paths and stripped.startswith("- path:"):
                    value = stripped[len("- path:"):].strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    out.append(value)
    return out


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


def test_project_profile_yaml_exists() -> None:
    assert _PROFILE.is_file(), (
        f"missing {_PROFILE.relative_to(_REPO)}"
    )
    assert _PROFILE.read_text(encoding="utf-8").strip(), "file is empty"


def test_project_profile_project_name_is_ste() -> None:
    text = _PROFILE.read_text(encoding="utf-8")
    assert _extract_top_scalar(text, "project_name") == (
        "short-term-trading-engine"
    )


def test_project_profile_review_mode_is_claude_review_only() -> None:
    text = _PROFILE.read_text(encoding="utf-8")
    assert _extract_top_scalar(text, "review_mode") == "claude-review-only"


def test_project_profile_memory_policy_posture() -> None:
    """C0.1 memory ceiling + memstores enabled by posture."""
    text = _PROFILE.read_text(encoding="utf-8")
    limit = _extract_nested_scalar(text, "memory_policy", "local_memory_limit_bytes")
    assert limit == "24400", f"expected 24400, got {limit!r}"
    enabled = _extract_nested_scalar(text, "memory_policy", "api_memstores_enabled")
    assert enabled == "true", f"expected api_memstores_enabled: true, got {enabled!r}"
    boundary_doc = _extract_nested_scalar(text, "memory_policy", "memory_boundary_doc")
    assert boundary_doc == "true", f"expected memory_boundary_doc: true, got {boundary_doc!r}"


def test_project_profile_memstore_ids_not_inlined() -> None:
    """Raw memstore IDs (``memstore_<>24 alphanumerics``) must NOT
    appear in PROJECT_PROFILE.yaml. They live only in
    ``docs/MEMSTORE_HANDOFF.md``. Per the dev-system adoption plan's
    cloud-memory rule §3."""
    text = _PROFILE.read_text(encoding="utf-8")
    memstore_id_re = re.compile(r"memstore_[A-Za-z0-9]{20,}")
    matches = memstore_id_re.findall(text)
    assert not matches, (
        f"raw memstore IDs found in PROJECT_PROFILE.yaml; they must "
        f"live only in docs/MEMSTORE_HANDOFF.md. count={len(matches)}"
    )


def test_project_profile_points_at_canonical_memstore_doc() -> None:
    text = _PROFILE.read_text(encoding="utf-8")
    pointer = _extract_nested_scalar(text, "memory_policy", "memstore_reference")
    assert pointer == "docs/MEMSTORE_HANDOFF.md", (
        f"memstore_reference must point at docs/MEMSTORE_HANDOFF.md; "
        f"got {pointer!r}"
    )
    # And the doc actually exists where the profile says it does.
    assert _MEMSTORE_HANDOFF.is_file(), (
        f"memory_policy.memstore_reference points at "
        f"{_MEMSTORE_HANDOFF.relative_to(_REPO)} which is missing"
    )


def test_project_profile_no_secret_like_values() -> None:
    """Defense in depth against accidental credential paste. None of
    these patterns may appear in the profile."""
    text = _PROFILE.read_text(encoding="utf-8")
    forbidden = (
        r"sk-ant-[A-Za-z0-9\-_]{20,}",      # Anthropic API key
        r"ghp_[A-Za-z0-9]{20,}",            # GitHub PAT
        r"gho_[A-Za-z0-9]{20,}",            # GitHub OAuth
        r"github_pat_[A-Za-z0-9_]{20,}",    # GitHub fine-grained PAT
        r"AKIA[0-9A-Z]{16}",                # AWS access key
        r"postgres://[^/\s]+:[^@\s]+@",     # connstring with creds
    )
    findings: list[str] = []
    for pat in forbidden:
        if re.search(pat, text):
            findings.append(pat)
    assert not findings, (
        f"secret-shape pattern(s) detected in PROJECT_PROFILE.yaml: "
        f"{findings}"
    )


def test_project_profile_critical_paths_match_registry_heavy_lane() -> None:
    """profile.critical_paths must equal exactly
    .claude/path_registry.yaml groups.heavy_lane.paths."""
    profile_text = _PROFILE.read_text(encoding="utf-8")
    registry_text = _REGISTRY.read_text(encoding="utf-8")
    profile_paths = _extract_path_list(
        profile_text, "critical_paths", "path_mapping",
    )
    registry_paths = _registry_group_paths(registry_text, "heavy_lane")
    assert registry_paths, (
        "registry has no heavy_lane paths — registry parser regression?"
    )
    assert profile_paths == registry_paths, (
        f"profile.critical_paths drifts from registry heavy_lane:\n"
        f"  profile  ({len(profile_paths)}): {profile_paths}\n"
        f"  registry ({len(registry_paths)}): {registry_paths}"
    )


def test_project_profile_claude_system_paths_match_registry() -> None:
    """profile.claude_system_paths must equal exactly
    .claude/path_registry.yaml groups.claude_system.paths."""
    profile_text = _PROFILE.read_text(encoding="utf-8")
    registry_text = _REGISTRY.read_text(encoding="utf-8")
    profile_paths = _extract_path_list(
        profile_text, "claude_system_paths", "string",
    )
    registry_paths = _registry_group_paths(registry_text, "claude_system")
    assert registry_paths, (
        "registry has no claude_system paths — parser regression?"
    )
    assert profile_paths == registry_paths, (
        f"profile.claude_system_paths drifts from registry claude_system:\n"
        f"  profile  ({len(profile_paths)}): {profile_paths}\n"
        f"  registry ({len(registry_paths)}): {registry_paths}"
    )


def test_project_profile_schema_version_pinned() -> None:
    text = _PROFILE.read_text(encoding="utf-8")
    assert _extract_top_scalar(text, "schema_version") == "1"


def test_project_profile_test_command_matches_ste_full_suite_discipline() -> None:
    """STE pytest discipline is whole-suite serial (ops/* package-shadow
    risk; .claude/rules/tests-and-ci.md). The profile must reflect
    that."""
    text = _PROFILE.read_text(encoding="utf-8")
    cmd = _extract_top_scalar(text, "test_command")
    assert cmd == "python -m pytest -p no:xdist -q", (
        f"test_command must be the serial full-suite command; got {cmd!r}"
    )
