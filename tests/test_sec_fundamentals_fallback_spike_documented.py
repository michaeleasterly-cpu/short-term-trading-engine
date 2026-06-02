"""SEC fundamentals-fallback spike empirical-result documentation sentinel.

Pins the load-bearing numbers from the 2026-06-02 dry-run spike so a
future "tidy" pass cannot silently drop:

* 9 of 10 tickers in scope (AGPU excluded as ``asset_class='spac'``).
* 72 inferred missing periods across the 9 in-scope tickers.
* 1 archive_rows_planned across the whole spike.
* 1.4% SEC-fillable yield.
* AEVA at 2021-03-31 is the only source-fillable hit.
* SPAC-merger Q1 pattern (CIK retained from InterPrivate II).
* 8 of 9 tickers source-unavailable via SEC companyfacts.
* No live writes / manifest_lifecycle / cache.upsert_payload calls.
* Operator decision: STOP automated cleanup; next arc is
  excluded_confirmed_data_gap validator-semantics, not live fallback.

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SPEC = _REPO / "docs" / "superpowers" / "specs" / (
    "2026-06-02-fundamentals-cadence-fail-triage.md"
)
_TODO = _REPO / "TODO.md"


def _spec_text() -> str:
    assert _SPEC.is_file(), f"missing {_SPEC.relative_to(_REPO)}"
    text = _SPEC.read_text(encoding="utf-8")
    assert text.strip(), "spec doc is empty"
    return text


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), "TODO.md is empty"
    return text


# ─────────────────────────────────────────────────────────────
# Spec doc — post-merge SEC fallback spike section
# ─────────────────────────────────────────────────────────────


def test_spec_carries_post_merge_sec_fallback_section() -> None:
    text = _spec_text()
    assert "## Post-merge SEC fallback spike result" in text, (
        "spec must carry the post-merge SEC fallback spike section"
    )


def test_spec_records_pr_448_merge_and_dryrun_enablement() -> None:
    text = _spec_text()
    assert "PR #448" in text and "88a7d36" in text, (
        "spec must record PR #448 merge + commit hash"
    )
    assert "`dry_run`" in text or "dry_run" in text, (
        "spec must explain dry_run enablement"
    )


def test_spec_records_universe_filter_and_nine_in_scope() -> None:
    text = _spec_text()
    # 10 tickers requested; 9 in scope after asset_class='stock' filter.
    assert "tier2_with_cik=9" in text or "**9**" in text, (
        "spec must record the 9-ticker in-scope universe"
    )
    assert "asset_class='stock'" in text, (
        "spec must name the universe predicate"
    )


def test_spec_records_agpu_spac_exclusion() -> None:
    text = _spec_text()
    assert "AGPU" in text and "spac" in text.lower(), (
        "spec must record AGPU's asset_class='spac' exclusion"
    )
    assert "Axe Compute" in text, (
        "spec must record the AGPU legal_name finding"
    )


def test_spec_records_inferred_missing_count_72() -> None:
    text = _spec_text()
    assert "72" in text, "spec must record 72 inferred missing periods"


def test_spec_records_archive_rows_planned_equals_1() -> None:
    text = _spec_text()
    # The decisive empirical: 1 archive_rows_planned across the whole spike.
    accepted = (
        "archive_rows_planned`** | **1**" in text
        or "archive_rows_planned=1" in text
        or "**1**" in text and "AEVA" in text
    )
    assert accepted, (
        "spec must record the SEC archive_rows_planned=1 result"
    )


def test_spec_records_one_point_four_percent_yield() -> None:
    text = _spec_text()
    assert "1.4 %" in text or "1.4%" in text, (
        "spec must record the 1.4% SEC-fillable yield"
    )


def test_spec_records_aeva_2021_03_31_spac_merger_q1() -> None:
    text = _spec_text()
    assert "AEVA" in text and "2021-03-31" in text, (
        "spec must name AEVA's source-fillable period"
    )
    assert "SPAC-merger Q1" in text or "SPAC merger" in text.lower(), (
        "spec must classify AEVA as a SPAC-merger Q1 pattern"
    )
    assert "InterPrivate" in text, (
        "spec must record InterPrivate II as the predecessor SPAC"
    )


def test_spec_records_eight_of_nine_source_unavailable() -> None:
    text = _spec_text()
    accepted = (
        "8 of 9" in text
        or "8 / 9" in text
        or "8/9" in text
        or "8 in-scope tickers" in text
        or "8 of 9 in-scope tickers" in text
    )
    assert accepted, (
        "spec must record 8 of 9 in-scope tickers source-unavailable"
    )


def test_spec_records_no_live_writes_invariant() -> None:
    text = _spec_text()
    for needle in (
        "0 DB writes",
        "`manifest_lifecycle` NOT called",
        "`cache.upsert_payload` NOT called",
    ):
        assert needle in text, (
            f"spec must record no-live-writes invariant: {needle!r}"
        )


def test_spec_records_117_cohort_source_unavailable_verdict() -> None:
    text = _spec_text()
    assert "117" in text, "spec must reference the 117-row cohort"
    accepted = (
        "SOURCE-UNAVAILABLE" in text
        or "source-unavailable" in text
    )
    assert accepted, (
        "spec must record the cohort source-unavailable verdict"
    )


def test_spec_routes_next_arc_to_excluded_confirmed_data_gap() -> None:
    text = _spec_text()
    assert "excluded_confirmed_data_gap" in text, (
        "spec must name the excluded_confirmed_data_gap follow-up arc"
    )
    accepted = (
        "NOT a live SEC fallback run" in text
        or "NOT a live SEC fallback" in text
    )
    assert accepted, (
        "spec must explicitly reject a live SEC fallback as the next arc"
    )


def test_spec_defers_agpu_classifier_reclassification() -> None:
    text = _spec_text()
    accepted = (
        "Not authorized to reclassify" in text
        or "Deferred follow-up" in text
        or "deferred" in text.lower() and "AGPU" in text
    )
    assert accepted, (
        "spec must defer AGPU reclassification as a separate follow-up"
    )


# ─────────────────────────────────────────────────────────────
# TODO.md — arc closeout marker
# ─────────────────────────────────────────────────────────────


def test_todo_records_sec_fallback_spike_closeout() -> None:
    text = _todo_text()
    assert "SEC fallback spike" in text, (
        "TODO must record the SEC fallback spike entry"
    )
    accepted = (
        "ARC CLOSEOUT" in text
        or "arc state: SEC fallback insufficient" in text
        or "[lane: closed for cohort cleanup]" in text
    )
    assert accepted, (
        "TODO must mark the arc as closed for cohort cleanup"
    )


def test_todo_carries_per_metric_spike_table() -> None:
    text = _todo_text()
    for needle in (
        "archive_rows_planned",
        "1.4 %",
        "tier ≤ 2",
        "SOURCE-UNAVAILABLE",
    ):
        assert needle in text, (
            f"TODO must carry the spike-metric line for {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# Safety surface — doc batch carries no secret-shape literal
# ─────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    text = _spec_text() + _todo_text()
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
    text = _spec_text() + _todo_text()
    for pat in forbidden:
        assert re.search(pat, text) is None, (
            f"doc batch must not contain credential-shape literal "
            f"matching {pat}"
        )
