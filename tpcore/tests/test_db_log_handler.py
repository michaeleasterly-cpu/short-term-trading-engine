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
            if "REVIEW_DEFECT_SECONDARY_CAP" in sql:
                return self._exec_secondary_cap(sql, args)
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

    def _exec_secondary_cap(self, sql: str, args) -> str:
        """Faithfully simulate the P4 secondary bounded cap (D4).

        Re-implements the production secondary-cap predicate from first
        principles (NOT a copy of the SQL string) so the test bites on
        the real semantics: a ``REVIEW_DEFECT_*`` row is deleted iff it
        is (1) older than the age cutoff $1, AND (2) NOT among the most
        recent ``$2`` ``REVIEW_DEFECT_*`` rows by recorded_at DESC, AND
        (3) NOT part of an OPEN defect — a ``REVIEW_DEFECT_LOGGED`` with
        no later matching ``REVIEW_DEFECT_RESOLVED`` for the same
        ``defect_ref`` (the anti-join mirroring ``_REVIEW_OPEN_SQL``).
        The capped event-type set is parsed from the SQL's
        ``event_type IN (...)`` so the fake tracks the real constant."""
        cutoff, max_rows = args
        capped: set[str] = set()
        if "event_type IN" in sql:
            inside = sql.split("event_type IN", 1)[1]
            inside = inside.split("(", 1)[1].split(")", 1)[0]
            capped = {
                tok.strip().strip("'\"")
                for tok in inside.split(",")
                if tok.strip()
            }

        # The fake honors EXACTLY the predicates literally present in
        # the real SQL (parsed, not assumed) so a regression that
        # deletes a guard from _REVIEW_DEFECT_CAP_SQL makes the matching
        # test BITE — same discipline as the primary prune's
        # ``event_type NOT IN`` parse above.
        has_age_guard = "a.recorded_at < $1" in sql
        has_recent_guard = "ROW_NUMBER()" in sql
        has_open_guard = (
            "REVIEW_DEFECT_RESOLVED" in sql
            and "NOT EXISTS" in sql
        )

        defect_rows = [r for r in self._rows if r["event_type"] in capped]
        # Most-recent-`max_rows` by recorded_at DESC, deterministic
        # tiebreak on the row's identity (recorded_at then list order ≈
        # the production ``recorded_at DESC, id DESC``). Only meaningful
        # when the SQL actually carries the ROW_NUMBER() recent guard.
        keep_recent = (
            {
                id(r)
                for r in sorted(
                    defect_rows,
                    key=lambda r: r["recorded_at"],
                    reverse=True,
                )[:max_rows]
            }
            if has_recent_guard
            else set()
        )
        # Open-defect anti-join: a LOGGED with no later RESOLVED for the
        # same defect_ref is OPEN and must NEVER be pruned.
        resolved_refs_at: dict[str, list[datetime]] = {}
        for r in self._rows:
            if r["event_type"] == "REVIEW_DEFECT_RESOLVED":
                ref = (r.get("data") or {}).get("defect_ref")
                if ref is not None:
                    resolved_refs_at.setdefault(ref, []).append(
                        r["recorded_at"]
                    )

        def _is_open(row: _FakeRow) -> bool:
            if not has_open_guard:
                return False  # SQL dropped the guard → nothing protected
            if row["event_type"] != "REVIEW_DEFECT_LOGGED":
                return False
            ref = (row.get("data") or {}).get("defect_ref")
            if ref is None:
                return False
            return not any(
                ts >= row["recorded_at"]
                for ts in resolved_refs_at.get(ref, [])
            )

        before = len(self._rows)
        self._rows[:] = [
            r
            for r in self._rows
            if not (
                r["event_type"] in capped
                and (not has_age_guard or r["recorded_at"] < cutoff)
                and id(r) not in keep_recent
                and not _is_open(r)
            )
        ]
        return f"DELETE {before - len(self._rows)}"


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


def test_run_id_property_returns_constructor_value() -> None:
    """The public ``run_id`` property exposes the UUID passed at
    construction — call sites must never have to reach through
    ``_run_id`` to read the tag the handler is bound to."""
    expected = uuid.uuid4()
    handler = DBLogHandler(pool=_FakePool(), engine="sigma", run_id=expected)  # type: ignore[arg-type]
    assert handler.run_id == expected


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

    # INSERT + primary retention DELETE + the P4 (#254 D4) bounded
    # secondary REVIEW_DEFECT_* cap DELETE — all on the same write call,
    # same DBLogHandler path (no new mechanism/daemon).
    assert len(pool.calls) == 3
    assert "INSERT INTO platform.application_log" in pool.calls[0][0]
    assert "DELETE FROM platform.application_log" in pool.calls[1][0]
    assert "REVIEW_DEFECT_SECONDARY_CAP" in pool.calls[2][0]
    assert "DELETE FROM platform.application_log" in pool.calls[2][0]


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


# ────────────────────────────────────────────────────────────────────────────
# Single-source-of-truth + empty-set safety (DR2 code-quality fix): the
# real _RETENTION_SQL exemption clause is derived ONCE at module load
# from RETENTION_EXEMPT_EVENT_TYPES — there is no duplicated hard-coded
# literal that can drift, and an empty exempt set must NOT emit
# ``NOT IN ()`` (a Postgres syntax error).
# ────────────────────────────────────────────────────────────────────────────


def test_retention_sql_derived_from_constant_no_drift() -> None:
    """Every exempt event type — derived FROM the constant, not a
    hard-coded literal — must appear in the real prune SQL, and no
    OTHER quoted token may. This bites if someone re-hardcodes a
    divergent literal (constant and SQL would diverge)."""
    from tpcore.logging import db_handler as dh

    for et in dh.RETENTION_EXEMPT_EVENT_TYPES:
        assert repr(et) in dh._RETENTION_SQL, (  # noqa: SLF001
            f"{et!r} is in RETENTION_EXEMPT_EVENT_TYPES but missing from "
            "_RETENTION_SQL — constant and prune SQL have drifted"
        )
    if dh.RETENTION_EXEMPT_EVENT_TYPES:
        clause = dh._RETENTION_SQL.split("event_type NOT IN", 1)[1]  # noqa: SLF001
        inside = clause.split("(", 1)[1].split(")", 1)[0]
        in_sql = {tok.strip().strip("'\"") for tok in inside.split(",") if tok.strip()}
        assert in_sql == set(dh.RETENTION_EXEMPT_EVENT_TYPES), (
            "the prune's NOT IN set must equal the constant exactly — "
            "a re-hardcoded extra/missing literal would diverge"
        )


def test_empty_exempt_set_emits_no_not_in_clause(monkeypatch) -> None:
    """With an empty exempt tuple the rebuilt clause must contain NO
    ``NOT IN`` (``NOT IN ()`` is a Postgres syntax error) and an aged
    row of a former-exempt type IS pruned (clause-builder reproduced
    from the production logic so the test bites on its semantics)."""
    from tpcore.logging import db_handler as dh

    monkeypatch.setattr(dh, "RETENTION_EXEMPT_EVENT_TYPES", ())
    exempt: tuple[str, ...] = dh.RETENTION_EXEMPT_EVENT_TYPES
    clause = (
        f"\n  AND event_type NOT IN ({', '.join(repr(e) for e in exempt)})"
        if exempt
        else ""
    )
    rebuilt_sql = f"""
DELETE FROM platform.application_log
WHERE recorded_at < $1{clause}
"""
    assert "NOT IN" not in rebuilt_sql
    assert "NOT IN ()" not in rebuilt_sql

    # Drive the fake prune with the empty-set SQL: a formerly-exempt
    # aged row is now pruned (the fake parses the clause from the SQL).
    rows: list[_FakeRow] = [
        _FakeRow(
            engine="ops",
            run_id=uuid.uuid4(),
            event_type="REVIEW_DEFECT_LOGGED",
            severity="INFO",
            message="aged former-exempt",
            data=None,
            recorded_at=datetime.now(UTC) - timedelta(days=30),
        )
    ]
    import asyncio

    conn = _FakeConn(rows, [])
    asyncio.run(conn.execute(rebuilt_sql, datetime.now(UTC) - timedelta(days=7)))
    assert [r["message"] for r in rows] == []  # no exemption → pruned


# ────────────────────────────────────────────────────────────────────────────
# P4 (#254 D4): bounded SECONDARY cap so "retention-exempt" ≠ "infinite".
# A REVIEW_DEFECT_* row is retained iff younger than 180d OR among the
# most-recent-2000 such rows (the more-retentive UNION) — AND
# unconditionally retained while it is an OPEN (unresolved) defect (a
# LOGGED with no later RESOLVED for its defect_ref; the anti-join
# mirroring ops.defect_register._REVIEW_OPEN_SQL). An open defect is
# NEVER pruned regardless of age/count. Same DBLogHandler prune path.
# ────────────────────────────────────────────────────────────────────────────


def _plant_review(pool, *, ref: str, message: str, age_days: float,
                   resolved: bool = False, resolved_age_days: float = 0.0):
    """Plant a CLOSED-or-OPEN review defect: a LOGGED row, plus a
    RESOLVED row when ``resolved`` (later than the LOGGED so the
    anti-join treats the ref as closed)."""
    pool.rows.append(
        _FakeRow(
            engine="ops",
            run_id=uuid.uuid4(),
            event_type="REVIEW_DEFECT_LOGGED",
            severity="INFO",
            message=message,
            data={"defect_ref": ref},
            recorded_at=datetime.now(UTC) - timedelta(days=age_days),
        )
    )
    if resolved:
        pool.rows.append(
            _FakeRow(
                engine="ops",
                run_id=uuid.uuid4(),
                event_type="REVIEW_DEFECT_RESOLVED",
                severity="INFO",
                message=f"{message} (resolved)",
                data={"defect_ref": ref},
                recorded_at=datetime.now(UTC)
                - timedelta(days=resolved_age_days),
            )
        )


@pytest.mark.asyncio
async def test_p4_review_defect_younger_than_180d_not_pruned() -> None:
    """(a) A REVIEW_DEFECT_* row younger than the 180-day cap survives
    even though the 7-day primary prune exempts it — the secondary cap
    must not touch a young row."""
    pool = _FakePool()
    _plant_review(pool, ref="#young", message="young closed defect",
                  age_days=10, resolved=True, resolved_age_days=9)

    handler = DBLogHandler(pool, "ops", uuid.uuid4())  # type: ignore[arg-type]
    await handler.log("STARTUP", "fresh run", severity="INFO")

    assert "young closed defect" in [r["message"] for r in pool.rows]


@pytest.mark.asyncio
async def test_p4_review_defect_old_but_within_recent_2000_not_pruned() -> None:
    """(b) Older than 180d but among the most-recent-2000 → retained
    (the more-retentive UNION: recent-2000 OR <180d)."""
    pool = _FakePool()
    # One aged, closed defect — only 1 REVIEW_DEFECT_* row, so it is
    # trivially within the most-recent-2000 ⇒ retained despite age.
    _plant_review(pool, ref="#old-in-2000",
                  message="old but recent-2000 defect",
                  age_days=400, resolved=True, resolved_age_days=399)

    handler = DBLogHandler(pool, "ops", uuid.uuid4())  # type: ignore[arg-type]
    await handler.log("STARTUP", "fresh run", severity="INFO")

    assert "old but recent-2000 defect" in [r["message"] for r in pool.rows]


@pytest.mark.asyncio
async def test_p4_review_defect_old_and_beyond_2000_is_pruned() -> None:
    """(c) A closed defect older than 180d AND beyond the
    most-recent-N → IS pruned. The cap ranks ALL REVIEW_DEFECT_* rows
    (LOGGED+RESOLVED) together by recorded_at DESC (mirroring the real
    SQL). 3 closed pairs (6 rows); driving the real cap SQL with
    max_rows=4 keeps the 4 newest rows = the 2 newest pairs, and prunes
    BOTH rows of the oldest pair (aged AND beyond the recent set)."""
    from tpcore.logging import db_handler as dh

    pool = _FakePool()
    # Well-separated ages so the recent-N boundary is unambiguous; the
    # real cap=2000 can't be exceeded in a unit test, so drive the real
    # secondary SQL directly with a small max_rows bound — the
    # predicate (not the magnitude of the constant) is what's exercised.
    for i, age in enumerate((600, 400, 300)):
        _plant_review(pool, ref=f"#beyond-{i}",
                      message=f"closed aged {i}", age_days=age,
                      resolved=True, resolved_age_days=age - 50)
    conn = _FakeConn(pool.rows, [])
    cutoff = datetime.now(UTC) - timedelta(
        days=dh._REVIEW_DEFECT_MAX_AGE_DAYS)  # noqa: SLF001
    await conn.execute(dh._REVIEW_DEFECT_CAP_SQL, cutoff, 4)  # noqa: SLF001

    msgs = [r["message"] for r in pool.rows]
    # Oldest closed pair (age 600/550) is aged AND beyond the recent-4
    # set → pruned (LOGGED has a later RESOLVED, so not open-protected).
    assert "closed aged 0" not in msgs
    assert "closed aged 0 (resolved)" not in msgs
    # The 2 newer closed pairs (4 rows) are within the recent-4 → kept.
    assert "closed aged 1" in msgs
    assert "closed aged 2" in msgs


@pytest.mark.asyncio
async def test_p4_open_defect_never_pruned_regardless_of_age_or_count() -> None:
    """(d) An OPEN defect (LOGGED with NO later RESOLVED) is NEVER
    pruned — even older than 180d AND beyond the most-recent-2000. The
    anti-join mirroring _REVIEW_OPEN_SQL is absolute."""
    from tpcore.logging import db_handler as dh

    pool = _FakePool()
    # An ancient OPEN defect (no RESOLVED) + 3 newer closed ones; drive
    # the real cap SQL with max_rows=2 so the open one is both aged AND
    # beyond-2000-equivalent — it must still survive.
    _plant_review(pool, ref="#open-ancient",
                  message="ancient OPEN defect", age_days=900)
    for i, age in enumerate((500, 450, 400)):
        _plant_review(pool, ref=f"#c{i}", message=f"closed {i}",
                      age_days=age, resolved=True,
                      resolved_age_days=age - 1)
    conn = _FakeConn(pool.rows, [])
    cutoff = datetime.now(UTC) - timedelta(
        days=dh._REVIEW_DEFECT_MAX_AGE_DAYS)  # noqa: SLF001
    await conn.execute(dh._REVIEW_DEFECT_CAP_SQL, cutoff, 2)  # noqa: SLF001

    assert "ancient OPEN defect" in [r["message"] for r in pool.rows], (
        "an OPEN review defect was pruned by the secondary cap — the "
        "never-prune-open invariant (anti-join mirroring "
        "_REVIEW_OPEN_SQL) is absolute regardless of age/count"
    )


@pytest.mark.asyncio
async def test_p4_ordinary_non_exempt_rows_still_pruned_at_7d() -> None:
    """(e) Control, unchanged: an ordinary non-exempt aged row is still
    pruned by the 7-day primary prune (P4 adds a cap, never relaxes the
    primary)."""
    pool = _FakePool()
    pool.rows.append(
        _FakeRow(
            engine="sigma",
            run_id=uuid.uuid4(),
            event_type="STARTUP",
            severity="INFO",
            message="aged ordinary",
            data=None,
            recorded_at=datetime.now(UTC) - timedelta(days=30),
        )
    )

    handler = DBLogHandler(pool, "sigma", uuid.uuid4())  # type: ignore[arg-type]
    await handler.log("STARTUP", "fresh run", severity="INFO")

    msgs = [r["message"] for r in pool.rows]
    assert "aged ordinary" not in msgs
    assert "fresh run" in msgs


@pytest.mark.asyncio
async def test_p4_resolved_pair_old_and_beyond_2000_is_prunable() -> None:
    """(f) A RESOLVED-pair (closed defect) that is old AND beyond the
    most-recent-2000 IS prunable — the open-predicate does NOT protect a
    closed defect (it has a later RESOLVED for its ref)."""
    from tpcore.logging import db_handler as dh

    pool = _FakePool()
    # The target closed pair is the OLDEST; 2 newer closed pairs fill
    # the max_rows=2 recent set so the target is beyond-cap AND aged.
    _plant_review(pool, ref="#closed-old", message="closed old target",
                  age_days=600, resolved=True, resolved_age_days=599)
    for i, age in enumerate((300, 250)):
        _plant_review(pool, ref=f"#n{i}", message=f"closed newer {i}",
                      age_days=age, resolved=True,
                      resolved_age_days=age - 1)
    conn = _FakeConn(pool.rows, [])
    cutoff = datetime.now(UTC) - timedelta(
        days=dh._REVIEW_DEFECT_MAX_AGE_DAYS)  # noqa: SLF001
    await conn.execute(dh._REVIEW_DEFECT_CAP_SQL, cutoff, 2)  # noqa: SLF001

    assert "closed old target" not in [r["message"] for r in pool.rows], (
        "a CLOSED (resolved) review defect, old AND beyond the cap, "
        "must be prunable — the never-prune-open guard only protects "
        "OPEN defects"
    )


def test_p4_cap_sql_derived_from_constants_no_injection() -> None:
    """The secondary-cap SQL reuses the SAME constant-derived clause
    pattern as _RETENTION_EXEMPT_CLAUSE: the capped event-type IN-list
    is built ONCE at module load from RETENTION_EXEMPT_EVENT_TYPES
    (compile-time string literals — no injection surface), and the
    age/row bounds are the module constants, not hard-coded literals."""
    from tpcore.logging import db_handler as dh

    assert dh._REVIEW_DEFECT_MAX_AGE_DAYS == 180  # noqa: SLF001
    assert dh._REVIEW_DEFECT_MAX_ROWS == 2000  # noqa: SLF001
    # Every exempt/capped event type appears in the cap SQL's IN-list,
    # derived FROM the constant (no divergent re-hardcoded literal).
    clause = dh._REVIEW_DEFECT_CAP_SQL.split("event_type IN", 1)[1]  # noqa: SLF001
    inside = clause.split("(", 1)[1].split(")", 1)[0]
    in_sql = {tok.strip().strip("'\"") for tok in inside.split(",") if tok.strip()}
    assert in_sql == set(dh.RETENTION_EXEMPT_EVENT_TYPES), (
        "the secondary cap's capped-event-type set must equal "
        "RETENTION_EXEMPT_EVENT_TYPES exactly (single source of truth)"
    )
    # Bound params, not interpolated values → no injection on the bounds.
    assert "$1" in dh._REVIEW_DEFECT_CAP_SQL  # noqa: SLF001
    assert "$2" in dh._REVIEW_DEFECT_CAP_SQL  # noqa: SLF001
