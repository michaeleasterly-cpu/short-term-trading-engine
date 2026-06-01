"""console-api data-pipeline endpoints — REQ-001..REQ-010 contract pins.

Spec: 2026-05-29 build_real_data_pipeline_operations_console.

These tests pin:
  TEST-001  status endpoint returns live shape (no hardcoded PASS)
  TEST-002  no-store cache headers present
  TEST-003  run-update endpoint dispatches OPERATOR_RUN_REQUESTED
  TEST-004  run-validation endpoint dispatches
  TEST-005  run-feed validates against the allowlist
  TEST-006  active-job conflict returns 409
  TEST-007  unknown check renders UNKNOWN, never PASS
  TEST-008  job-status reflects lifecycle event terminal states
  TEST-009  audit row written to application_log with actor
  TEST-010  concurrent clicks rejected via 409
  TEST-011  bearer-token guard: 503 when env unset
  TEST-012  bearer-token guard: 403 on invalid token
  TEST-013  abort endpoint writes OPERATOR_RUN_ABORTED row
  TEST-014  bootstrap behavior: empty backend renders UNKNOWN lane
  TEST-015  active_job shape matches contract

All tests use a fake asyncpg pool — no live DB. The fake replays the
shapes that production rows return (asyncpg.Record-like dicts).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CONSOLE_API = _REPO / "console-api"

# CI venv has no fastapi (mirrors the streamlit precedent in
# .claude/rules/tests-and-ci.md — "CI venv has no streamlit → never
# import dashboard.py in a CI test"). console-api/main.py imports
# fastapi, so the tests that load main.py only run when fastapi is
# available locally. The data_pipeline module tests (most of the
# file) need only asyncpg + json and run everywhere.
_fastapi_required = pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None,
    reason=(
        "fastapi not installed in this env — console-api/main.py "
        "tests are operator-local only (matches the streamlit/CI "
        "precedent in .claude/rules/tests-and-ci.md)"
    ),
)


@pytest.fixture(scope="module")
def data_pipeline_module():
    """Load console-api/data_pipeline.py as a fresh module."""
    spec = importlib.util.spec_from_file_location(
        "_console_api_dp_under_test", _CONSOLE_API / "data_pipeline.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_console_api_dp_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def console_api_app(data_pipeline_module):
    """Load console-api/main.py + return the FastAPI app. We must add
    console-api to sys.path so the ``import data_pipeline`` inside
    main.py finds the module under test."""
    # Set DATABASE_URL to a noop string so lifespan can be skipped.
    os.environ.setdefault("DATABASE_URL", "postgresql://noop:noop@localhost/none")
    sys.path.insert(0, str(_CONSOLE_API))
    if "main" in sys.modules:
        del sys.modules["main"]
    # main.py does `import data_pipeline as data_pipeline_module` —
    # alias the under-test module so the import succeeds.
    sys.modules["data_pipeline"] = data_pipeline_module
    spec = importlib.util.spec_from_file_location(
        "_console_api_main_under_test", _CONSOLE_API / "main.py",
    )
    assert spec is not None and spec.loader is not None
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["_console_api_main_under_test"] = main_mod
    spec.loader.exec_module(main_mod)
    yield main_mod.app
    sys.path.remove(str(_CONSOLE_API))


# ───── fake asyncpg substrate ─────


class _FakeRecord(dict):
    """Sufficient stand-in for asyncpg.Record — supports r["key"]."""
    pass


class _FakeConn:
    """Substring-routed asyncpg.Connection stand-in.

    fixture['fetchrow'] is a list of (substring, row) tuples; on each
    call we walk the list and return the first row whose substring
    appears in the query. Same for fetchval / fetch. This avoids the
    brittle exact-query-key matching that exact whitespace formatting
    breaks."""

    def __init__(self, fixture: dict):
        self.fixture = fixture
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchval(self, query: str, *args):
        for needle, val in self.fixture.get("fetchval", []):
            if needle in query:
                return val
        return self.fixture.get("default_fetchval")

    async def fetchrow(self, query: str, *args):
        for needle, row in self.fixture.get("fetchrow", []):
            if needle in query:
                return _FakeRecord(row) if row else None
        return None

    async def fetch(self, query: str, *args):
        for needle, rows in self.fixture.get("fetch", []):
            if needle in query:
                return [_FakeRecord(r) for r in rows]
        return []

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, fixture: dict | None = None):
        self.fixture = fixture or {}
        self.connections: list[_FakeConn] = []

    def acquire(self):
        conn = _FakeConn(self.fixture)
        self.connections.append(conn)
        return _PoolCtx(conn)


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _base_fixture(**overrides) -> dict:
    """Default fixture — empty DB, no rows anywhere. Routing tables
    are lists of (substring, payload) tuples."""
    fx: dict = {
        "fetchval": [],
        "fetchrow": [],
        "fetch": [],
        "default_fetchval": None,
    }
    fx.update(overrides)
    return fx


# ───────────────── TEST-001 ─────────────────


@pytest.mark.asyncio
async def test_status_endpoint_returns_live_shape(data_pipeline_module):
    """REQ-003: status payload matches the contract — keys, types,
    no hardcoded PASS rows."""
    pool = _FakePool(_base_fixture())
    payload = await data_pipeline_module.fetch_status_payload(pool)
    # Top-level shape.
    assert set(payload.keys()) == {
        "status", "last_refreshed_at", "latest_run_id",
        "latest_data_ops_event", "summary", "checks",
        "self_heal_log", "active_job",
    }
    # summary keys.
    assert set(payload["summary"].keys()) == {
        "passed", "warnings", "failed", "confidence",
        "tickers_tracked", "daily_bars_60d", "cycle_latency",
        "forensics_open",
    }
    # Empty DB → UNKNOWN lane, not GREEN.
    assert payload["status"] == "UNKNOWN"
    # No hardcoded PASS in any check row.
    for c in payload["checks"]:
        assert c["status"] in ("UNKNOWN", "PASS", "WARN", "FAIL")
        # Empty DB → UNKNOWN for every check.
        assert c["status"] == "UNKNOWN"


# ───────────────── TEST-007 ─────────────────


@pytest.mark.asyncio
async def test_unknown_check_renders_unknown_not_pass(data_pipeline_module):
    """REQ-010: no false green. A check without a data_quality_log
    row in the last 72 h MUST render UNKNOWN, never PASS."""
    checks = await data_pipeline_module._fetch_validation_rows(
        _FakeConn(_base_fixture()),
    )
    assert all(c["status"] == "UNKNOWN" for c in checks)
    assert all(c["last_checked_at"] is None for c in checks)
    assert all(
        "no data_quality_log row" in c["notes"] for c in checks
    )


# ───────────────── TEST-014 ─────────────────


@pytest.mark.asyncio
async def test_bootstrap_empty_backend_renders_unknown_lane(
    data_pipeline_module,
):
    """REQ-010: fresh database with NO rows must render UNKNOWN,
    not GREEN (the operator must know the lane is unverified)."""
    pool = _FakePool(_base_fixture())
    payload = await data_pipeline_module.fetch_status_payload(pool)
    assert payload["status"] == "UNKNOWN"
    assert payload["latest_data_ops_event"]["status"] == "MISSING"
    assert payload["active_job"] is None


# ───────────────── TEST-008 ─────────────────


@pytest.mark.asyncio
async def test_job_status_terminal_states(data_pipeline_module):
    """REQ-007: job-status endpoint reflects the lifecycle event."""
    run_id = uuid.uuid4()
    base = datetime.now(UTC)
    rows = [
        _FakeRecord({
            "recorded_at": base, "event_type": "OPERATOR_RUN_REQUESTED",
            "severity": "INFO", "message": "queued",
            "data": json.dumps({"action": "run_update"}),
        }),
        _FakeRecord({
            "recorded_at": base + timedelta(seconds=5),
            "event_type": "OPERATOR_RUN_STARTED", "severity": "INFO",
            "message": "started", "data": "{}",
        }),
        _FakeRecord({
            "recorded_at": base + timedelta(seconds=200),
            "event_type": "OPERATOR_RUN_COMPLETED", "severity": "INFO",
            "message": "completed", "data": "{}",
        }),
    ]
    # Substring needle: this query targets application_log filtered by
    # run_id — pick a unique substring from the SQL in
    # data_pipeline.fetch_job_status.
    fixture = {
        "fetch": [("WHERE run_id = $1", rows)],
        "fetchval": [], "fetchrow": [],
    }
    pool = _FakePool(fixture)
    status = await data_pipeline_module.fetch_job_status(
        pool, str(run_id),
    )
    assert status is not None
    assert status["status"] == "SUCCESS"
    assert status["elapsed_seconds"] == 200
    assert len(status["events"]) == 3
    assert status["events"][-1]["event_type"] == "OPERATOR_RUN_COMPLETED"


# ───────────────── TEST-009 / TEST-013 ─────────────────


@pytest.mark.asyncio
async def test_audit_row_written_with_actor(data_pipeline_module):
    """REQ-008 audit: request_operator_run writes an
    OPERATOR_RUN_REQUESTED row with the supplied actor.

    TEST-003 from the 2026-05-29 task spec — covers run_update
    dispatch. F-001 (2026-05-29 expert review) fix: the row's
    ``data`` bind is a json-encoded STRING with explicit ``$4::jsonb``
    cast, not a raw Python dict (asyncpg can't auto-encode dicts to
    jsonb without a registered codec; production siblings already use
    this idiom)."""
    pool = _FakePool(_base_fixture())
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice@example.com", action="run_update",
    )
    assert desc["action"] == "run_update"
    assert desc["status"] == "QUEUED"
    # The fake conn captured the INSERT.
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    assert len(insert_calls) == 1
    sql, args = insert_calls[0]
    # F-001 contract pin: bind is a JSON-encoded string, NOT a dict,
    # AND the SQL has the $4::jsonb cast.
    assert "$4::jsonb" in sql, (
        "INSERT must cast the bind to jsonb explicitly"
    )
    assert isinstance(args[3], str), (
        "payload bind must be a JSON-encoded string, not a dict — "
        "asyncpg cannot encode raw dicts to jsonb without a codec"
    )
    payload = json.loads(args[3])
    assert args[0] == data_pipeline_module.OPERATOR_RUN_ENGINE
    assert args[2].startswith("operator alice@example.com requested")
    assert payload["actor"] == "alice@example.com"
    assert payload["action"] == "run_update"
    # F-003 fix: requested_at in payload == queued_at in descriptor.
    assert payload["requested_at"] == desc["queued_at"]


# ───────────────── TEST-004 ─────────────────


@pytest.mark.asyncio
async def test_run_validation_writes_validation_stage(data_pipeline_module):
    """TEST-004: action='run_validation' yields a descriptor with
    stage='data_validation' AND the audit row's payload reflects the
    same canonical stage. This is the spec-mandated TEST-004 that
    was previously implicit — explicit pin per F-007 (2026-05-29
    expert review)."""
    pool = _FakePool(_base_fixture())
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="run_validation",
    )
    assert desc["action"] == "run_validation"
    assert desc["stage"] == "data_validation"
    assert desc["status"] == "QUEUED"
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    assert len(insert_calls) == 1
    sql, args = insert_calls[0]
    assert "$4::jsonb" in sql
    payload = json.loads(args[3])
    assert payload["action"] == "run_validation"
    assert payload["stage"] == "data_validation"


@pytest.mark.asyncio
async def test_abort_writes_aborted_row(data_pipeline_module):
    """REQ-006 abort: abort_operator_run writes an
    OPERATOR_RUN_ABORTED row for the given job_id. F-001 contract pin
    on the jsonb cast carries through to abort as well."""
    pool = _FakePool(_base_fixture())
    job_id = str(uuid.uuid4())
    result = await data_pipeline_module.abort_operator_run(
        pool, actor="alice", job_id=job_id,
    )
    assert result["job_id"] == job_id
    assert result["status"] == "ABORTED"
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    assert len(insert_calls) == 1
    sql, args = insert_calls[0]
    assert "$4::jsonb" in sql
    assert isinstance(args[3], str)
    payload = json.loads(args[3])
    assert args[1] == uuid.UUID(job_id)
    assert payload["actor"] == "alice"


# ───────────────── TEST-005 ─────────────────


@pytest.mark.asyncio
async def test_run_feed_allowlist_blocks_arbitrary_stage(data_pipeline_module):
    """REQ-008: run-feed must validate stage names against an
    allowlist — arbitrary names are 400."""
    pool = _FakePool(_base_fixture())
    with pytest.raises(ValueError, match="not in RUN_FEED_ALLOWLIST"):
        await data_pipeline_module.request_operator_run(
            pool, actor="alice", action="run_feed",
            stage="rm_minus_rf_arbitrary",
        )


@pytest.mark.asyncio
async def test_run_feed_allowlist_accepts_known_stage(data_pipeline_module):
    """run-feed with an allowlisted stage produces a valid descriptor."""
    pool = _FakePool(_base_fixture())
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="run_feed", stage="daily_bars",
    )
    assert desc["stage"] == "daily_bars"
    assert desc["status"] == "QUEUED"


# ───────────────── TEST-006 / TEST-010 ─────────────────


@pytest.mark.asyncio
async def test_active_run_conflict_returns_conflict_error(data_pipeline_module):
    """REQ-008 concurrency: if an unresolved OPERATOR_RUN_REQUESTED
    exists, a second request raises ConflictError."""
    now = datetime.now(UTC)
    active_run_id = uuid.uuid4()
    op_row = _FakeRecord({
        "run_id": active_run_id,
        "recorded_at": now - timedelta(minutes=5),
        "message": "an active run",
        "data": json.dumps({"action": "run_update", "actor": "bob"}),
    })
    fixture = {
        "fetchrow": [
            # Order matters — match operator-run query before cron query.
            # data_pipeline._fetch_active_job calls operator first, then
            # cron. The operator SQL contains "OPERATOR_RUN_REQUESTED";
            # cron contains "INGESTION_START".
            ("OPERATOR_RUN_REQUESTED", op_row),
            ("INGESTION_START", None),
        ],
        "fetchval": [], "fetch": [],
    }
    pool = _FakePool(fixture)
    with pytest.raises(data_pipeline_module.ConflictError) as exc_info:
        await data_pipeline_module.request_operator_run(
            pool, actor="charlie", action="run_update",
        )
    detail = exc_info.value.args[0]
    assert detail["code"] == "active_run"
    assert detail["active_job"]["status"] == "RUNNING"


# ───────────────── TEST-011 / TEST-012 ─────────────────


@_fastapi_required
def test_bearer_token_guard_503_when_unset(
    console_api_app, monkeypatch,
):
    """REQ-008: until CONSOLE_OPS_TOKEN is configured, every operator
    action must 503 with a runbook reference. Belt-and-braces — the
    Next.js side ALSO blocks but console-api must not silently accept
    actions while unprotected."""
    monkeypatch.delenv("CONSOLE_OPS_TOKEN", raising=False)
    sys.path.insert(0, str(_CONSOLE_API))
    try:
        import main as console_main  # type: ignore[import]
        with pytest.raises(Exception) as exc_info:
            console_main._require_operator_token(
                authorization="Bearer anything",
            )
        # FastAPI HTTPException carries status_code=503.
        assert getattr(exc_info.value, "status_code", None) == 503
    finally:
        sys.path.remove(str(_CONSOLE_API))


@_fastapi_required
def test_bearer_token_guard_403_on_invalid_token(monkeypatch):
    """REQ-008: a request with a non-matching bearer token is 403."""
    monkeypatch.setenv("CONSOLE_OPS_TOKEN", "the-right-token")
    sys.path.insert(0, str(_CONSOLE_API))
    try:
        # Re-import so the env-var change takes effect.
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as console_main  # type: ignore[import]
        with pytest.raises(Exception) as exc_info:
            console_main._require_operator_token(
                authorization="Bearer the-wrong-token",
            )
        assert getattr(exc_info.value, "status_code", None) == 403
    finally:
        sys.path.remove(str(_CONSOLE_API))


@_fastapi_required
def test_bearer_token_guard_accepts_valid_token(monkeypatch):
    """Happy path — the right token is accepted, returns 'operator'."""
    monkeypatch.setenv("CONSOLE_OPS_TOKEN", "the-right-token")
    sys.path.insert(0, str(_CONSOLE_API))
    try:
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as console_main  # type: ignore[import]
        actor = console_main._require_operator_token(
            authorization="Bearer the-right-token",
        )
        assert actor == "operator"
    finally:
        sys.path.remove(str(_CONSOLE_API))


@_fastapi_required
def test_bearer_token_actor_header_accepted_when_bearer_valid(monkeypatch):
    """When the bearer is valid AND X-Console-Actor is set, the actor
    string flows from the header. This is how the Next.js forwarder
    surfaces the authenticated user."""
    monkeypatch.setenv("CONSOLE_OPS_TOKEN", "the-right-token")
    sys.path.insert(0, str(_CONSOLE_API))
    try:
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as console_main  # type: ignore[import]
        actor = console_main._require_operator_token(
            authorization="Bearer the-right-token",
            actor_header="alice@example.com",
        )
        assert actor == "alice@example.com"
    finally:
        sys.path.remove(str(_CONSOLE_API))


@_fastapi_required
def test_bearer_token_actor_header_ignored_when_bearer_invalid(monkeypatch):
    """An attacker who guesses the X-Console-Actor header but lacks
    the bearer token must still get 403 — the actor header is NOT a
    bypass."""
    monkeypatch.setenv("CONSOLE_OPS_TOKEN", "the-right-token")
    sys.path.insert(0, str(_CONSOLE_API))
    try:
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as console_main  # type: ignore[import]
        with pytest.raises(Exception) as exc_info:
            console_main._require_operator_token(
                authorization="Bearer wrong",
                actor_header="ceo@victim.com",
            )
        assert getattr(exc_info.value, "status_code", None) == 403
    finally:
        sys.path.remove(str(_CONSOLE_API))


# ───────────────── TEST-015 ─────────────────


@pytest.mark.asyncio
async def test_active_job_shape_matches_contract(data_pipeline_module):
    """REQ-011: active_job shape includes the timeline + progress
    fields needed by the UI RunningBanner."""
    now = datetime.now(UTC)
    active_run_id = uuid.uuid4()
    op_row = _FakeRecord({
        "run_id": active_run_id,
        "recorded_at": now - timedelta(minutes=5),
        "message": "queued",
        "data": json.dumps({"action": "run_update"}),
    })
    fixture = {
        "fetchrow": [
            ("OPERATOR_RUN_REQUESTED", op_row),
            ("INGESTION_START", None),
        ],
        "fetchval": [], "fetch": [],
    }
    pool = _FakePool(fixture)
    payload = await data_pipeline_module.fetch_status_payload(pool)
    job = payload["active_job"]
    assert job is not None
    expected_keys = {
        "job_id", "run_id", "type", "status", "started_at", "updated_at",
        "elapsed_seconds", "current_stage", "current_check",
        "completed_stages", "pending_stages", "failed_stage",
        "latest_log", "progress", "triggered_by",
    }
    assert expected_keys.issubset(set(job.keys()))
    assert job["status"] in ("RUNNING", "TIMEOUT")
    assert job["triggered_by"] == "operator"


# ───────────────── REQ-002 cache headers ─────────────────


@_fastapi_required
@pytest.mark.asyncio
async def test_data_pipeline_endpoint_sets_no_store_headers(console_api_app):
    """REQ-002: the endpoint sets Cache-Control: no-store + Pragma:
    no-cache before returning. We exercise the route function directly
    (bypassing the FastAPI lifespan to avoid a real-DB connection
    attempt) and assert the headers ended up on the Response."""
    from fastapi import Response

    # Find the FastAPI route function for /api/data-pipeline. Routes
    # are stored on app.routes; pick by path.
    route_fn = None
    for r in console_api_app.routes:
        if getattr(r, "path", None) == "/api/data-pipeline":
            route_fn = r.endpoint
            break
    assert route_fn is not None, "/api/data-pipeline route not found"

    console_api_app.state.pool = _FakePool(_base_fixture())
    response = Response()
    payload = await route_fn(response=response)
    assert isinstance(payload, dict)
    cache_ctrl = response.headers.get("cache-control", "").lower()
    assert "no-store" in cache_ctrl
    assert response.headers.get("pragma") == "no-cache"


# ───────────────── Surgical remediation TEST-001..TEST-010 ─────────────────
# Spec: 2026-05-29 make_data_pipeline_console_surgical_and_honest


@pytest.mark.asyncio
async def test_remediation_classification_covers_known_checks(data_pipeline_module):
    """Every CONSOLE_VALIDATION_CHECKS entry must have a
    CHECK_REMEDIATION entry, AND the class must be one of the seven
    valid classes. Drift here = console silently fails to surface a
    new check's remediation."""
    valid = data_pipeline_module.VALID_REMEDIATION_CLASSES
    for name in data_pipeline_module.CONSOLE_VALIDATION_CHECKS:
        assert name in data_pipeline_module.CHECK_REMEDIATION, (
            f"check {name!r} missing from CHECK_REMEDIATION map"
        )
        cls = data_pipeline_module.CHECK_REMEDIATION[name]["class"]
        assert cls in valid, (
            f"check {name!r} has invalid class {cls!r}; "
            f"expected one of {sorted(valid)}"
        )


@pytest.mark.asyncio
async def test_scoped_repair_dispatches_only_failed_tickers(data_pipeline_module):
    """TEST-001 (spec): a scoped repair for fundamentals_quarterly_
    completeness sends ONLY the affected tickers — does NOT trigger a
    full-stage refresh."""
    pool = _FakePool(_base_fixture())
    failed = ["ADTX", "ADV", "AER", "AEVA"]
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="repair_failed_scope",
        stage="fundamentals_refresh", tickers=failed,
    )
    assert desc["action"] == "repair_failed_scope"
    assert desc["stage"] == "fundamentals_refresh"
    assert desc["tickers"] == failed
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    assert len(insert_calls) == 1
    sql, args = insert_calls[0]
    payload = json.loads(args[3])
    assert payload["tickers"] == failed
    assert payload["action"] == "repair_failed_scope"


@pytest.mark.asyncio
async def test_sec_fallback_runs_with_scoped_tickers(data_pipeline_module):
    """TEST-002 (spec): SEC EDGAR fallback dispatches with the same
    ticker scope as the primary FMP repair."""
    pool = _FakePool(_base_fixture())
    failed = ["ADV", "ARDT"]
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="run_fallback_source",
        stage="sec_fundamentals_fallback", tickers=failed,
    )
    assert desc["stage"] == "sec_fundamentals_fallback"
    assert desc["tickers"] == failed


@pytest.mark.asyncio
async def test_blocked_vendor_class_branch_present(data_pipeline_module, monkeypatch):
    """TEST-004 (spec): blocked_vendor class infrastructure remains in
    place for future vendor-disabled feeds, even after greeks_max_pain
    retirement removed the only live producer. Synthesizes a fixture
    entry to exercise the branch — UI still suppresses run_scoped_feed
    / repair_failed_scope for blocked_vendor and surfaces view_blocker.
    """
    synthetic = {
        "class": "blocked_vendor",
        "vendor": "synthetic_vendor",
        "blocker_reason": "synthetic fixture exercising the blocked_vendor branch",
        "scope_kind": "full",
    }
    monkeypatch.setitem(
        data_pipeline_module.CHECK_REMEDIATION,
        "synthetic_blocked_vendor_check",
        synthetic,
    )
    remediation = data_pipeline_module._check_remediation(
        "synthetic_blocked_vendor_check"
    )
    assert "run_scoped_feed" not in remediation["allowed_actions"]
    assert "repair_failed_scope" not in remediation["allowed_actions"]
    assert "view_blocker" in remediation["allowed_actions"]


@pytest.mark.asyncio
async def test_corporate_actions_bootstrap_classified_honestly(data_pipeline_module):
    """TEST-006: corporate_actions_completeness classified as bootstrap
    (one-shot baseline), NOT generic full-stage rerun."""
    spec = data_pipeline_module.CHECK_REMEDIATION["corporate_actions_completeness"]
    assert spec["class"] == "bootstrap"
    remediation = data_pipeline_module._check_remediation(
        "corporate_actions_completeness"
    )
    assert "bootstrap_baseline" in remediation["allowed_actions"]


@pytest.mark.asyncio
async def test_daemon_freshness_is_operator_required(data_pipeline_module):
    """TEST-007: daemon_freshness is operator_required, NOT feed-healable."""
    spec = data_pipeline_module.CHECK_REMEDIATION["daemon_freshness"]
    assert spec["class"] == "operator_required"
    assert "daemon" in spec["operator_procedure"].lower()
    remediation = data_pipeline_module._check_remediation("daemon_freshness")
    assert "run_scoped_feed" not in remediation["allowed_actions"]


@pytest.mark.asyncio
async def test_repair_failed_scope_allowlist(data_pipeline_module):
    """TEST-009: server-side stage allowlist still rejects arbitrary
    stage names even with the new scoped path."""
    pool = _FakePool(_base_fixture())
    with pytest.raises(ValueError, match="not in RUN_FEED_ALLOWLIST"):
        await data_pipeline_module.request_operator_run(
            pool, actor="alice", action="repair_failed_scope",
            stage="rm_minus_rf_arbitrary",
            tickers=["AAPL"],
        )


@pytest.mark.asyncio
async def test_tickers_sanitized_and_capped(data_pipeline_module):
    """Tickers are whitespace-stripped, upper-cased, deduped, capped
    at 500. Prevents accidental 50k-ticker paste from blowing up the
    command line."""
    pool = _FakePool(_base_fixture())
    raw = ["aapl", "AAPL", "  MSFT  ", "msft", *[f"X{i}" for i in range(600)]]
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="repair_failed_scope",
        stage="fundamentals_refresh", tickers=raw,
    )
    out = desc["tickers"]
    assert out is not None
    assert "AAPL" in out and "MSFT" in out
    assert len(out) <= 500
    assert len(out) == len(set(out))


@pytest.mark.asyncio
async def test_validation_row_full_remediation_contract(data_pipeline_module):
    """REQ-001: every check row carries the full remediation contract."""
    pool = _FakePool(_base_fixture())
    payload = await data_pipeline_module.fetch_status_payload(pool)
    for c in payload["checks"]:
        for field in (
            "remediation_class", "target_stage", "scope_kind",
            "allowed_actions", "affected_symbols",
        ):
            name = c["name"]
            assert field in c, f"check {name!r} missing {field}"
        assert isinstance(c["allowed_actions"], list)
        assert isinstance(c["affected_symbols"], list)


@pytest.mark.asyncio
async def test_failed_symbols_extracted_from_notes_details(data_pipeline_module):
    """REQ-002 hook: _extract_failed_symbols pulls unique tickers from
    FailureDetail list. Sentinel rows (<corporate_actions>) excluded."""
    details = [
        {"ticker": "ADV", "reason": "missing_quarter"},
        {"ticker": "ADTX", "reason": "missing_quarter"},
        {"ticker": "ADV", "reason": "another"},
        {"ticker": "<corporate_actions>", "reason": "no_prior_archive"},
    ]
    out = data_pipeline_module._extract_failed_symbols(details)
    assert out == ["ADV", "ADTX"]


@pytest.mark.asyncio
async def test_check_name_disambiguates_multi_check_stage(data_pipeline_module):
    """F-008 fix (2026-05-29 expert review pass 2): when a stage
    produces multiple checks with DIFFERENT params (e.g. daily_bars
    → prices_daily_completeness {repair_gaps: True} vs
    prices_daily_freshness {repair_coverage: True}), passing the
    explicit check_name MUST drive the params lookup. Without this,
    first-match-wins via dict insertion order silently dispatches the
    wrong params for the second check."""
    pool = _FakePool(_base_fixture())
    # prices_daily_freshness should send repair_coverage=True, NOT
    # repair_gaps=True (which is prices_daily_completeness's param).
    await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="run_scoped_feed",
        stage="daily_bars",
        check_name="prices_daily_freshness",
    )
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    payload = json.loads(insert_calls[0][1][3])
    assert payload["params"].get("repair_coverage") is True, (
        "F-008: prices_daily_freshness must dispatch with "
        "repair_coverage=True, not the prices_daily_completeness "
        "{repair_gaps: True} catalog default"
    )
    assert "repair_gaps" not in payload["params"]


@pytest.mark.asyncio
async def test_check_name_completeness_uses_repair_gaps(data_pipeline_module):
    """F-008 inverse: prices_daily_completeness's click DOES dispatch
    with repair_gaps=True."""
    pool = _FakePool(_base_fixture())
    await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="run_scoped_feed",
        stage="daily_bars",
        check_name="prices_daily_completeness",
    )
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    payload = json.loads(insert_calls[0][1][3])
    assert payload["params"].get("repair_gaps") is True


@pytest.mark.asyncio
async def test_legacy_stage_only_dispatch_documented_first_match(
    data_pipeline_module,
):
    """Legacy / direct-API callers that don't pass check_name still
    get a valid params dict via stage reverse-lookup, but the
    contract is documented as imprecise. This pins that fall-back
    works (doesn't crash) without claiming it's correct."""
    pool = _FakePool(_base_fixture())
    await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="run_scoped_feed",
        stage="fundamentals_refresh",
        # No check_name passed.
    )
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    payload = json.loads(insert_calls[0][1][3])
    # Whatever params landed are deterministic but documented as
    # "first match wins"; we just assert it returned A dict.
    assert isinstance(payload["params"], dict)


@pytest.mark.asyncio
async def test_catalog_params_forwarded_to_audit_payload(data_pipeline_module):
    """F-001 fix (2026-05-29 expert review): the CHECK_REMEDIATION
    ``params`` dict (skip_guard_days=0, repair_gaps=True, etc.) MUST
    be threaded through to the lane daemon. Without this, the surgical
    repair sends only the tickers list and the stage falls through to
    its full-universe defaults — the EXACT failure mode the operator's
    complaint targets."""
    pool = _FakePool(_base_fixture())
    # fundamentals_quarterly_completeness → fundamentals_refresh
    # with catalog params {skip_guard_days: 0}.
    await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="repair_failed_scope",
        stage="fundamentals_refresh", tickers=["ADV", "ADTX"],
    )
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    sql, args = insert_calls[0]
    payload = json.loads(args[3])
    # The catalog params must be in the dispatched payload.
    assert payload["params"]["skip_guard_days"] == 0, (
        "F-001: catalog params not forwarded — operator-supplied "
        "tickers without skip_guard_days=0 falls through to the "
        "24h-skip-fresh logic and silently no-ops"
    )


@pytest.mark.asyncio
async def test_caller_params_override_catalog(data_pipeline_module):
    """Explicit operator-supplied params beat the catalog default
    (allows manual override for unusual situations)."""
    pool = _FakePool(_base_fixture())
    await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="repair_failed_scope",
        stage="fundamentals_refresh", tickers=["AAPL"],
        params={"skip_guard_days": 7},
    )
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    payload = json.loads(insert_calls[0][1][3])
    assert payload["params"]["skip_guard_days"] == 7


@pytest.mark.asyncio
async def test_tickers_truncation_surfaced_in_audit(data_pipeline_module):
    """F-005 fix: when the ticker list exceeds the 500 cap, the audit
    payload must record the original count so the operator can see
    via job-status events that truncation happened."""
    pool = _FakePool(_base_fixture())
    raw = [f"T{i}" for i in range(700)]
    desc = await data_pipeline_module.request_operator_run(
        pool, actor="alice", action="repair_failed_scope",
        stage="fundamentals_refresh", tickers=raw,
    )
    assert desc["tickers_truncated_from"] == 700
    assert len(desc["tickers"]) == 500
    insert_calls = [
        c for c in pool.connections[0].execute_calls
        if "INSERT INTO" in c[0]
    ]
    payload = json.loads(insert_calls[0][1][3])
    assert payload["tickers_truncated_from"] == 700


def test_blocked_vendor_runtime_toggle(data_pipeline_module, monkeypatch):
    """F-004 fix: CONSOLE_VENDOR_ENABLED enables specific vendors at
    runtime without a code change. Honest round-trip with arbitrary
    vendor identifier strings."""
    monkeypatch.delenv("CONSOLE_VENDOR_ENABLED", raising=False)
    assert not data_pipeline_module._vendor_enabled("synthetic_vendor")
    monkeypatch.setenv("CONSOLE_VENDOR_ENABLED", "synthetic_vendor,iborrow")
    assert data_pipeline_module._vendor_enabled("synthetic_vendor")
    assert data_pipeline_module._vendor_enabled("iborrow")
    assert not data_pipeline_module._vendor_enabled("polygon")


def test_run_fallback_source_allowlist_strict(data_pipeline_module):
    """F-003 fix: removed the inline allowlist bootstrap for
    run_fallback_source. Arbitrary fallback stages now fail with the
    same strict ValueError as run_feed."""
    import asyncio
    pool = _FakePool(_base_fixture())
    with pytest.raises(ValueError, match="not in RUN_FEED_ALLOWLIST"):
        asyncio.run(data_pipeline_module.request_operator_run(
            pool, actor="alice", action="run_fallback_source",
            stage="some_made_up_stage", tickers=["AAPL"],
        ))


@pytest.mark.asyncio
async def test_full_pipeline_not_default_for_narrow_failure(data_pipeline_module):
    """TEST-010 (spec): for narrow validation failures (a check with
    scoped_auto_heal class and affected_symbols < 100), the primary
    action surface includes ``repair_failed_scope`` — NOT ``run_update``
    (which would run the full pipeline)."""
    # Verify the class config for a known scoped_auto_heal check.
    spec = data_pipeline_module.CHECK_REMEDIATION["fundamentals_quarterly_completeness"]
    assert spec["class"] == "scoped_auto_heal"
    remediation = data_pipeline_module._check_remediation(
        "fundamentals_quarterly_completeness"
    )
    assert "repair_failed_scope" in remediation["allowed_actions"]
    # Fallback offered too.
    assert "run_fallback_source" in remediation["allowed_actions"]

