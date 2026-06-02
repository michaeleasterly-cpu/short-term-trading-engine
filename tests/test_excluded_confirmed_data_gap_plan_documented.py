"""`excluded_confirmed_data_gap` validator-semantics plan documentation sentinel.

Pins the load-bearing claims of the plan PR so a future "tidy" pass
cannot silently drop:

* The resolved operator decisions (180-day freshness, dry-run no-write,
  inference clamp SPLIT, on-demand cadence only).
* The migration shape (PK + outcome CHECK + source CHECK).
* The 4 outcome enum values + 3 source enum values.
* The evidence-populator stage knobs + defaults.
* The validator join semantics (freshness + dual-source + no fetch_failure).
* The CheckResult frozen-shape preservation.
* The _infer_missing_period_ends non-change in this arc.
* AEVA / ARDT / AGPU edge-case handling.
* Rollback / no-op safety + to_regclass existence check.
* Hard rules.
* Operator live-run sequence post-impl.

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PLAN = _REPO / "docs" / "superpowers" / "plans" / (
    "2026-06-02-excluded-confirmed-data-gap-validator-semantics-plan.md"
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
# §2 — resolved operator decisions
# ─────────────────────────────────────────────────────────────


def test_plan_resolves_freshness_180_days() -> None:
    text = _plan_text()
    accepted = (
        "**180 days**" in text
        or "CONFIRMED_DATA_GAP_FRESHNESS_DAYS = 180" in text
        or "180 days" in text
    )
    assert accepted, (
        "plan must resolve the freshness window to 180 days"
    )


def test_plan_resolves_dry_run_no_writes() -> None:
    text = _plan_text()
    accepted = (
        "Dry-run MUST NOT write" in text
        or "dry-run never writes" in text.lower()
        or "DOES NOT write" in text
        or "does NOT write" in text
    )
    assert accepted, (
        "plan must require dry-run to perform no DB writes"
    )


def test_plan_splits_inference_clamp_to_separate_arc() -> None:
    text = _plan_text()
    accepted = (
        "SPLIT to separate arc" in text
        or "split from this" in text.lower()
        or "does NOT modify `_infer_missing_period_ends`" in text
    )
    assert accepted, (
        "plan must split the inference clamp to a separate arc"
    )


def test_plan_resolves_on_demand_cadence_only() -> None:
    text = _plan_text()
    accepted = (
        "Operator-on-demand one-shot" in text
        or "one-shot first" in text
        or "No scheduler / background service" in text.lower()
        or "No scheduler / background daemon integration" in text
    )
    assert accepted, (
        "plan must resolve cadence to operator-on-demand one-shot only"
    )


# ─────────────────────────────────────────────────────────────
# §3 — migration shape
# ─────────────────────────────────────────────────────────────


def test_plan_names_migration_revision() -> None:
    text = _plan_text()
    assert "20260602_0200" in text, (
        "plan must name the migration revision slot"
    )
    assert "platform.fundamentals_period_source_evidence" in text, (
        "plan must name the new table"
    )


def test_plan_specifies_primary_key() -> None:
    text = _plan_text()
    assert "PRIMARY KEY (ticker, period_end_date, source)" in text, (
        "plan must specify the PK triple"
    )


def test_plan_specifies_outcome_check_constraint() -> None:
    text = _plan_text()
    for outcome in ("'yielded'", "'empty'", "'extract_none'", "'fetch_failure'"):
        assert outcome in text, (
            f"plan must list outcome enum value {outcome!r}"
        )


def test_plan_specifies_source_check_constraint() -> None:
    text = _plan_text()
    for source in ("'fmp_historical'", "'fmp_refresh'", "'sec_companyfacts'"):
        assert source in text, (
            f"plan must list source enum value {source!r}"
        )


# ─────────────────────────────────────────────────────────────
# §4 — outcome semantics
# ─────────────────────────────────────────────────────────────


def test_plan_classifies_qualifying_outcomes() -> None:
    text = _plan_text()
    # The outcome table must distinguish qualifying vs non-qualifying outcomes.
    assert "Qualifies for exclusion?" in text, (
        "plan must explicitly classify each outcome's exclusion eligibility"
    )


def test_plan_rejects_fetch_failure_qualification() -> None:
    text = _plan_text()
    accepted = (
        "fetch_failure" in text and "needs re-attempt" in text.lower()
    )
    assert accepted, (
        "plan must reject fetch_failure as a qualifying outcome"
    )


# ─────────────────────────────────────────────────────────────
# §5 — evidence-populator stage
# ─────────────────────────────────────────────────────────────


def test_plan_names_populator_stage() -> None:
    text = _plan_text()
    assert "confirmed_data_gap_evidence_populator" in text, (
        "plan must name the new populator stage"
    )


def test_plan_populator_dry_run_default_true() -> None:
    text = _plan_text()
    accepted = (
        "`dry_run`" in text and "**Hard true**" in text
    ) or (
        '`True` (str `"true"`)' in text
    )
    assert accepted, (
        "plan must specify dry_run default True at the stage layer"
    )


def test_plan_populator_use_bulk_zip_default_true() -> None:
    text = _plan_text()
    assert "use_bulk_zip" in text, (
        "plan must specify the use_bulk_zip knob"
    )
    assert "`False` raises" in text or "raises before any HTTP call" in text, (
        "plan must require use_bulk_zip=false to raise (no per-row crawl)"
    )


def test_plan_populator_idempotent_upsert() -> None:
    text = _plan_text()
    accepted = (
        "ON CONFLICT" in text and "DO UPDATE" in text
    )
    assert accepted, (
        "plan must specify ON CONFLICT DO UPDATE for evidence upsert"
    )


# ─────────────────────────────────────────────────────────────
# §6 — bulk/S3-first invariants
# ─────────────────────────────────────────────────────────────


def test_plan_preserves_bulk_first_invariants() -> None:
    text = _plan_text()
    accepted = (
        "Bulk/S3-first invariants" in text
        or "bulk-first" in text.lower()
    )
    assert accepted, (
        "plan must preserve bulk/S3-first invariants"
    )


def test_plan_forbids_per_row_crawl() -> None:
    text = _plan_text()
    accepted = (
        "No per-row crawl" in text
        or "no per-row crawl" in text.lower()
    )
    assert accepted, (
        "plan must forbid per-row crawl"
    )


# ─────────────────────────────────────────────────────────────
# §7 — FMP/SEC handler evidence extensions
# ─────────────────────────────────────────────────────────────


def test_plan_extends_sec_handler() -> None:
    text = _plan_text()
    assert "`handle_sec_fundamentals_fallback`" in text, (
        "plan must name the SEC handler"
    )
    accepted = (
        "evidence_rows_pending" in text
        or "evidence rows" in text.lower() and "sec_companyfacts" in text
    )
    assert accepted, (
        "plan must extend the SEC handler to write evidence rows"
    )


def test_plan_extends_fmp_handler() -> None:
    text = _plan_text()
    assert "`handle_historical_fundamentals_quarterly`" in text, (
        "plan must name the FMP cascade handler"
    )


def test_plan_dry_run_skips_evidence_writes() -> None:
    text = _plan_text()
    accepted = (
        "If `dry_run=true`, evidence rows are\nNOT written" in text
        or "evidence rows are\nNOT written" in text
        or "do NOT write evidence" in text.lower()
    )
    assert accepted, (
        "plan must specify dry-run skips evidence writes"
    )


# ─────────────────────────────────────────────────────────────
# §8 — validator join logic
# ─────────────────────────────────────────────────────────────


def test_plan_specifies_validator_evidence_join_sql() -> None:
    text = _plan_text()
    for needle in (
        "fundamentals_period_source_evidence",
        "INTERVAL '180 days'",
        "fmp_historical",
        "fmp_refresh",
        "sec_companyfacts",
    ):
        assert needle in text, (
            f"plan must specify the join SQL fragment {needle!r}"
        )


def test_plan_excludes_fetch_failure_in_join() -> None:
    text = _plan_text()
    accepted = (
        "NOT bool_or(outcome = 'fetch_failure')" in text
        or "NOT bool_or(outcome='fetch_failure')" in text
        or ("fetch_failure" in text and "NOT bool_or" in text)
    )
    assert accepted, (
        "plan join SQL must explicitly exclude fetch_failure"
    )


# ─────────────────────────────────────────────────────────────
# §9 — sub-counter reporting
# ─────────────────────────────────────────────────────────────


def test_plan_introduces_sub_counter_field() -> None:
    text = _plan_text()
    assert "excluded_confirmed_data_gap_evidenced: int = 0" in text or (
        "excluded_confirmed_data_gap_evidenced" in text
    ), "plan must introduce the new sub-counter field"


def test_plan_preserves_frozen_check_result() -> None:
    text = _plan_text()
    accepted = (
        "frozen `CheckResult` itself is unchanged" in text
        or "CheckResult` stays frozen" in text
        or "No `CheckResult` shape change" in text
    )
    assert accepted, (
        "plan must preserve the frozen CheckResult invariant"
    )


# ─────────────────────────────────────────────────────────────
# §10 — dashboard surfacing
# ─────────────────────────────────────────────────────────────


def test_plan_specifies_dashboard_panel_surfacing() -> None:
    text = _plan_text()
    accepted = (
        "dashboard_components" in text
        or "dashboard panel" in text.lower()
        or "Dashboard surface" in text
    )
    assert accepted, (
        "plan must specify the dashboard surfacing of the new sub-counter"
    )


# ─────────────────────────────────────────────────────────────
# §11 — edge cases
# ─────────────────────────────────────────────────────────────


def test_plan_handles_aeva_yielded_path() -> None:
    text = _plan_text()
    assert "AEVA" in text and "yielded" in text.lower(), (
        "plan must handle AEVA's SEC-yielded path (does NOT exclude)"
    )


def test_plan_handles_ardt_watchlist_override() -> None:
    text = _plan_text()
    assert "ARDT" in text, "plan must reference ARDT"
    accepted = (
        "ARDT_WATCHLIST" in text
        or "watchlist override" in text.lower()
    )
    assert accepted, (
        "plan must add an ARDT watchlist override forcing excluded_dark"
    )


def test_plan_defers_agpu_classifier_triage() -> None:
    text = _plan_text()
    assert "AGPU" in text, "plan must reference AGPU"
    accepted = (
        "No change in implementation PR" in text
        or "Deferred classifier triage" in text
        or "No change" in text and "AGPU" in text
    )
    assert accepted, (
        "plan must defer AGPU reclassification to a separate triage"
    )


# ─────────────────────────────────────────────────────────────
# §14 — rollback / no-op
# ─────────────────────────────────────────────────────────────


def test_plan_handles_table_missing_via_to_regclass() -> None:
    text = _plan_text()
    accepted = (
        "to_regclass" in text
        or "table doesn't exist" in text.lower()
    )
    assert accepted, (
        "plan must handle the table-missing rollback case gracefully"
    )


def test_plan_specifies_additive_only_stage() -> None:
    text = _plan_text()
    accepted = (
        "additive-only" in text.lower()
        or "additive only" in text.lower()
    )
    assert accepted, (
        "plan must specify the populator stage is additive-only"
    )


# ─────────────────────────────────────────────────────────────
# §15 — operator live-run sequence
# ─────────────────────────────────────────────────────────────


def test_plan_specifies_operator_live_run_sequence() -> None:
    text = _plan_text()
    for needle in (
        "Apply migration",
        "limit=50",
        "Re-run the validator",
        "DATA_OPERATIONS_COMPLETE",
    ):
        assert needle in text, (
            f"plan must specify operator live-run step: {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# §16 — non-goals
# ─────────────────────────────────────────────────────────────


def test_plan_non_goals_carried_from_spec() -> None:
    text = _plan_text()
    for needle in (
        "No `_infer_missing_period_ends` change",
        "No validator threshold loosening",
        "No `fundamentals_quarterly` cleanup",
        "No AGPU reclassification",
    ):
        assert needle in text, (
            f"plan must carry non-goal {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# TODO.md — arc state marker
# ─────────────────────────────────────────────────────────────


def test_todo_marks_spec_plus_plan_landed() -> None:
    text = _todo_text()
    accepted = (
        "SPEC + PLAN LANDED" in text
        or "spec + plan arc" in text.lower()
    )
    assert accepted, (
        "TODO must mark the arc as spec+plan landed"
    )


def test_todo_records_resolved_decisions() -> None:
    text = _todo_text()
    for needle in (
        "Freshness window: 180 days",
        "Dry-run NEVER writes",
        "Inference clamp: SPLIT",
        "Backfill cadence: operator-on-demand one-shot",
    ):
        assert needle in text, (
            f"TODO must record resolved decision {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# Safety surface — plan batch carries no secret-shape literal
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
