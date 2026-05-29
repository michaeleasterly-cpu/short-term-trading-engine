"""FinalLaneVerdict control plane — TEST-001..TEST-010 from the
2026-05-29 long_term_data_operations_control_plane_fix task spec.

The audit (2026-05-29) found:
  - cmd_update's Wave-1 cascade NEVER re-runs data_validation after
    healing reds. val_result stays FAILED; UpdateSummary.exit_code
    stays 1; the wrapper aborts before Step 6; DATA_OPERATIONS_COMPLETE
    never emits.
  - The fix introduces a FinalLaneVerdict that is the SINGLE source of
    truth for downstream gates (process exit code, wrapper Step 6
    emission, engine sweep eligibility). _build_final_lane_verdict
    re-runs data_validation ONCE after the cascade and constructs the
    verdict from the proven result.

This file pins:
  TEST-001  first_pass_green
  TEST-002  first_pass_red_cascade_heals
  TEST-003  first_pass_red_stage_ok_but_validation_still_red
  TEST-004  recovery_stage_failure
  TEST-005  unhealable_blocking_check
  TEST-006  vendor_late_only_preserved
  TEST-007  d14_chunked_cascade_regression
  TEST-008  wrapper_step6_guard (refused on red verdict)
  TEST-009  wrapper_step6_allowed (allowed on green verdict)
  TEST-010  no_false_auto_recovered_event

The wrapper-side tests (TEST-008/009) exercise the verdict-to-exit-code
contract that scripts/run_data_operations.sh Step 6 depends on; the
shell script's own pre-emit probe is documented but not directly run
here (it's a defense-in-depth tripwire, not the primary gate).
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

# ops-shadow load — same pattern as
# tests/test_validation_failures_auto_cascade_wave1.py.
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_final_verdict", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_final_verdict"] = ops
_spec.loader.exec_module(ops)


pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── fakes ─────────────────────────


class _FakePool:
    pass


class _FakeDBLog:
    def __init__(self) -> None:
        self.run_id = uuid.uuid4()
        self.events: list[dict] = []

    async def log(self, event_type, message, severity="INFO", data=None):
        self.events.append({
            "event_type": event_type,
            "message": message,
            "severity": severity,
            "data": data or {},
        })


def _new_summary() -> ops.UpdateSummary:
    return ops.UpdateSummary(
        run_id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        stages=[],
    )


def _add_stage(summary, name, status, *, error=None, detail=None, ms=10):
    summary.stages.append(ops.StageResult(
        name=name, status=status, duration_ms=ms,
        detail=detail or {}, error=error,
    ))


def _patch_revalidate(monkeypatch, *, passed: bool, raise_with: str | None = None):
    """Stub _stage_data_validation so _build_final_lane_verdict's
    re-run returns the requested outcome."""
    async def _stub(_pool):
        if raise_with is not None:
            raise RuntimeError(raise_with)
        if passed:
            return {"passed": True, "checks": 5}
        # _stage_data_validation raises on red — mirror that.
        raise RuntimeError("validation suite failed: ['mystery_check']")
    monkeypatch.setattr(ops, "_stage_data_validation", _stub)


import structlog  # noqa: E402

_log = structlog.get_logger("test.final_verdict")


# ───────────────────────── TEST-001 ─────────────────────────


@pytest.mark.asyncio
async def test_first_pass_green_no_cascade_attempted(monkeypatch):
    """First-pass green. No cascade fires. Verdict GREEN, exit 0,
    emission allowed."""
    summary = _new_summary()
    _add_stage(summary, "data_validation", "OK", detail={"passed": True})
    db_log = _FakeDBLog()
    # Don't patch _stage_data_validation — verdict should NOT call it.
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "GREEN"
    assert verdict.exit_code == 0
    assert verdict.emission_allowed is True
    assert verdict.engine_dispatch_allowed is True
    assert verdict.cascade_attempted is False
    assert verdict.post_cascade_validation_status is None
    # No proven-recovery event because no recovery was needed.
    assert not any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    )
    # UpdateSummary.exit_code consults the verdict.
    summary.final_verdict = verdict
    assert summary.exit_code == 0


# ───────────────────────── TEST-002 ─────────────────────────


@pytest.mark.asyncio
async def test_first_pass_red_cascade_heals_then_revalidates_green(monkeypatch):
    """Cascade dispatched recovery stages; post-cascade re-validation
    is GREEN. Verdict GREEN, exit 0, INGESTION_AUTO_RECOVERED_VALIDATION
    emitted ONLY by the verdict builder (proven recovery)."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['fundamentals_quarterly_completeness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["fundamentals_quarterly_completeness"],
            "handled": ["fundamentals_quarterly_completeness"],
            "skipped": [],
            "vendor_late": [],
        },
    )
    _patch_revalidate(monkeypatch, passed=True)
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "GREEN"
    assert verdict.exit_code == 0
    assert verdict.emission_allowed is True
    assert verdict.cascade_attempted is True
    assert verdict.post_cascade_validation_status == "GREEN"
    assert "fundamentals_quarterly_completeness" in verdict.recovered_checks
    # The proven-recovery event was emitted.
    assert any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    )
    # Stage status was flipped to OK (so the legacy exit_code fallback
    # agrees with the verdict).
    val = next(s for s in summary.stages if s.name == "data_validation")
    assert val.status == "OK"
    assert val.detail.get("post_cascade_passed") is True


# ───────────────────────── TEST-003 ─────────────────────────


@pytest.mark.asyncio
async def test_recovery_stage_ok_but_validation_still_red(monkeypatch):
    """Cascade reports the refresh stage was dispatched OK, but the
    post-cascade re-validation still finds reds (e.g., data didn't
    propagate within the cycle). Verdict RED, no false AUTO_RECOVERED."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['fundamentals_quarterly_completeness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["fundamentals_quarterly_completeness"],
            "handled": ["fundamentals_quarterly_completeness"],
            "skipped": [],
            "vendor_late": [],
        },
    )
    _patch_revalidate(monkeypatch, passed=False)
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert verdict.emission_allowed is False
    assert verdict.engine_dispatch_allowed is False
    assert verdict.post_cascade_validation_status == "RED"
    # Optimistic event must NOT be emitted.
    assert not any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    )
    # The RECOVERY_FAILED event documents what happened.
    assert any(
        e["event_type"] == "INGESTION_AUTO_RECOVERY_FAILED"
        for e in db_log.events
    )
    val = next(s for s in summary.stages if s.name == "data_validation")
    assert val.status == "FAILED"
    assert val.detail.get("post_cascade_passed") is False


# ───────────────────────── TEST-004 ─────────────────────────


@pytest.mark.asyncio
async def test_recovery_stage_failure_blocks_emission(monkeypatch):
    """At least one cascade recovery stage failed (refresh_outcome
    status != OK). That check should have been moved from `handled`
    to `skipped` by the cascade. The verdict sees `handled` is empty,
    skips re-validation (NOT_RUN), and reports RED."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['corporate_actions_completeness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["corporate_actions_completeness"],
            "handled": [],          # cascade moved it to skipped after failure
            "skipped": ["corporate_actions_completeness"],
            "vendor_late": [],
        },
    )
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert verdict.emission_allowed is False
    assert verdict.post_cascade_validation_status == "NOT_RUN"
    # The proven-recovery event must NOT be emitted.
    assert not any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    )


# ───────────────────────── TEST-005 ─────────────────────────


@pytest.mark.asyncio
async def test_unhealable_blocking_check_no_loop(monkeypatch):
    """A check whose HealSpec is healable=False (e.g., the
    operator-disabled options_max_pain_freshness). Cascade puts it in
    `skipped`; verdict classifies as unhealable, does NOT re-run
    data_validation, reports RED."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['options_max_pain_freshness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["options_max_pain_freshness"],
            "handled": [],
            "skipped": ["options_max_pain_freshness"],
            "vendor_late": [],
        },
    )
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    # Operator-visible reason via the HealSpec registry.
    assert "options_max_pain_freshness" in verdict.unhealable_checks
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert verdict.emission_allowed is False
    assert verdict.post_cascade_validation_status == "NOT_RUN"


# ───────────────────────── TEST-006 ─────────────────────────


@pytest.mark.asyncio
async def test_vendor_late_only_preserved(monkeypatch):
    """vendor_late-only state: D11 classification put a check in
    vendor_late; no actual recovery attempted. Per the 'classification
    not relaxation' invariant (orchestrator.py:74-78), exit_code stays
    1 and emission stays blocked."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['aaii_sentiment_freshness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["aaii_sentiment_freshness"],
            "handled": [],
            "skipped": [],
            "vendor_late": ["aaii_sentiment_freshness"],
        },
    )
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert "aaii_sentiment_freshness" in verdict.vendor_late_checks
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert verdict.emission_allowed is False
    assert verdict.engine_dispatch_allowed is False
    assert verdict.post_cascade_validation_status == "NOT_RUN"


# ───────────────────────── TEST-007 ─────────────────────────


@pytest.mark.asyncio
async def test_d14_chunked_cascade_no_regression(monkeypatch):
    """D14 (chunked TIMEOUT recovery, scripts/ops.py:~10202-10282)
    already flips status='OK' on its own. _build_final_lane_verdict
    must not regress: when the data_validation row arrives status='OK'
    via D14's flip, verdict still treats it as first-pass green."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "OK",
        detail={
            "cascade": True,
            "cascade_mode": "validation_chunked",
            "chunked_recovery": True,
        },
    )
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "GREEN"
    assert verdict.exit_code == 0
    assert verdict.emission_allowed is True
    # D14's own AUTO_RECOVERED_VALIDATION_CHUNKED event is preserved
    # (it fires from _auto_cascade_validation_timeout, not from us).
    # Our function does NOT emit a duplicate AUTO_RECOVERED_VALIDATION
    # because cascade_attempted from our perspective was False.
    assert not any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    )


# ───────────────────────── TEST-008 ─────────────────────────


def test_wrapper_step6_refuses_on_red_verdict():
    """When ops.py exits 1 from a red verdict, scripts/run_data_operations.sh
    must not reach Step 6. This is enforced by the wrapper's existing
    `exit $UPDATE_RC` at line ~287-294. We pin the control-flow text:
    the wrapper's Step 1+2 check predicates Step 6 reachability on a
    zero UPDATE_RC, which now means proven-green verdict."""
    text = (_REPO / "scripts" / "run_data_operations.sh").read_text()
    # The early-exit on non-zero ops.py result must precede the Step 6
    # block by source-line ordering.
    assert "▶ STEP 1+2 / 6  download + upload" in text
    assert "if [[ $UPDATE_RC -ne 0 ]]; then" in text
    assert "exit $UPDATE_RC" in text
    assert "▶ STEP 6 / 6  emit DATA_OPERATIONS_COMPLETE" in text
    step12_idx = text.index("if [[ $UPDATE_RC -ne 0 ]]; then")
    step6_idx = text.index("▶ STEP 6 / 6  emit DATA_OPERATIONS_COMPLETE")
    assert step12_idx < step6_idx, (
        "Step 1+2 abort must precede Step 6 in source order so a "
        "red ops.py exit blocks DATA_OPERATIONS_COMPLETE emission"
    )


# ───────────────────────── TEST-009 ─────────────────────────


def test_wrapper_step6_has_predeploy_probe():
    """Wrapper Step 6 now has a defense-in-depth pre-emit probe that
    reads data_quality_log latest-per-source and refuses the INSERT if
    any latest validation row is stale or confidence<1.0. Pins the
    probe is present (REQ-005 defense-in-depth)."""
    text = (_REPO / "scripts" / "run_data_operations.sh").read_text()
    assert "PROBE_RED_COUNT" in text
    assert "data_quality_log" in text
    assert "100%-green-or-don't-trade invariant" in text
    # The probe must be inside the Step 6 block AND before the INSERT.
    step6_idx = text.index("▶ STEP 6 / 6  emit DATA_OPERATIONS_COMPLETE")
    probe_idx = text.index("PROBE_RED_COUNT", step6_idx)
    insert_idx = text.index("'DATA_OPERATIONS_COMPLETE'", probe_idx)
    assert step6_idx < probe_idx < insert_idx, (
        "pre-emit probe must run between the Step 6 header and the "
        "DATA_OPERATIONS_COMPLETE INSERT"
    )


# ───────────────────────── TEST-010 ─────────────────────────


@pytest.mark.asyncio
async def test_no_false_auto_recovered_event_when_revalidate_skipped(monkeypatch):
    """When the cascade is skipped (nothing dispatched — all reds were
    vendor_late or unhealable), no AUTO_RECOVERED_VALIDATION event
    must fire. Same test as TEST-004/005/006 but specifically asserts
    the event-absence (REQ-003 + REQ-010 in the spec)."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['options_max_pain_freshness', 'aaii_sentiment_freshness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": [
                "options_max_pain_freshness",
                "aaii_sentiment_freshness",
            ],
            "handled": [],
            "skipped": ["options_max_pain_freshness"],
            "vendor_late": ["aaii_sentiment_freshness"],
        },
    )
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert not any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    ), (
        "INGESTION_AUTO_RECOVERED_VALIDATION must never fire when "
        "no recovery was dispatched — even with vendor_late + unhealable "
        "reds, the cascade was a no-op."
    )


# ───────────────────────── TEST-011 ─────────────────────────


@pytest.mark.asyncio
async def test_revalidate_timeout_falls_red_with_synthetic_check(monkeypatch):
    """F-001 expert review fix: the post-cascade re-validation is wrapped
    in asyncio.wait_for(300.0). When it times out, the verdict must go
    RED with a synthetic ``data_validation_revalidate_timeout`` entry in
    remaining_failed_checks AND no AUTO_RECOVERED_VALIDATION event.

    Reproduces the failure mode where the re-run's queries themselves
    block (huge cascade-emitted backlog, statement_timeout exhaustion,
    etc.) — without the bound, ops.py would hang indefinitely and the
    cron-driven Step 1+2 phase would never return."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['fundamentals_quarterly_completeness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["fundamentals_quarterly_completeness"],
            "handled": ["fundamentals_quarterly_completeness"],
            "skipped": [],
            "vendor_late": [],
        },
    )

    # Make the re-validate raise asyncio.TimeoutError so the verdict
    # builder's except-branch fires. We monkeypatch asyncio.wait_for
    # itself to short-circuit the 300-s sleep in tests.
    async def _wait_for_stub(_coro, timeout):
        # Cancel the wrapped coroutine and raise TimeoutError directly.
        try:
            _coro.close()
        except Exception:
            pass
        raise TimeoutError

    monkeypatch.setattr(ops.asyncio, "wait_for", _wait_for_stub)
    # Provide a callable so the builder has something to wrap.
    async def _ignored(_pool):
        return {"passed": True}
    monkeypatch.setattr(ops, "_stage_data_validation", _ignored)

    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert verdict.emission_allowed is False
    assert verdict.engine_dispatch_allowed is False
    assert verdict.post_cascade_validation_status == "RED"
    assert (
        "data_validation_revalidate_timeout"
        in verdict.remaining_failed_checks
    )
    # Optimistic proven-recovery event must NOT fire on timeout.
    assert not any(
        e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
        for e in db_log.events
    )


# ───────────────────────── TEST-012 ─────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_cadence_check_self_seeds_on_fresh_db(monkeypatch):
    """F-002 expert review fix: the cadence check
    ``data_operations_complete_cadence`` is healable=False and goes RED
    on a fresh DB / wiped application_log (reason='never_emitted'). The
    check exists to surface a silently-broken lane in steady state, but
    in the bootstrap state the gate event can never emit because the
    check is red, and the check can never go green because no event
    exists — chicken-and-egg.

    The verdict builder's bootstrap special-case unblocks this: when
    cadence is the SOLE unhealable AND every other check is green AND
    no vendor_late entries AND post-cascade re-validation was GREEN,
    the verdict allows GREEN so this cycle's emission seeds the cadence
    row naturally.

    From cycle 2 onward, the cadence check returns GREEN because the
    seed row is <30 h old; the override never fires again."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['data_operations_complete_cadence']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": ["data_operations_complete_cadence"],
            "handled": [],
            # Cadence is healable=False → cascade moves it to skipped
            # (treated as unhealable by the verdict builder).
            "skipped": ["data_operations_complete_cadence"],
            "vendor_late": [],
        },
    )
    # When cadence is the only first-pass red, handled=[] so
    # should_revalidate=False → post_status='NOT_RUN'. The bootstrap
    # special-case admits NOT_RUN (nothing healable existed to
    # re-validate) the same way it admits GREEN. We stub re-validate
    # anyway in case the verdict builder ever changes its mind.
    _patch_revalidate(monkeypatch, passed=True)
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    # The special-case unblocks emission so the first cycle seeds the gate.
    assert (
        "data_operations_complete_cadence"
        in verdict.unhealable_checks
    )
    assert verdict.final_status == "GREEN"
    assert verdict.exit_code == 0
    assert verdict.emission_allowed is True
    assert verdict.engine_dispatch_allowed is True


# ───────────────────────── TEST-013 ─────────────────────────


@pytest.mark.asyncio
async def test_vendor_late_blocks_emission_even_after_revalidate_green(monkeypatch):
    """F-003 expert review fix: the prior special-case override at
    scripts/ops.py:8811-8815 unconditionally set final='GREEN' when
    post_status=='GREEN', which leaked vendor_late entries past the
    gate. Per orchestrator.py:74-78 ("vendor_late is CLASSIFICATION,
    not RELAXATION") any vendor_late entry MUST keep the row red.

    Scenario: one healable red was handled and recovered; one
    vendor_late entry remains. Even though the post-cascade re-validate
    reports passed=True (the vendor_late check might have temporarily
    flipped within the cycle), the sacred-gate invariant says
    vendor_late entries STAY red. Verdict must be RED with emission
    blocked."""
    summary = _new_summary()
    _add_stage(
        summary, "data_validation", "FAILED",
        error="validation suite failed: ['fundamentals_quarterly_completeness', 'aaii_sentiment_freshness']",
        detail={
            "cascade": True,
            "cascade_mode": "validation_failures",
            "failed_checks": [
                "fundamentals_quarterly_completeness",
                "aaii_sentiment_freshness",
            ],
            "handled": ["fundamentals_quarterly_completeness"],
            "skipped": [],
            "vendor_late": ["aaii_sentiment_freshness"],
        },
    )
    # Re-validate reports GREEN. The F-003 fix means that's still NOT
    # enough to unblock emission because vendor_late entries remain.
    _patch_revalidate(monkeypatch, passed=True)
    db_log = _FakeDBLog()
    verdict = await ops._build_final_lane_verdict(
        summary, _FakePool(), log=_log, db_log=db_log,
    )
    assert "aaii_sentiment_freshness" in verdict.vendor_late_checks
    assert (
        "fundamentals_quarterly_completeness" in verdict.recovered_checks
    )
    # Sacred vendor_late invariant: classification, not relaxation.
    assert verdict.final_status == "RED"
    assert verdict.exit_code == 1
    assert verdict.emission_allowed is False
    assert verdict.engine_dispatch_allowed is False
