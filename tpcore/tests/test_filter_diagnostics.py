"""Tests for :class:`FilterDiagnostics` + the extended ``DBLogHandler.signal``.

Five tests per the build spec:

1. Model instantiates with defaults (int fields 0, optional fields None)
2. ``model_dump(exclude_none=True)`` drops the engine-specific None fields
3. JSON round-trip is identity
4. ``signal(..., extra_data=...)`` merges extras into the data payload
5. ``signal(...)`` without ``extra_data`` is backward-compatible
"""
from __future__ import annotations

import json
import uuid

import pytest

from tpcore.backtest import FilterDiagnostics
from tpcore.logging.db_handler import DBLogHandler

# ────────────────────────────────────────────────────────────────────────────
# Fake pool — same pattern as tpcore/tests/test_db_log_handler.py.
# Kept self-contained so this test file is independently runnable.
# ────────────────────────────────────────────────────────────────────────────


class _FakeRow(dict):
    pass


class _FakeConn:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    async def execute(self, sql: str, *args) -> str:
        if "INSERT INTO platform.application_log" in sql:
            engine, run_id, event_type, severity, message, data_json = args
            self._rows.append(
                _FakeRow(
                    engine=engine, run_id=run_id, event_type=event_type,
                    severity=severity, message=message,
                    data=json.loads(data_json) if data_json else None,
                )
            )
            return "INSERT 0 1"
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

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(_FakeConn(self.rows))


# ────────────────────────────────────────────────────────────────────────────
# Test 1 — Model instantiates with defaults
# ────────────────────────────────────────────────────────────────────────────


def test_model_instantiates_with_defaults():
    diag = FilterDiagnostics()
    # Common counters default to 0
    assert diag.universe_total == 0
    assert diag.coarse_liquidity_blocked == 0
    assert diag.candidates_passed == 0
    # Engine-specific counters default to None
    assert diag.gate1_value_blocked is None
    assert diag.gate2_earnings_blocked is None
    assert diag.adx_blocked is None
    assert diag.z_score_blocked is None


# ────────────────────────────────────────────────────────────────────────────
# Test 2 — exclude_none drops engine-specific None fields
# ────────────────────────────────────────────────────────────────────────────


def test_model_excludes_none_on_dump():
    diag = FilterDiagnostics(universe_total=100, candidates_passed=5)
    dumped = diag.model_dump(exclude_none=True)
    # The three common counters are present (they default to int(0), not None).
    assert dumped == {
        "universe_total": 100,
        "coarse_liquidity_blocked": 0,
        "candidates_passed": 5,
    }
    # No engine-specific keys present
    for k in ("gate1_value_blocked", "adx_blocked", "z_score_blocked", "rsi_blocked"):
        assert k not in dumped


# ────────────────────────────────────────────────────────────────────────────
# Test 3 — JSON round-trip is identity
# ────────────────────────────────────────────────────────────────────────────


def test_model_json_roundtrip():
    diag = FilterDiagnostics(
        universe_total=15, coarse_liquidity_blocked=2, candidates_passed=4,
        gate1_value_blocked=3, gate2_earnings_blocked=5, gate3_technical_blocked=1,
    )
    js = diag.model_dump_json()
    rebuilt = FilterDiagnostics.model_validate_json(js)
    assert rebuilt == diag
    # Round-tripped dict matches too
    assert rebuilt.model_dump() == diag.model_dump()


# ────────────────────────────────────────────────────────────────────────────
# Test 4 — signal() merges extra_data into the payload
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_extra_data_merge():
    pool = _FakePool()
    handler = DBLogHandler(pool=pool, engine="vector", run_id=uuid.uuid4())  # type: ignore[arg-type]
    diag = FilterDiagnostics(universe_total=15, candidates_passed=2, gate1_value_blocked=4)
    extra = {"filter_diagnostics": diag.model_dump(exclude_none=True)}
    await handler.signal(ticker="AAPL", score=78.0, direction="LONG", extra_data=extra)
    assert len(pool.rows) == 1
    payload = pool.rows[0]["data"]
    # Base keys preserved
    assert payload["ticker"] == "AAPL"
    assert payload["score"] == 78.0
    assert payload["direction"] == "LONG"
    # Extra keys merged in
    assert "filter_diagnostics" in payload
    assert payload["filter_diagnostics"]["universe_total"] == 15
    assert payload["filter_diagnostics"]["gate1_value_blocked"] == 4
    # None-defaulted keys still absent in the round-trip
    assert "gate2_earnings_blocked" not in payload["filter_diagnostics"]


# ────────────────────────────────────────────────────────────────────────────
# Test 5 — backward-compat: signal() without extra_data still works
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_backward_compatible_no_extra_data():
    pool = _FakePool()
    handler = DBLogHandler(pool=pool, engine="vector", run_id=uuid.uuid4())  # type: ignore[arg-type]
    await handler.signal(ticker="MSFT", score=82.5, direction="LONG")
    assert len(pool.rows) == 1
    payload = pool.rows[0]["data"]
    assert payload == {"ticker": "MSFT", "score": 82.5, "direction": "LONG"}
