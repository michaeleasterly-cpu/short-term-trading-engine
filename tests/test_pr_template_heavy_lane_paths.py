"""Sentinel — every ``.claude/path_registry.yaml`` heavy_lane path has
its own dedicated checkbox line in ``.github/pull_request_template.md``.

S2-discovered defect surfaced by ``scripts/run_dev_system_audit.sh``:
the registry listed ``ops/engine_sdlc/**`` but the PR template carried
a single combined checkbox ``ops/engine_sdlc.py or ops/engine_sdlc/**``,
which neither the manual reviewer nor the dev-system manifest-linter
recognized as covering both paths. The portable `check_manifests`
parser specifically looks for the exact ``- [ ] `<path>` `` shape per
path.

This sentinel pins the contract so the gap cannot reopen via a
future rename, reorder, or "let's combine these for readability"
refactor.

Stdlib only.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO / ".claude" / "path_registry.yaml"
_PR_TEMPLATE = _REPO / ".github" / "pull_request_template.md"


def _registry_heavy_lane_paths() -> list[str]:
    """Narrow stdlib reader for ``groups.heavy_lane.paths`` —
    matches the shape used by scripts/check_manifests.py."""
    text = _REGISTRY.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_groups = False
    in_heavy = False
    in_paths = False
    out: list[str] = []
    for raw in lines:
        if raw.startswith("groups:"):
            in_groups = True
            continue
        if not in_groups:
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Top-level key after groups → done with groups block.
        if indent == 0 and stripped.endswith(":"):
            break
        # ``  heavy_lane:`` at indent 2 enters the heavy_lane group.
        if indent == 2 and stripped == "heavy_lane:":
            in_heavy = True
            in_paths = False
            continue
        if in_heavy:
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


def test_pr_template_present() -> None:
    assert _PR_TEMPLATE.is_file(), (
        f"missing {_PR_TEMPLATE.relative_to(_REPO)}"
    )
    assert _PR_TEMPLATE.read_text(encoding="utf-8").strip(), "empty"


def test_every_heavy_lane_path_has_dedicated_checkbox() -> None:
    """For every path P in registry groups.heavy_lane.paths, the PR
    template must contain a checkbox line whose body starts with the
    backtick-quoted path: ``- [ ] `<P>` ``.

    Combining two paths into one checkbox (``- [ ] `A` or `B` ``)
    fails this test by design — that's exactly the S2-surfaced
    drift we are fixing. The dev-system manifest-linter parses
    needle-exact, so anything that hides a path in a combined or
    grouped line is invisible to it."""
    registry_paths = _registry_heavy_lane_paths()
    assert registry_paths, "registry parser returned no heavy_lane paths"
    template_text = _PR_TEMPLATE.read_text(encoding="utf-8")
    missing: list[str] = []
    for path in registry_paths:
        needle = f"- [ ] `{path}`"
        if needle not in template_text:
            missing.append(path)
    assert not missing, (
        "PR template is missing a dedicated heavy-lane checkbox for "
        f"the following registry path(s): {missing}. The portable "
        "check_manifests parser looks for the exact needle "
        "``- [ ] `<path>` `` per path; combining two paths into one "
        "line (`- [ ] `A` or `B` `) fails both the parser and the "
        "manual reviewer's checklist UI."
    )


def test_engine_sdlc_dir_glob_specifically_present() -> None:
    """Explicit S2 regression pin — the specific path the S2 wrapper
    flagged as missing must be present as its own checkbox, even if a
    future change reorders the surrounding entries."""
    needle = "- [ ] `ops/engine_sdlc/**`"
    text = _PR_TEMPLATE.read_text(encoding="utf-8")
    assert needle in text, (
        f"PR template must contain {needle!r} as its own dedicated "
        "checkbox (S2 regression pin — do not recombine with "
        "ops/engine_sdlc.py)"
    )
