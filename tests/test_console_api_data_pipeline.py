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
