"""Lab core — transient-DB retry helper (Supabase pooler resilience).

Pinned 2026-05-20 after three consecutive ``python -m ops.lab`` runs
against the live Supabase transaction-pooler died at the same point —
the inter-window panel-load query with
``connection was closed in the middle of operation``. The Lab core's
per-window ``ctx_loader(...)`` is one long-running query (panel-load
~50-80s on a multi-year window of T1+T2 mega-caps); the pooler closes
the connection at some point during that load. A bare exception
propagates and the whole run dies — partial trial-budget spend, NO
dossier verdict, no honest gate read. The retry helper:

  1. Catches the canonical Supabase / asyncpg transient connection
     errors (substring match on the well-defined message families).
  2. Retries up to ``max_attempts`` times with a backoff.
  3. Re-raises on non-transient errors or after exhausting attempts.

Crucially, the retry is **on the panel-load only** — never on the
per-trial scoring math or the credibility/dossier writes, which are
deterministic CPU work whose failure modes are NOT transient. The
helper sits next to the long-running DB query that ACTUALLY observed
the failure mode, and nowhere else.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ops.lab.run import _is_transient_db_error, _retry_transient_db


def test_is_transient_db_error_matches_canonical_messages() -> None:
    """The substring-match table is the contract — every observed
    Supabase / asyncpg transient error message family must match."""
    for msg in (
        # The exact 2026-05-20 vector_composite probe failure.
        "connection was closed in the middle of operation",
        # asyncpg connection-loss family.
        "connection is closed",
        "Connection was closed",
        # Server-side drop.
        "server closed the connection unexpectedly",
        # Pool exhaustion / refusal (often transient on Supabase pooler).
        "connection refused",
        "cannot get connection",
    ):
        assert _is_transient_db_error(Exception(msg)), (
            f"expected transient: {msg!r}")


def test_is_transient_db_error_does_not_match_logic_errors() -> None:
    """Non-transient errors must propagate immediately — a retry on a
    logic error wastes ledger budget without producing a verdict."""
    for msg in (
        "syntax error at or near",
        "relation 'foo' does not exist",
        "ZeroDivisionError",
        "AttributeError: 'NoneType' has no attribute 'x'",
        "permission denied for table prices_daily",
    ):
        assert not _is_transient_db_error(Exception(msg)), (
            f"unexpected transient: {msg!r}")


@pytest.mark.asyncio
async def test_retry_succeeds_on_first_attempt() -> None:
    """A coroutine that succeeds first call returns immediately —
    no retry, no backoff sleep."""
    fn = AsyncMock(return_value="ok")
    result = await _retry_transient_db(
        fn, max_attempts=3, backoff_secs=0.0, label="test")
    assert result == "ok"
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_recovers_after_one_transient_then_success() -> None:
    """The canonical happy path for this helper: one Supabase drop,
    one retry, success on attempt 2."""
    call_log: list[int] = []

    async def fn() -> str:
        call_log.append(1)
        if len(call_log) == 1:
            raise Exception("connection was closed in the middle of operation")
        return "recovered"

    result = await _retry_transient_db(
        fn, max_attempts=3, backoff_secs=0.0, label="test")
    assert result == "recovered"
    assert len(call_log) == 2


@pytest.mark.asyncio
async def test_retry_reraises_non_transient_immediately() -> None:
    """A non-transient error MUST propagate on the first attempt —
    retrying a logic bug burns ledger budget without producing a
    verdict (the gate-discipline failure mode this helper avoids)."""
    fn = AsyncMock(side_effect=ValueError("relation 'foo' does not exist"))
    with pytest.raises(ValueError, match="relation 'foo' does not exist"):
        await _retry_transient_db(
            fn, max_attempts=3, backoff_secs=0.0, label="test")
    assert fn.call_count == 1, (
        "non-transient errors must NOT be retried")


@pytest.mark.asyncio
async def test_retry_raises_after_max_attempts_on_persistent_transient() -> None:
    """If every attempt observes the same transient error, the helper
    re-raises the last one after max_attempts — gracefully degraded,
    not silently green."""
    fn = AsyncMock(side_effect=Exception("connection was closed in the middle of operation"))
    with pytest.raises(Exception, match="connection was closed"):
        await _retry_transient_db(
            fn, max_attempts=3, backoff_secs=0.0, label="test")
    assert fn.call_count == 3
