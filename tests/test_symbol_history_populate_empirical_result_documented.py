"""Symbol-history populate empirical-result documentation sentinel (2026-06-02).

Pins the empirical numbers from the live populate + the 5-bucket
post-populate cleanup dry-runs so a future "tidy" pass cannot silently
lose:

* The live populate deltas (PR #444 + #445 forward fix).
* The bucket dry-run disposition matrix showing rank-3 NEVER fires.
* The "0 of 1,304 weak-keep rows would flip under reframe" finding.
* The cleanup-arc STOPPED disposition + why.
* The richer-source / different-framing unblock options.

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PLAN = _REPO / "docs" / "superpowers" / "plans" / (
    "2026-06-02-symbol-history-evidence-backfill-plan.md"
)
_TODO = _REPO / "TODO.md"


def _plan_text() -> str:
    assert _PLAN.is_file(), f"missing {_PLAN.relative_to(_REPO)}"
    text = _PLAN.read_text(encoding="utf-8")
    assert text.strip(), "plan doc is empty"
    return text


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), "TODO.md is empty"
    return text


# ─────────────────────────────────────────────────────────────
# §16 — Post-populate empirical result
# ─────────────────────────────────────────────────────────────


def test_plan_carries_section_16_post_populate_result() -> None:
    text = _plan_text()
    assert "## 16. Post-populate empirical result" in text, (
        "plan must carry the §16 post-populate closeout section"
    )


def test_plan_records_live_populate_deltas() -> None:
    text = _plan_text()
    for needle in (
        "+5,173",  # ticker_history delta (1 short of 5,174 forecast)
        "+64",     # issuer_securities delta
        "+5,164",  # ticker_classifications delta
        "+5,076",  # data_quality_log delta
    ):
        assert needle in text, (
            f"plan must record live populate delta {needle!r}"
        )


def test_plan_records_fundamentals_quarterly_invariant_held() -> None:
    text = _plan_text()
    # Two near-mentions in §16.1 (table row) and §16.4 (narrative).
    accepted = (
        "**invariant held**" in text or "invariant held" in text.lower()
    )
    assert accepted, (
        "plan must record fundamentals_quarterly invariant held"
    )


# ─────────────────────────────────────────────────────────────
# §16.2 — bucket dry-run disposition matrix
# ─────────────────────────────────────────────────────────────


def test_plan_records_six_thousand_sixteen_total_candidates() -> None:
    text = _plan_text()
    # The 6,016 total spans the residual that motivated the entire arc.
    assert "6,016" in text or "6016" in text, (
        "plan must record the 6,016 total candidate-row count"
    )


def test_plan_records_zero_high_confidence_across_all_buckets() -> None:
    text = _plan_text()
    # The per-bucket counts surface 0 high_confidence in every row of
    # the §16.2 matrix; ensure that finding is asserted in narrative form.
    accepted = (
        "rank-3 NEVER fires" in text
        or "rank-3 never fires" in text.lower()
        or "0 high_confidence" in text
    )
    assert accepted, (
        "plan must state rank-3 never fires across the populated substrate"
    )


def test_plan_records_per_bucket_candidate_counts() -> None:
    text = _plan_text()
    # The §16.2 table cells.
    for n in ("74", "267", "2,536", "1,255", "1,884"):
        assert n in text, (
            f"plan must record per-bucket candidate count {n}"
        )


# ─────────────────────────────────────────────────────────────
# §16.3 — structural finding
# ─────────────────────────────────────────────────────────────


def test_plan_records_rank3_substrate_empty_for_weak_keep() -> None:
    text = _plan_text()
    # The decisive finding: 0 of 1,304 weak-keep rows have rank-3 substrate.
    for needle in ("1,304", "551", "would flip to", "0** (zero) of those 551"):
        accepted = needle in text or needle.replace(",", "") in text
        assert accepted, (
            f"plan must record the rank-3-reframe-zero-impact finding "
            f"({needle!r})"
        )


def test_plan_explains_fmp_only_substrate_skips_issuer_securities() -> None:
    text = _plan_text()
    accepted = (
        "FMP-only-minted classification_ids that **deliberately skip**"
        in text
        or "FMP-only-minted classification_ids that deliberately skip"
        in text
    )
    assert accepted, (
        "plan must explain why FMP-only substrate doesn't populate "
        "issuer_securities (the rank-3-blocking structural reason)"
    )


def test_plan_records_path_c_resolution_rate_one_three_percent() -> None:
    text = _plan_text()
    accepted = (
        "1.3%" in text
        or "68 of 5,334" in text
        or "68/5,334" in text
    )
    assert accepted, (
        "plan must record the Path C resolution rate (68/5,334 ≈ 1.3%) "
        "as the upstream coverage bottleneck"
    )


# ─────────────────────────────────────────────────────────────
# §16.4 — decision: arc STOPPED
# ─────────────────────────────────────────────────────────────


def test_plan_marks_arc_stopped() -> None:
    text = _plan_text()
    accepted = (
        "arc STOPPED" in text
        or "Status: CLOSEOUT" in text
        or "arc closeout" in text.lower()
    )
    assert accepted, (
        "plan must mark the cleanup arc as STOPPED / CLOSEOUT"
    )


def test_plan_rejects_classifier_reframe_on_empirical_grounds() -> None:
    text = _plan_text()
    accepted = (
        "rank-priority reframe is NOT JUSTIFIED" in text
        or "reframe is NOT JUSTIFIED" in text
        or "reframe would change zero dispositions" in text
        or "reframe would change nothing" in text.lower()
    )
    assert accepted, (
        "plan must record that the classifier reframe is NOT justified "
        "by the empirical evidence"
    )


def test_plan_lists_unblock_options() -> None:
    text = _plan_text()
    for needle in (
        "Richer ticker→issuer historical-mapping source",
        "different cleanup framing",
    ):
        assert needle in text, (
            f"plan must list the unblock option {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# TODO.md closeout marker
# ─────────────────────────────────────────────────────────────


def test_todo_marks_arc_stopped() -> None:
    text = _todo_text()
    accepted = (
        "[lane: closed] [gate: none — arc STOPPED]" in text
        or "arc STOPPED" in text
    )
    assert accepted, (
        "TODO.md must mark the symbol-history arc as STOPPED with the "
        "lane:closed marker"
    )


def test_todo_records_post_populate_disposition_counts() -> None:
    text = _todo_text()
    for n in ("4,688 (77.9%)", "1,328 (22.1%)", "6,016", "783"):
        accepted = n in text or n.replace(",", "") in text
        assert accepted, (
            f"TODO.md must record the aggregated disposition count {n!r}"
        )


# ─────────────────────────────────────────────────────────────
# Safety surface — doc-only batch introduces no secret-shape literal
# ─────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    text = _plan_text() + _todo_text()
    pat = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    assert not pat.findall(text), (
        "doc batch must not contain raw memstore-ID literal"
    )


def test_no_credential_shape_introduced() -> None:
    forbidden = (
        r"sk-ant-[A-Za-z0-9\-_]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"postgres://[^/\s]+:[^@\s]+@",
    )
    text = _plan_text() + _todo_text()
    for pat in forbidden:
        assert re.search(pat, text) is None, (
            f"doc batch must not contain credential-shape literal "
            f"matching {pat}"
        )
