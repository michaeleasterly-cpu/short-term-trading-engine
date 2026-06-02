"""§12.2 empirical-stop sentinel.

Pins the load-bearing claims of the 2026-06-02 TODO/spec update that
captures the §12.2 bounded-live result: the
``historical_fundamentals_quarterly`` stage is mechanically correct
but the FMP source does not contain the inferred missing periods, so
the §12.3 full live must remain blocked. A future "tidy TODO" or spec
rewrite cannot silently drop these claims.

Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TODO = _REPO / "TODO.md"
_SPEC = _REPO / "docs" / "superpowers" / "specs" / (
    "2026-06-02-fundamentals-cadence-fail-triage.md"
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


# ─────────────────────────────────────────────────────────────────────
# TODO.md must document §12.2 STOP + no full live
# ─────────────────────────────────────────────────────────────────────


def test_todo_records_12_2_empirically_stopped() -> None:
    text = _todo_text()
    assert "§12.2" in text, (
        "TODO.md must reference §12.2 explicitly so a future operator "
        "session can grep for it"
    )
    assert "EMPIRICALLY STOPPED" in text or "empirically stopped" in text, (
        "TODO.md must mark the §12.2 result as empirically stopped"
    )


def test_todo_forbids_12_3_full_live() -> None:
    text = _todo_text()
    assert "§12.3" in text, (
        "TODO.md must reference §12.3 (the blocked full-live step)"
    )
    has_block = (
        "is NOT recommended" in text
        or "NOT to be run" in text
        or "is not recommended" in text.lower()
    )
    assert has_block, (
        "TODO.md must say §12.3 full live is NOT recommended / NOT to be run"
    )


def test_todo_records_144_to_144_no_improvement() -> None:
    text = _todo_text()
    # Accept either of the two natural framings — Markdown-rendered
    # table cells separate the two 144s; raw text uses an arrow.
    accepted_framings = (
        "144 → 144",
        "144 -> 144",
        "144 → 144",  # explicit unicode codepoint
        "Total per-ticker FAIL count           | **144 → 144**",
    )
    assert any(f in text for f in accepted_framings), (
        "TODO.md must record the 144 → 144 (delta = 0) empirical result"
    )
    assert "delta = 0" in text or "delta=0" in text, (
        "TODO.md must make the delta = 0 framing explicit so the no-improvement "
        "finding can't be missed at a skim"
    )


def test_todo_records_9_of_10_unchanged() -> None:
    text = _todo_text()
    accepted_framings = (
        "9 others UNCHANGED",
        "9 others unchanged",
        "1 of 10",
    )
    assert any(f in text for f in accepted_framings), (
        "TODO.md must record the 9/10 unchanged (or equivalent 1/10 improvement) "
        "empirical signal that drives the STOP decision"
    )


def test_todo_keeps_c1_c2_validator_semantics_arc_open() -> None:
    text = _todo_text()
    assert "C1" in text and "C2" in text, (
        "TODO.md must keep the C1 (recent-filer) and C2 (annual-filer) "
        "validator-semantics arc visible"
    )
    has_open_signal = (
        "validator-semantics arc remains" in text
        or "Validator-semantics" in text
        or "still applies" in text
    )
    assert has_open_signal, (
        "TODO.md must signal that C1+C2 validator-semantics work is still "
        "open / unblocked"
    )


def test_todo_records_ardt_physical_truth_anomaly() -> None:
    text = _todo_text()
    assert "ARDT" in text, "TODO.md must surface the ARDT-specific follow-up"
    assert "physical_truth" in text, (
        "TODO.md must name the physical_truth gate so the follow-up is "
        "discoverable"
    )


# ─────────────────────────────────────────────────────────────────────
# Spec doc must record the post-execution result + verdict correction
# ─────────────────────────────────────────────────────────────────────


def test_spec_records_post_execution_section() -> None:
    text = _spec_text()
    assert "Post-execution result" in text, (
        "spec doc must include the post-execution result section"
    )
    for needle in ("§12.2", "23.5 s", "physical_truth"):
        assert needle in text, (
            f"spec doc post-execution section must include {needle!r}"
        )


def test_spec_states_stage_mechanically_correct() -> None:
    text = _spec_text()
    accepted_framings = (
        "stage is mechanically correct",
        "mechanically sufficient but empirically insufficient",
        "mechanically correct. The source data is the problem",
    )
    assert any(f in text for f in accepted_framings), (
        "spec doc must explicitly state the stage is mechanically correct so a "
        "future reader doesn't blame the stage code"
    )


def test_spec_blocks_12_3_full_live() -> None:
    text = _spec_text()
    assert "§12.3 explicitly blocked" in text or "NOT to be run" in text, (
        "spec doc must state §12.3 is explicitly blocked"
    )


def test_spec_corrects_b_bucket_taxonomy() -> None:
    text = _spec_text()
    assert "FMP-unreachable historical residual" in text, (
        "spec doc must reclassify the B-bucket as FMP-unreachable historical "
        "residual"
    )
    assert "B-bucket" in text or "B — likely historical backfill" in text, (
        "spec doc must name the original B-bucket label being corrected"
    )


# ─────────────────────────────────────────────────────────────────────
# Safety surface — doc-only PR introduces no secret-shape literal
# ─────────────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    memstore_id_re = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    for label, text in (("TODO.md", _todo_text()), ("spec", _spec_text())):
        assert not memstore_id_re.findall(text), (
            f"{label} must not contain raw memstore-ID literal"
        )


def test_no_anthropic_api_write_surface_introduced() -> None:
    forbidden = (
        # write-side memstore endpoint shape
        re.compile(r"POST\s+https://api\.anthropic\.com/v1/memory_stores"),
        re.compile(r"PUT\s+https://api\.anthropic\.com/v1/memory_stores"),
        re.compile(r"DELETE\s+https://api\.anthropic\.com/v1/memory_stores"),
    )
    for label, text in (("TODO.md", _todo_text()), ("spec", _spec_text())):
        for pat in forbidden:
            assert pat.search(text) is None, (
                f"{label} must not introduce an Anthropic memstore write "
                f"surface (matched {pat.pattern})"
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
                f"{label} must not contain credential-shape literal matching "
                f"{pat}"
            )
