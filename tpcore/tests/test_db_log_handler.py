"""Tests for ``tpcore.logging.db_handler.DBLogHandler``.

The fake pool below maintains a tiny in-memory simulation of
``platform.application_log`` so the retention test ("old row gets deleted
when a new event is logged") exercises the same INSERT/DELETE pair the
real handler issues.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tpcore.logging.db_handler import DBLogHandler

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — minimal in-memory application_log table simulation
# ────────────────────────────────────────────────────────────────────────────


class _FakeRow(dict):
    """asyncpg.Record-ish — supports dict-style access."""


class _FakeConn:
    """Backs INSERT and DELETE against a shared in-memory list of rows."""

    def __init__(self, rows: list[_FakeRow], calls: list[tuple]) -> None:
        self._rows = rows
        self._calls = calls

    async def execute(self, sql: str, *args) -> str:
        self._calls.append((sql, args))
        if "INSERT INTO platform.application_log" in sql:
            engine, run_id, event_type, severity, message, data_json = args
            self._rows.append(
                _FakeRow(
                    engine=engine,
                    run_id=run_id,
                    event_type=event_type,
                    severity=severity,
                    message=message,
                    data=json.loads(data_json) if data_json is not None else None,
                    recorded_at=datetime.now(UTC),
                )
            )
            return "INSERT 0 1"
        if "DELETE FROM platform.application_log" in sql:
            (cutoff,) = args
            before = len(self._rows)
            # Honor the real prune's retention-exemption clause: rows
            # whose event_type is named in an ``event_type NOT IN (...)``
            # predicate survive regardless of age (DR2.1 — the
            # REVIEW_DEFECT_* primitive must not silently expire). Parsed
            # from the real SQL so the fake tracks the actual prune.
            exempt: set[str] = set()
            if "event_type NOT IN" in sql:
                inside = sql.split("event_type NOT IN", 1)[1]
                inside = inside.split("(", 1)[1].split(")", 1)[0]
                exempt = {
                    tok.strip().strip("'\"")
                    for tok in inside.split(",")
                    if tok.strip()
                }
            self._rows[:] = [
                r
                for r in self._rows
                if r["recorded_at"] >= cutoff or r["event_type"] in exempt
            ]
            removed = before - len(self._rows)
            return f"DELETE {removed}"
        return "OK"


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.rows: list[_FakeRow] = []
        self.calls: list[tuple] = []

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(_FakeConn(self.rows, self.calls))


class _ExplodingPool:
    """Acquires fail. Used to verify the handler swallows DB errors."""

    def acquire(self):  # pragma: no cover - never returns
        raise RuntimeError("simulated pool failure")


# ────────────────────────────────────────────────────────────────────────────
# Construction
# ────────────────────────────────────────────────────────────────────────────


def test_init_rejects_none_pool() -> None:
    with pytest.raises(ValueError, match="requires a connection pool"):
        DBLogHandler(pool=None, engine="sigma", run_id=uuid.uuid4())  # type: ignore[arg-type]


def test_init_rejects_zero_retention() -> None:
    with pytest.raises(ValueError, match="retention_days must be >= 1"):
        DBLogHandler(pool=_FakePool(), engine="sigma", run_id=uuid.uuid4(), retention_days=0)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────────────
# log() inserts a row and the row is queryable via the simulation
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_inserts_row_with_tags_and_data() -> None:
    pool = _FakePool()
    run_id = uuid.uuid4()
    handler = DBLogHandler(pool, "sigma", run_id)  # type: ignore[arg-type]

    await handler.log(
        "SCAN_COMPLETE",
        "scan produced 3 candidate(s)",
        severity="INFO",
        data={"candidates": 3, "duration_ms": 412},
    )

    assert len(pool.rows) == 1
    row = pool.rows[0]
    assert row["engine"] == "sigma"
    assert row["run_id"] == run_id
    assert row["event_type"] == "SCAN_COMPLETE"
    assert row["severity"] == "INFO"
    assert row["data"] == {"candidates": 3, "duration_ms": 412}

    # INSERT + retention DELETE on the same call.
    assert len(pool.calls) == 2
    assert "INSERT INTO platform.application_log" in pool.calls[0][0]
    assert "DELETE FROM platform.application_log" in pool.calls[1][0]


# ────────────────────────────────────────────────────────────────────────────
# Convenience methods write the right event_type / severity
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_writes_startup_event() -> None:
    pool = _FakePool()
    handler = DBLogHandler(pool, "sigma", uuid.uuid4())  # type: ignore[arg-type]

    await handler.startup(commit_sha="abc1234")

    assert len(pool.rows) == 1
    assert pool.rows[0]["event_type"] == "STARTUP"
    assert pool.rows[0]["severity"] == "INFO"
    assert pool.rows[0]["data"] == {"commit_sha": "abc1234"}


@pytest.mark.asyncio
async def test_shutdown_severity_tracks_exit_code() -> None:
    pool = _FakePool()
    handler = DBLogHandler(pool, "sigma", uuid.uuid4())  # type: ignore[arg-type]

    await handler.shutdown(duration_ms=12_345, exit_code=0)
    await handler.shutdown(duration_ms=12_345, exit_code=1)

    assert pool.rows[0]["event_type"] == "SHUTDOWN"
    assert pool.rows[0]["severity"] == "INFO"
    assert pool.rows[1]["severity"] == "ERROR"
    assert pool.rows[0]["data"] == {"duration_ms": 12_345, "exit_code": 0}


@pytest.mark.asyncio
async def test_error_captures_traceback() -> None:
    pool = _FakePool()
    handler = DBLogHandler(pool, "sigma", uuid.uuid4())  # type: ignore[arg-type]

    try:
        raise ValueError("boom")
    except ValueError as exc:
        await handler.error(exc, context="scheduler_crash")

    assert len(pool.rows) == 1
    row = pool.rows[0]
    assert row["event_type"] == "ERROR"
    assert row["severity"] == "ERROR"
    assert row["data"]["context"] == "scheduler_crash"
    assert row["data"]["exception_type"] == "ValueError"
    assert "ValueError: boom" in row["data"]["traceback"]


# ────────────────────────────────────────────────────────────────────────────
# Robustness — DB failure does not raise
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_swallows_db_errors() -> None:
    """Trading must continue even if the audit DB is unhealthy."""
    handler = DBLogHandler(_ExplodingPool(), "sigma", uuid.uuid4())  # type: ignore[arg-type]

    # Should not raise — error is logged to structlog and swallowed.
    await handler.log("STARTUP", "starting", severity="INFO")


# ────────────────────────────────────────────────────────────────────────────
# Full sequence — every event the schedulers emit lands in order
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_run_sequence() -> None:
    pool = _FakePool()
    handler = DBLogHandler(pool, "sigma", uuid.uuid4())  # type: ignore[arg-type]

    await handler.startup(commit_sha="deadbeef")
    await handler.scan_complete(candidates=2, duration_ms=850)
    await handler.signal("AAPL", score=72.5, direction="LONG")
    await handler.order_submitted("AAPL", quantity=100, order_id="ord-1")
    await handler.fill_confirmed("MSFT", fill_price="402.10", pnl="125.00")
    await handler.shutdown(duration_ms=1_500, exit_code=0)

    assert [r["event_type"] for r in pool.rows] == [
        "STARTUP",
        "SCAN_COMPLETE",
        "SIGNAL",
        "ORDER_SUBMITTED",
        "FILL_CONFIRMED",
        "SHUTDOWN",
    ]


# ────────────────────────────────────────────────────────────────────────────
# Retention — old rows are deleted on every write
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retention_deletes_rows_older_than_window() -> None:
    pool = _FakePool()
    # Plant an "old" row directly (older than the 7-day default).
    pool.rows.append(
        _FakeRow(
            engine="sigma",
            run_id=uuid.uuid4(),
            event_type="STARTUP",
            severity="INFO",
            message="ancient run",
            data=None,
            recorded_at=datetime.now(UTC) - timedelta(days=14),
        )
    )
    assert len(pool.rows) == 1

    handler = DBLogHandler(pool, "sigma", uuid.uuid4())  # type: ignore[arg-type]
    await handler.log("STARTUP", "fresh run", severity="INFO")

    # Old row swept; the fresh one remains.
    assert len(pool.rows) == 1
    assert pool.rows[0]["message"] == "fresh run"


@pytest.mark.asyncio
async def test_retention_window_respects_constructor_value() -> None:
    """retention_days=2 must clip the cutoff at 2 days, not the 7-day default."""
    pool = _FakePool()
    handler = DBLogHandler(pool, "sigma", uuid.uuid4(), retention_days=2)  # type: ignore[arg-type]

    pool.rows.append(
        _FakeRow(
            engine="sigma",
            run_id=uuid.uuid4(),
            event_type="STARTUP",
            severity="INFO",
            message="3 days ago",
            data=None,
            recorded_at=datetime.now(UTC) - timedelta(days=3),
        )
    )
    pool.rows.append(
        _FakeRow(
            engine="sigma",
            run_id=uuid.uuid4(),
            event_type="STARTUP",
            severity="INFO",
            message="1 day ago",
            data=None,
            recorded_at=datetime.now(UTC) - timedelta(days=1),
        )
    )

    await handler.log("STARTUP", "now", severity="INFO")

    messages = [r["message"] for r in pool.rows]
    assert "3 days ago" not in messages  # past 2-day window
    assert "1 day ago" in messages  # inside 2-day window
    assert "now" in messages


# ────────────────────────────────────────────────────────────────────────────
# Retention exemption — REVIEW_DEFECT_* rows survive past the window
# (DR2.1, #254): the consolidated defect register's only durable
# primitive lives on application_log; the 7-day prune must NEVER delete
# an open review-found defect or it silently expires.
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event_type", ["REVIEW_DEFECT_LOGGED", "REVIEW_DEFECT_RESOLVED"]
)
@pytest.mark.asyncio
async def test_retention_exempts_review_defect_events(event_type) -> None:
    """An aged REVIEW_DEFECT_* row is NOT pruned; a same-age ordinary
    row IS (control). Drives the real prune SQL against the fake pool."""
    pool = _FakePool()
    aged = datetime.now(UTC) - timedelta(days=30)
    pool.rows.append(
        _FakeRow(
            engine="ops",
            run_id=uuid.uuid4(),
            event_type=event_type,
            severity="INFO",
            message="aged review defect",
            data={"defect_ref": "#254"},
            recorded_at=aged,
        )
    )
    pool.rows.append(
        _FakeRow(
            engine="sigma",
            run_id=uuid.uuid4(),
            event_type="STARTUP",  # control — ordinary, same age
            severity="INFO",
            message="aged ordinary",
            data=None,
            recorded_at=aged,
        )
    )

    handler = DBLogHandler(pool, "ops", uuid.uuid4())  # type: ignore[arg-type]
    await handler.log("STARTUP", "fresh run", severity="INFO")

    msgs = [r["message"] for r in pool.rows]
    assert "aged review defect" in msgs, (
        f"{event_type} was pruned — an open review-found defect must "
        "survive the retention window (it would silently expire)"
    )
    assert "aged ordinary" not in msgs  # control: ordinary aged row IS pruned
    assert "fresh run" in msgs
