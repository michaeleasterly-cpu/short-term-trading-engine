"""Identity substrate / data-flow audit documentation sentinel (2026-06-03).

Pins the load-bearing facts of the audit doc so a future "tidy" pass cannot
silently drop:

* The 49 platform tables / 15 SCD-2 assignment triggers baseline.
* The prices_daily mis-attribution counts (1,296,359 pre-FPFD bars across
  1,149 tickers; 92,318 rows with a different valid ticker_history cls;
  19,964 rows outside the ticker_history valid window).
* The fundamentals_quarterly defect counts (6,017 pre-FPFD rows across
  775 tickers; 2,153 NULL classification_id rows; 109 duplicate logical
  quarters; 1,034 rows without active classification).
* The lifetime_start sentinel + 16.6% FPFD coverage findings.
* The fundamentals_period_source_evidence polluted/paused status.
* The identity-first repair order.
* The moratorium rules (no new tables; no validator patches first).
* The docs-only invariant of this PR (no DB writes, no migrations,
  no code changes).

Stdlib only. No DB. No network.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AUDIT = _REPO / "docs" / "audits" / "2026-06-03-identity-substrate-data-flow.md"
_TODO = _REPO / "TODO.md"


def _audit_text() -> str:
    assert _AUDIT.is_file(), f"missing {_AUDIT.relative_to(_REPO)}"
    text = _AUDIT.read_text(encoding="utf-8")
    assert text.strip(), "audit doc is empty"
    return text


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), "TODO.md is empty"
    return text


# ─────────────────────────────────────────────────────────────
# §1 — Verdict + baseline numbers
# ─────────────────────────────────────────────────────────────


def test_audit_states_49_platform_tables() -> None:
    text = _audit_text()
    assert "49 platform tables" in text, (
        "audit must state 49 platform tables"
    )


def test_audit_states_15_scd2_assignment_triggers() -> None:
    text = _audit_text()
    accepted = (
        "15 SCD-2 assignment triggers" in text
        or "15 platform tables carry `BEFORE INSERT` triggers" in text
        or "15 platform tables" in text and "BEFORE INSERT" in text
    )
    assert accepted, (
        "audit must state the 15 SCD-2 assignment trigger finding"
    )


def test_audit_states_write_side_attribution_correct() -> None:
    text = _audit_text()
    accepted = (
        "write-side identity-attribution architecture is **correct**" in text
        or "Write-side" in text and "correct" in text.lower()
    )
    assert accepted, (
        "audit must state that write-side attribution architecture is correct"
    )


# ─────────────────────────────────────────────────────────────
# §2.5 — ticker_history sparsity
# ─────────────────────────────────────────────────────────────


def test_audit_states_ticker_history_96_pct_single_row() -> None:
    text = _audit_text()
    accepted = (
        "17,554 tickers (96%) have exactly 1 history row" in text
        or "96% of tickers in `ticker_history` have a single open-ended row" in text
        or ("96%" in text and "single" in text.lower())
    )
    assert accepted, (
        "audit must state the 96% single-row ticker_history finding"
    )


# ─────────────────────────────────────────────────────────────
# §2.6 — prices_daily numbers
# ─────────────────────────────────────────────────────────────


def test_audit_states_prices_daily_pre_fpfd_count() -> None:
    text = _audit_text()
    assert "1,296,359" in text, (
        "audit must state 1,296,359 pre-FPFD bars"
    )
    assert "1,149 tickers" in text, (
        "audit must state 1,149 affected tickers"
    )


def test_audit_states_prices_daily_92318_different_valid_cls() -> None:
    text = _audit_text()
    assert "92,318" in text, (
        "audit must state the 92,318 different-valid-cls bar count"
    )
    assert "266 tickers" in text, (
        "audit must state the 266-ticker scope of the 92,318 finding"
    )


def test_audit_states_prices_daily_19964_outside_window() -> None:
    text = _audit_text()
    assert "19,964" in text, (
        "audit must state 19,964 bars outside ticker_history valid window"
    )


# ─────────────────────────────────────────────────────────────
# §2.7 — fundamentals_quarterly numbers
# ─────────────────────────────────────────────────────────────


def test_audit_states_fq_6017_pre_fpfd() -> None:
    text = _audit_text()
    assert "6,017" in text, "audit must state 6,017 pre-FPFD FQ rows"
    assert "775 tickers" in text, "audit must state 775 affected tickers"


def test_audit_states_fq_2153_null_cls() -> None:
    text = _audit_text()
    assert "2,153" in text, (
        "audit must state 2,153 fundamentals_quarterly rows with NULL "
        "classification_id"
    )


def test_audit_states_fq_109_duplicate_quarters() -> None:
    text = _audit_text()
    assert "109" in text, "audit must state 109 duplicate logical quarters"


def test_audit_states_fq_1034_orphan_rows() -> None:
    text = _audit_text()
    assert "1,034" in text, (
        "audit must state 1,034 fundamentals_quarterly rows without "
        "active classification"
    )


# ─────────────────────────────────────────────────────────────
# §2.3 — identity master coverage
# ─────────────────────────────────────────────────────────────


def test_audit_states_lifetime_start_sentinel() -> None:
    text = _audit_text()
    accepted = (
        "100% of active rows" in text and "1900-01-01" in text
        or "lifetime_start = '1900-01-01'" in text
        or "lifetime_start" in text and "sentinel" in text.lower()
    )
    assert accepted, (
        "audit must state the lifetime_start sentinel finding"
    )


def test_audit_states_fpfd_coverage_16_6_percent() -> None:
    text = _audit_text()
    accepted = (
        "FPFD: **16.6%**" in text
        or "16.6%" in text and "FPFD" in text
    )
    assert accepted, (
        "audit must state the 16.6% FPFD coverage"
    )


def test_audit_states_issuer_securities_sparse() -> None:
    text = _audit_text()
    assert "89" in text and "issuer_securities" in text, (
        "audit must reference issuer_securities's 89-row sparsity"
    )


# ─────────────────────────────────────────────────────────────
# §2.12 — recent side effects
# ─────────────────────────────────────────────────────────────


def test_audit_marks_evidence_table_polluted() -> None:
    text = _audit_text()
    accepted = (
        "POLLUTED" in text and "fundamentals_period_source_evidence" in text
        or "polluted" in text.lower() and "fundamentals_period_source_evidence" in text
    )
    assert accepted, (
        "audit must mark fundamentals_period_source_evidence as polluted"
    )


def test_audit_states_reset_before_reuse() -> None:
    text = _audit_text()
    accepted = (
        "Reset before reuse" in text
        or "reset before reuse" in text.lower()
    )
    assert accepted, (
        "audit must direct reset-before-reuse of the polluted evidence table"
    )


# ─────────────────────────────────────────────────────────────
# §2.9 — read-side bypass
# ─────────────────────────────────────────────────────────────


def test_audit_states_engine_bypass() -> None:
    text = _audit_text()
    assert "Bypass" in text, "audit must call out the engine bypass"
    for needle in (
        "price_loader",
        "PricesRepo",
        "momentum/backtest",
        "catalyst/backtest",
    ):
        assert needle in text, (
            f"audit must name the bypass consumer: {needle}"
        )


def test_audit_states_broker_routing_not_contaminated() -> None:
    text = _audit_text()
    accepted = (
        "Order routing not contaminated" in text
        or "not directly polluted" in text.lower()
        or "Risk path not contaminated" in text
    )
    assert accepted, (
        "audit must state broker/order routing is not directly contaminated"
    )


# ─────────────────────────────────────────────────────────────
# §2.13 — DATA_OPERATIONS_COMPLETE
# ─────────────────────────────────────────────────────────────


def test_audit_states_doc_blocked_by_fundamentals_completeness() -> None:
    text = _audit_text()
    assert "DATA_OPERATIONS_COMPLETE" in text, (
        "audit must reference DATA_OPERATIONS_COMPLETE"
    )
    assert "fundamentals_quarterly_completeness" in text, (
        "audit must name the blocking check"
    )
    assert "111" in text, "audit must state the 111-ticker FAIL count"


# ─────────────────────────────────────────────────────────────
# §3 — table classification matrix
# ─────────────────────────────────────────────────────────────


def test_audit_classifies_key_tables() -> None:
    text = _audit_text()
    # The minimum 15 tables operator-listed in the task spec.
    must_classify = [
        "ticker_classifications",
        "ticker_history",
        "issuer_securities",
        "prices_daily",
        "fundamentals_quarterly",
        "corporate_actions",
        "corporate_events",
        "ticker_lifecycle_events",
        "issuer_history",
        "fundamentals_period_source_evidence",
        "fundamentals_quarterly_archive",
        "fundamentals_quarterly_quarantine",
        "failed_alpha_ledger",
        "data_quality_log",
        "application_log",
    ]
    for t in must_classify:
        assert t in text, (
            f"audit table classification must include {t!r}"
        )


def test_audit_uses_classification_categories() -> None:
    text = _audit_text()
    for cat in (
        "KEEP",
        "MERGE_CANDIDATE",
        "POLLUTED_RESET_CANDIDATE",
        "EMPTY_SPECULATIVE",
    ):
        assert cat in text, (
            f"audit must use the classification category {cat}"
        )


# ─────────────────────────────────────────────────────────────
# §4 — moratorium rules
# ─────────────────────────────────────────────────────────────


def test_audit_lists_moratorium_no_new_tables() -> None:
    text = _audit_text()
    accepted = (
        "No new platform tables" in text
        or "No new data tables" in text
        or "freeze new table creation" in text.lower()
    )
    assert accepted, (
        "audit must include moratorium on new platform tables"
    )


def test_audit_lists_moratorium_no_validator_patches_first() -> None:
    text = _audit_text()
    accepted = (
        "No validator patches before identity-substrate repair" in text
        or "No validator patches before identity" in text
    )
    assert accepted, (
        "audit must include moratorium on validator patches before identity repair"
    )


def test_audit_lists_moratorium_no_populator_runs() -> None:
    text = _audit_text()
    accepted = (
        "No `confirmed_data_gap_evidence_populator` runs" in text
        or "No confirmed_data_gap_evidence_populator runs" in text
    )
    assert accepted, (
        "audit must include moratorium on populator runs until reset"
    )


def test_audit_lists_moratorium_no_fmp_primary_for_us_ciks() -> None:
    text = _audit_text()
    accepted = (
        "No FMP-primary identity repair for U.S. CIK-backed issuers" in text
        or "SEC/CIK is authoritative for U.S. issuers" in text
    )
    assert accepted, (
        "audit must include moratorium on FMP-primary identity for US CIK-backed issuers"
    )


# ─────────────────────────────────────────────────────────────
# §5 — repair order
# ─────────────────────────────────────────────────────────────


def test_audit_repair_order_identity_first() -> None:
    text = _audit_text()
    accepted = (
        "identity-first" in text.lower()
        or "Identity-first" in text
        or "Repair sequence is deliberately **identity-first**" in text
    )
    assert accepted, (
        "audit must state repair is identity-first"
    )


def test_audit_repair_order_references_scd2_update_pass() -> None:
    text = _audit_text()
    accepted = (
        "SCD-2 trigger logic as an idempotent `UPDATE` pass" in text
        or "SCD-2 trigger logic as an idempotent UPDATE pass" in text
        or "trigger logic as an idempotent UPDATE pass" in text
    )
    assert accepted, (
        "audit must include the SCD-2 trigger UPDATE-pass step"
    )


def test_audit_repair_order_step_count() -> None:
    text = _audit_text()
    # Step 1 ... Step 15 numbered list.
    for n in range(1, 16):
        # Match "1." through "15." at the start of repair-order lines.
        # The audit doc uses Markdown ordered-list "N." format.
        assert re.search(rf"^{n}\.\s", text, flags=re.MULTILINE), (
            f"audit repair order must include numbered step {n}"
        )


# ─────────────────────────────────────────────────────────────
# §6 — docs-only invariant
# ─────────────────────────────────────────────────────────────


def test_audit_states_no_db_writes_no_migrations() -> None:
    text = _audit_text()
    for needle in (
        "No DB writes",
        "No migrations",
        "No code changes",
    ):
        assert needle in text, (
            f"audit must state docs-only invariant: {needle!r}"
        )


# ─────────────────────────────────────────────────────────────
# TODO.md — moratorium echo + repair-order pointer
# ─────────────────────────────────────────────────────────────


def test_todo_carries_audit_entry() -> None:
    text = _todo_text()
    accepted = (
        "## ⚑ Identity substrate / data-flow audit — 2026-06-03" in text
        or "Identity substrate / data-flow audit" in text
    )
    assert accepted, (
        "TODO.md must carry the audit entry"
    )


def test_todo_echoes_moratorium_rules() -> None:
    text = _todo_text()
    for needle in (
        "No new platform tables",
        "No validator patches before identity-substrate repair",
        "No FMP-primary identity repair for U.S. CIK-backed issuers",
    ):
        assert needle in text, (
            f"TODO.md must echo moratorium rule: {needle!r}"
        )


def test_todo_pauses_confirmed_data_gap_arc() -> None:
    text = _todo_text()
    accepted = (
        "**PAUSED 2026-06-03**" in text
        or "PAUSED 2026-06-03" in text
    )
    assert accepted, (
        "TODO.md must mark the confirmed_data_gap arc as PAUSED"
    )


# ─────────────────────────────────────────────────────────────
# Safety surface — doc batch carries no secret-shape literal
# ─────────────────────────────────────────────────────────────


def test_no_raw_memstore_id_introduced() -> None:
    text = _audit_text() + _todo_text()
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
    text = _audit_text() + _todo_text()
    for pat in forbidden:
        assert re.search(pat, text) is None, (
            f"doc batch must not contain credential-shape literal "
            f"matching {pat}"
        )
