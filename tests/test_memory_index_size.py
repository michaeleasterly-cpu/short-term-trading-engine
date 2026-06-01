"""C0.1 (2026-06-01) — MEMORY.md size sentinel.

Claude Code truncates the auto-loaded memory index at session start
above ~24 400 bytes. Entries past the cutoff vanish silently — the
specific defect the C0.1 hardening was scoped to prevent.

This sentinel red-lines CI if any tracked ``MEMORY.md`` (repo-root or
under ``docs/memory/``) exceeds the limit. The operator-local memstore
at ``~/.claude/projects/-Users-michael-short-term-trading-engine/memory/``
is NOT tracked and cannot be policed here — ``docs/MEMORY_MAINTENANCE.md``
documents the operator-side remediation procedure for the local case.

Contract (per the C0.1 spec):

  * If no tracked ``MEMORY.md`` exists, the test passes (no policy
    breach when there's nothing to police).
  * If a tracked ``MEMORY.md`` exists and is at-or-under 24 400 bytes,
    the test passes.
  * If a tracked ``MEMORY.md`` exists and is over 24 400 bytes, the
    test fails with the exact byte count and pointer to the
    remediation procedure in ``docs/MEMORY_MAINTENANCE.md``.

The test must not print file contents, must not inspect secrets, and
must be deterministic + stdlib-only (pathlib only, no third-party).
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MEMORY_LIMIT_BYTES = 24_400

# Locations a tracked MEMORY.md is conventionally found in this repo.
# Both are checked; either over the ceiling fails the test.
_TRACKED_CANDIDATES: tuple[Path, ...] = (
    _REPO / "MEMORY.md",
    _REPO / "docs" / "memory" / "MEMORY.md",
)


def _existing_tracked_memory_indices() -> list[Path]:
    return [p for p in _TRACKED_CANDIDATES if p.is_file()]


def test_memory_index_under_24400_bytes() -> None:
    indices = _existing_tracked_memory_indices()
    if not indices:
        # No tracked MEMORY.md present — nothing to police. This is
        # the expected steady state for this repo (the operator's
        # local memstore at ~/.claude/projects/.../memory/MEMORY.md
        # is intentionally not tracked).
        return
    over_limit: list[tuple[Path, int]] = []
    for index_path in indices:
        size = index_path.stat().st_size
        if size > _MEMORY_LIMIT_BYTES:
            rel = index_path.relative_to(_REPO)
            over_limit.append((rel, size))
    if over_limit:
        lines = [
            f"{rel}: {size} bytes (limit {_MEMORY_LIMIT_BYTES})"
            for rel, size in over_limit
        ]
        details = "\n  ".join(lines)
        raise AssertionError(
            "Tracked MEMORY.md exceeds Claude Code's startup-load "
            "ceiling (24 400 bytes); entries past the cutoff are "
            "silently truncated. See docs/MEMORY_MAINTENANCE.md "
            "(MEMORY.md over the 24 400-byte ceiling) for the "
            "remediation procedure. Files over limit:\n  "
            + details
        )
