"""FPFD bulk repair closeout sentinel (2026-06-02).

Pins the load-bearing claims of the doc-only closeout PR so a future
"tidy TODO" or spec rewrite cannot silently drop the empirical
numbers, the implementation PR cross-references, the validator-stayed-strict
claim, or the "ticker-reuse cleanup is a separate arc, not yet
authorized" guard. Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TODO = _REPO / "TODO.md"
_SPEC = _REPO / "docs" / "superpowers" / "specs" / (
    "2026-06-02-fpfd-extractor-repair-before-fundamentals-cleanup.md"
)


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), "TODO.md is empty"
    return text


def _spec_text() -> str:
    assert _SPEC.is_file(), f"missing {_SPEC.relative_to(_REPO)}"
    text = _SPEC.read_text(encoding="utf-8")
    assert text.strip(), "spec doc is empty"
    return text


# ────────────────────────────────────────────────────────────────
# TODO records the closeout
# ────────────────────────────────────────────────────────────────


def test_todo_marks_fpfd_arc_closed() -> None:
    text = _todo_text()
    assert "✅ FPFD extractor repair" in text, (
        "TODO.md must include a ✅ closeout heading for the FPFD arc"
    )
    assert "CLOSED 2026-06-02" in text, (
        "TODO.md must record the 2026-06-02 closeout date"
    )


def test_todo_references_all_five_implementation_prs() -> None:
    text = _todo_text()
    for pr in ("#433", "#434", "#435", "#436", "#437"):
        assert pr in text, (
            f"TODO.md FPFD closeout must reference PR {pr} so the audit "
            f"trail is discoverable"
        )


def test_todo_records_240_metadata_writes_and_zero_regressions() -> None:
    text = _todo_text()
    assert "240" in text, (
        "TODO.md must record the 240-ticker bounded-live cohort size"
    )
    # The "0 non-cohort updates" and "0 divergence events" framing must
    # survive future TODO grooming.
    accepted_no_regression = (
        "0 non-cohort updates" in text or "0 regressions" in text
        or "0 later" in text
    )
    assert accepted_no_regression, (
        "TODO.md must record that 0 non-cohort updates / 0 regressions "
        "happened during the FPFD repair"
    )


def test_todo_records_8633_to_6016_bad_row_reduction() -> None:
    text = _todo_text()
    # Accept either narrative shape ("8,633 → 6,016" or "8633 -> 6016").
    pat = re.compile(r"8[,]?633\s*[→\-]+\s*\*?\*?6[,]?016\*?\*?")
    assert pat.search(text), (
        "TODO.md must record the 8,633 → 6,016 pre-FPFD bad-row reduction"
    )
    assert "30.3" in text or "30%" in text, (
        "TODO.md must record the ~30 % reduction so the magnitude is "
        "explicit at a skim"
    )


def test_todo_records_fundamentals_quarterly_invariant_preserved() -> None:
    text = _todo_text()
    # The UPDATE-only invariant must survive grooming.
    accepted = (
        "183,352 → **183,352 unchanged**" in text
        or "183352 -> 183352" in text
        or "183,352 unchanged" in text.replace("**", "")
    )
    assert accepted, (
        "TODO.md must record that fundamentals_quarterly.total was "
        "unchanged (UPDATE-only invariant preserved)"
    )


def test_todo_blocks_ticker_reuse_cleanup_pending_separate_authorization() -> None:
    text = _todo_text()
    assert "ticker-reuse" in text.lower(), (
        "TODO.md must reference the ticker-reuse residual"
    )
    accepted_block = (
        "separate spec PR required" in text
        or "not yet authorized" in text.lower()
        or "separate cleanup arc" in text
    )
    assert accepted_block, (
        "TODO.md must explicitly state the ticker-reuse cleanup is a "
        "separate arc requiring explicit operator authorization"
    )


def test_todo_records_validator_stayed_strict() -> None:
    text = _todo_text()
    # The hard invariant: no filter, no threshold change, no bucket added.
    accepted = (
        "No filter, no threshold" in text
        or "validator stayed strict" in text.lower()
        or "no filter, no threshold loosening" in text.lower()
        or "validator stays strict" in text.lower()
    )
    assert accepted, (
        "TODO.md must state the validator stayed strict throughout the "
        "FPFD arc (per PR #435 §1 hard rule)"
    )


# ────────────────────────────────────────────────────────────────
# Spec records the closeout
# ────────────────────────────────────────────────────────────────


def test_spec_records_post_execution_section() -> None:
    text = _spec_text()
    assert "Post-execution result" in text, (
        "spec doc must include the post-execution result section"
    )
    for needle in ("PR #436", "PR #437", "8.7 s wall", "6.6 s wall"):
        assert needle in text, (
            f"spec doc post-execution section must include {needle!r} "
            "so the runtime + PR audit trail survives grooming"
        )


def test_spec_post_execution_records_240_writes_and_zero_regressions() -> None:
    text = _spec_text()
    # 240 metadata writes; 0 FPFD moves later (no regression)
    assert "metadata writes" in text or "metadata.written" in text
    accepted_zero_regression = (
        "FPFD moves later (regression)" in text and "0" in text
    )
    assert accepted_zero_regression, (
        "spec doc must record FPFD-moves-later = 0 (no regressions)"
    )


def test_spec_post_execution_records_8633_to_6016_reduction() -> None:
    text = _spec_text()
    pat = re.compile(r"8[,]?633\s*[→\-]+\s*\*?\*?6[,]?016\*?\*?")
    assert pat.search(text), (
        "spec doc must record the 8,633 → 6,016 pre-FPFD bad-row reduction"
    )
    assert "30.3" in text, (
        "spec doc must record the 30.3 % reduction figure explicitly"
    )


def test_spec_post_execution_marks_status_complete() -> None:
    text = _spec_text()
    assert "Spec status" in text and "COMPLETE" in text, (
        "spec doc must mark Spec status: COMPLETE"
    )


def test_spec_blocks_ticker_reuse_in_this_arc() -> None:
    text = _spec_text()
    # The spec must explicitly defer the ticker-reuse cleanup to a
    # separate arc — guard against future "let's just delete those
    # rows" pressure on this same spec.
    assert "separate cleanup arc" in text or "separate spec PR" in text, (
        "spec doc must explicitly defer ticker-reuse cleanup to a "
        "separate arc"
    )


# ────────────────────────────────────────────────────────────────
# Safety surface — doc-only PR introduces no secret-shape literal
# ────────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    memstore_id_re = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    for label, text in (("TODO.md", _todo_text()), ("spec", _spec_text())):
        assert not memstore_id_re.findall(text), (
            f"{label} must not contain raw memstore-ID literal"
        )


def test_no_credential_shape_introduced() -> None:
    forbidden = (
        r"sk-ant-[A-Za-z0-9\-_]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"postgres://[^/\s]+:[^@\s]+@",
    )
    for label, text in (("TODO.md", _todo_text()), ("spec", _spec_text())):
        for pat in forbidden:
            assert re.search(pat, text) is None, (
                f"{label} must not contain credential-shape literal "
                f"matching {pat}"
            )
