"""SP-G — diff-scope allow-list (build-time fence).

Spec §4.4: the agent's draft PR is allowed to touch ONLY:

- ``docs/superpowers/specs/<date>-<candidate>-lab-candidate.md`` (the
  rendered spec)
- ``docs/lab/<date>-<candidate>-emitted-spec.json`` (the machine-
  readable sidecar)
- A single new test file under the target engine
  (``<engine>/tests/test_lab_<candidate>_byte_identical.py`` — the
  Readiness §3 characterization test stub)

The agent is FORBIDDEN from touching ``tpcore/``, ``ops/`` (other than
the sidecar — but the sidecar lives under ``docs/lab/`` not ``ops/``,
so the rule degenerates to "no ``ops/`` edits at all"), any engine
``backtest.py`` / ``scheduler.py`` / ``plugs/`` / ``order_manager.py``,
``pyproject.toml``, ``platform/migrations/``, the ``.claude/`` tree, or
any SoT/roster file.

The agent calls ``enforce_diff_scope(changed_paths, spec)`` before
``gh pr create --draft``; a violation raises ``DiffScopeViolation`` and
the PR is NOT opened. The unit test deliberately constructs an
over-broad diff and asserts the fence trips.

Engine-FREE: stdlib only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


# Spec §4.4 forbidden prefixes: any path whose POSIX form startswith one
# of these reds the build. These are the structural fences — touching
# any of them means the agent is operating outside its sandbox.
FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = (
    "tpcore/",
    "ops/",
    "platform/migrations/",
    "platform/",
    ".claude/",
    ".github/",
    "scripts/",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Makefile",
    "alembic.ini",
)

# Engine-package subdir patterns that are ALWAYS forbidden — even though
# the agent IS allowed to add a test stub under ``<engine>/tests/``, it
# is NEVER allowed to touch the engine's plugs / scheduler / order
# manager / backtest. The renderer mechanically writes the path
# ``<engine>/tests/test_lab_<candidate>_byte_identical.py`` and nothing
# else under the engine package.
FORBIDDEN_ENGINE_SUBDIRS: tuple[str, ...] = (
    "plugs/",
    "scheduler.py",
    "order_manager.py",
    "backtest.py",
    "models.py",
    "config.py",
)


class DiffScopeViolation(Exception):
    """Raised when ``enforce_diff_scope`` sees a forbidden path in the
    PR's changed-paths list. The agent must NOT open the draft PR — the
    SP-A ledger row is already written (spec §3.4 step 5; operator
    runbook documents the orphaned-spend recovery).
    """

    def __init__(self, *, violating_paths: tuple[str, ...]) -> None:
        self.violating_paths = violating_paths
        super().__init__(
            f"SP-G diff-scope violation — emitted PR touches forbidden "
            f"paths: {list(violating_paths)!r}. The agent's allow-list "
            f"is the rendered spec + the JSON sidecar + a single engine-"
            f"test stub. See spec §4.4 + the diff-fence unit test."
        )


def _is_allowed_spec_path(path: str, *, candidate: str) -> bool:
    """``docs/superpowers/specs/<date>-<candidate>-lab-candidate.md``.

    The date prefix is variable but slash-free; we anchor on the
    ``-<candidate>-lab-candidate.md`` suffix. The agent renders the path
    deterministically, but the fence accepts any conformant filename so
    a clock-skew rename does not trip the fence falsely.
    """
    if not path.startswith("docs/superpowers/specs/"):
        return False
    return path.endswith(f"-{candidate}-lab-candidate.md")


def _is_allowed_sidecar_path(path: str, *, candidate: str) -> bool:
    """``docs/lab/<date>-<candidate>-emitted-spec.json``."""
    if not path.startswith("docs/lab/"):
        return False
    return path.endswith(f"-{candidate}-emitted-spec.json")


def _is_allowed_test_stub_path(
    path: str, *, candidate: str, target_engine: str
) -> bool:
    """``<engine>/tests/test_lab_<candidate>_byte_identical.py``.

    The candidate slug uses hyphens; Python identifiers use underscores,
    so the renderer writes the slug with hyphens replaced by underscores
    (matching the Sentinel SP-E precedent
    ``test_lab_activation_threshold_byte_identical.py`` shape).
    """
    underscored = candidate.replace("-", "_")
    expected = f"{target_engine}/tests/test_lab_{underscored}_byte_identical.py"
    return path == expected


def enforce_diff_scope(
    changed_paths: Iterable[str],
    *,
    candidate: str,
    target_engine: str,
) -> None:
    """Reject any changed path outside the three allow-list slots.

    ``changed_paths`` is a POSIX-relative list (the shape ``git diff
    --name-only`` returns). The fence:

    1. Rejects any path that startswith a member of
       ``FORBIDDEN_PATH_PREFIXES`` (the structural fence — touching
       ``tpcore/`` / ``ops/`` / ``platform/`` / ``.claude/`` / etc. is
       always wrong for an LLM-emitted PR).
    2. Rejects any path under the target engine that is NOT the
       allow-listed test stub (e.g. an attempted edit to
       ``<engine>/backtest.py`` or ``<engine>/plugs/``).
    3. Rejects any path that is NOT one of the three allow-listed
       slots (the rendered spec, the JSON sidecar, the engine test
       stub).

    Empty input returns silently — a no-op PR is a degenerate failure
    that the gh CLI catches downstream.
    """
    violations: list[str] = []
    for raw in changed_paths:
        path = raw.strip()
        if not path:
            continue

        # (1) Structural fence: forbidden prefixes are always wrong.
        if any(path.startswith(prefix) for prefix in FORBIDDEN_PATH_PREFIXES):
            violations.append(path)
            continue

        # (2) Engine-package subdir fence: only the test stub is allowed.
        if path.startswith(f"{target_engine}/"):
            if _is_allowed_test_stub_path(
                path, candidate=candidate, target_engine=target_engine
            ):
                continue
            violations.append(path)
            continue

        # Any OTHER engine package is forbidden outright — the agent
        # operates on the named target engine, never a sibling.
        # Detect "<somename>/..." where <somename> is a plausible engine
        # package by guarding against forbidden engine subdirs.
        head, _, tail = path.partition("/")
        if head and tail and head != target_engine and head not in {"docs"}:
            # If the path is under SOME other top-level dir that isn't
            # docs/<allowlisted>, it's outside the allowed slots.
            violations.append(path)
            continue

        # (3) The three docs allow-list slots.
        if _is_allowed_spec_path(path, candidate=candidate):
            continue
        if _is_allowed_sidecar_path(path, candidate=candidate):
            continue

        # Anything else under docs/ (or any other top-level) is denied.
        violations.append(path)

    if violations:
        raise DiffScopeViolation(violating_paths=tuple(violations))


__all__ = [
    "FORBIDDEN_ENGINE_SUBDIRS",
    "FORBIDDEN_PATH_PREFIXES",
    "DiffScopeViolation",
    "enforce_diff_scope",
]
