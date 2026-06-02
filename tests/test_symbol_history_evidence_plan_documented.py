"""Symbol-history evidence backfill plan documentation sentinel (2026-06-02).

Pins the load-bearing claims of the doc-only plan PR so a future "tidy
plan" or grooming pass can't silently drop:

* Path B primary (FMP /stable/symbol-change bulk).
* Path C resolver (SEC submissions.zip cross-walk).
* Path A deferred (R2 roster snapshots unavailable).
* TKR-14 historical predecessor mint discipline.
* Bulk/S3-first artifact handling + archive parity check.
* Idempotent ticker_history + issuer_securities upserts.
* No per-ticker crawl.
* No cleanup / quarantine / delete in this PR or the implementation PR.
* The 1969-12-31 sentinel-date handling.
* Same-CIK ticker change vs different-issuer reuse decision.

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PLAN = _REPO / "docs" / "superpowers" / "plans" / (
    "2026-06-02-symbol-history-evidence-backfill-plan.md"
)


def _plan_text() -> str:
    assert _PLAN.is_file(), f"missing {_PLAN.relative_to(_REPO)}"
    text = _PLAN.read_text(encoding="utf-8")
    assert text.strip(), "plan doc is empty"
    return text


# ─────────────────────────────────────────────────────────────
# Source-path decisions (§2)
# ─────────────────────────────────────────────────────────────


def test_plan_picks_path_b_fmp_symbol_change_primary() -> None:
    text = _plan_text()
    assert "/stable/symbol-change" in text, (
        "plan must name the FMP /stable/symbol-change endpoint"
    )
    assert "PRIMARY" in text or "primary" in text, (
        "plan must mark Path B as primary"
    )


def test_plan_picks_path_c_sec_submissions_crosswalk_resolver() -> None:
    text = _plan_text()
    assert "submissions.zip" in text, (
        "plan must name SEC submissions.zip as the cross-walk source"
    )
    assert "SECSubmissionsBulkReader" in text, (
        "plan must name the existing bulk-reader class for Path C"
    )


def test_plan_defers_path_a_r2_roster_snapshots() -> None:
    text = _plan_text()
    assert "Path A" in text and (
        "UNAVAILABLE" in text or "DEFERRED" in text or "deferred" in text
    ), "plan must mark Path A as unavailable/deferred"


def test_plan_records_fmp_endpoint_empirical_shape() -> None:
    text = _plan_text()
    assert "5,334" in text or "5334" in text, (
        "plan must record the empirical FMP symbol-change row count"
    )
    assert "1969-12-31" in text, (
        "plan must record the FMP sentinel-date floor"
    )


# ─────────────────────────────────────────────────────────────
# TKR-14 historical predecessor mint discipline (§2.3)
# ─────────────────────────────────────────────────────────────


def test_plan_uses_tkr14_deterministic_mint_for_predecessors() -> None:
    text = _plan_text()
    assert "TKR-14" in text or "tkr14" in text.lower(), (
        "plan must reference the TKR-14 smart-key for predecessor mint"
    )
    assert "tpcore.identity.tkr14.mint" in text, (
        "plan must point at the existing mint function"
    )


def test_plan_uses_z_sentinel_venue_for_unknown_historical_venue() -> None:
    text = _plan_text()
    # Quoted "Z" appears as the sentinel; the table row spells it out.
    assert '"Z"' in text, (
        "plan must use the Z sentinel ipo_venue for historical mints"
    )


def test_plan_predecessor_lifetime_end_nonnull() -> None:
    text = _plan_text()
    assert "lifetime_end" in text and "non-NULL" in text, (
        "plan must require historical predecessor rows to carry "
        "lifetime_end non-NULL"
    )


# ─────────────────────────────────────────────────────────────
# Bulk/S3-first invariants (§7)
# ─────────────────────────────────────────────────────────────


def test_plan_enforces_bulk_s3_first_read() -> None:
    text = _plan_text()
    assert "Archive-first read" in text or "archive-first read" in text.lower(), (
        "plan must encode the archive-first read invariant"
    )
    assert "Archive-after-download" in text or "archive-after-download" in text.lower(), (
        "plan must encode the archive-after-download invariant"
    )


def test_plan_requires_local_archive_parity_check() -> None:
    text = _plan_text()
    accepted = (
        "Local/archive parity check" in text
        or "local/archive parity" in text.lower()
        or "sha256" in text.lower()
    )
    assert accepted, (
        "plan must require a local/archive parity check before DB writes"
    )


def test_plan_forbids_per_ticker_crawl() -> None:
    text = _plan_text()
    assert "No per-ticker crawl" in text or "no per-ticker crawl" in text.lower(), (
        "plan must forbid per-ticker crawl"
    )
    assert "use_bulk_zip" in text, (
        "plan must wire the use_bulk_zip hard-true sentinel"
    )


# ─────────────────────────────────────────────────────────────
# Idempotent writes (§5)
# ─────────────────────────────────────────────────────────────


def test_plan_ticker_history_upsert_is_idempotent() -> None:
    text = _plan_text()
    assert "ticker_history" in text, "plan must name ticker_history"
    assert "ON CONFLICT" in text and "DO NOTHING" in text, (
        "plan must specify ON CONFLICT DO NOTHING for idempotency"
    )


def test_plan_issuer_securities_upsert_is_idempotent() -> None:
    text = _plan_text()
    assert "issuer_securities" in text, "plan must name issuer_securities"
    # Both upserts mention the natural key triple structure.
    nk_patterns = (
        r"\(issuer_id,\s*classification_id,\s*valid_from\)",
    )
    for pat in nk_patterns:
        assert re.search(pat, text), (
            f"plan must specify the natural key {pat} for issuer_securities"
        )


# ─────────────────────────────────────────────────────────────
# Same-CIK ticker change vs different-issuer reuse (§3.3)
# ─────────────────────────────────────────────────────────────


def test_plan_distinguishes_same_cik_from_different_issuer() -> None:
    text = _plan_text()
    assert "same-CIK ticker change" in text or "same_cik_ticker_change" in text, (
        "plan must call out same-CIK ticker change as a distinct disposition"
    )
    assert "different-issuer ticker reuse" in text or "different_issuer_reuse" in text, (
        "plan must call out different-issuer ticker reuse as a distinct disposition"
    )


def test_plan_does_not_authorize_high_confidence_delete_on_same_cik() -> None:
    text = _plan_text()
    accepted = (
        "no high_confidence delete" in text.lower()
        or "weak evidence" in text.lower()
        or "not high-confidence" in text.lower()
    )
    assert accepted, (
        "plan must keep same-CIK ticker change out of the high_confidence path"
    )


# ─────────────────────────────────────────────────────────────
# 1969-12-31 sentinel-date handling (§9)
# ─────────────────────────────────────────────────────────────


def test_plan_handles_1969_sentinel_date_explicitly() -> None:
    text = _plan_text()
    assert "1969-12-31" in text, "plan must name the sentinel date"
    assert "data_quality_log" in text and "fmp_symbol_change_sentinel_date" in text, (
        "plan must route sentinel-date rows to data_quality_log"
    )


# ─────────────────────────────────────────────────────────────
# Non-goals + cleanup-stage boundary (§10 + §14)
# ─────────────────────────────────────────────────────────────


def test_plan_does_not_authorize_cleanup_quarantine_or_delete() -> None:
    text = _plan_text()
    # The plan PR + implementation PR are evidence-population only.
    accepted = (
        "evidence-population only" in text.lower()
        or "No cleanup, quarantine, or delete" in text
        or "no cleanup, quarantine, or delete" in text.lower()
    )
    assert accepted, (
        "plan must state cleanup/quarantine/delete are not authorized "
        "in this PR or the implementation PR"
    )


def test_plan_keeps_validator_strict() -> None:
    text = _plan_text()
    assert "No validator change" in text or "validator stays strict" in text.lower(), (
        "plan must not relax the validator"
    )


def test_plan_does_not_touch_fundamentals_quarterly_schema() -> None:
    text = _plan_text()
    accepted = (
        "No fundamentals_quarterly schema change" in text
        or "no fundamentals_quarterly schema change" in text.lower()
    )
    assert accepted, (
        "plan must keep fundamentals_quarterly schema untouched"
    )


# ─────────────────────────────────────────────────────────────
# Post-backfill cleanup re-run (§10)
# ─────────────────────────────────────────────────────────────


def test_plan_defers_cleanup_rerun_to_separate_pr() -> None:
    text = _plan_text()
    assert "Post-backfill cleanup re-run" in text or "cleanup re-run" in text.lower(), (
        "plan must reference the post-backfill cleanup re-run as a "
        "separate downstream PR"
    )
    assert "cleanup_ticker_reuse_fundamentals" in text, (
        "plan must name the cleanup stage that will be re-run later"
    )


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
