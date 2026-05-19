"""Anti-rot tripwire: docs/DEV_PIPELINE_STANDARD.md must exist and keep
its load-bearing clauses. Mirrors the gen_engine_manifest /
test_xdist_group_manifest manifest-discipline (the Agents+Dev-Env spec's
anti-rot-sentinel work). This is a PRESENCE check, NOT a behavioural test
of the process (the process is operator + reviewer discipline,
un-testable here)."""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "DEV_PIPELINE_STANDARD.md"

# Self-exemption (conservative, by construction): the scan is
# intentionally doc-scoped — it reads ONLY _DOC and never globs the test
# tree, so this file's own _ANCHORS literals below cannot self-trip the
# tripwire. This mirrors the conservative-self-exemption reasoning
# documented in tests/test_xdist_group_manifest.py (its
# test_xdist_group_manifest.py self-exempt `continue`): a sentinel must
# not red on its own pattern strings.
#
# Load-bearing literal anchors — if any vanishes, the standard has been
# silently gutted; red CI. Keep this list == the doc's must-contain
# literals (the anti-rot-sentinel anchor set).
_ANCHORS = (
    "gh pr checks",
    "no:xdist",
    'xdist_group("ops_shadow")',
    "split-review",
    "git stash",
    "expert subagent",
    "spec-read gate",
    "order-flip",
)


def test_dev_pipeline_standard_present_and_intact() -> None:
    assert _DOC.is_file(), f"missing canonical standard: {_DOC}"
    src = _DOC.read_text()
    missing = [a for a in _ANCHORS if a not in src]
    assert not missing, (
        "DEV_PIPELINE_STANDARD.md lost load-bearing clauses "
        f"(silent rot): {missing}")
