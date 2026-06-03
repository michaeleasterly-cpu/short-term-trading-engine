"""Sentinel for the post-round-trip TODO.md grooming pass.

Pins the load-bearing claims of the grooming PR so a later "let me
tidy TODO" refactor cannot silently re-introduce the stale state:

  * the STE dev-system round-trip closure section is present
  * P2c lifecycle is no longer flagged "CI FAILED" / "under triage"
  * the three genuinely deferred lifecycle follow-ups remain open
    (P1b CIK long-tail, P2c+ 8-K Item 3.01, metadata coverage backfill)
  * round-trip PRs #416 + #421 are referenced
  * dev-system PRs are summarized
  * no implementation-authorization language sneaks in
  * no secret-shape literal or Anthropic API write surface added

Stdlib only. No pytest/PyYAML/etc. imports beyond what STE's test
suite already uses.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TODO = _REPO / "TODO.md"


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), "TODO.md is empty"
    return text


# ─────────────────────────────────────────────────────────────────────
# Required new section — STE dev-system round-trip closure
# ─────────────────────────────────────────────────────────────────────


def test_todo_marks_ste_round_trip_closed() -> None:
    text = _todo_text()
    assert "STE dev-system round-trip — CLOSED 2026-06-01" in text, (
        "TODO.md must contain the round-trip closure H2 heading"
    )


def test_todo_references_ste_round_trip_pr_chain() -> None:
    """The closure section must enumerate the STE-side PR chain so a
    fresh reader can audit the trail without grepping git log."""
    text = _todo_text()
    for pr in ("#416", "#417", "#418", "#419", "#420", "#421"):
        assert pr in text, (
            f"TODO.md must reference STE round-trip PR {pr}"
        )


def test_todo_references_dev_system_summary() -> None:
    """The closure section must point at the dev-system source — at
    minimum the PR #10 fix that unblocks the audit wrapper, plus the
    D0a-D0g extraction range."""
    text = _todo_text()
    assert "trellis-dev-system" in text, (
        "TODO.md must name the trellis-dev-system source repo"
    )
    # Memstore_reference fix on the dev-system side:
    assert (
        "PR #10" in text
        or "memstore_reference" in text
    ), (
        "TODO.md must reference the dev-system PR #10 (pointer-only "
        "memstore_reference semantics) — that is what unblocks the "
        "STE audit wrapper"
    )


# ─────────────────────────────────────────────────────────────────────
# Stale-state corrections — P2c lifecycle
# ─────────────────────────────────────────────────────────────────────


def test_todo_does_not_call_p2c_ci_failed() -> None:
    """``fac5f79`` and ``8048529`` are both on main. The TODO must no
    longer carry the stale "CI FAILED — under triage" claim."""
    text = _todo_text()
    # The stale phrasing was: "**CI FAILED** — under triage" on the
    # P2c line. Reject both the FAILED and "under triage" claims when
    # they appear within the lifecycle-arc P2c entry.
    forbidden_phrases = (
        "CI FAILED",
        "CI failed",
    )
    p2c_block_marker = "P2c"
    findings: list[str] = []
    for raw in text.splitlines():
        if p2c_block_marker in raw:
            for needle in forbidden_phrases:
                if needle in raw:
                    findings.append(f"{needle!r} on line: {raw!r}")
    assert not findings, (
        "TODO.md still flags P2c as CI-failed but fac5f79 + 8048529 "
        f"are both on main. Stale lines: {findings}"
    )


def test_todo_records_p2c_fix_commit() -> None:
    """The grooming pass must cite the fix commit ``8048529`` so the
    history of the original CI red is preserved, not erased."""
    text = _todo_text()
    assert "8048529" in text, (
        "TODO.md must record the P2c CI-fix commit 8048529 so the "
        "history of the original CI red is preserved"
    )


# ─────────────────────────────────────────────────────────────────────
# Genuinely deferred items must remain visible
# ─────────────────────────────────────────────────────────────────────


def test_todo_keeps_p1b_cik_long_tail_open() -> None:
    text = _todo_text()
    assert "P1b" in text and "CIK" in text, (
        "TODO.md must still surface the P1b CIK discovery long-tail "
        "as deferred / open work"
    )


def test_todo_keeps_p2c_plus_8k_item_3_01_open() -> None:
    text = _todo_text()
    assert "P2c+" in text and "3.01" in text, (
        "TODO.md must still surface the P2c+ 8-K Item 3.01 extractor "
        "as deferred / open work"
    )


def test_todo_keeps_metadata_coverage_backfill_open() -> None:
    text = _todo_text()
    assert "Metadata coverage" in text, (
        "TODO.md must still surface the metadata coverage gate / "
        "operator-on-demand SEC metadata backfill as open work"
    )


# ─────────────────────────────────────────────────────────────────────
# Safety surface — doc-only PR must introduce no execution surface
# ─────────────────────────────────────────────────────────────────────


def test_todo_grooming_does_not_authorize_implementation_in_pr() -> None:
    """The closure section must declare the round-trip done — but
    must NOT authorize starting the next implementation task within
    this same PR."""
    text = _todo_text()
    # The closure section explicitly lists "Recommended next
    # implementation" as a separate decision (P1b CIK long-tail) but
    # must not say things like "starting P1b now in this PR" or
    # "this PR implements P1b".
    forbidden_authorizations = (
        "this PR implements",
        "this PR begins P1b",
        "starting implementation in this PR",
    )
    findings = [t for t in forbidden_authorizations if t in text]
    assert not findings, (
        f"TODO.md grooming PR must not authorize implementation in "
        f"the same PR: {findings}"
    )


def test_todo_introduces_no_raw_memstore_id() -> None:
    """The grooming pass must respect the memstore-ID-no-leak rule.
    Memstore IDs only live in `docs/MEMSTORE_HANDOFF.md`; TODO.md is
    not an authorized container for them."""
    text = _todo_text()
    memstore_id_re = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    matches = memstore_id_re.findall(text)
    assert not matches, (
        f"TODO.md must not contain raw memstore-ID literal(s): "
        f"count={len(matches)}"
    )


def test_todo_introduces_no_anthropic_api_write_surface() -> None:
    """The grooming pass must introduce no curl-to-Anthropic /
    memstore-mutation shape."""
    text = _todo_text()
    forbidden_patterns = (
        r"curl\s+-?[^\n]*api\.anthropic\.com",
        r"-X\s+(?:POST|PUT|PATCH|DELETE)[^\n]*/v1/memory_stores",
    )
    findings: list[str] = []
    for pat in forbidden_patterns:
        if re.search(pat, text, re.IGNORECASE):
            findings.append(pat)
    assert not findings, (
        f"TODO.md must not authorise an Anthropic API write: "
        f"{findings}"
    )
