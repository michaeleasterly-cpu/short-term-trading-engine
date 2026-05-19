"""Thin guard for the Phase P3a vulture dead-code gate.

The authoritative dead-code gate is the ``vulture (fail on new dead
code)`` step in ``.github/workflows/ci.yml`` (allowlist-baselined: it
fails ONLY on NEW un-allowlisted dead code). This test is the cheap
local sentinel: it asserts the committed baseline allowlist
(``vulture_allowlist.py``) exists and is syntactically parseable so a
corrupt/missing baseline can never silently disable the CI gate.

It does NOT shell out to vulture (slow, and the gate of record is the
CI step) and it NEVER touches the live repo's git.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "vulture_allowlist.py"


def test_vulture_allowlist_exists() -> None:
    """The baseline allowlist must be committed at the repo root."""
    assert ALLOWLIST.is_file(), (
        f"{ALLOWLIST} is missing — the vulture CI gate would have no "
        "baseline and could not fail-on-NEW-only."
    )


def test_vulture_allowlist_parses() -> None:
    """The allowlist must be valid Python (vulture passes it as a path)."""
    source = ALLOWLIST.read_text(encoding="utf-8")
    compile(source, str(ALLOWLIST), "exec")
