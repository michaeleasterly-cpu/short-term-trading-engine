"""Thin guard for the Phase P3a vulture dead-code gate.

The authoritative dead-code gate is the ``vulture (fail on new dead
code)`` step in ``.github/workflows/ci.yml`` (allowlist-baselined: it
fails ONLY on NEW un-allowlisted dead code). This test is the cheap
local sentinel: it asserts the committed baseline allowlist
(``vulture_allowlist.py``) exists and is syntactically parseable so a
corrupt/missing baseline can never silently disable the CI gate.

It also pins the CI invocation to ``--min-confidence 60``: vulture 2.16
reports an unused module-level function (the primary dead-code class) at
only 60% confidence, so a regression back to ``--min-confidence 80``
would make the gate structurally inert (a fresh dead function passes —
fake green). This is a parse/string check only; it does NOT shell out to
vulture (slow, and the gate of record is the CI step) and it NEVER
touches the live repo's git.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "vulture_allowlist.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


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


def test_ci_vulture_gate_uses_min_confidence_60() -> None:
    """The CI gate MUST run at --min-confidence 60.

    vulture 2.16 flags an unused module-level function (the primary
    dead-code class) at only 60% confidence. A regression to
    --min-confidence 80 silently suppresses that class and renders the
    whole gate structurally inert (fresh dead function ⇒ EXIT 0, fake
    green). Pin it here so the inertness defect cannot come back unseen.
    """
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    assert (
        "vulture --min-confidence 60 tpcore ops reversion vector "
        "momentum sentinel canary dashboard_components "
        "vulture_allowlist.py" in ci
    ), (
        "The ci.yml vulture step must invoke --min-confidence 60 "
        "(80 makes the dead-function gate structurally inert)."
    )
    assert "vulture --min-confidence 80" not in ci, (
        "ci.yml still references --min-confidence 80 — the inert "
        "threshold. The dead-code gate would not catch dead functions."
    )
