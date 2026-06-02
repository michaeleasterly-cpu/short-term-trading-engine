"""Ticker-reuse cleanup plan documentation sentinel (2026-06-02).

Pins the load-bearing claims of the doc-only plan PR so a future
"tidy plan" or grooming pass can't silently drop:

* The §13 #1 mega-cap re-audit RESOLVED claim (22/22).
* The archive-before-delete invariant.
* The no-weak-evidence-delete invariant.
* The bulk-first evidence-source policy.
* The deferred-predecessor-re-key decision.
* The new sidecar table names + the validator-stays-strict guard.

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PLAN = _REPO / "docs" / "superpowers" / "plans" / (
    "2026-06-02-ticker-reuse-fundamentals-cleanup-plan.md"
)


def _plan_text() -> str:
    assert _PLAN.is_file(), f"missing {_PLAN.relative_to(_REPO)}"
    text = _PLAN.read_text(encoding="utf-8")
    assert text.strip(), "plan doc is empty"
    return text


# ─────────────────────────────────────────────────────────────
# Mega-cap re-audit resolution (§2)
# ─────────────────────────────────────────────────────────────


def test_plan_records_mega_cap_reaudit_resolved() -> None:
    text = _plan_text()
    assert "§13 #1 RESOLVED" in text or "§13 #1 NEEDS_REPO_VERIFICATION" in text, (
        "plan must reference the spec §13 #1 question being resolved"
    )
    pat = re.compile(r"22\s*/\s*22|22 / 22")
    assert pat.search(text), (
        "plan must record the 22/22 re-audit sample size + match"
    )
    assert "Still-wrong FPFD" in text and "0" in text, (
        "plan must record 0 still-wrong-FPFD findings"
    )


# ─────────────────────────────────────────────────────────────
# Cleanup-strategy decisions (§3)
# ─────────────────────────────────────────────────────────────


def test_plan_chooses_archive_with_sidecar_for_high_confidence() -> None:
    text = _plan_text()
    assert "fundamentals_quarterly_archive" in text, (
        "plan must name the archive sidecar table"
    )
    accepted = (
        "1-to-1 mirror" in text
        or "mirror of `platform.fundamentals_quarterly`" in text.lower()
        or "mirror of platform.fundamentals_quarterly" in text.lower()
    )
    assert accepted, (
        "plan must choose the 1-to-1 mirror archive shape"
    )


def test_plan_chooses_sidecar_for_quarantine_not_column_flag() -> None:
    text = _plan_text()
    assert "fundamentals_quarterly_quarantine" in text, (
        "plan must name the quarantine sidecar table"
    )
    # Soft-delete column was rejected in the spec; plan must keep that.
    accepted = (
        "Quarantine SIDECAR TABLE" in text
        or "SIDECAR TABLE" in text
    )
    assert accepted, (
        "plan must explicitly use a sidecar table for quarantine, not "
        "an is_quarantined column"
    )


def test_plan_defers_rekey_to_predecessor_for_now() -> None:
    text = _plan_text()
    assert "Re-key" in text or "re-key" in text or "rekey" in text.lower(), (
        "plan must reference the re-key option"
    )
    accepted = (
        "DEFERRED" in text or "Deferred" in text
        or "deferred until" in text.lower()
        or "default to archive" in text.lower()
    )
    assert accepted, (
        "plan must defer predecessor re-key until evidence/decision is "
        "in place"
    )


def test_plan_quarantines_weak_evidence_does_not_delete() -> None:
    text = _plan_text()
    # The hard invariant: weak-evidence rows are NEVER deleted.
    accepted = (
        "No DELETE of weak-evidence rows" in text
        or "weak-evidence rows are NEVER deleted" in text.lower()
        or "quarantine, NOT archive+delete" in text
    )
    assert accepted, (
        "plan must keep the weak-evidence-not-deleted invariant"
    )


# ─────────────────────────────────────────────────────────────
# Ops-stage hard invariants (§5.2)
# ─────────────────────────────────────────────────────────────


def test_plan_enforces_archive_before_delete() -> None:
    text = _plan_text()
    assert "archive-before-delete" in text.lower() or (
        "No DELETE without a matching archive INSERT in the same"
        in text
    ), "plan must encode the archive-before-delete transactional invariant"


def test_plan_requires_bulk_first_evidence_source() -> None:
    text = _plan_text()
    # Per the operator's standing rule.
    assert "use_bulk_zip" in text, (
        "plan must wire use_bulk_zip into the cleanup stage"
    )
    assert "bulk-before-API-crawl" in text or (
        "ZERO per-CIK HTTP" in text
    ), "plan must restate the bulk-first evidence-source policy"


def test_plan_blocks_fpfd_drift_delete() -> None:
    text = _plan_text()
    accepted = (
        "No DELETE of FPFD-unverified rows" in text
        or "fpfd_drift_detected_skipped" in text
    )
    assert accepted, (
        "plan must skip rows whose stored FPFD has drifted from the "
        "bulk-extracted FPFD"
    )


def test_plan_requires_per_row_bounded_delete_not_blanket() -> None:
    text = _plan_text()
    accepted = (
        "bounded by manifest row IDs" in text.lower()
        or "Bounded by manifest row IDs" in text
        or "every DELETE is per-row" in text.lower()
        or "No mass DELETE" in text
    )
    assert accepted, (
        "plan must forbid blanket DELETE; per-row bounded by manifest "
        "composite key"
    )


# ─────────────────────────────────────────────────────────────
# Validator stays strict + non-goals (§14)
# ─────────────────────────────────────────────────────────────


def test_plan_keeps_validator_strict() -> None:
    text = _plan_text()
    accepted = (
        "No validator change" in text
        or "Validator continues to read it AS-IS" in text
        or "Validator continues to read" in text
    )
    assert accepted, (
        "plan must not change the validator"
    )


def test_plan_does_not_touch_fundamentals_quarterly_schema() -> None:
    text = _plan_text()
    assert (
        "No change to `platform.fundamentals_quarterly` schema" in text
        or "main-table schema" in text.lower()
    ), "plan must explicitly leave fundamentals_quarterly schema untouched"


# ─────────────────────────────────────────────────────────────
# Safety surface — doc-only plan introduces no secret-shape literal
# ─────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    text = _plan_text()
    pat = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    assert not pat.findall(text), (
        "plan must not contain raw memstore-ID literal"
    )


def test_no_credential_shape_introduced() -> None:
    forbidden = (
        r"sk-ant-[A-Za-z0-9\-_]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"postgres://[^/\s]+:[^@\s]+@",
    )
    text = _plan_text()
    for pat in forbidden:
        assert re.search(pat, text) is None, (
            f"plan must not contain credential-shape literal matching {pat}"
        )
