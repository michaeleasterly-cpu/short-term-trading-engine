"""`excluded_confirmed_data_gap` validator-semantics spec documentation sentinel.

Pins the load-bearing claims of the spec so a future "tidy" pass cannot
silently drop:

* The dual-source-evidence prerequisite (FMP + SEC both attempted + both empty).
* The freshness gate (default 180 days).
* The new evidence substrate (`platform.fundamentals_period_source_evidence`).
* Period-level (NOT ticker- or bulk-level) exclusion.
* The frozen `CheckResult` invariant + sub-counter strategy.
* No threshold loosening.
* No `_infer_missing_period_ends` change in this arc.
* No live SEC fallback writes / cleanup / quarantine / delete.
* Operator-decisions deferred to plan PR.

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SPEC = _REPO / "docs" / "superpowers" / "specs" / (
    "2026-06-02-excluded-confirmed-data-gap-validator-semantics.md"
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
# §1 — verdict + existing-bucket extension framing
# ─────────────────────────────────────────────────────────────


def test_spec_extends_existing_bucket_not_new_name() -> None:
    text = _spec_text()
    assert "excluded_confirmed_data_gap" in text, (
        "spec must reference the existing bucket name"
    )
    accepted = (
        "extends its semantic" in text
        or "already exists" in text.lower()
    )
    assert accepted, (
        "spec must frame this as extending the existing bucket, not "
        "creating a new one"
    )


def test_spec_records_current_narrow_semantic_lines() -> None:
    text = _spec_text()
    # The spec must cite the existing < 2 filings + past grace path.
    accepted = (
        "< 2 filings" in text
        or "< 2 filings AND first-filing past grace" in text
    )
    assert accepted, (
        "spec must record the current narrow `< 2 filings + past grace` "
        "semantic so future readers understand what is being extended"
    )


# ─────────────────────────────────────────────────────────────
# §3 — empirical evidence motivating the extension
# ─────────────────────────────────────────────────────────────


def test_spec_cites_pr_448_and_pr_449() -> None:
    text = _spec_text()
    for pr in ("PR #448", "PR #449"):
        assert pr in text, f"spec must cite {pr}"


def test_spec_records_one_point_four_percent_yield_and_117_cohort() -> None:
    text = _spec_text()
    assert "1.4%" in text, (
        "spec must record the SEC-spike SEC-fillable yield"
    )
    assert "117" in text, "spec must reference the 117-row cohort"


def test_spec_records_144_per_ticker_fails() -> None:
    text = _spec_text()
    assert "144" in text, (
        "spec must reference the 144 per-ticker FAIL baseline"
    )


# ─────────────────────────────────────────────────────────────
# §4 — dual-source-attempted prerequisite
# ─────────────────────────────────────────────────────────────


def test_spec_requires_dual_source_evidence() -> None:
    text = _spec_text()
    # Both providers must have been attempted + both empty.
    assert "FMP attempt evidence" in text, (
        "spec must require FMP attempt evidence"
    )
    assert "SEC attempt evidence" in text, (
        "spec must require SEC attempt evidence"
    )


def test_spec_requires_freshness_gate() -> None:
    text = _spec_text()
    accepted = (
        "180 days" in text
        or "CONFIRMED_DATA_GAP_FRESHNESS_DAYS" in text
        or "freshness gate" in text.lower()
    )
    assert accepted, (
        "spec must require a freshness gate on evidence rows"
    )


def test_spec_rejects_outage_shaped_evidence() -> None:
    text = _spec_text()
    accepted = (
        "fetch_failure" in text
        or "fetch failure" in text.lower()
        or "DataProviderOutage" in text
    )
    assert accepted, (
        "spec must reject fetch-failure outcomes as qualifying evidence"
    )


# ─────────────────────────────────────────────────────────────
# §5 — evidence substrate
# ─────────────────────────────────────────────────────────────


def test_spec_proposes_new_evidence_table() -> None:
    text = _spec_text()
    assert "platform.fundamentals_period_source_evidence" in text, (
        "spec must name the new evidence-substrate table"
    )
    assert "PRIMARY KEY" in text, (
        "spec must specify the PK for the evidence table"
    )


def test_spec_rejects_data_quality_log_alternative() -> None:
    text = _spec_text()
    accepted = (
        "Alternative considered: extend `data_quality_log`" in text
        or "Rejected because" in text
        or "rejected" in text.lower() and "data_quality_log" in text
    )
    assert accepted, (
        "spec must explicitly consider AND reject the data_quality_log "
        "alternative substrate"
    )


# ─────────────────────────────────────────────────────────────
# §6 + §10 — validator wiring + CheckResult frozen
# ─────────────────────────────────────────────────────────────


def test_spec_preserves_frozen_check_result() -> None:
    text = _spec_text()
    accepted = (
        "CheckResult` stays frozen" in text
        or "CheckResult is frozen" in text
        or "frozen=True" in text
    )
    assert accepted, (
        "spec must preserve the CheckResult frozen invariant"
    )


def test_spec_introduces_sub_counter() -> None:
    text = _spec_text()
    assert "excluded_confirmed_data_gap_evidenced" in text, (
        "spec must introduce the new sub-counter"
    )


def test_spec_period_level_not_ticker_level() -> None:
    text = _spec_text()
    accepted = (
        "Period-level exclusion" in text
        or "period-level" in text.lower()
    )
    assert accepted, (
        "spec must specify period-level (not ticker-level) exclusion"
    )


# ─────────────────────────────────────────────────────────────
# §8 — edge cases
# ─────────────────────────────────────────────────────────────


def test_spec_handles_aeva_spac_merger_q1_case() -> None:
    text = _spec_text()
    assert "AEVA" in text, "spec must reference the AEVA SPAC-merger case"
    assert "yielded" in text or "would yield" in text.lower(), (
        "spec must explain that AEVA's SEC hit means it does NOT qualify "
        "for exclusion"
    )


def test_spec_handles_annual_filers() -> None:
    text = _spec_text()
    accepted = (
        "Annual filers" in text
        or "annual filers" in text.lower()
        or "20-F" in text
        or "40-F" in text
    )
    assert accepted, (
        "spec must cover the annual-filer edge case"
    )


def test_spec_handles_agpu_spac_classifier_deferral() -> None:
    text = _spec_text()
    assert "AGPU" in text, "spec must reference the AGPU edge case"
    accepted = (
        "until reclassified" in text.lower()
        or "until SPAC merger completes" in text
        or "deferred follow-up" in text.lower()
    )
    assert accepted, (
        "spec must defer AGPU reclassification"
    )


# ─────────────────────────────────────────────────────────────
# §11 — defenses against hiding gaps
# ─────────────────────────────────────────────────────────────


def test_spec_lists_four_defenses() -> None:
    text = _spec_text()
    for needle in (
        "Freshness gate",
        "Operator-facing surfacing",
        "Audit trail",
        "Sentinel tests",
    ):
        assert needle in text, (
            f"spec must list defense: {needle!r}"
        )


def test_spec_records_dashboard_surfacing_obligation() -> None:
    text = _spec_text()
    assert "dashboard" in text.lower(), (
        "spec must record the dashboard surfacing obligation"
    )


# ─────────────────────────────────────────────────────────────
# §14 — non-goals
# ─────────────────────────────────────────────────────────────


def test_spec_no_threshold_loosening() -> None:
    text = _spec_text()
    assert "No threshold loosening" in text, (
        "spec must explicitly reject threshold loosening"
    )


def test_spec_no_inference_change() -> None:
    text = _spec_text()
    assert "_infer_missing_period_ends" in text, (
        "spec must name the inference function"
    )
    accepted = (
        "No `_infer_missing_period_ends` change" in text
        or "No _infer_missing_period_ends change" in text
        or "does NOT change `_infer_missing_period_ends`" in text
    )
    assert accepted, (
        "spec must defer inference changes to a separate arc"
    )


def test_spec_no_live_sec_fallback_run() -> None:
    text = _spec_text()
    accepted = (
        "No live SEC fallback run" in text
        or "no live SEC fallback" in text.lower()
    )
    assert accepted, (
        "spec must explicitly reject a live SEC fallback in this PR"
    )


def test_spec_no_cleanup_quarantine_delete() -> None:
    text = _spec_text()
    accepted = (
        "No cleanup / quarantine / delete" in text
        or "no cleanup, quarantine, or delete" in text.lower()
        or "No cleanup / quarantine" in text
    )
    assert accepted, (
        "spec must explicitly reject cleanup/quarantine/delete"
    )


# ─────────────────────────────────────────────────────────────
# §15 — open operator decisions
# ─────────────────────────────────────────────────────────────


def test_spec_lists_open_operator_decisions() -> None:
    text = _spec_text()
    assert "## 15. Open operator decisions" in text, (
        "spec must carry the §15 open-questions section"
    )
    for needle in (
        "Freshness window",
        "Dry-run population",
        "Inference clamp",
    ):
        assert needle in text, (
            f"spec must list open operator decision: {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# TODO.md closeout marker
# ─────────────────────────────────────────────────────────────


def test_todo_records_spec_arc_landed() -> None:
    text = _todo_text()
    assert "`excluded_confirmed_data_gap` validator-semantics spec arc" in text, (
        "TODO must record the spec arc entry"
    )
    accepted = (
        "SPEC LANDED" in text
        or "[lane: heavy]" in text
    )
    assert accepted, (
        "TODO must mark spec landed + heavy lane gate state"
    )


def test_todo_references_fundamentals_period_source_evidence_table() -> None:
    text = _todo_text()
    assert "platform.fundamentals_period_source_evidence" in text, (
        "TODO must reference the new evidence table"
    )


# ─────────────────────────────────────────────────────────────
# Safety surface — spec batch carries no secret-shape literal
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
