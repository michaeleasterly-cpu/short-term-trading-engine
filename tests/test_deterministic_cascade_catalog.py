"""Sentinel test for the deterministic self-heal cascade catalog.

Locks in the deterministic-first design from
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-expansion-design.md``:
every failure shape that the spec marks DONE must have its cascade
function AND its terminal event name actually present in the
codebase. If a future PR adds a new failure-mode row to the spec
without wiring the deterministic recovery, this test reds CI before
the LLM persona gets to backstop it (which would be the wrong design
per the operator directive 2026-05-21 — deterministic-first).

The catalog below is the SOURCE OF TRUTH for this test. When a new
Wave PR lands, add a row here. When a spec row is implemented (status
**DONE**), add it here. Drift between the spec and this catalog is
caught by ``test_catalog_matches_spec_done_rows``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_OPS = REPO_ROOT / "scripts" / "ops.py"
ENGINE_DISPATCH = REPO_ROOT / "ops" / "engine_dispatch.py"
ORDER_TRANSIENT_RETRY = REPO_ROOT / "tpcore" / "order_management" / "transient_retry.py"
# Wave-4 cascade-hosting source files.
AAR_DEFERRED = REPO_ROOT / "tpcore" / "aar" / "deferred.py"
AAR_WRITER = REPO_ROOT / "tpcore" / "aar" / "writer.py"
LIFECYCLE_PAUSE = REPO_ROOT / "tpcore" / "risk" / "lifecycle_pause.py"
EXECUTION_RISK_SKIP = REPO_ROOT / "tpcore" / "order_management" / "execution_risk_skip.py"
SPEC = REPO_ROOT / "docs" / "superpowers" / "specs" / "2026-05-21-deterministic-self-heal-coverage-expansion-design.md"
# Search across all cascade-hosting source files. Data-lane lives in scripts/ops.py;
# engine-lane lives in ops/engine_dispatch.py (E1/E9) + tpcore/order_management/
# transient_retry.py (E3). E2 (per-engine setup_detection retry) lives in
# tpcore/engine/transient_retry.py — the helper is opted-into by individual engine
# plugs. Wave-4 added E4 (tpcore/aar/deferred.py + writer.py), E7+E11
# (tpcore/risk/lifecycle_pause.py), E10 (tpcore/order_management/
# execution_risk_skip.py).
CASCADE_SOURCE_FILES = [
    SCRIPTS_OPS,
    ENGINE_DISPATCH,
    ORDER_TRANSIENT_RETRY,
    AAR_DEFERRED,
    AAR_WRITER,
    LIFECYCLE_PAUSE,
    EXECUTION_RISK_SKIP,
]


# ROW_ID → (must-have-symbol-in-scripts/ops.py, must-have-event-name-in-scripts/ops.py)
# Symbol may be a function name (private OK), a constant, or a map key — the test
# does a simple substring search on the actual source text. Event name must appear
# verbatim as a string literal (the event is emitted via db_log.log).
#
# Add rows as Waves land. Initial seed reflects state on main 2026-05-22 after
# PRs #227, #231, #236, #235, #239, #260, #261, #262.
EXPECTED_CASCADES: dict[str, tuple[str, str]] = {
    # D1 coverage_collapse — orchestrator cascade (PR #227 + #231 smart-feed)
    "D1": ("_auto_cascade_coverage_collapse", "INGESTION_AUTO_RECOVERED"),
    # D2 daily_bars stage timeout → re-invoke with chunked path (PR #262)
    "D2": ("_cascade_d2_daily_bars_timeout", "INGESTION_AUTO_RECOVERED_TIMEOUT"),
    # D3 connection drop mid-stage → orchestrator-level re-invoke once (PR #262)
    "D3": ("_cascade_d3_connection_drop", "INGESTION_AUTO_RECOVERED_CONNDROP"),
    # D4 SIP transient 403 — handled inside D1's cascade (probe → IEX), not a
    # separate cascade function. The SIP probe lives in ops.py.
    "D4": ("_alpaca_sip_available", "INGESTION_AUTO_RECOVERY_DEGRADED"),
    # D5 provider 401 — retry-then-skip-cleanly (PR #262)
    "D5": ("_cascade_d5_provider_auth", "PROVIDER_AUTH_ESCALATED"),
    # D6 validation suite partial failure (PR #261)
    "D6": ("_VALIDATION_CASCADE_MAP", "INGESTION_AUTO_RECOVERED_VALIDATION"),
    # D7 monotonicity violation (PR #261)
    "D7": ("_MONOTONE_CASCADE_MAP", "INGESTION_AUTO_RECOVERED_MONOTONE"),
    # D8 macro per-indicator gap (PR #261)
    "D8": ("_MACRO_COMPLETENESS_CHECK", "INGESTION_AUTO_RECOVERED_MACRO_GAP"),
    # D9 liquidity_tiers missing tickers (PR #261)
    "D9": ("liquidity_tiers_completeness", "INGESTION_AUTO_RECOVERED_TIER"),
    # D10 ticker_classifications missing (PR #261)
    "D10": ("ticker_classifications_coverage", "INGESTION_AUTO_RECOVERED_CLASSIFICATION"),
    # D11 freshness vendor_late classification — orchestrator-level recognition
    # using tpcore.selfheal.probes.VENDOR_PROBES; skip-without-failing for
    # known weekly-publish feeds (AAII Thursday, fear_greed daily).
    "D11": ("_auto_cascade_vendor_late", "INGESTION_VENDOR_LATE_SKIPPED"),
    # D13 pool exhaustion → recycle_asyncpg_pool + retry (PR #262)
    "D13": ("_cascade_d13_pool_exhaustion", "POOL_CIRCUIT_BREAKER_TRIPPED"),
    # D14 data_validation stage timeout → chunked re-run + synthesize FAILED
    # entry whose error matches the Wave-1 cascade's parser contract.
    "D14": ("_chunk_validation_suite", "INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED"),
    # E1 engine scheduler stage failure → retry once + ENGINE_STAGE_ESCALATED (PR #267)
    "E1": ("ENGINE_STAGE_ESCALATED_EVENT", "ENGINE_STAGE_ESCALATED"),
    # E3 order placement transient → retry + ORDER_ESCALATED (PR #267)
    "E3": ("submit_with_transient_retry", "ORDER_ESCALATED"),
    # E4 AAR write failure → defer to platform.aar_deferred + AAR_DEFERRED (Wave-4)
    "E4": ("DeferredAARWriter", "AAR_DEFERRED"),
    # E7 credibility drop → N=3 consecutive sub-floor scores → ENGINE_CREDIBILITY_DROP + ENGINE_HELD (Wave-4)
    "E7": ("check_credibility_drop", "ENGINE_CREDIBILITY_DROP"),
    # E9 engine package import error → ENGINE_IMPORT_FAILED + skip cycle (PR #267)
    "E9": ("ENGINE_IMPORT_FAILED_EVENT", "ENGINE_IMPORT_FAILED"),
    # E10 per-trade execution_risk failure → cancel + EXECUTION_RISK_ESCALATED + skip trade (Wave-4)
    "E10": ("execute_with_risk_skip", "EXECUTION_RISK_ESCALATED"),
    # E11 lifecycle degraded → N=5 consecutive sub-floor scores → ENGINE_LIFECYCLE_DEGRADED + ENGINE_HELD (Wave-4)
    "E11": ("check_lifecycle_degraded", "ENGINE_LIFECYCLE_DEGRADED"),
}


def _ops_py_source() -> str:
    """Concatenated source across all cascade-hosting files."""
    return "\n".join(p.read_text(encoding="utf-8") for p in CASCADE_SOURCE_FILES)


def _spec_source() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_every_catalogued_cascade_has_its_function_or_symbol() -> None:
    """Every (row_id, symbol) tuple must appear in scripts/ops.py."""
    source = _ops_py_source()
    missing: list[str] = []
    for row_id, (symbol, _event) in EXPECTED_CASCADES.items():
        if symbol not in source:
            missing.append(f"{row_id}: symbol {symbol!r} not found in scripts/ops.py")
    assert not missing, (
        "Catalogued cascade rows have lost their implementing symbol — drift between "
        "the spec catalog and the code. If you removed a symbol, either restore it OR "
        "remove the row from EXPECTED_CASCADES + mark the spec row no longer DONE:\n"
        + "\n".join(missing)
    )


def test_every_catalogued_cascade_has_its_event_name() -> None:
    """Every (row_id, event_name) tuple must appear in scripts/ops.py as a string literal."""
    source = _ops_py_source()
    missing: list[str] = []
    for row_id, (_symbol, event) in EXPECTED_CASCADES.items():
        # Event name must appear as a quoted string literal (the emitter writes it
        # via db_log.log("EVENT_NAME", ...)). Allow both single and double quotes.
        if f'"{event}"' not in source and f"'{event}'" not in source:
            missing.append(f"{row_id}: event {event!r} not found as quoted literal in scripts/ops.py")
    assert not missing, (
        "Catalogued cascade rows have lost their event-name emit — the cascade may "
        "still run but won't produce its terminal event. Restore the event emit OR "
        "update EXPECTED_CASCADES to reflect the new event name:\n"
        + "\n".join(missing)
    )


def test_catalog_matches_spec_done_rows() -> None:
    """Every row marked DONE in the spec catalog must appear in EXPECTED_CASCADES.

    The spec is the source of truth. If a row is marked DONE there but not in
    this test's catalog, that's a sentinel-drift defect — the test isn't
    actually enforcing the spec's claim.
    """
    spec_text = _spec_source()
    # Match rows like `| D6 | ... | ... | None | **NEW:** ... |` OR `| D1 | ... | **DONE** ... |`
    # Find rows where the rightmost column contains DONE or DONE-VIA-#
    row_re = re.compile(r"^\|\s*(D\d+|E\d+)\s*\|", re.MULTILINE)
    spec_rows = {m.group(1) for m in row_re.finditer(spec_text)}

    # A row is "DONE" if its rightmost column contains **DONE** or **DONE-VIA-#NNN**
    # (the convention from the spec). Use full-row match to look at the rightmost cell.
    done_row_re = re.compile(
        r"^\|\s*(D\d+|E\d+)\s*\|.*?\|.*?\|.*?\|\s*\*\*DONE(?:-VIA-#\d+)?\*\*",
        re.MULTILINE,
    )
    done_in_spec = {m.group(1) for m in done_row_re.finditer(spec_text)}

    # Every row in EXPECTED_CASCADES must be a spec row (sanity)
    catalog_only = set(EXPECTED_CASCADES) - spec_rows
    assert not catalog_only, (
        f"Catalog has rows not present in the spec: {catalog_only}. "
        "Either add the rows to the spec or remove from this catalog."
    )

    # Every row marked DONE in the spec must be in EXPECTED_CASCADES.
    # Exclusions — rows whose "deterministic recovery" exists but lives outside
    # the CASCADE_SOURCE_FILES set (so the symbol+event search model doesn't apply):
    #   D12 — CSV archive R3 is env-pluggable backend, not a cascade
    #   E2 — per-engine setup_detection retry uses tpcore/engine/transient_retry.py
    #        but the per-engine opt-in lives in each engine's setup_detection plug;
    #        a dedicated test_engine_transient_retry_pilot pins reversion's wire
    #   E5 — capital_gate raise → skip-cycle behavior in tpcore/quality/validation/capital_gate.py
    #   E6 — drawdown breach → RiskGovernor auto-pause in tpcore/risk/
    #   E8 — stale-order auto-cancel in tpcore/order_management/stale_order_cancel.py
    backend_done = {"D12", "E2", "E5", "E6", "E8"}
    spec_done_missing = (done_in_spec - set(EXPECTED_CASCADES)) - backend_done
    assert not spec_done_missing, (
        f"Spec marks these rows DONE but they're missing from EXPECTED_CASCADES: "
        f"{spec_done_missing}. Either add them to the catalog (with symbol + event) "
        "or update the spec to no longer claim DONE."
    )


def test_event_names_unique() -> None:
    """Each cascade row's event name must be unique. Two rows emitting the same
    event = ambiguous telemetry (operator can't tell which cascade fired)."""
    events = [event for (_sym, event) in EXPECTED_CASCADES.values()]
    duplicates = {e for e in events if events.count(e) > 1}
    assert not duplicates, (
        f"Multiple cascade rows share an event name: {duplicates}. "
        "Each cascade must have a unique terminal event so application_log "
        "telemetry is unambiguous."
    )
