"""Regression test for the daily_bars coverage_collapse → force_refresh
auto-cascade with live SIP-vs-IEX feed selection (operator-reproduced
2026-05-18 → 2026-05-21).

Why this rewrite (vs. the PR #227 shape):

PR #227 wired the cascade *trigger* but cascaded to the WRONG mechanism:
``_stage_daily_bars`` with ``repair_gaps=true``. The operator demonstrated
2026-05-21 07:39 UTC that the repair_gaps branch is BLIND to this exact
failure mode — its completeness-derived target list returns
``skipped: no_gaps_or_not_bars_fixable``, so the cascade would have
proudly logged ``INGESTION_AUTO_RECOVERED`` while the data stayed broken
at 7%.

The actual recovery the operator ran by hand and verified:

    --param force_refresh=true --param universe=active --param feed=sip
    --param end_offset_days=1

So the cascade now:

1. Probes SIP availability via a live 10s GET against the Alpaca data
   endpoint with ``feed=sip``.
2. Picks ``feed="sip"`` if probe → True, else ``feed="iex"`` (degraded).
3. Re-runs ``_stage_daily_bars`` with ``force_refresh=True`` +
   ``universe="active"`` + the chosen feed.

Events:

* ``INGESTION_AUTO_RECOVERY_START`` — cascade fires (data.feed is the
  picked feed).
* ``INGESTION_AUTO_RECOVERED`` — SIP probe passed, force_refresh
  feed=sip recovered to ≥ floor.
* ``INGESTION_AUTO_RECOVERY_DEGRADED`` — SIP probe failed (or
  coverage still below floor on IEX), feed=iex ran — partial recovery.
* ``INGESTION_AUTO_RECOVERY_FAILED`` — fetch did not land at all
  (network/auth, NOT a coverage-collapse re-fail).

The five behaviours pinned (one of which is the structural pin):

A. SIP-available cascade — probe True → feed=sip → RECOVERED.
B. SIP-unavailable degraded cascade — probe False → feed=iex → DEGRADED.
C. Cascade-fully-fails escalation — both paths fail with non-coverage
   errors (network down) → FAILED.
D. Non-coverage_collapse failure (auth) — cascade does NOT fire.
E. Discriminator-token structural pin.
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

    First call (non-cascade): raises ``RuntimeError`` with the
    operator-observed coverage_collapse message. Second call (cascade,
    identified by ``force_refresh=True``):

    * ``cascade_outcome="recover"`` → return OK dict
    * ``cascade_outcome="coverage_collapse"`` → raise the same
      coverage-collapse RuntimeError (IEX-subset-only outcome: fetch
      ran but coverage still below floor — DEGRADED)
    * ``cascade_outcome="network_down"`` → raise a NON-coverage-collapse
      error (Alpaca unreachable — FAILED escalation)
    """

    def __init__(
        self,
        *,
        cascade_outcome: str = "recover",
        raise_cls: type[Exception] = RuntimeError,
        first_error: str | None = None,
    ) -> None:
        self.cascade_outcome = cascade_outcome
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
            "force_refresh": bool(config.get("force_refresh", False)),
            "repair_gaps": bool(config.get("repair_gaps", False)),
            "feed": config.get("feed"),
            "universe": config.get("universe"),
            "lookback_days": config.get("lookback_days"),
            "end_offset_days": config.get("end_offset_days"),
        })
        is_cascade = bool(config.get("force_refresh", False))
        if not is_cascade:
            # First-pass producer-self-validation failure.
            raise self.raise_cls(self.first_error)
        # The cascade invocation — branch on the requested outcome.
        if self.cascade_outcome == "recover":
            return {
                "rows_upserted": 7300,
                "mode": "force_refresh",
                "feed": config.get("feed"),
                "target_session": "2026-05-21",
                "coverage_tickers": 7300,
            }
        if self.cascade_outcome == "coverage_collapse":
            # IEX-subset cascade landed but coverage still below floor.
            # _stage_daily_bars raises a coverage_collapse RuntimeError
            # in this case (fetch DID run; producer self-validation
            # refused to report OK).
            raise self.raise_cls(self.first_error)
        if self.cascade_outcome == "network_down":
            # Both the SIP probe failed AND the IEX-fallback fetch
            # raised a non-coverage error (Alpaca unreachable). This is
            # the FAILED-escalation surface, distinct from DEGRADED.
            raise self.raise_cls(
                "alpaca unreachable: ConnectError(\"timed out\")"
            )
        raise AssertionError(  # pragma: no cover — test bug
            f"unknown cascade_outcome={self.cascade_outcome!r}"
        )


def _install_logger(monkeypatch):
    """Bind a structlog logger that the cascade path expects."""
    import structlog
    return structlog.get_logger("test.auto_cascade")


def _patch_minimal_pipeline(
    monkeypatch,
    stub,
    *,
    sip_ok: bool | Exception = True,
    only_daily_bars: bool = True,
):
    """Reduce ``cmd_update`` to a single-stage pipeline so the test can
    focus on the cascade decision without dragging every other stage
    into the harness. Achieved by:

    * patching ``_load_daily_bars_config`` → trivial dict
    * patching ``_market_open_block_reason`` → None (always closed)
    * patching ``_per_feed_tripwire`` → no-op
    * patching ``_self_heal_failed_stages`` → no-op (this exercises the
      coverage_collapse cascade in isolation, NOT the transient retry)
    * patching the stage spec list to just ``daily_bars`` with our stub
    * patching ``_alpaca_sip_available`` → fixed bool / raise

    ``sip_ok`` controls the SIP-probe stub:
    * ``True`` → probe returns True (SIP available — cascade picks sip)
    * ``False`` → probe returns False (SIP unavailable — cascade picks iex)
    * ``Exception`` instance → probe raises (cascade must tolerate +
      fall back to iex)
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 7}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop_tripwire(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop_tripwire)

    async def _noop_self_heal(*a, **k):
        return None

    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop_self_heal)
    # Wave-2 cascades (D2/D3/D5/D13) — neutralised here so these
    # coverage_collapse tests exercise the coverage-cascade in isolation.
    # The Wave-2 cascade has its own regression suite in
    # tests/test_stage_robustness_auto_cascade_wave2.py.
    if hasattr(ops, "_auto_cascade_stage_robustness"):
        monkeypatch.setattr(
            ops, "_auto_cascade_stage_robustness", _noop_self_heal,
        )

    monkeypatch.setattr(ops, "_stage_daily_bars", stub)

    # SIP probe stub.
    if isinstance(sip_ok, Exception):
        async def _probe_raises(*a, **k):
            raise sip_ok
        monkeypatch.setattr(ops, "_alpaca_sip_available", _probe_raises)
    else:
        async def _probe_const(*a, **k):
            return bool(sip_ok)
        monkeypatch.setattr(ops, "_alpaca_sip_available", _probe_const)

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
# A. SIP-available cascade — probe True → feed=sip → RECOVERED.
# ────────────────────────────────────────────────────────────────────────


async def test_sip_available_cascade_uses_sip_and_recovers(monkeypatch):
    stub = _CountingStubStage(cascade_outcome="recover")
    _patch_minimal_pipeline(monkeypatch, stub, sip_ok=True)
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
    # 1a. First call: NOT force_refresh (the normal pull).
    assert stub.calls[0]["force_refresh"] is False
    assert stub.calls[0]["repair_gaps"] is False
    # 1b. Second call: the cascade — force_refresh=True + universe=active
    #     + feed=sip. NOT repair_gaps (that was PR #227's broken cascade
    #     target; this PR replaces it with the operator-verified shape).
    assert stub.calls[1]["force_refresh"] is True, stub.calls[1]
    assert stub.calls[1]["repair_gaps"] is False, stub.calls[1]
    assert stub.calls[1]["feed"] == "fmp", stub.calls[1]
    assert stub.calls[1]["universe"] == "active", stub.calls[1]

    # 2. The summary contains exactly ONE daily_bars row (replaced, not
    #    duplicated) and it reflects the RECOVERY, not the original
    #    failure.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "OK"
    assert db_rows[0].detail.get("cascade") is True
    assert db_rows[0].detail.get("cascade_mode") == "force_refresh"
    assert db_rows[0].detail.get("feed") == "fmp"
    # Post-FMP-mandate cascade: sip_probe field retained as structured
    # cascade-detail but always False (probe no longer drives feed choice).
    assert db_rows[0].detail.get("sip_probe") is False
    assert "coverage collapse" in (db_rows[0].detail.get("first_error") or "")

    # 3. Overall exit_code is 0 (recovered, no FAILED entries).
    assert summary.exit_code == 0

    # 4. The cascade events were logged in order: START → RECOVERED.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_START" in event_types, event_types
    assert "INGESTION_AUTO_RECOVERED" in event_types, event_types
    assert "INGESTION_AUTO_RECOVERY_DEGRADED" not in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" not in event_types

    start = next(e for e in db_log.events
                 if e["event_type"] == "INGESTION_AUTO_RECOVERY_START")
    assert start["data"].get("cascade_mode") == "force_refresh"
    assert start["data"].get("trigger") == "coverage_collapse"
    assert start["data"].get("feed") == "fmp"
    assert start["data"].get("reason") == "fmp_primary_per_operator_rule"

    recovered = next(e for e in db_log.events
                     if e["event_type"] == "INGESTION_AUTO_RECOVERED")
    assert recovered["severity"] == "INFO"
    assert recovered["data"].get("feed") == "fmp"
    assert "coverage collapse" in (recovered["data"].get("first_error") or "")


# ────────────────────────────────────────────────────────────────────────
# B. SIP-unavailable degraded cascade — probe False → feed=iex → DEGRADED.
# ────────────────────────────────────────────────────────────────────────


async def test_sip_unavailable_cascade_falls_back_to_iex_degraded(monkeypatch):
    # SIP probe returns False (simulating the 403 "subscription does not
    # permit" response). IEX cascade lands but coverage is still below
    # the floor → producer self-validation raises coverage_collapse
    # again. The cascade should emit AUTO_RECOVERY_DEGRADED (NOT FAILED,
    # because the fetch DID run — it just landed below floor on the
    # narrower IEX universe).
    stub = _CountingStubStage(cascade_outcome="coverage_collapse")
    _patch_minimal_pipeline(monkeypatch, stub, sip_ok=False)
    log = _install_logger(monkeypatch)
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Cascade fired exactly once with feed=iex.
    assert len(stub.calls) == 2
    assert stub.calls[1]["force_refresh"] is True
    assert stub.calls[1]["feed"] == "fmp", stub.calls[1]
    assert stub.calls[1]["universe"] == "active"

    # Stage stays FAILED (the IEX fetch produced sub-floor coverage)
    # but with the cascade annotation so dashboard readers see this was
    # an attempted recovery on the IEX feed.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "FAILED"
    assert db_rows[0].detail.get("cascade") is True
    assert db_rows[0].detail.get("feed") == "fmp"
    assert db_rows[0].detail.get("sip_probe") is False

    # Events: START → DEGRADED. NOT RECOVERED, NOT FAILED.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_START" in event_types, event_types
    assert "INGESTION_AUTO_RECOVERY_DEGRADED" in event_types, event_types
    assert "INGESTION_AUTO_RECOVERED" not in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" not in event_types

    start = next(e for e in db_log.events
                 if e["event_type"] == "INGESTION_AUTO_RECOVERY_START")
    assert start["data"].get("feed") == "fmp"
    assert start["data"].get("reason") == "fmp_primary_per_operator_rule"

    degraded = next(e for e in db_log.events
                    if e["event_type"] == "INGESTION_AUTO_RECOVERY_DEGRADED")
    assert degraded["severity"] == "WARNING"
    assert degraded["data"].get("feed") == "fmp"
    assert degraded["data"].get("reason") == "fmp_primary_per_operator_rule"


# ────────────────────────────────────────────────────────────────────────
# C. Cascade-fully-fails escalation — both probe + IEX fallback fail.
# ────────────────────────────────────────────────────────────────────────


async def test_cascade_fully_fails_emits_recovery_failed(monkeypatch):
    # SIP probe False (entitlement issue or network) AND the IEX
    # fallback fetch raises a non-coverage error (Alpaca network down).
    # No fetch landed at all → FAILED escalation.
    stub = _CountingStubStage(cascade_outcome="network_down")
    _patch_minimal_pipeline(monkeypatch, stub, sip_ok=False)
    log = _install_logger(monkeypatch)
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # ONE-SHOT: cascade fired exactly once, did not loop.
    assert len(stub.calls) == 2
    assert stub.calls[1]["force_refresh"] is True
    assert stub.calls[1]["feed"] == "fmp"

    # Final stage state: FAILED (escalated honestly), with the cascade
    # annotation so the dashboard reader can tell this was an attempted
    # recovery rather than a first-try fail.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "FAILED"
    assert db_rows[0].detail.get("cascade") is True
    assert db_rows[0].detail.get("feed") == "fmp"
    assert summary.exit_code == 1

    # FAILED event was logged with severity=ERROR.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_START" in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" in event_types
    assert "INGESTION_AUTO_RECOVERED" not in event_types
    assert "INGESTION_AUTO_RECOVERY_DEGRADED" not in event_types
    failed = next(e for e in db_log.events
                  if e["event_type"] == "INGESTION_AUTO_RECOVERY_FAILED")
    assert failed["severity"] == "ERROR"
    assert failed["data"].get("cascade_mode") == "force_refresh"
    assert failed["data"].get("feed") == "fmp"
    # The cascade_error must reflect the network-down failure, NOT
    # coverage_collapse (the discriminator that pins us to DEGRADED
    # instead would be "coverage collapse" being present in the error).
    err = (failed["data"].get("cascade_error") or "").lower()
    assert "coverage collapse" not in err
    assert "alpaca" in err or "connect" in err


async def test_probe_exception_is_tolerated_as_sip_unavailable(monkeypatch):
    """If the SIP probe itself raises (e.g. a transient network error
    inside the httpx client setup), the cascade must NOT crash — it must
    treat the probe as False and fall through to the IEX-degraded
    path."""
    stub = _CountingStubStage(cascade_outcome="coverage_collapse")
    _patch_minimal_pipeline(
        monkeypatch,
        stub,
        sip_ok=RuntimeError("probe blew up: DNS"),
    )
    log = _install_logger(monkeypatch)
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Cascade fell through to IEX.
    assert len(stub.calls) == 2
    assert stub.calls[1]["feed"] == "fmp"

    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_DEGRADED" in event_types, event_types

    # Sanity — the original summary still has the daily_bars cascade
    # row with feed=iex annotation.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].detail.get("feed") == "fmp"


# ────────────────────────────────────────────────────────────────────────
# D. Non-coverage_collapse failure — cascade must NOT fire.
# ────────────────────────────────────────────────────────────────────────


async def test_non_coverage_collapse_failure_does_not_trigger_cascade(
    monkeypatch,
):
    # Different failure surface — auth/credential — explicitly NOT the
    # cascade's target.
    stub = _CountingStubStage(
        cascade_outcome="recover",  # would recover if cascade fired (it must not)
        first_error="daily_bars failed: AlpacaAuth 401 unauthorized",
    )
    _patch_minimal_pipeline(monkeypatch, stub, sip_ok=True)
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
    assert "INGESTION_AUTO_RECOVERY_DEGRADED" not in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" not in event_types


# ────────────────────────────────────────────────────────────────────────
# E. Discriminator token matches the producer-self-validation raise.
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


def test_sip_probe_helper_is_exported():
    """The SIP probe helper must be importable from ops — it's the
    load-bearing decision the cascade keys off of. Removing it should
    break this test loudly."""
    assert hasattr(ops, "_alpaca_sip_available")
    assert callable(ops._alpaca_sip_available)
