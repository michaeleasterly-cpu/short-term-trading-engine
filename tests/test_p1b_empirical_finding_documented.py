"""P1b empirical-finding sentinel.

Pins the load-bearing claims of the 2026-06-02 TODO/plan update that
captures the live-smoke result: P1b implementation works, but the
hypothesis that FMP /stable/profile would resolve part of the 1,419
long-tail bucket is empirically not supported. A future "tidy TODO"
refactor cannot silently drop the no-match record, the
do-not-run-uncapped instruction, or the metadata-coverage-still-open
note.

Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TODO = _REPO / "TODO.md"
_PLAN = _REPO / "docs" / "superpowers" / "plans" / (
    "2026-06-01-p1b-cik-long-tail-backfill-plan.md"
)


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), "TODO.md is empty"
    return text


def _plan_text() -> str:
    assert _PLAN.is_file(), f"missing {_PLAN.relative_to(_REPO)}"
    text = _PLAN.read_text(encoding="utf-8")
    assert text.strip(), "plan doc is empty"
    return text


# ─────────────────────────────────────────────────────────────────────
# TODO.md must record the empirical no-match result
# ─────────────────────────────────────────────────────────────────────


def test_todo_marks_p1b_implementation_done() -> None:
    text = _todo_text()
    # Look for both "P1b" and DONE within the same vicinity (the entry
    # bullet). The grooming PR #422 already proved the H2-level "DONE"
    # pattern; here we need a narrower match on the bullet itself.
    p1b_entry = re.search(
        r"P1b — CIK discovery long tail.*?(?=^- \*\*P)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert p1b_entry is not None, (
        "TODO.md must contain the P1b bullet at its expected location"
    )
    block = p1b_entry.group(0)
    assert (
        "Implementation DONE" in block
        or "implementation DONE" in block
    ), (
        "P1b bullet must mark implementation DONE so a future reader "
        "doesn't repeat the spec→plan→impl arc"
    )
    # Cross-references to the actual PR chain.
    for pr in ("#423", "#424", "#425"):
        assert pr in block, (
            f"P1b bullet must reference PR {pr} so the audit trail is "
            f"discoverable from TODO.md"
        )


def test_todo_records_zero_of_one_hundred_empirical_result() -> None:
    """Specific assertion the operator's task spec called for."""
    text = _todo_text()
    # Accept "0 of 100" or "0/100" framing.
    assert (
        "0 of 100" in text
        or "0/100" in text
        or "resolved: 0, no_match: 100" in text
    ), (
        "TODO.md must record the 0/100 empirical no-match result from "
        "the 2026-06-02 live smoke"
    )


def test_todo_forbids_uncapped_full_pass_without_triage() -> None:
    """The full ``fmp_max_unresolved=0`` pass is the spec's optional
    Step 4; the empirical evidence flips it to "do not run until
    triage". The TODO must surface that."""
    text = _todo_text()
    # Look for the stop signal: ⛔ marker OR explicit "DO NOT" framing.
    has_stop_marker = "⛔" in text
    has_explicit_block = (
        "DO NOT run the uncapped" in text
        or "do not run the uncapped" in text.lower()
    )
    assert has_stop_marker or has_explicit_block, (
        "TODO.md must explicitly block the uncapped fmp_max_unresolved=0 "
        "full pass until P1c source triage is complete"
    )
    # And the specific knob name must appear so the operator can grep
    # for it before re-running.
    assert "fmp_max_unresolved=0" in text, (
        "TODO.md must name the specific knob (fmp_max_unresolved=0) "
        "that's blocked, so a future operator session can grep for it"
    )


def test_todo_adds_p1c_triage_followup() -> None:
    text = _todo_text()
    assert "P1c" in text, (
        "TODO.md must introduce the P1c follow-up (unresolved-security-"
        "source triage) so the empirical finding has a path forward"
    )
    # P1c must explicitly NOT authorize another P1b-style implementation.
    assert "Do not implement" in text or "do not implement" in text, (
        "P1c entry must explicitly forbid another P1b-style stage "
        "extension until non-zero-hit evidence is found"
    )


def test_todo_keeps_metadata_coverage_backfill_open() -> None:
    """P1b did not advance the coverage gate; the separate
    metadata-coverage-backfill item must remain visible as the
    highest-leverage next move for DATA_OPERATIONS_COMPLETE."""
    text = _todo_text()
    assert "Metadata coverage" in text, (
        "TODO.md must keep the metadata coverage gate item visible"
    )
    assert "STILL OPEN" in text or "still open" in text.lower(), (
        "TODO.md must explicitly flag the metadata coverage gate as "
        "still open after P1b — the empirical finding showed P1b does "
        "NOT close it"
    )


# ─────────────────────────────────────────────────────────────────────
# Plan doc must record the live-smoke result section
# ─────────────────────────────────────────────────────────────────────


def test_plan_doc_records_post_merge_live_smoke_section() -> None:
    text = _plan_text()
    assert "Post-merge live-smoke result" in text, (
        "plan doc must include the post-merge live-smoke result section"
    )
    # The three steps must be enumerated.
    for needle in ("dry_run=true", "dry_run=false", "fmp_max_unresolved"):
        assert needle in text, (
            f"plan doc live-smoke section must include the {needle!r} "
            "command knob so the audit trail names the actual invocation"
        )


# ─────────────────────────────────────────────────────────────────────
# Safety surface — doc-only PR must introduce no secret-shape literal
# ─────────────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    todo_text = _todo_text()
    plan_text = _plan_text()
    memstore_id_re = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    for label, text in (("TODO.md", todo_text), ("plan", plan_text)):
        assert not memstore_id_re.findall(text), (
            f"{label} must not contain raw memstore-ID literal"
        )


def test_no_credential_shape_introduced() -> None:
    todo_text = _todo_text()
    plan_text = _plan_text()
    forbidden = (
        r"sk-ant-[A-Za-z0-9\-_]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"postgres://[^/\s]+:[^@\s]+@",
    )
    for label, text in (("TODO.md", todo_text), ("plan", plan_text)):
        for pat in forbidden:
            assert re.search(pat, text) is None, (
                f"{label} must not contain credential-shape literal "
                f"matching {pat}"
            )
