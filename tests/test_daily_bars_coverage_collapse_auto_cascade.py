"""Regression test for the daily_bars coverage_collapse → repair_gaps
auto-cascade (operator-reproduced failure 2026-05-18 → 2026-05-20).

Failure mode being closed:

* nightly data daemon ran 21:30 UTC Mon/Tue/Wed
* each night ``_stage_daily_bars`` produced
  ``RuntimeError("daily_bars coverage collapse: <date> has <n> tickers = 6%")``
  via the producer-self-validation block
* the StageResult became ``FAILED`` and the orchestrator returned exit 1
* **zero** auto-recovery fired despite the codebase already shipping the
  bounded ``repair_gaps`` heal that is the canonical fix
* the operator had to intervene every morning

What the cascade ships (in ``scripts/ops.py:cmd_update``):

* exactly-once recovery — coverage_collapse only (not auth, not schema
  drift, not other RuntimeError modes)
* runs ``_stage_daily_bars(repair_gaps=true)`` once
* logs ``INGESTION_AUTO_RECOVERY_START``, then either
  ``INGESTION_AUTO_RECOVERED`` (success) or
  ``INGESTION_AUTO_RECOVERY_FAILED`` (escalation surface)
* replaces the failed ``daily_bars`` StageResult with the cascade
  result, annotated ``cascade=True`` + ``cascade_mode=repair_gaps``
* never burns Lab n_trials

These tests construct a minimal harness — they monkey-patch
``_stage_daily_bars`` itself so the test stays hermetic (no DB, no
network) and the cascade's INVOCATION can be observed independently of
the heal's internals (the heal already has its own unit tests).

Three behaviours pinned:

1. coverage_collapse → cascade fires → daily_bars entry replaced by an
   OK cascade result + ``INGESTION_AUTO_RECOVERED`` logged.
2. coverage_collapse + cascade ALSO fails → daily_bars entry stays
   FAILED + ``INGESTION_AUTO_RECOVERY_FAILED`` logged (operator sees
   the escalation, not a silent give-up).
3. non-coverage_collapse FAILED (e.g. an auth failure) → cascade does
   NOT fire (no second invocation, no auto-recovery event).

A fourth structural test pins that the cascade's discriminator token
matches the actual RuntimeError message format raised by
``_stage_daily_bars`` (so the cascade can never silently miss the
exact wording it's keyed off).
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

# Load scripts/ops.py by path under a private name (canonical
# ops-shadow pattern — same trick test_handle_daily_bars_multi.py +
# test_data_repair_service.py + test_cron_fundamentals_refresh.py use).
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_auto_cascade", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_auto_cascade"] = ops
_spec.loader.exec_module(ops)


# pytest-xdist: pin this ops-shadow module to one worker so its
# scripts/ops.py load stays single-process (the ops/ package-shadow is
# a single-process invariant).
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ────────────────────────────────────────────────────────────────────────
# Fakes — no DB, no network. Just enough surface to invoke cmd_update
# down to the cascade decision and assert the post-state.
# ────────────────────────────────────────────────────────────────────────


class _FakePool:
    """No-op pool. The stage implementations are stubbed so the pool is
    only passed through; nothing ever touches a connection."""


class _FakeDBLog:
    """Captures every (event_type, message, severity, data) call so the
    test can assert on the exact sequence of events emitted."""

    def __init__(self) -> None:
        self.run_id = uuid.uuid4()
        self.events: list[dict] = []

    async def log(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        data: dict | None = None,
    ) -> None:
        self.events.append({
            "event_type": event_type,
            "message": message,
            "severity": severity,
            "data": data or {},
        })


class _CountingStubStage:
    """Stand-in for ``_stage_daily_bars`` that records every call.

    First call: raises ``RuntimeError`` with the operator-observed
    coverage_collapse message. Second call (the cascade): if
    ``recover`` is True, returns an OK result with mode=repair_gaps;
    else raises a fresh failure (escalation path)."""

    def __init__(self, *, recover: bool, raise_cls: type[Exception] = RuntimeError,
                 first_error: str | None = None) -> None:
        self.recover = recover
        self.raise_cls = raise_cls
        # Operator's actual message (2026-05-18 application_log).
        # The cascade discriminator must match this format.
        self.first_error = first_error or (
            "daily_bars coverage collapse: 2026-05-18 has 480 tickers = 6% "
            "of the trailing-20-session avg (7,300); floor is 6,570 (90%). "
            "Refusing to report OK — partial/failed ingest"
        )
        self.calls: list[dict] = []

    async def __call__(self, pool, config):
        self.calls.append({
            "repair_gaps": bool(config.get("repair_gaps", False)),
            "repair_coverage": bool(config.get("repair_coverage", False)),
            "force_refresh": bool(config.get("force_refresh", False)),
        })
        is_cascade = bool(config.get("repair_gaps", False))
        if not is_cascade:
            # First-pass producer-self-validation failure.
            raise self.raise_cls(self.first_error)
        if self.recover:
            return {
                "rows_upserted": 480,
                "mode": "repair_gaps",
                "tickers_repaired": 480,
                "lookback_days": 14,
            }
        raise RuntimeError("repair_gaps also failed: SEC EDGAR rate-limited")


def _install_logger(monkeypatch):
    """Bind a structlog logger that the cascade path expects."""
    import structlog
    return structlog.get_logger("test.auto_cascade")


def _patch_minimal_pipeline(monkeypatch, stub, *, only_daily_bars: bool = True):
    """Reduce ``cmd_update`` to a single-stage pipeline so the test can
    focus on the cascade decision without dragging every other stage
    into the harness. Achieved by:

    * patching ``_load_daily_bars_config`` → trivial dict
    * patching ``_market_open_block_reason`` → None (always closed)
    * patching ``_per_feed_tripwire`` → no-op
    * patching ``_self_heal_failed_stages`` → no-op (this exercises the
      coverage_collapse cascade in isolation, NOT the transient retry)
    * patching the stage spec list to just ``daily_bars`` with our stub
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 10}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop_tripwire(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop_tripwire)

    async def _noop_self_heal(*a, **k):
        return None

    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop_self_heal)

    monkeypatch.setattr(ops, "_stage_daily_bars", stub)

    if only_daily_bars:
        # Build a single-stage spec list using the LIVE timeout constant
        # so we exercise the real _run_stage path.
        spec = (
            (
                "daily_bars",
                lambda pool, cfg: (lambda: ops._stage_daily_bars(pool, cfg)),
                ops.HEAVY_STAGE_TIMEOUT_SEC,
            ),
        )
        monkeypatch.setattr(ops, "_STAGE_SPECS", spec)


# ────────────────────────────────────────────────────────────────────────
# 1. Happy path — coverage_collapse → cascade fires → recovery.
# ────────────────────────────────────────────────────────────────────────


async def test_coverage_collapse_triggers_repair_gaps_cascade_and_recovers(
    monkeypatch,
):
    stub = _CountingStubStage(recover=True)
    _patch_minimal_pipeline(monkeypatch, stub)
    log = _install_logger(monkeypatch)
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. _stage_daily_bars was invoked TWICE — once first-pass, once cascade.
    assert len(stub.calls) == 2, (
        f"expected 2 _stage_daily_bars calls (first-pass + cascade), "
        f"got {len(stub.calls)}: {stub.calls}"
    )
    # 1a. First call: NOT repair_gaps (the normal pull).
    assert stub.calls[0]["repair_gaps"] is False
    # 1b. Second call: repair_gaps=true (the cascade).
    assert stub.calls[1]["repair_gaps"] is True

    # 2. The summary contains exactly ONE daily_bars row (replaced, not
    #    duplicated) and it reflects the RECOVERY, not the original
    #    failure.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "OK"
    assert db_rows[0].detail.get("cascade") is True
    assert db_rows[0].detail.get("cascade_mode") == "repair_gaps"
    assert "coverage collapse" in (db_rows[0].detail.get("first_error") or "")

    # 3. Overall exit_code is 0 (recovered, no FAILED entries).
    assert summary.exit_code == 0

    # 4. The cascade events were logged in order: START → RECOVERED.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_START" in event_types, event_types
    assert "INGESTION_AUTO_RECOVERED" in event_types, event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" not in event_types

    # 5. The escalation event was NOT emitted (this is the green path).
    start = next(e for e in db_log.events
                 if e["event_type"] == "INGESTION_AUTO_RECOVERY_START")
    assert start["data"].get("cascade_mode") == "repair_gaps"
    assert start["data"].get("trigger") == "coverage_collapse"
    recovered = next(e for e in db_log.events
                     if e["event_type"] == "INGESTION_AUTO_RECOVERED")
    assert recovered["severity"] == "INFO"
    assert "coverage collapse" in (recovered["data"].get("first_error") or "")


# ────────────────────────────────────────────────────────────────────────
# 2. Escalation — cascade ALSO fails.
# ────────────────────────────────────────────────────────────────────────


async def test_cascade_also_fails_emits_escalation_event(monkeypatch):
    stub = _CountingStubStage(recover=False)
    _patch_minimal_pipeline(monkeypatch, stub)
    log = _install_logger(monkeypatch)
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # ONE-SHOT: cascade fired exactly once, did not loop.
    assert len(stub.calls) == 2
    assert stub.calls[1]["repair_gaps"] is True

    # Final stage state: FAILED (escalated honestly), with the cascade
    # annotation so the dashboard reader can tell this was an attempted
    # recovery rather than a first-try fail.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "FAILED"
    assert db_rows[0].detail.get("cascade") is True
    assert summary.exit_code == 1

    # Escalation event was logged with severity=ERROR.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_START" in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" in event_types
    assert "INGESTION_AUTO_RECOVERED" not in event_types
    failed = next(e for e in db_log.events
                  if e["event_type"] == "INGESTION_AUTO_RECOVERY_FAILED")
    assert failed["severity"] == "ERROR"
    assert failed["data"].get("cascade_mode") == "repair_gaps"


# ────────────────────────────────────────────────────────────────────────
# 3. Non-coverage_collapse failure — cascade must NOT fire.
# ────────────────────────────────────────────────────────────────────────


async def test_non_coverage_collapse_failure_does_not_trigger_cascade(
    monkeypatch,
):
    # Different failure surface — auth/credential — explicitly NOT the
    # cascade's target.
    stub = _CountingStubStage(
        recover=True,  # would recover if cascade fired (it must not)
        first_error="daily_bars failed: AlpacaAuth 401 unauthorized",
    )
    _patch_minimal_pipeline(monkeypatch, stub)
    log = _install_logger(monkeypatch)
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Stage ran EXACTLY ONCE — no cascade.
    assert len(stub.calls) == 1, (
        f"cascade fired on non-coverage_collapse failure: {stub.calls}"
    )

    # Stage stays FAILED, no cascade annotation.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "FAILED"
    assert "cascade" not in (db_rows[0].detail or {})

    # No cascade events at all.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_START" not in event_types
    assert "INGESTION_AUTO_RECOVERED" not in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" not in event_types


# ────────────────────────────────────────────────────────────────────────
# 4. Discriminator token matches the producer-self-validation raise.
# ────────────────────────────────────────────────────────────────────────


def test_cascade_token_matches_stage_self_validation_raise():
    """The cascade discriminator (_DAILY_BARS_COVERAGE_COLLAPSE_TOKEN)
    must match the EXACT message the producer-self-validation block in
    _stage_daily_bars raises. A reword in either place that breaks the
    pairing silently turns the cascade off — this test is the structural
    pin that prevents that drift."""
    token = ops._DAILY_BARS_COVERAGE_COLLAPSE_TOKEN
    src = _OPS_PATH.read_text()
    # The raise lives in _stage_daily_bars and uses an f-string that
    # starts with "daily_bars coverage collapse: …".
    assert "daily_bars coverage collapse:" in src, (
        "_stage_daily_bars no longer raises a 'daily_bars coverage "
        "collapse' RuntimeError — cascade discriminator is now orphaned. "
        f"Token: {token!r}"
    )
    assert token.lower() in "daily_bars coverage collapse:".lower()
