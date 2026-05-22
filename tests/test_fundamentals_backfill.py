"""Sentinel tests for ``tpcore.data.fundamentals_backfill``.

PR fix/feed-audit-wave-1-critical-path-blockers — Wave-1 critical-path
heal for the largest single corpus-integrity red on ``main``
(``fundamentals_quarterly_completeness`` — 285/1090 tickers failing).

These tests pin the module-level CONTRACT (resumability, event-type
constant, target enumeration delegation) — NOT the FMP HTTP path
(that's covered by ``tpcore/fmp/`` adapter tests). The shape mirrors
the earnings-events-backfill / survivorship-backfill test pattern.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from tpcore.data.fundamentals_backfill import (
    DEFAULT_HISTORY_LIMIT_QUARTERS,
    PROGRESS_EVENT_TYPE,
    already_completed_tickers,
    backfill_one_ticker,
    backfill_universe,
)

# ── Fake asyncpg pool — minimal surface for the resumability query ────


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        # The only query the module issues against the pool is the
        # application_log resume probe. The fake responds with the
        # canned ticker list configured on the pool.
        return [
            {"ticker": t} for t in self._owner.completed_tickers
        ]


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, completed_tickers: list[str] | None = None) -> None:
        self.completed_tickers = completed_tickers or []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


# ── F1 — event-type constant is stable (resume probes pin on it) ──────


def test_F1_progress_event_type_is_stable() -> None:
    # The event-type string is the resume probe's selector — changing it
    # silently breaks resumability for every existing in-flight backfill.
    # If a rename IS needed, do it deliberately with a migration probe;
    # the test is the canary.
    assert PROGRESS_EVENT_TYPE == "FUNDAMENTALS_BACKFILL_TICKER_DONE"


# ── F2 — DEFAULT_HISTORY_LIMIT_QUARTERS is deep enough for audit gaps


def test_F2_default_history_limit_covers_audit_gap_horizon() -> None:
    # The audit's representative gap (ABCL 2019-07-01) is ~7 years
    # before 2026-05-22. The deeper-than-canonical-40 limit is the
    # whole point of this new stage. Lock the floor at 60 quarters
    # (~15 years) so a future tuning doesn't silently drop below the
    # audit horizon.
    assert DEFAULT_HISTORY_LIMIT_QUARTERS >= 60


# ── F3 — resume probe returns the configured ticker set ────────────────


async def test_F3_already_completed_tickers_reads_application_log() -> None:
    pool = _Pool(completed_tickers=["AAPL", "MSFT", "ABCL"])
    out = await already_completed_tickers(pool, lookback_days=30)
    assert out == {"AAPL", "MSFT", "ABCL"}


async def test_F3b_already_completed_tickers_returns_empty_set_when_no_log() -> None:
    pool = _Pool(completed_tickers=[])
    out = await already_completed_tickers(pool, lookback_days=30)
    assert out == set()


# ── F4 — backfill_one_ticker emits the resume marker on success ────────


async def test_F4_backfill_one_ticker_emits_progress_event() -> None:
    cache = AsyncMock()
    cache.backfill = AsyncMock(return_value=12)
    db_log = AsyncMock()
    db_log.log = AsyncMock()

    rows = await backfill_one_ticker(cache, db_log, "AAPL", end=None)
    assert rows == 12
    # The event-type constant must land on the application_log call.
    assert db_log.log.await_count == 1
    args, kwargs = db_log.log.call_args
    assert args[0] == PROGRESS_EVENT_TYPE
    # The per-ticker data payload carries the row count.
    assert kwargs["data"]["ticker"] == "AAPL"
    assert kwargs["data"]["rows_written"] == 12


async def test_F4b_backfill_one_ticker_emits_marker_on_no_data() -> None:
    # FMP "no usable fundamentals" → permanent skip (ETF). The marker
    # MUST still land so the resume probe doesn't keep retrying the
    # dead ticker.
    from tpcore.outage import DataProviderOutage

    cache = AsyncMock()
    cache.backfill = AsyncMock(
        side_effect=DataProviderOutage("no usable fundamentals for SPY"),
    )
    db_log = AsyncMock()
    db_log.log = AsyncMock()

    rows = await backfill_one_ticker(cache, db_log, "SPY", end=None)
    assert rows == 0
    assert db_log.log.await_count == 1
    args, _ = db_log.log.call_args
    assert args[0] == PROGRESS_EVENT_TYPE


# ── F5 — backfill_universe skips already-done tickers when resume=True ─


class _AsyncCM:
    """Async-context-manager that yields a stubbed adapter — matches
    the ``async with FMPFundamentalsAdapter()`` shape inside
    ``backfill_universe`` so we can patch the *symbol path* the source
    module looks up, not module-level globals (which don't exist
    because the import is deferred inside the function body)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def __aenter__(self) -> Any:
        return self._inner

    async def __aexit__(self, *exc) -> None:
        return None


async def test_F5_backfill_universe_resume_skips_done_tickers() -> None:
    # Two tickers in the universe; one is already in the application_log
    # → only one should be attempted.
    from unittest.mock import patch

    pool = _Pool(completed_tickers=["AAPL"])
    db_log = AsyncMock()
    db_log.log = AsyncMock()

    async def _fake_backfill_one(
        cache, db_log, symbol, *, end=None,
    ) -> int:
        return 5

    # Patch the SOURCE module where the deferred import resolves. The
    # ``async with FMPFundamentalsAdapter()`` line inside
    # ``backfill_universe`` ends up calling
    # ``tpcore.fmp.FMPFundamentalsAdapter`` directly.
    fake_adapter = AsyncMock()
    with patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        new=_fake_backfill_one,
    ), patch(
        "tpcore.fmp.FMPFundamentalsAdapter",
        return_value=_AsyncCM(fake_adapter),
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache",
    ):
        result = await backfill_universe(
            pool, db_log, ["AAPL", "MSFT"],
            resume=True, inter_symbol_sleep_s=0.0,
        )
    assert result["universe_size"] == 2
    assert result["resumed_skipped"] == 1
    assert result["tickers_attempted"] == 1
    assert result["tickers_succeeded"] == 1
    assert result["tickers_failed"] == 0


async def test_F5b_backfill_universe_no_resume_attempts_all() -> None:
    from unittest.mock import patch

    pool = _Pool(completed_tickers=["AAPL"])
    db_log = AsyncMock()
    db_log.log = AsyncMock()

    async def _fake_backfill_one(
        cache, db_log, symbol, *, end=None,
    ) -> int:
        return 3

    fake_adapter = AsyncMock()
    with patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        new=_fake_backfill_one,
    ), patch(
        "tpcore.fmp.FMPFundamentalsAdapter",
        return_value=_AsyncCM(fake_adapter),
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache",
    ):
        result = await backfill_universe(
            pool, db_log, ["AAPL", "MSFT"],
            resume=False, inter_symbol_sleep_s=0.0,
        )
    assert result["resumed_skipped"] == 0
    assert result["tickers_attempted"] == 2
    assert result["tickers_succeeded"] == 2


# ── F6 — enumerate_gap_tickers delegates to the validation check ──────


async def test_F6_enumerate_gap_tickers_delegates_to_repair_targets() -> None:
    from unittest.mock import patch

    pool = _Pool()
    expected = ["ABCL", "EXAMPLE2"]

    async def _fake_repair_targets(_pool):
        return expected, 42

    with patch(
        "tpcore.quality.validation.checks.fundamentals_quarterly_completeness."
        "compute_fundamentals_repair_targets",
        new=_fake_repair_targets,
    ):
        from tpcore.data.fundamentals_backfill import enumerate_gap_tickers

        got = await enumerate_gap_tickers(pool)
    assert got == expected
