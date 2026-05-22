"""Wave-2 deterministic self-heal cascade — D2 / D3 / D5 / D13 regression.

Pins each row in the spec
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md`` §1 rows D2 D3 D5 D13 (Wave 2 / robustness bundled
PR). Each test mocks the row-specific failure shape on a stage and
asserts the cascade's recovery shape + event name fires; a pin test
asserts that NON-matching failures do NOT trigger any Wave-2 cascade
(fail-fast on unknown shapes).

Tests:
* D2 — daily_bars TIMEOUT on a non-chunked invocation → re-invoke with
  ``force_refresh=True universe=active feed=sip``;
  ``INGESTION_AUTO_RECOVERED_TIMEOUT`` event lands.
* D3 — stage error contains "connection was closed in the middle of
  operation" → ONE re-invoke with same config;
  ``INGESTION_AUTO_RECOVERED_CONNDROP`` event lands; a second-failure
  variant proves the cascade does NOT loop.
* D5 — stage error contains "401" → retry once; on a second 401 the
  ``PROVIDER_AUTH_ESCALATED`` event lands AND cmd_update keeps going
  (daemon stays alive — the §4-Q2 invariant).
* D13 — stage error contains asyncpg pool-exhaustion token → pool
  recycle + retry; ``POOL_CIRCUIT_BREAKER_TRIPPED`` event lands.
* Pin — a NON-matching failure (RuntimeError with random text) does
  NOT trigger any of these cascades.
* Structural — every Wave-2 cascade event name is referenced in ops.py.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

# Load scripts/ops.py by path under a private name (canonical
# ops-shadow pattern — same trick used by the Wave-1 test).
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_robustness_cascade", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_robustness_cascade"] = ops
_spec.loader.exec_module(ops)


# pytest-xdist: pin this ops-shadow module to one worker so the
# scripts/ops.py load stays single-process (ops-package-shadow invariant).
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────


class _FakePool:
    """No-op pool — stages are stubbed so the pool is never touched."""

    async def close(self):
        return None


class _FakeDBLog:
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


def _install_logger():
    import structlog
    return structlog.get_logger("test.robustness_cascade")


def _patch_single_stage_pipeline(
    monkeypatch,
    *,
    stage_name: str,
    stub_fn,
    timeout_sec: float | None = None,
):
    """Reduce ``cmd_update`` to a single-stage pipeline whose stage is
    backed by ``stub_fn``. Side cascades (coverage_collapse, Wave-1
    validation) are no-op'd so this test only exercises Wave-2.
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 7}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_coverage_collapse", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_validation_failures", _noop)
    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop)

    timeout = timeout_sec if timeout_sec is not None else ops.HEAVY_STAGE_TIMEOUT_SEC
    spec = (
        (
            stage_name,
            lambda pool, cfg: (lambda: stub_fn(pool, cfg)),
            timeout,
        ),
    )
    monkeypatch.setattr(ops, "_STAGE_SPECS", spec)


# ────────────────────────────────────────────────────────────────────────
# D2 — daily_bars TIMEOUT (non-chunked) → chunked force_refresh
# ────────────────────────────────────────────────────────────────────────


async def test_d2_daily_bars_timeout_triggers_chunked_force_refresh(monkeypatch):
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({
            "force_refresh": bool(cfg.get("force_refresh", False)),
            "feed": cfg.get("feed"),
            "universe": cfg.get("universe"),
            "end_offset_days": cfg.get("end_offset_days"),
        })
        is_cascade = bool(cfg.get("force_refresh", False))
        if not is_cascade:
            # First-pass non-chunked timeout — emulate the exact message
            # `_run_stage` emits on TimeoutError.
            raise TimeoutError("timed out after 3600s")
        # Cascade invocation — recovers OK.
        return {"rows_upserted": 7300, "mode": "force_refresh_chunked"}

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="daily_bars", stub_fn=_stub, timeout_sec=0.5,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. _stage_daily_bars was invoked TWICE — first-pass + Wave-2 cascade.
    assert len(calls) == 2, calls
    # 2. First call: NOT force_refresh (the normal pull that timed out).
    assert calls[0]["force_refresh"] is False
    # 3. Cascade call: force_refresh + feed=sip + universe=active.
    assert calls[1]["force_refresh"] is True
    assert calls[1]["feed"] == "sip"
    assert calls[1]["universe"] == "active"
    # 4. Event landed.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_TIMEOUT" in event_types, event_types
    ev = next(
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_TIMEOUT"
    )
    assert ev["data"].get("stage") == "daily_bars"
    assert ev["data"].get("cascade_mode") == "force_refresh_chunked"
    # 5. The replaced StageResult shows OK with cascade annotation.
    db_rows = [s for s in summary.stages if s.name == "daily_bars"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "OK"
    assert db_rows[0].detail.get("cascade") is True
    assert db_rows[0].detail.get("trigger") == "timeout"


async def test_d2_does_not_fire_if_invocation_was_already_chunked(monkeypatch):
    """Pin: when the daily_bars_config already carries force_refresh=True
    (operator's manual chunked path), a timeout on THAT run does NOT
    trigger D2's cascade — there's no escalation path beyond chunking.
    """
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"force_refresh": bool(cfg.get("force_refresh", False))})
        raise TimeoutError("timed out after 3600s")

    async def _fake_load_config(_pool):
        return {
            "universe": "active",
            "lookback_days": 7,
            "force_refresh": True,
        }

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="daily_bars", stub_fn=_stub, timeout_sec=0.5,
    )
    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    log = _install_logger()
    db_log = _FakeDBLog()

    await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Only ONE invocation — the cascade did NOT fire.
    assert len(calls) == 1, calls
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_TIMEOUT" not in event_types


# ────────────────────────────────────────────────────────────────────────
# D3 — connection-drop mid-stage → one-shot re-invoke
# ────────────────────────────────────────────────────────────────────────


async def test_d3_connection_drop_triggers_one_shot_reinvoke(monkeypatch):
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1})
        if len(calls) == 1:
            raise RuntimeError(
                "connection was closed in the middle of operation"
            )
        return {"rows_upserted": 1000}

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Cascade fired exactly once → 2 stage-calls total.
    assert len(calls) == 2, calls
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_CONNDROP" in event_types, event_types
    db_rows = [s for s in summary.stages if s.name == "fundamentals_refresh"]
    assert len(db_rows) == 1
    assert db_rows[0].status == "OK"
    assert db_rows[0].detail.get("trigger") == "connection_drop"


async def test_d3_second_failure_does_not_loop(monkeypatch):
    """A second connection-drop on the cascade re-invoke leaves the stage
    FAILED and does NOT trigger a third invocation."""
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1})
        raise RuntimeError("connection was closed in the middle of operation")

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Exactly TWO calls — first-pass + ONE cascade re-invoke, no loop.
    assert len(calls) == 2, calls
    # The cascade emitted START + RECOVERY_FAILED, NOT RECOVERED_CONNDROP.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_CONNDROP" not in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" in event_types
    db_rows = [s for s in summary.stages if s.name == "fundamentals_refresh"]
    assert db_rows[0].status == "FAILED"


# ────────────────────────────────────────────────────────────────────────
# D5 — provider 401 → retry once → ESCALATED on second 401
# ────────────────────────────────────────────────────────────────────────


async def test_d5_first_401_recovers_on_retry(monkeypatch):
    """First 401 → cascade retries → recovery on second call."""
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1})
        if len(calls) == 1:
            raise RuntimeError(
                "Client error '401 Unauthorized' for url "
                "https://api.example.com/v1/data"
            )
        return {"rows_upserted": 1}

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    await ops.cmd_update(_FakePool(), log, db_log, dry_run=False, force=True)

    assert len(calls) == 2
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_AUTH" in event_types
    assert "PROVIDER_AUTH_ESCALATED" not in event_types


async def test_d5_second_401_emits_escalation_and_daemon_continues(monkeypatch):
    """Both calls 401 → PROVIDER_AUTH_ESCALATED + daemon stays alive."""
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1})
        raise RuntimeError(
            "Client error '401 Unauthorized' for url "
            "https://api.example.com/v1/data"
        )

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # ONE retry — second call confirms bad creds.
    assert len(calls) == 2, calls
    event_types = [e["event_type"] for e in db_log.events]
    assert "PROVIDER_AUTH_ESCALATED" in event_types, event_types
    ev = next(
        e for e in db_log.events
        if e["event_type"] == "PROVIDER_AUTH_ESCALATED"
    )
    # Provider inferred + URL snippet carried.
    assert ev["data"].get("provider") == "fmp"
    assert "https://api.example.com/v1/data" in (
        ev["data"].get("failed_url_snippet") or ""
    )
    assert ev["data"].get("daemon_continuing") is True
    # The stage stays FAILED in the summary; cmd_update returned a
    # summary (didn't raise). That's the daemon-alive invariant.
    db_rows = [s for s in summary.stages if s.name == "fundamentals_refresh"]
    assert db_rows[0].status == "FAILED"


# ────────────────────────────────────────────────────────────────────────
# D13 — pool exhaustion → recycle pool + retry once
# ────────────────────────────────────────────────────────────────────────


async def test_d13_pool_exhaustion_recycles_and_retries(monkeypatch):
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1, "pool_id": id(pool)})
        if len(calls) == 1:
            raise RuntimeError(
                "asyncpg.exceptions.TooManyConnectionsError: "
                "connection slots are reserved for non-replication "
                "superuser connections"
            )
        return {"rows_upserted": 5}

    # Patch the recycle helper inside tpcore.db so we don't touch the DB.
    fresh_pool = _FakePool()
    recycle_calls: list[dict] = []

    async def _stub_recycle(old_pool, db_url, *, max_size=4, **kwargs):
        recycle_calls.append({"db_url": db_url, "max_size": max_size})
        return fresh_pool

    import tpcore.db
    monkeypatch.setattr(tpcore.db, "recycle_asyncpg_pool", _stub_recycle)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Stage called twice; recycle helper called exactly once.
    assert len(calls) == 2, calls
    assert len(recycle_calls) == 1, recycle_calls
    # Second stage-call used the fresh pool (id differs from first).
    assert calls[0]["pool_id"] != calls[1]["pool_id"]
    # Events: TRIPPED → RECOVERED_POOL.
    event_types = [e["event_type"] for e in db_log.events]
    assert "POOL_CIRCUIT_BREAKER_TRIPPED" in event_types
    assert "INGESTION_AUTO_RECOVERED_POOL" in event_types
    tripped = next(
        e for e in db_log.events
        if e["event_type"] == "POOL_CIRCUIT_BREAKER_TRIPPED"
    )
    assert tripped["data"].get("scope") == "daemon_local_pool_only"
    # The replaced StageResult shows OK with cascade annotation.
    db_rows = [s for s in summary.stages if s.name == "fundamentals_refresh"]
    assert db_rows[0].status == "OK"
    assert db_rows[0].detail.get("cascade_mode") == "pool_recycle"


async def test_d13_recycle_failure_does_not_crash_daemon(monkeypatch):
    """If the pool-recycle helper ITSELF raises, the cascade logs
    INGESTION_AUTO_RECOVERY_FAILED + daemon keeps going (cmd_update
    returns)."""
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1})
        raise RuntimeError(
            "asyncpg.exceptions.TooManyConnectionsError: "
            "connection slots are reserved"
        )

    async def _stub_recycle_raises(old_pool, db_url, **kwargs):
        raise RuntimeError("Supabase capacity exhausted across cluster")

    import tpcore.db
    monkeypatch.setattr(tpcore.db, "recycle_asyncpg_pool", _stub_recycle_raises)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # No second stage-call — recycle failed before retry.
    assert len(calls) == 1, calls
    event_types = [e["event_type"] for e in db_log.events]
    assert "POOL_CIRCUIT_BREAKER_TRIPPED" in event_types
    assert "INGESTION_AUTO_RECOVERY_FAILED" in event_types
    assert "INGESTION_AUTO_RECOVERED_POOL" not in event_types
    # Summary returned — daemon alive.
    assert summary is not None


# ────────────────────────────────────────────────────────────────────────
# PIN — random unknown failure does NOT trigger any Wave-2 cascade
# ────────────────────────────────────────────────────────────────────────


async def test_unknown_failure_shape_triggers_no_wave2_cascade(monkeypatch):
    calls: list[dict] = []

    async def _stub(pool, cfg):
        calls.append({"call": len(calls) + 1})
        # A failure shape that matches none of the Wave-2 token sets.
        raise RuntimeError(
            "schema drift detected: column foo missing on platform.bar"
        )

    _patch_single_stage_pipeline(
        monkeypatch, stage_name="fundamentals_refresh", stub_fn=_stub,
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # Stage called ONCE — no cascade fired.
    assert len(calls) == 1, calls
    event_types = [e["event_type"] for e in db_log.events]
    # NONE of the Wave-2 event names fired.
    for evname in (
        "INGESTION_AUTO_RECOVERED_TIMEOUT",
        "INGESTION_AUTO_RECOVERED_CONNDROP",
        "INGESTION_AUTO_RECOVERED_AUTH",
        "PROVIDER_AUTH_ESCALATED",
        "POOL_CIRCUIT_BREAKER_TRIPPED",
        "INGESTION_AUTO_RECOVERED_POOL",
    ):
        assert evname not in event_types, (evname, event_types)
    db_rows = [s for s in summary.stages if s.name == "fundamentals_refresh"]
    assert db_rows[0].status == "FAILED"


# ────────────────────────────────────────────────────────────────────────
# Structural — Wave-2 cascade event name contract pinned to ops.py
# ────────────────────────────────────────────────────────────────────────


def test_wave2_event_name_contract_pinned():
    """The 5 Wave-2 cascade event names must all live in scripts/ops.py
    — drift between spec + code surfaces as a missing event-name.
    """
    expected_event_names = {
        "INGESTION_AUTO_RECOVERED_TIMEOUT",       # D2
        "INGESTION_AUTO_RECOVERED_CONNDROP",      # D3
        "PROVIDER_AUTH_ESCALATED",                # D5
        "INGESTION_AUTO_RECOVERED_AUTH",          # D5 (success path)
        "POOL_CIRCUIT_BREAKER_TRIPPED",           # D13 trip event
        "INGESTION_AUTO_RECOVERED_POOL",          # D13 recovery event
    }
    src = _OPS_PATH.read_text()
    for name in expected_event_names:
        assert name in src, f"missing Wave-2 cascade event name in ops.py: {name}"


# ────────────────────────────────────────────────────────────────────────
# Helper unit tests
# ────────────────────────────────────────────────────────────────────────


def test_matches_any_substring_lowercase():
    assert ops._matches_any("HTTP 401 Unauthorized", ops._AUTH_401_TOKENS)
    assert ops._matches_any(
        "Status code 401 something", ops._AUTH_401_TOKENS,
    )
    assert not ops._matches_any("HTTP 404 not found", ops._AUTH_401_TOKENS)
    assert not ops._matches_any(None, ops._AUTH_401_TOKENS)
    assert not ops._matches_any("", ops._AUTH_401_TOKENS)


def test_extract_failed_url_snippet():
    err = (
        "Client error '401 Unauthorized' for url "
        "https://api.fmp.com/v3/quote/AAPL?apikey=secret"
    )
    out = ops._extract_failed_url_snippet(err)
    assert out.startswith("https://api.fmp.com/v3/quote/AAPL")
    assert ops._extract_failed_url_snippet("no url here") == ""
    assert ops._extract_failed_url_snippet("") == ""


def test_infer_provider_from_stage_mapped_and_default():
    assert ops._infer_provider_from_stage("daily_bars") == "alpaca"
    assert ops._infer_provider_from_stage("fundamentals_refresh") == "fmp"
    assert ops._infer_provider_from_stage("sec_filings") == "sec"
    assert ops._infer_provider_from_stage("totally_unknown_stage") == "unknown"
