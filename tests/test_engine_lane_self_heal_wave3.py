"""Wave-3 engine-lane deterministic self-heal — E1 E2 E3 E9 regression.

Pins the four rows in
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md`` Wave 3:

* **E1** — engine scheduler stage failure: ``_invoke_scheduler_with_recovery``
  retries ONCE on rc≠0; on the second failure emits
  ``ENGINE_STAGE_ESCALATED``; the sweep continues to the next engine
  (does NOT abort engine_service).
* **E2** — ``setup_detection`` panel-load with transient-DB retry:
  :func:`tpcore.engine.transient_retry.fetch_with_transient_retry`
  retries 3 times with exponential backoff on a transient asyncpg-class
  error; on exhaustion the original exception is re-raised; a
  non-transient exception bypasses retry entirely.
* **E3** — order placement transient retry:
  :func:`tpcore.order_management.transient_retry.submit_with_transient_retry`
  retries ONCE on a pre-response transient (httpx NetworkError /
  TimeoutException); on second-failure emits ``ORDER_ESCALATED``
  + ``ENGINE_POSITION_DEGRADED`` and re-raises. A 4xx APIError is
  NEVER retried (hard-reject path unchanged).
* **E9** — engine package import error: ``_pre_check_engine_import``
  detects the missing module BEFORE spawning the subprocess; emits
  ``ENGINE_IMPORT_FAILED``; the sweep continues to the next engine.

Plus a NEGATIVE-PIN: a non-matching error class (e.g. ``ValueError``
during get_daily_bars, or an ``httpx.HTTPStatusError`` 422 during
submit) does NOT trigger any of these cascades — the exception is
re-raised unchanged on the first attempt.

Tests are hermetic (no real DB / network / subprocess / broker). The
ops.engine_dispatch tests use the ``ops_shadow`` xdist group because the
import touches ``sys.modules['ops']`` (per the
``test_xdist_group_manifest`` clockwork).
"""
from __future__ import annotations

import contextlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Repo-root on sys.path so ``import ops.engine_dispatch`` resolves the
# regular package (under full-suite collection a prior test, typically
# tpcore/tests/test_ops.py, loads scripts/ops.py as ``ops`` without a
# ``__path__``, which would otherwise mask the regular package).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Deferred / function-local imports for ``ops.engine_dispatch``-side
# fixtures. We DELIBERATELY do NOT import ``ed`` (a.k.a.
# ``ops.engine_dispatch``) at module-collection time, because doing so
# under xdist + the shared ``ops_shadow`` worker group can leave the
# ``ops`` package's ``engine_dispatch`` attribute cached at the
# first-loaded module instance, breaking the
# eviction-then-submodule-path-reimport pattern that
# ``scripts/tests/test_engine_dispatch.py`` uses (whose ``from
# ops.engine_dispatch import dispatch_once`` would otherwise land on a
# fresh-but-divergent re-executed module).
#
# By keeping ``ed`` function-local (re-fetched from ``sys.modules`` on
# every test invocation), we avoid module-collection-time state and
# inherit whatever the prior ops_shadow test's eviction-and-reimport
# left in place. This is the same isolation strategy
# ``tpcore/tests/test_ops.py``'s ``ops_module`` fixture uses for the
# scripts/ops.py shadow case, scoped to test-execution not collection.
#
# tpcore-side helpers (transient_retry, supervisor events) are pure
# library functions with no module-shadow risk, so they import at
# collection time as usual.
from tpcore.engine.transient_retry import (  # noqa: E402
    fetch_with_transient_retry,
    is_transient_db_error,
)
from tpcore.engine_profile import FireDecision  # noqa: E402
from tpcore.order_management.transient_retry import (  # noqa: E402
    DEGRADED_POSITION_EVENT,
    ORDER_ESCALATED_EVENT,
    is_pre_response_transient,
    submit_with_transient_retry,
)


@pytest.fixture
def ed():
    """Return the live ``ops.engine_dispatch`` module.

    Function-scope fixture (re-evaluated per test) so a sibling
    ops-shadow module's collection-time eviction-and-reimport doesn't
    strand us on a stale instance. If sys.modules no longer has
    ``ops.engine_dispatch`` (because another test file evicted it),
    re-import via ``importlib.import_module`` to repopulate sys.modules
    AND the ``ops`` package attribute with the SAME module that future
    submodule-path imports will return.

    Mirrors the lazy-import discipline in
    ``tpcore/tests/test_ops.py``'s ``ops_module`` fixture (which uses
    the same reason: scripts/ops.py-shadow + ops/ package coexistence)."""
    import importlib

    if "ops.engine_dispatch" not in sys.modules:
        return importlib.import_module("ops.engine_dispatch")
    import ops.engine_dispatch as _ed_mod
    return _ed_mod

# pytest-xdist: pin this ops-shadow module to one worker so the
# sys.modules['ops'] manipulation stays single-process (ops/ package-
# shadow is a single-process invariant). Matches PR #261 + every
# scripts/tests/test_engine_*.py precedent.
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ────────────────────────────────────────────────────────────────────────
# Helpers (fakes)
# ────────────────────────────────────────────────────────────────────────


class _RecordingPool:
    """Async-pool fake that records application_log inserts in-memory.

    Mirrors the shape ``_emit_engine_dispatch_event`` /
    ``submit_with_transient_retry._emit_application_log`` expect:
    ``pool.acquire()`` returns an async-context-manager whose target
    has ``execute(sql, *args)``. Inserts are captured as
    ``{"sql", "args"}`` dicts so tests can assert event_type / engine
    / payload without coupling to the exact SQL string."""

    def __init__(self) -> None:
        self.inserts: list[dict] = []

    @contextlib.asynccontextmanager
    async def acquire(self):
        outer = self

        class _Conn:
            async def execute(self, sql, *args):
                outer.inserts.append({"sql": sql, "args": args})

            async def fetch(self, *_a, **_k):
                return []

            async def fetchrow(self, *_a, **_k):
                return None

            async def fetchval(self, *_a, **_k):
                return None

        yield _Conn()


def _emitted_events(pool: _RecordingPool) -> list[dict]:
    """Pluck (engine, event_type, severity, message, payload) out of the
    INSERT-into-application_log rows the helpers wrote.

    The INSERT SQL shape is ``(engine, run_id, event_type, severity,
    message, data::jsonb)`` — args[0] is engine, args[2] is event_type,
    args[5] is the JSON payload string. We deserialize ``data`` so
    test assertions can read structured fields."""
    import json as _json

    out: list[dict] = []
    for ins in pool.inserts:
        if "INSERT INTO platform.application_log" not in ins["sql"]:
            continue
        args = ins["args"]
        # (engine, run_id, event_type, severity, message, data_json)
        if len(args) < 6:
            continue
        out.append({
            "engine": args[0],
            "run_id": args[1],
            "event_type": args[2],
            "severity": args[3],
            "message": args[4],
            "data": _json.loads(args[5]) if args[5] is not None else None,
        })
    return out


# ════════════════════════════════════════════════════════════════════════
# E1 — engine scheduler stage failure: retry-once + ENGINE_STAGE_ESCALATED
# ════════════════════════════════════════════════════════════════════════


async def test_e1_scheduler_rc0_first_attempt_no_retry_no_emit(ed):
    """A clean rc=0 first attempt: no retry, no emit. The cascade is
    silent on the happy path (no behavior delta on success)."""
    pool = _RecordingPool()
    token = ed._dispatch_pool.set(pool)
    try:
        with patch.object(ed, "_invoke_scheduler",
                          new=AsyncMock(return_value=0)) as inv, \
             patch.object(ed, "_pre_check_engine_import",
                          return_value=(True, None)):
            rc = await ed._invoke_scheduler_with_recovery("reversion")
    finally:
        ed._dispatch_pool.reset(token)
    assert rc == 0
    assert inv.await_count == 1
    events = _emitted_events(pool)
    assert events == []  # silent on success


async def test_e1_scheduler_rc_nonzero_then_zero_retry_succeeds(ed):
    """First attempt rc=1, retry returns rc=0: success after one retry.
    No ENGINE_STAGE_ESCALATED event (we recovered)."""
    pool = _RecordingPool()
    rcs = iter([1, 0])
    token = ed._dispatch_pool.set(pool)
    try:
        with patch.object(ed, "_invoke_scheduler",
                          new=AsyncMock(side_effect=lambda _e: next(rcs))) as inv, \
             patch.object(ed, "_pre_check_engine_import",
                          return_value=(True, None)):
            rc = await ed._invoke_scheduler_with_recovery("reversion")
    finally:
        ed._dispatch_pool.reset(token)
    assert rc == 0
    assert inv.await_count == 2  # initial + ONE retry
    events = _emitted_events(pool)
    assert events == []


async def test_e1_scheduler_both_attempts_rc_nonzero_emits_escalated(ed):
    """Both attempts return rc=2: emit ENGINE_STAGE_ESCALATED with
    attempts=2 + final rc; return the final rc (do NOT raise — sweep
    continues to next engine)."""
    pool = _RecordingPool()
    token = ed._dispatch_pool.set(pool)
    try:
        with patch.object(ed, "_invoke_scheduler",
                          new=AsyncMock(return_value=2)) as inv, \
             patch.object(ed, "_pre_check_engine_import",
                          return_value=(True, None)):
            rc = await ed._invoke_scheduler_with_recovery("reversion")
    finally:
        ed._dispatch_pool.reset(token)
    assert rc == 2
    assert inv.await_count == 2  # initial + ONE retry (no third)
    events = _emitted_events(pool)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event_type"] == ed.ENGINE_STAGE_ESCALATED_EVENT
    assert ev["engine"] == "reversion"
    assert ev["severity"] == "ERROR"
    assert ev["data"]["engine"] == "reversion"
    assert ev["data"]["attempts"] == 2
    assert ev["data"]["returncode"] == 2


async def test_e1_one_engine_failing_does_not_abort_full_sweep(ed):
    """The engine_service-invariant: an engine that hits both retries
    and escalates must NOT prevent the rest of the ROSTER from running.
    Mirrors ``test_invoke_failure_is_isolated_per_engine`` (which covers
    the RAISING case); this covers the rc≠0-twice case."""
    fire = FireDecision(True, "ready", {"data_ready": True})
    calls: list[str] = []

    async def _inv_with_recovery(engine):
        calls.append(engine)
        # Reversion escalates (returns 2 after both attempts); the rest
        # return 0 cleanly. The sweep must visit every engine in ROSTER.
        return 2 if engine == "reversion" else 0

    pool = _RecordingPool()
    with patch.object(ed, "should_fire", AsyncMock(return_value=fire)), \
         patch.object(ed, "_invoke_scheduler_with_recovery",
                      new=AsyncMock(side_effect=_inv_with_recovery)), \
         patch.object(ed, "engine_supervisor", MagicMock(supervise=AsyncMock())), \
         patch.object(ed, "_safe_autotune", new=AsyncMock()), \
         patch.object(ed, "_dispatch_allocator", new=AsyncMock()):
        await ed.dispatch_once(pool, now=datetime(2026, 5, 22, 13, 30, tzinfo=UTC))
    # Every ROSTER engine attempted despite reversion escalating.
    assert calls == list(ed.ROSTER)


# ════════════════════════════════════════════════════════════════════════
# E9 — engine package import error: ENGINE_IMPORT_FAILED + skip
# ════════════════════════════════════════════════════════════════════════


async def test_e9_pre_check_emits_import_failed_when_module_missing(ed):
    """A missing engine package: pre-check returns (False, repr) → emit
    ENGINE_IMPORT_FAILED + skip subprocess entirely (no rc≠0 path)."""
    pool = _RecordingPool()
    token = ed._dispatch_pool.set(pool)
    try:
        with patch.object(ed, "_invoke_scheduler",
                          new=AsyncMock()) as inv, \
             patch.object(ed, "_pre_check_engine_import",
                          return_value=(False, "ModuleNotFoundError: foo.scheduler")):
            rc = await ed._invoke_scheduler_with_recovery("foo")
    finally:
        ed._dispatch_pool.reset(token)
    assert rc == 127  # canonical "command not found" rc
    inv.assert_not_called()  # subprocess never spawned
    events = _emitted_events(pool)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event_type"] == ed.ENGINE_IMPORT_FAILED_EVENT
    assert ev["engine"] == "foo"
    assert ev["severity"] == "ERROR"
    assert "ModuleNotFoundError" in (ev["data"].get("error") or "")
    assert ev["data"]["module"] == "foo.scheduler"


def test_e9_pre_check_returns_true_for_real_engine_module(ed):
    """The pre-check resolves cleanly for a real engine package (smoke
    test: this proves find_spec is the right primitive — a Python import
    of ``reversion.scheduler`` would actually execute the module and is
    too heavy here)."""
    ok, err = ed._pre_check_engine_import("reversion")
    assert ok is True, err
    assert err is None


def test_e9_pre_check_returns_false_for_nonexistent_engine(ed):
    """find_spec returns None for a fictitious engine package."""
    ok, err = ed._pre_check_engine_import("nonexistent_engine_xyz")
    assert ok is False
    assert err is not None
    assert "No module named" in err or "ModuleNotFoundError" in err


async def test_e9_import_failure_does_not_abort_full_sweep(ed):
    """A missing engine (e.g. operator typo in roster, package deleted)
    must NOT prevent the rest of the ROSTER from running."""
    fire = FireDecision(True, "ready", {"data_ready": True})
    visited: list[str] = []

    def _pre_check(engine):
        visited.append(engine)
        if engine == "reversion":
            return False, "ModuleNotFoundError: reversion.scheduler"
        return True, None

    invoked: list[str] = []

    async def _inv_scheduler(engine):
        invoked.append(engine)
        return 0

    pool = _RecordingPool()
    with patch.object(ed, "should_fire", AsyncMock(return_value=fire)), \
         patch.object(ed, "_invoke_scheduler",
                      new=AsyncMock(side_effect=_inv_scheduler)), \
         patch.object(ed, "_pre_check_engine_import", _pre_check), \
         patch.object(ed, "engine_supervisor", MagicMock(supervise=AsyncMock())), \
         patch.object(ed, "_safe_autotune", new=AsyncMock()), \
         patch.object(ed, "_dispatch_allocator", new=AsyncMock()):
        await ed.dispatch_once(pool, now=datetime(2026, 5, 22, 13, 30, tzinfo=UTC))
    # Every ROSTER engine pre-checked; reversion skipped subprocess; the
    # others spawned normally.
    assert visited == list(ed.ROSTER)
    assert "reversion" not in invoked
    assert invoked == [e for e in ed.ROSTER if e != "reversion"]


# ════════════════════════════════════════════════════════════════════════
# E2 — setup_detection panel-load: 3-attempt transient retry
# ════════════════════════════════════════════════════════════════════════


class _FakeTransientDBError(Exception):
    """Mimics asyncpg.exceptions.ConnectionDoesNotExistError by NAME —
    the is_transient_db_error check is by class name, so this fake is
    treated as transient without importing asyncpg in the test."""


# Rename via __qualname__ so the type().__name__ check matches one of
# _TRANSIENT_NAMES from tpcore.engine.transient_retry.
_FakeTransientDBError.__name__ = "ConnectionDoesNotExistError"


class _FakeQueryCanceled(Exception):
    pass


_FakeQueryCanceled.__name__ = "QueryCanceledError"


async def test_e2_transient_db_error_retries_three_times_then_succeeds():
    """3-attempt retry: two transients then success — final call wins.
    Sleeps are stubbed so the test is deterministic + fast."""
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def _fetch():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _FakeTransientDBError(f"attempt {attempts['n']} blip")
        return ["bar1", "bar2"]

    async def _sleep(t):
        sleeps.append(t)

    out = await fetch_with_transient_retry(
        _fetch, engine="reversion", op="get_daily_bars",
        sleep=_sleep, _rand=lambda: 0.5,  # deterministic jitter
    )
    assert out == ["bar1", "bar2"]
    assert attempts["n"] == 3  # initial + 2 retries
    # Two sleeps (after attempts 1 and 2). Exponential: base=1.0,
    # 2**0=1 then 2**1=2. With jitter=0.5: 0.75 + 0.5*0.5 = 1.0 → unchanged.
    assert len(sleeps) == 2
    assert sleeps[0] <= 1.0 and sleeps[1] <= 2.0


async def test_e2_transient_db_error_exhausts_three_attempts_then_raises():
    """All three attempts transient: re-raise the original exception
    (the caller's existing try/except still works)."""
    attempts = {"n": 0}

    async def _fetch():
        attempts["n"] += 1
        raise _FakeTransientDBError(f"attempt {attempts['n']} blip")

    async def _sleep(_):
        return None

    with pytest.raises(_FakeTransientDBError):
        await fetch_with_transient_retry(
            _fetch, engine="reversion", op="get_daily_bars",
            sleep=_sleep,
        )
    assert attempts["n"] == 3  # exactly 3 attempts total


async def test_e2_non_transient_error_does_not_retry():
    """A non-transient exception (e.g. ValueError) must NOT trigger any
    retry — re-raise immediately on the FIRST attempt. Pin against
    over-broad retry."""
    attempts = {"n": 0}

    async def _fetch():
        attempts["n"] += 1
        raise ValueError("this is a real bug, not transient")

    async def _sleep(_):
        return None

    with pytest.raises(ValueError, match="real bug"):
        await fetch_with_transient_retry(
            _fetch, engine="reversion", op="get_daily_bars",
            sleep=_sleep,
        )
    assert attempts["n"] == 1  # no retry


async def test_e2_query_canceled_is_transient():
    """QueryCanceledError (statement_timeout / pool-side cancel) is
    classified transient — same class batched_fetchers retries (PR #163)."""
    assert is_transient_db_error(_FakeQueryCanceled("canceled"))


async def test_e2_asyncio_timeout_is_transient():
    """asyncio.TimeoutError on pool.acquire() with timeout=N is a
    pool-exhaustion blip — counts as transient. ``asyncio.TimeoutError``
    is aliased to the builtin ``TimeoutError`` on Python 3.11; we test
    against the builtin to satisfy UP041 while preserving the pool-
    exhaustion semantic."""
    assert is_transient_db_error(TimeoutError())


async def test_e2_value_error_is_not_transient():
    """A plain ValueError is NOT a transient DB error — pin against the
    is_transient_db_error classifier over-matching."""
    assert is_transient_db_error(ValueError("nope")) is False


# ════════════════════════════════════════════════════════════════════════
# E3 — order placement: retry-once + ORDER_ESCALATED + DEGRADED
# ════════════════════════════════════════════════════════════════════════


async def test_e3_first_attempt_succeeds_no_retry_no_emit():
    """The happy path: first attempt succeeds → no retry, no escalate."""
    pool = _RecordingPool()
    submit_calls = {"n": 0}

    async def _submit():
        submit_calls["n"] += 1
        return {"broker_order_id": "ack-123"}

    out = await submit_with_transient_retry(
        _submit, pool=pool, engine="reversion",
        client_order_id="cid-1", ticker="AAPL",
    )
    assert out == {"broker_order_id": "ack-123"}
    assert submit_calls["n"] == 1
    assert _emitted_events(pool) == []


async def test_e3_transient_timeout_then_success_retries_once():
    """First attempt raises httpx.TimeoutException (pre-response), second
    attempt succeeds — silent recovery, no ORDER_ESCALATED event."""
    pool = _RecordingPool()
    submit_calls = {"n": 0}

    async def _submit():
        submit_calls["n"] += 1
        if submit_calls["n"] == 1:
            raise httpx.ReadTimeout("read timed out")
        return {"broker_order_id": "ack-456"}

    out = await submit_with_transient_retry(
        _submit, pool=pool, engine="vector",
        client_order_id="cid-2", ticker="MSFT",
    )
    assert out == {"broker_order_id": "ack-456"}
    assert submit_calls["n"] == 2
    assert _emitted_events(pool) == []  # silent recovery


async def test_e3_two_transient_attempts_emits_order_escalated_and_degraded():
    """Both attempts raise a pre-response transient → emit
    ORDER_ESCALATED + ENGINE_POSITION_DEGRADED, then re-raise the second
    exception. The two emits land BEFORE the raise."""
    pool = _RecordingPool()
    submit_calls = {"n": 0}

    async def _submit():
        submit_calls["n"] += 1
        raise httpx.ConnectError(f"connect attempt {submit_calls['n']}")

    with pytest.raises(httpx.ConnectError, match="connect attempt 2"):
        await submit_with_transient_retry(
            _submit, pool=pool, engine="reversion",
            client_order_id="cid-3", ticker="AAPL",
            telemetry={"notional_usd": "10000", "direction": "buy"},
        )
    assert submit_calls["n"] == 2  # retry ONCE
    events = _emitted_events(pool)
    event_types = [e["event_type"] for e in events]
    assert ORDER_ESCALATED_EVENT in event_types, event_types
    assert DEGRADED_POSITION_EVENT in event_types, event_types
    # The escalated event carries telemetry forwarded by the caller.
    esc = next(e for e in events if e["event_type"] == ORDER_ESCALATED_EVENT)
    assert esc["engine"] == "reversion"
    assert esc["severity"] == "ERROR"
    assert esc["data"]["engine"] == "reversion"
    assert esc["data"]["ticker"] == "AAPL"
    assert esc["data"]["client_order_id"] == "cid-3"
    assert esc["data"]["attempts"] == 2
    assert esc["data"]["notional_usd"] == "10000"
    assert esc["data"]["direction"] == "buy"
    # The degraded marker carries the matching cid for reconciliation.
    deg = next(e for e in events if e["event_type"] == DEGRADED_POSITION_EVENT)
    assert deg["data"]["client_order_id"] == "cid-3"
    assert deg["data"]["reason"] == "order_submit_escalated_transient"


async def test_e3_4xx_api_error_is_not_retried_hard_reject_pass_through():
    """A 4xx HTTPStatusError (hard reject) is NEVER retried — the
    RiskGovernor / broker_adapter own that path. The transient_retry
    helper must pass-through immediately on the first attempt, no
    ORDER_ESCALATED emit (this isn't a transient escalation; it's a
    genuine reject)."""
    pool = _RecordingPool()
    submit_calls = {"n": 0}

    response = MagicMock(spec=httpx.Response)
    response.status_code = 422
    err = httpx.HTTPStatusError(
        "Unprocessable Entity",
        request=MagicMock(spec=httpx.Request),
        response=response,
    )

    async def _submit():
        submit_calls["n"] += 1
        raise err

    with pytest.raises(httpx.HTTPStatusError):
        await submit_with_transient_retry(
            _submit, pool=pool, engine="reversion",
            client_order_id="cid-hard-reject", ticker="AAPL",
        )
    assert submit_calls["n"] == 1  # NO retry on hard reject
    assert _emitted_events(pool) == []  # no escalation surface


async def test_e3_5xx_status_error_is_not_retried_conservative_policy():
    """5xx HTTPStatusError is NOT classified pre-response (the server
    received the request; the order may have landed) — passes through
    without retry, no escalation. The conservative policy that mirrors
    the broker_adapter's documented double-order safety. The spec's
    broader '5xx-retry' interpretation is intentionally deferred to a
    sibling PR that also updates the broker_adapter policy."""
    pool = _RecordingPool()
    submit_calls = {"n": 0}

    response = MagicMock(spec=httpx.Response)
    response.status_code = 503
    err = httpx.HTTPStatusError(
        "Service Unavailable",
        request=MagicMock(spec=httpx.Request),
        response=response,
    )

    async def _submit():
        submit_calls["n"] += 1
        raise err

    with pytest.raises(httpx.HTTPStatusError):
        await submit_with_transient_retry(
            _submit, pool=pool, engine="reversion",
            client_order_id="cid-5xx", ticker="AAPL",
        )
    assert submit_calls["n"] == 1
    assert _emitted_events(pool) == []


async def test_e3_value_error_is_not_pre_response_transient():
    """A non-network exception (ValueError) is NEVER pre-response
    transient — pass through unchanged. Pin against over-broad
    classifier."""
    assert is_pre_response_transient(ValueError("nope")) is False


async def test_e3_network_and_timeout_are_pre_response_transient():
    """NetworkError + TimeoutException — both classified pre-response
    transient (the cases where the broker definitely never saw the
    request)."""
    assert is_pre_response_transient(httpx.ConnectError("refused"))
    assert is_pre_response_transient(httpx.ReadTimeout("timed out"))


async def test_e3_first_transient_then_non_transient_re_raises_second():
    """First attempt transient (network), second attempt non-transient
    (HTTPStatusError 422): the second exception is re-raised directly —
    NO ORDER_ESCALATED emit (the escalation is reserved for the
    transient-twice case)."""
    pool = _RecordingPool()
    submit_calls = {"n": 0}

    response = MagicMock(spec=httpx.Response)
    response.status_code = 422
    second_err = httpx.HTTPStatusError(
        "Unprocessable Entity",
        request=MagicMock(spec=httpx.Request),
        response=response,
    )

    async def _submit():
        submit_calls["n"] += 1
        if submit_calls["n"] == 1:
            raise httpx.ConnectError("connect refused")
        raise second_err

    with pytest.raises(httpx.HTTPStatusError):
        await submit_with_transient_retry(
            _submit, pool=pool, engine="reversion",
            client_order_id="cid-4", ticker="AAPL",
        )
    assert submit_calls["n"] == 2
    assert _emitted_events(pool) == []  # no escalation on non-transient final


# ════════════════════════════════════════════════════════════════════════
# E1 cascade-map shape pin (mirrors PR #261's _VALIDATION_CASCADE_MAP)
# ════════════════════════════════════════════════════════════════════════


def test_e1_cascade_map_has_default_max_attempts_two(ed):
    """The default cascade is "retry ONCE" per spec — initial + 1 retry
    = 2 total attempts."""
    assert ed._DEFAULT_STAGE_CASCADE["max_attempts"] == 2


def test_e1_event_names_distinct_from_supervisor_events(ed):
    """Wave-3 event names must NOT collide with the supervisor's existing
    HELD/ESCALATED/RECOVERED event names (the supervisor owns per-engine
    ladder transitions; Wave-3 owns the in-cycle cascade decision)."""
    from tpcore.supervisor_state import (
        CLEARED_EVENT,
        ESCALATED_EVENT,
        HELD_EVENT,
        RECOVERED_EVENT,
    )
    sup_events = {HELD_EVENT, ESCALATED_EVENT, RECOVERED_EVENT, CLEARED_EVENT}
    wave3_events = {
        ed.ENGINE_STAGE_ESCALATED_EVENT,
        ed.ENGINE_IMPORT_FAILED_EVENT,
        ORDER_ESCALATED_EVENT,
        DEGRADED_POSITION_EVENT,
    }
    assert sup_events.isdisjoint(wave3_events), (
        f"Wave-3 event names collide with supervisor events: "
        f"{sup_events & wave3_events}"
    )


# ════════════════════════════════════════════════════════════════════════
# Reversion pilot wiring (E2): assert setup_detection's get_daily_bars
# call sites are wrapped through fetch_with_transient_retry.
# ════════════════════════════════════════════════════════════════════════


def test_e2_reversion_setup_detection_imports_transient_retry():
    """The reversion pilot wires the shared helper. A regression that
    drops the import (or replaces it with a local copy) trips this pin —
    the helper is the engine-lane SoT for Wave-3 E2 panel-load retry."""
    from reversion.plugs import setup_detection
    # Module-attr pin: the helper is imported at module scope so call
    # sites can opt in without re-importing per call.
    assert hasattr(setup_detection, "fetch_with_transient_retry")
    assert (setup_detection.fetch_with_transient_retry
            is fetch_with_transient_retry)


async def test_e2_reversion_scan_retries_then_succeeds_on_transient_panel_load():
    """End-to-end pin for the pilot wiring: a transient asyncpg-class
    error during ``self._data.get_daily_bars(SPY, ...)`` triggers the
    3-attempt retry; the second attempt succeeds and ``scan()`` returns
    normally. Proves the call-site wrap reaches the helper.

    We isolate the SPY fetch (the FIRST call from ``_market_context``)
    and stub everything downstream to a no-op so the test stays focused
    on the retry behavior at the seam."""
    from datetime import date

    from reversion.plugs.setup_detection import ReversionSetupDetection

    attempts = {"n": 0}

    class _FakeData:
        async def get_daily_bars(self, symbol, start, end):
            # Only SPY drives the retry — VIX returns empty so the proxy
            # path runs; the universe is empty so per-symbol loop skips.
            if symbol == "SPY":
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise _FakeTransientDBError("first SPY fetch blip")
                # Subsequent attempts succeed with empty bars.
                return []
            return []

    plug = ReversionSetupDetection(data=_FakeData(), universe=())
    # Stub asyncio.sleep so the test is deterministic + fast.
    with patch("tpcore.engine.transient_retry.asyncio.sleep",
               new=AsyncMock()):
        out = await plug.scan(as_of=date(2026, 5, 22))
    # SPY was retried (1 transient + 1 success); plug.scan returned
    # normally (empty list — universe was empty). The fact that scan()
    # didn't raise the transient proves the wrapper engaged.
    assert attempts["n"] == 2, attempts
    assert out == []
