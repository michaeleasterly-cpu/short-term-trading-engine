"""Autonomous data-lane recovery — end-to-end + per-stage coverage
(2026-05-21 flip: operator directive "automate the god damn triage,
no operator-task bullshit in the self heal").

Coverage:
  A. LLM picks a valid action, stage subprocess succeeds → SUCCEEDED
     event emitted, no PR, no human gate.
  B. LLM picks an action OUTSIDE the whitelist → REJECTED event, no
     subprocess launched.
  C. LLM picks a whitelisted stage but invalid params → REJECTED event,
     no subprocess launched.
  D. Subprocess runs and exits non-zero → FAILED event, no recursion.
  E. End-to-end: synthetic INGESTION_AUTO_RECOVERY_FAILED event on the
     bus → triage service's data co-task consumes → LLM-mocked returns
     valid action → stage runs → SUCCEEDED event lands.

No real LLM. No real subprocess against scripts/ops.py. The Anthropic
client + the subprocess runner are both injected by the tests.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Touches sys.modules['ops'] via the scripts/ops.py shadow workaround
# (see test_llm_triage_service.py for the original rationale). Pin to
# the ops_shadow xdist group so it runs in the same worker as the
# sibling service test — single-process invariant.
pytestmark = pytest.mark.xdist_group("ops_shadow")


_REPO_ROOT = Path(__file__).resolve().parent.parent
_RECOVERY_PATH = _REPO_ROOT / "ops" / "llm_data_recovery.py"
_LTS_PATH = _REPO_ROOT / "ops" / "llm_triage_service.py"


# ── load ops.llm_data_recovery by file path (ops/ package-shadow safe).
_SAVED = {
    k: sys.modules.get(k)
    for k in (
        "ops",
        "ops.llm_data_recovery",
        "ops.engine_llm_triage",
        "ops.llm_lab_emitter",
    )
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_RECOVERY_PATH.parent)]
        sys.modules["ops"] = _pkg

    _r_spec = importlib.util.spec_from_file_location(
        "_recovery_under_test", _RECOVERY_PATH
    )
    assert _r_spec is not None and _r_spec.loader is not None
    rec = importlib.util.module_from_spec(_r_spec)
    sys.modules["_recovery_under_test"] = rec
    # Make the daemon's `from ops.llm_data_recovery import …` resolve to
    # this same module instance during the case-E daemon-side check.
    sys.modules["ops.llm_data_recovery"] = rec
    _r_spec.loader.exec_module(rec)

    # Stub engine + lab_emitter so the daemon can be imported alongside.
    _estub = types.ModuleType("ops.engine_llm_triage")

    async def _stub_engine_run_triage(*_a, **_k):  # pragma: no cover
        raise AssertionError("engine triage stub must not be called")

    _estub.run_triage = _stub_engine_run_triage
    sys.modules["ops.engine_llm_triage"] = _estub

    _lestub = types.ModuleType("ops.llm_lab_emitter")

    async def _stub_lab_emitter(*_a, **_k):  # pragma: no cover
        raise AssertionError("lab emitter stub must not be called")

    _lestub.run_lab_emitter_cotask = _stub_lab_emitter
    _lestub.LAB_EMITTER_TRIGGER_EVENT_TYPES = ()
    sys.modules["ops.llm_lab_emitter"] = _lestub

    # Load the daemon module (for case E end-to-end).
    _lts_spec = importlib.util.spec_from_file_location(
        "_lts_under_test_autonomous", _LTS_PATH
    )
    assert _lts_spec is not None and _lts_spec.loader is not None
    lts = importlib.util.module_from_spec(_lts_spec)
    sys.modules["_lts_under_test_autonomous"] = lts
    _lts_spec.loader.exec_module(lts)
finally:
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


# ────────────────────────────────────────────────────────────────────────
# Fakes — DB pool, Anthropic client, subprocess runner.
# ────────────────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def fetch(self, sql: str, *args):
        # build_data_recovery_context's recent-log query: return empty.
        return []

    async def fetchrow(self, sql: str, *args):
        # run_autonomous_recovery._LATEST_TRIGGER_SQL → return the most
        # recent matching event from pool.events; or
        # llm_triage_service._find_new_trigger.
        event_types = set(args[0])
        if "WHERE event_type = ANY" in sql and "ORDER BY recorded_at DESC" in sql:
            hits = [
                e for e in self._pool.events if e["event_type"] in event_types
            ]
            if not hits:
                # `_find_new_trigger` also passes a cursor as arg2 — filter on it.
                return None
            if len(args) >= 2:
                cursor = args[1]
                hits = [e for e in hits if e["recorded_at"] > cursor]
            if not hits:
                return None
            newest = max(hits, key=lambda e: e["recorded_at"])
            # _find_new_trigger only reads recorded_at; the full row works.
            return newest
        return None

    async def execute(self, sql: str, *args):
        # Capture emitted events for inspection.
        # signature: (engine, run_id, event_type, severity, message, data_json)
        self._pool.emitted.append(
            {
                "engine": args[0],
                "run_id": args[1],
                "event_type": args[2],
                "severity": args[3],
                "message": args[4],
                "data": json.loads(args[5]) if args[5] else {},
            }
        )


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events: list[dict] = events or []
        self.emitted: list[dict] = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, inp: int = 100, out: int = 50) -> None:
        self.input_tokens = inp
        self.output_tokens = out


class _Resp:
    def __init__(self, text: str, *, inp: int = 100, out: int = 50) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage(inp, out)


class _FakeMessages:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.create_calls: list[dict] = []

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._resp


class _FakeClient:
    def __init__(self, resp: _Resp) -> None:
        self.messages = _FakeMessages(resp)

    async def aclose(self) -> None:
        return None


def _make_client_factory(resp: _Resp):
    client = _FakeClient(resp)

    def factory():
        return client

    factory.client = client  # for introspection in tests
    return factory


def _trigger_event(event_type: str = "DATA_REPAIR_ESCALATED") -> dict:
    return {
        "event_type": event_type,
        "message": f"synthetic {event_type}",
        "recorded_at": datetime.now(UTC),
        "data": {"request_id": "req-syn-1"},
    }


# ────────────────────────────────────────────────────────────────────────
# Case A — valid action + successful subprocess → SUCCEEDED.
# ────────────────────────────────────────────────────────────────────────


async def test_case_a_valid_action_success_emits_succeeded(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {"repair_gaps": True, "feed": "iex"},
                "rationale": "completeness gap; targeted heal",
                "confidence": 0.85,
            }
        )
    )
    factory = _make_client_factory(resp)

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append({"argv": argv, "env": env, "cwd": cwd, "timeout": timeout})
        return 0, "ok\n", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event("DATA_REPAIR_ESCALATED"),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert len(runner_calls) == 1
    argv = runner_calls[0]["argv"]
    assert argv[1].endswith("scripts/ops.py")
    assert "--stage" in argv and "daily_bars" in argv
    assert "--param" in argv and "repair_gaps=true" in argv
    assert "--param" in argv and "feed=iex" in argv

    # Credential-starved env — no ANTHROPIC* / ALPACA* / *_KEY.
    env = runner_calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    for k in env:
        assert not k.startswith("ANTHROPIC_"), k
        assert not k.startswith("ALPACA_"), k

    # Terminal event — SUCCEEDED, no draft-PR side effect possible.
    terminal = [e for e in pool.emitted if e["event_type"].startswith("DATA_RECOVERY_ACTION_")]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert terminal[0]["data"]["action"]["stage_name"] == "daily_bars"
    # No "pr_link" key — autonomous lane never opens a PR.
    assert "pr_link" not in terminal[0]["data"]


# ────────────────────────────────────────────────────────────────────────
# Case B — LLM picks a stage OUTSIDE the whitelist → REJECTED, no run.
# ────────────────────────────────────────────────────────────────────────


async def test_case_b_off_whitelist_stage_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    pool = _Pool()
    # 'sentinel_lab_probe' is not in the autonomous data-lane whitelist.
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "sentinel_lab_probe",
                "params": {},
                "rationale": "make-believe",
                "confidence": 0.5,
            }
        )
    )
    factory = _make_client_factory(resp)

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover — must not run
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event("DATA_REPAIR_ESCALATED"),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_REJECTED"
    assert runner_calls == []  # NO subprocess
    terminal = [e for e in pool.emitted if e["event_type"].startswith("DATA_RECOVERY_ACTION_")]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_REJECTED"
    assert "not in whitelist" in terminal[0]["data"]["reason"]


# ────────────────────────────────────────────────────────────────────────
# Case C — whitelisted stage, invalid params → REJECTED, no run.
# ────────────────────────────────────────────────────────────────────────


async def test_case_c_invalid_param_value_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    pool = _Pool()
    # lookback_days=999 is out of [1, 30] sanity range.
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {"lookback_days": 999},
                "rationale": "speculative",
                "confidence": 0.4,
            }
        )
    )
    factory = _make_client_factory(resp)

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool, _trigger_event(), client_factory=factory, runner=fake_runner
    )
    assert out == "DATA_RECOVERY_ACTION_REJECTED"
    assert runner_calls == []
    terminal = [e for e in pool.emitted if e["event_type"].startswith("DATA_RECOVERY_ACTION_")]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_REJECTED"
    assert "out of range" in terminal[0]["data"]["reason"]


async def test_case_c_off_whitelist_param_rejected(monkeypatch) -> None:
    """A whitelisted stage with a NON-whitelisted param name → REJECTED."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "data_validation",
                "params": {"force_refresh": True},  # data_validation takes NO params
                "rationale": "shoehorn",
                "confidence": 0.3,
            }
        )
    )
    factory = _make_client_factory(resp)

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool, _trigger_event(), client_factory=factory, runner=fake_runner
    )
    assert out == "DATA_RECOVERY_ACTION_REJECTED"
    assert runner_calls == []


async def test_case_c_force_refresh_all_active_blocked(monkeypatch) -> None:
    """Operator-banned catastrophic combo — force_refresh + all_active."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {"force_refresh": True, "universe": "all_active"},
                "rationale": "ambitious",
                "confidence": 0.9,
            }
        )
    )
    factory = _make_client_factory(resp)

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool, _trigger_event(), client_factory=factory, runner=fake_runner
    )
    assert out == "DATA_RECOVERY_ACTION_REJECTED"
    assert runner_calls == []
    terminal = [e for e in pool.emitted if e["event_type"].startswith("DATA_RECOVERY_ACTION_")]
    assert "operator-banned" in terminal[0]["data"]["reason"]


# ────────────────────────────────────────────────────────────────────────
# Case D — subprocess runs and exits non-zero → FAILED. No recursion.
# ────────────────────────────────────────────────────────────────────────


async def test_case_d_subprocess_failure_emits_failed_no_recursion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "data_validation",
                "params": {},
                "rationale": "re-run validation",
                "confidence": 0.7,
            }
        )
    )
    factory = _make_client_factory(resp)

    invocations: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        invocations.append(argv)
        return 2, "", "validation failed\n"

    out = await rec.handle_data_recovery_escalation(
        pool, _trigger_event(), client_factory=factory, runner=fake_runner
    )
    assert out == "DATA_RECOVERY_ACTION_FAILED"
    # Single-shot: exactly ONE subprocess invocation, NEVER retried in-process.
    assert len(invocations) == 1
    terminal = [e for e in pool.emitted if e["event_type"].startswith("DATA_RECOVERY_ACTION_")]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_FAILED"
    assert terminal[0]["data"]["result"]["returncode"] == 2


# ────────────────────────────────────────────────────────────────────────
# Case E — end-to-end through the daemon's autonomous trigger fetcher.
# ────────────────────────────────────────────────────────────────────────


async def test_case_e_end_to_end_ingestion_auto_recovery_failed(
    monkeypatch,
) -> None:
    """Synthetic INGESTION_AUTO_RECOVERY_FAILED on the bus →
    ``run_autonomous_recovery`` picks it up, the LLM-mocked client
    returns a valid action, the stage subprocess runs, and a
    DATA_RECOVERY_ACTION_SUCCEEDED event lands. Zero operator action."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    bus_event = {
        "event_type": "INGESTION_AUTO_RECOVERY_FAILED",
        "severity": "ERROR",
        "message": "auto-cascade FAILED: daily_bars coverage_collapse",
        "recorded_at": datetime.now(UTC),
        "data": {
            "stage": "daily_bars",
            "cascade_mode": "repair_gaps",
            "first_error": "coverage collapse: 2026-05-21 has 6900 tickers",
        },
    }
    pool = _Pool(events=[bus_event])

    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {"repair_coverage": True},
                "rationale": "coverage_collapse → repair_coverage targeted heal",
                "confidence": 0.9,
            }
        )
    )
    factory = _make_client_factory(resp)

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.run_autonomous_recovery(
        pool, client_factory=factory, runner=fake_runner
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    # The stage subprocess was invoked exactly once with the LLM-chosen
    # action — no draft PR, no human-merge gate.
    assert len(runner_calls) == 1
    argv = runner_calls[0]
    assert "--stage" in argv and "daily_bars" in argv
    assert "repair_coverage=true" in argv

    terminal = [
        e for e in pool.emitted
        if e["event_type"].startswith("DATA_RECOVERY_ACTION_")
    ]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert (
        terminal[0]["data"]["trigger_event_type"]
        == "INGESTION_AUTO_RECOVERY_FAILED"
    )


async def test_case_e_no_open_trigger_returns_none_emits_nothing(
    monkeypatch,
) -> None:
    """No matching event on the bus → safe no-op, no terminal emit."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool(events=[])

    factory = _make_client_factory(_Resp("{}"))

    def fake_runner(*_a, **_kw):  # pragma: no cover
        raise AssertionError("runner must not be called on no-trigger path")

    out = await rec.run_autonomous_recovery(
        pool, client_factory=factory, runner=fake_runner
    )
    assert out is None
    assert pool.emitted == []


# ────────────────────────────────────────────────────────────────────────
# No-key — safe no-op (operator may run the daemon without a key set).
# ────────────────────────────────────────────────────────────────────────


async def test_no_api_key_rejects_without_subprocess(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pool = _Pool()

    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event(),
        client_factory=_make_client_factory(_Resp("{}")),
        runner=fake_runner,
    )
    # No key → llm_recovery_decision returns None → REJECTED, no subprocess.
    assert out == "DATA_RECOVERY_ACTION_REJECTED"
    assert runner_calls == []


# ────────────────────────────────────────────────────────────────────────
# Structural — the autonomous triggers cover the three escalation classes
# the operator specified in the directive.
# ────────────────────────────────────────────────────────────────────────


def test_autonomous_trigger_event_types_is_the_three_data_lane_classes() -> None:
    assert rec.AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES == (
        "DATA_REPAIR_ESCALATED",
        "DATA_SOURCE_ESCALATED",
        "INGESTION_AUTO_RECOVERY_FAILED",
    )


def test_daemon_data_lane_routes_through_autonomous_recovery() -> None:
    """The daemon's data co-task wires ``run_autonomous_recovery`` as
    ``triage_fn`` — the autonomous chain, not the draft-PR advisory path.
    """
    src = (_REPO_ROOT / "ops" / "llm_triage_service.py").read_text(
        encoding="utf-8"
    )
    assert "from ops.llm_data_recovery import" in src
    assert "run_autonomous_recovery" in src
    assert "triage_fn=run_autonomous_recovery" in src
    # The advisory data-lane import is gone (the engine lane is still
    # PR-gated; this assertion is data-lane-only).
    assert "from ops.llm_data_triage import run_triage" not in src


def test_whitelist_excludes_engine_and_roster_mutation_paths() -> None:
    """Authority scope: operational re-runs of existing ops.py stages
    only. No engine code paths, no roster mutations, no LIVE-trading
    actions in the whitelist."""
    allowed_stage_names = {name for name, _ in rec._AUTONOMOUS_DATA_ACTIONS}  # noqa: SLF001
    forbidden = {
        "engine_profile_mutate",
        "roster_mutate",
        "live_trade",
        "kill_switch",
        "allocate",
    }
    assert allowed_stage_names.isdisjoint(forbidden)


def test_persona_file_exists_and_v2_default_pinned() -> None:
    # v2 is the default (PR-#NNN ship); v1 file MUST also still exist
    # for the env-var rollback path (LLM_DATA_RECOVERY_PERSONA_VERSION=v1).
    assert rec.PERSONA_VERSION == "v2"
    v1_path = _REPO_ROOT / "docs" / "llm_triage_personas" / "data_recovery_v1.md"
    v2_path = _REPO_ROOT / "docs" / "llm_triage_personas" / "data_recovery_v2.md"
    assert v1_path.exists(), "v1 persona must remain for rollback"
    assert v2_path.exists(), "v2 persona is the default"


def test_validate_recovery_action_accepts_canonical_repair_gaps() -> None:
    action = rec.RecoveryAction(
        stage_name="daily_bars",
        params={"repair_gaps": True, "feed": "iex", "lookback_days": 5},
        rationale="test",
        confidence=0.9,
    )
    ok, reason = rec.validate_recovery_action(action)
    assert ok, reason


def test_validate_recovery_action_rejects_unknown_universe_csv_too_long() -> None:
    big_csv = ",".join(["TICKER"] * 1000)  # well over 4kb
    action = rec.RecoveryAction(
        stage_name="daily_bars",
        params={"universe": big_csv},
        rationale="too big",
        confidence=0.1,
    )
    ok, reason = rec.validate_recovery_action(action)
    assert not ok and "csv length" in reason


# ────────────────────────────────────────────────────────────────────────
# v2 — six pattern-mapped cases + one negative-pattern + one regression.
# Each test mocks the LLM returning the action the v2 persona documents
# for the pattern, then asserts the dispatcher behavior matches the
# operator-locked decision for that pattern.
# ────────────────────────────────────────────────────────────────────────


def _trigger_event_with_message(
    event_type: str, message: str, *, data: dict | None = None
) -> dict:
    return {
        "event_type": event_type,
        "message": message,
        "recorded_at": datetime.now(UTC),
        "data": data or {"request_id": "req-syn-1"},
    }


async def test_v2_pattern_1_timeout_3600s_repair_coverage(monkeypatch) -> None:
    """Pattern 1 — daily_bars 3600s timeout → narrow-scope
    daily_bars(force_refresh=true, repair_coverage=true)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {"force_refresh": True, "repair_coverage": True},
                "rationale": "pattern=timeout_3600s_narrow_scope",
                "confidence": 0.85,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "INGESTION_AUTO_RECOVERY_FAILED",
            "daily_bars timed out after 3600.0s on chunk 4/12",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert len(runner_calls) == 1
    argv = runner_calls[0]
    assert "daily_bars" in argv
    assert "force_refresh=true" in argv
    assert "repair_coverage=true" in argv


async def test_v2_pattern_2_pooler_drop_full_refresh_sip(monkeypatch) -> None:
    """Pattern 2 — Supabase pooler drop → daily_bars force_refresh active sip e1."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {
                    "force_refresh": True,
                    "universe": "active",
                    "feed": "sip",
                    "end_offset_days": 1,
                },
                "rationale": "pattern=pooler_drop_reinvoke",
                "confidence": 0.9,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "INGESTION_AUTO_RECOVERY_FAILED",
            "daily_bars failed: connection was closed in the middle of operation",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert len(runner_calls) == 1
    argv = runner_calls[0]
    assert "force_refresh=true" in argv
    assert "universe=active" in argv
    assert "feed=sip" in argv
    assert "end_offset_days=1" in argv


async def test_v2_pattern_3_sip_403_iex_failover_degraded(monkeypatch) -> None:
    """Pattern 3 — Alpaca SIP 403 → LLM picks IEX failover → dispatcher
    emits INGESTION_AUTO_RECOVERY_DEGRADED alongside the stage run."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {
                    "force_refresh": True,
                    "universe": "active",
                    "feed": "iex",
                    "end_offset_days": 1,
                },
                "rationale": "pattern=sip_403_to_iex",
                "confidence": 0.75,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "INGESTION_AUTO_RECOVERY_FAILED",
            "daily_bars 403 alpaca: subscription does not permit querying recent SIP data",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert len(runner_calls) == 1
    # Degraded marker emitted alongside the SUCCEEDED — both must land
    # on the bus so the operator sees the IEX failover cause.
    degraded = [
        e
        for e in pool.emitted
        if e["event_type"] == "INGESTION_AUTO_RECOVERY_DEGRADED"
    ]
    assert len(degraded) == 1
    assert "IEX failover" in degraded[0]["message"]
    assert degraded[0]["data"]["action"]["params"]["feed"] == "iex"
    terminal = [
        e
        for e in pool.emitted
        if e["event_type"].startswith("DATA_RECOVERY_ACTION_")
    ]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_SUCCEEDED"


async def test_v2_pattern_4_greeks_pro_401_skipped(monkeypatch) -> None:
    """Pattern 4 — greeks_pro 401: the LLM picks greeks_max_pain (skip-with-
    warning stage). Dispatcher emits DATA_RECOVERY_ACTION_SKIPPED and does
    NOT invoke the runner."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "greeks_max_pain",
                "params": {},
                "rationale": "pattern=provider_auth_failure",
                "confidence": 0.95,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "DATA_REPAIR_ESCALATED",
            "greeks_max_pain failed: greeks_pro /api/analytics/maxpain returned 401",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SKIPPED"
    assert runner_calls == []  # NO subprocess
    skipped = [
        e for e in pool.emitted if e["event_type"] == "DATA_RECOVERY_ACTION_SKIPPED"
    ]
    assert len(skipped) == 1
    assert skipped[0]["data"]["reason"] == "provider_auth_failure"
    # ``greeks_max_pain`` is NOT in the autonomous whitelist — pure
    # skip-only landmine. The skip happens BEFORE the validator that
    # would otherwise reject it on whitelist-miss; both fences working
    # is fine, but the SKIPPED event should be the terminal that lands.
    assert "greeks_max_pain" in rec.SKIP_WITH_WARNING_ACTIONS


async def test_v2_pattern_5_fundamentals_refresh_dispatch(monkeypatch) -> None:
    """Pattern 5 — fundamentals_quarterly_complete validation defect →
    fundamentals_refresh stage (now in whitelist)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "fundamentals_refresh",
                "params": {},
                "rationale": "pattern=fundamentals_completeness_refresh",
                "confidence": 0.8,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "DATA_REPAIR_ESCALATED",
            "data_validation failed: validation suite failed: ['fundamentals_quarterly_complete']",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert len(runner_calls) == 1
    argv = runner_calls[0]
    assert "fundamentals_refresh" in argv
    # Whitelist self-check — fundamentals_refresh must be present now.
    allowed = {name for name, _ in rec._AUTONOMOUS_DATA_ACTIONS}
    assert "fundamentals_refresh" in allowed


async def test_v2_pattern_6_negative_coverage_collapse_repair_gaps_rejected(
    monkeypatch,
) -> None:
    """Pattern 6 (NEGATIVE) — error contains 'coverage collapse' AND the
    LLM returns repair_gaps → REJECTED with reason=negative_pattern_match.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "daily_bars",
                "params": {"repair_gaps": True},
                "rationale": "wrong pick — repair_gaps on coverage_collapse",
                "confidence": 0.4,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):  # pragma: no cover
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "INGESTION_AUTO_RECOVERY_FAILED",
            "daily_bars coverage collapse: 2026-05-21 has 6900 tickers",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_REJECTED"
    assert runner_calls == []
    terminal = [
        e
        for e in pool.emitted
        if e["event_type"].startswith("DATA_RECOVERY_ACTION_")
    ]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "DATA_RECOVERY_ACTION_REJECTED"
    assert terminal[0]["data"]["reason"] == "negative_pattern_match"
    # The matched (substring, banned_stage) tuple is in the payload for
    # the audit log — operator can correlate.
    assert (
        terminal[0]["data"]["negative_pattern"]["banned_stage_name"]
        == "repair_gaps"
    )


async def test_v2_pattern_regression_unknown_failure_falls_through(
    monkeypatch,
) -> None:
    """Pattern 7 (regression) — unfamiliar failure shape; the LLM returns a
    valid whitelisted action via general reasoning. The dispatcher MUST
    dispatch it normally (no pattern-match was required)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pool = _Pool()
    resp = _Resp(
        json.dumps(
            {
                "stage_name": "data_validation",
                "params": {},
                "rationale": "no pattern match — re-run the suite",
                "confidence": 0.6,
            }
        )
    )
    factory = _make_client_factory(resp)
    runner_calls: list = []

    def fake_runner(argv, *, env, cwd, timeout):
        runner_calls.append(argv)
        return 0, "", ""

    out = await rec.handle_data_recovery_escalation(
        pool,
        _trigger_event_with_message(
            "DATA_REPAIR_ESCALATED",
            "unknown new failure shape — nothing pattern-mapped",
        ),
        client_factory=factory,
        runner=fake_runner,
    )
    assert out == "DATA_RECOVERY_ACTION_SUCCEEDED"
    assert len(runner_calls) == 1
    assert "data_validation" in runner_calls[0]


def test_v2_negative_patterns_set_contains_coverage_collapse_repair_gaps() -> None:
    """Public surface check — the documented negative pattern is present
    in the frozen ``NEGATIVE_PATTERNS`` set."""
    assert ("coverage collapse", "repair_gaps") in rec.NEGATIVE_PATTERNS


def test_v2_skip_actions_set_contains_greeks_max_pain() -> None:
    """Public surface check — greeks_max_pain is skip-only, not whitelisted."""
    assert "greeks_max_pain" in rec.SKIP_WITH_WARNING_ACTIONS
    allowed = {name for name, _ in rec._AUTONOMOUS_DATA_ACTIONS}
    assert "greeks_max_pain" not in allowed
