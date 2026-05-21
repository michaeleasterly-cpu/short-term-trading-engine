"""Lab core — final-holdout chunked replay (statement_timeout mitigation,
2026-05-21).

Operator-reproduced 2026-05-21 reversion sweep crashed in the final-
holdout replay with ``canceling statement due to statement timeout``
even after PR #166 raised the read-only/Lab pool's statement_timeout to
30 min. Root cause: the legacy single-call replay loaded
``[train_start, final_holdout_end]`` (≈7-8 years × T1+T2 universe in ONE
SELECT — case #1 of the diagnosis, one long SQL).

These tests prove the chunked replay:

  1. ``chunk_final_holdout`` partitions the final-holdout span disjointly
     into per-year chunks (no double-counting, no gaps; the union of
     chunk slices equals the original [holdout_start, holdout_end]).
  2. ``_run_final_holdout_chunked`` issues MULTIPLE ``ctx_loader`` calls
     (one per chunk), so no single SQL load spans the full ~7-year
     monolith — the statement_timeout-trip surface.
  3. The aggregate trade list is the same universe of held-back trades
     the monolithic replay would have produced (disjoint chunk slices,
     trades preserved exactly once per chunk).
  4. The verdict (DSR, n_trades floor, credibility) is computed on the
     AGGREGATE — chunking is purely a transport mitigation, not an
     N-independent-run multiplication.
  5. The SP-A ``record_trial_spend`` ledger spend is upstream of the
     replay (called ONCE per Lab run) — chunking the replay does NOT
     multiply the spend.

Wrapped in ``pytest.mark.xdist_group("ops_shadow")`` per the
ops-package-shadow CI rule (any test touching ``ops.*`` imports goes
into the ops_shadow group).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


# ── (1) chunk_final_holdout is the partition contract ───────────────────


def test_chunk_final_holdout_partitions_per_year():
    """The 4.5-year operator-reproduced final-holdout (2022-01-01 →
    2026-05-15) splits into 5 per-year chunks (2022, 2023, 2024, 2025,
    2026-Jan→May-15). Each chunk's slice is bounded, the slices are
    disjoint, and their union equals the original span exactly."""
    from ops.lab.run import chunk_final_holdout

    chunks = chunk_final_holdout(
        holdout_start=date(2022, 1, 1),
        holdout_end=date(2026, 5, 15),
        chunk_months=12,
    )
    # 5 per-year chunks (2022, 2023, 2024, 2025, 2026 partial).
    assert len(chunks) == 5

    # Each chunk slice is non-empty; load_start precedes chunk_start by
    # the warmup window (default 365d).
    for load_start, chunk_start, chunk_end in chunks:
        assert load_start < chunk_start
        assert chunk_start <= chunk_end
        assert (chunk_start - load_start).days == 365  # default warmup

    # Disjoint: chunk[i].chunk_end + 1 day == chunk[i+1].chunk_start.
    for i in range(len(chunks) - 1):
        _, _, end_i = chunks[i]
        _, start_next, _ = chunks[i + 1]
        assert end_i + timedelta(days=1) == start_next, (
            f"chunks {i} and {i+1} are not disjoint: "
            f"end_i={end_i} start_next={start_next}"
        )

    # Union covers exactly the original span.
    first_chunk_start = chunks[0][1]
    last_chunk_end = chunks[-1][2]
    assert first_chunk_start == date(2022, 1, 1)
    assert last_chunk_end == date(2026, 5, 15)


def test_chunk_final_holdout_per_month_fallback():
    """Per-month chunking (chunk_months=1) is the documented fallback if
    per-year still hits the timeout. It must also partition cleanly."""
    from ops.lab.run import chunk_final_holdout

    chunks = chunk_final_holdout(
        holdout_start=date(2024, 1, 1),
        holdout_end=date(2024, 12, 31),
        chunk_months=1,
    )
    assert len(chunks) == 12
    # Disjoint and complete.
    for i in range(len(chunks) - 1):
        _, _, end_i = chunks[i]
        _, start_next, _ = chunks[i + 1]
        assert end_i + timedelta(days=1) == start_next
    assert chunks[0][1] == date(2024, 1, 1)
    assert chunks[-1][2] == date(2024, 12, 31)


def test_chunk_final_holdout_single_chunk_when_span_short():
    """A 6-month final-holdout fits in ONE per-year chunk — the chunking
    helper degrades cleanly to a single-chunk single-load (no spurious
    second SQL when the span is already small)."""
    from ops.lab.run import chunk_final_holdout

    chunks = chunk_final_holdout(
        holdout_start=date(2024, 1, 1),
        holdout_end=date(2024, 6, 30),
        chunk_months=12,
    )
    assert len(chunks) == 1
    _, chunk_start, chunk_end = chunks[0]
    assert chunk_start == date(2024, 1, 1)
    assert chunk_end == date(2024, 6, 30)


def test_chunk_final_holdout_rejects_inverted_span():
    """A holdout_end < holdout_start is an invariant violation — fail
    loud, never silently produce 0 chunks (which would skip the replay
    entirely and ship a false-green verdict)."""
    from ops.lab.run import chunk_final_holdout

    with pytest.raises(ValueError, match="cannot chunk"):
        chunk_final_holdout(
            holdout_start=date(2025, 1, 1),
            holdout_end=date(2024, 12, 31),
        )


# ── (2) _run_final_holdout_chunked issues N loads, ONE run aggregation ──


@pytest.mark.asyncio
async def test_chunked_replay_issues_one_load_per_chunk_and_aggregates():
    """The 4.5-year replay must call the engine's ``run_for_search``
    FIVE times (one per per-year chunk), NOT once with the full 7-year
    monolith. Each chunk contributes its in-slice trades to the
    aggregate exactly once (no double-counting at boundaries)."""
    from ops.lab.run import _run_final_holdout_chunked

    runner_calls: list[dict] = []

    @dataclass
    class _RR:
        credibility_score: int
        credibility_rubric: object
        trade_log: list

    async def _runner(*, db_url, start, end, overrides, universe):
        runner_calls.append({
            "db_url": db_url, "start": start, "end": end,
            "overrides": dict(overrides or {}), "universe": universe,
        })
        # Each chunk emits one trade dated mid-chunk and one in the
        # warmup region; the chunker MUST drop the warmup trade via its
        # per-chunk [chunk_start, chunk_end] slice filter.
        chunk_start = start + timedelta(days=365)  # warmup = 365d default
        in_slice = _Trade(
            entry_date=chunk_start + timedelta(days=30), pnl_pct=0.01)
        outside_slice = _Trade(
            entry_date=start + timedelta(days=10), pnl_pct=-0.99)
        return _RR(
            credibility_score=80,
            credibility_rubric=f"rubric_for_{end.isoformat()}",
            trade_log=[outside_slice, in_slice],
        )

    held_trades, cred_score, cred_rubric = await _run_final_holdout_chunked(
        runner=_runner,
        db_url="postgres://fake/db",
        train_start=date(2018, 1, 1),  # NEVER used as a chunk load_start
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2026, 5, 15),
        overrides={"signal_mode": "price_z"},
        universe=("AAPL", "MSFT"),
        chunk_months=12,
    )

    # FIVE chunk runner calls (one per chunk), NOT one monolithic call
    # over 2018→2026.
    assert len(runner_calls) == 5, (
        f"chunked replay must issue one runner call per chunk; got "
        f"{len(runner_calls)} calls")
    # No chunk runner call spans more than ~1.25 years (chunk_months=12
    # + 365d warmup is bounded — guarantees each SQL is well under the
    # 30-min timeout on T1+T2-class universes; this is the transport
    # invariant).
    for call in runner_calls:
        span_days = (call["end"] - call["start"]).days
        assert span_days <= 366 + 366, (
            f"chunk SQL span {span_days}d exceeds bounded ~1.25y window "
            f"— statement_timeout invariant broken")
        # train_start (2018-01-01) is NEVER used as a load_start (that
        # was the monolith that timed out).
        assert call["start"] > date(2019, 1, 1), (
            f"chunk load_start {call['start']} is inside the legacy "
            f"monolithic train_start span — chunking did NOT shrink the "
            f"SQL")

    # Aggregate: ONE trade per chunk (the outside-slice trade per chunk
    # MUST be dropped by the per-chunk filter). FIVE chunks → FIVE
    # aggregated trades.
    assert len(held_trades) == 5, (
        f"aggregate must equal sum of in-slice trades across chunks; "
        f"got {len(held_trades)} trades — boundary filter is wrong")
    # No outside-slice (-0.99) trade leaked through.
    assert all(t.pnl_pct == 0.01 for t in held_trades), (
        "warmup-region trades leaked past the chunker's per-chunk slice "
        "filter — invariant broken")

    # Credibility taken from the LAST chunk's run (documented behaviour).
    last_chunk_end = max(c["end"] for c in runner_calls)
    assert cred_score == 80
    assert cred_rubric == f"rubric_for_{last_chunk_end.isoformat()}"


@pytest.mark.asyncio
async def test_chunked_replay_retries_on_transient_db_error():
    """Each chunk's runner invocation is wrapped in the SAME transient-DB
    retry as the per-window panel-load (PR #163 contract). A Supabase
    pooler drop on the FIRST attempt of chunk 2 must NOT terminate the
    replay — retry, succeed on attempt 2, continue to chunk 3+."""
    from ops.lab.run import _run_final_holdout_chunked

    call_log: list[date] = []
    drop_once_at: date | None = None  # the chunk_start that drops the pool

    @dataclass
    class _RR:
        credibility_score = 80
        credibility_rubric = "rr"
        trade_log: list

    async def _runner(*, db_url, start, end, overrides, universe):
        nonlocal drop_once_at
        call_log.append(start)
        # On the FIRST visit to the second chunk, drop the pooler once.
        if drop_once_at is None and len(call_log) == 2:
            drop_once_at = start
            raise Exception(
                "connection was closed in the middle of operation")
        return _RR(trade_log=[])

    # Patch the backoff so the test is fast.
    import ops.lab.run as lab_run
    original_sleep = lab_run.asyncio.sleep
    lab_run.asyncio.sleep = AsyncMock(return_value=None)
    try:
        held_trades, _, _ = await _run_final_holdout_chunked(
            runner=_runner,
            db_url="postgres://fake/db",
            train_start=date(2022, 1, 1),
            final_holdout_start=date(2024, 1, 1),
            final_holdout_end=date(2024, 12, 31),
            overrides={},
            universe=None,
            chunk_months=3,  # 4 quarterly chunks
        )
    finally:
        lab_run.asyncio.sleep = original_sleep
    # The retry succeeded — replay completed across all chunks.
    assert held_trades == []
    # 4 chunks + 1 retry on chunk 2 = 5 total runner invocations.
    assert len(call_log) == 5, (
        f"expected 4 chunks + 1 retry = 5 runner calls; got "
        f"{len(call_log)}: {call_log!r}")
    # The dropped chunk_start appears TWICE in the log (one failed
    # attempt + one retry attempt).
    assert drop_once_at is not None
    assert call_log.count(drop_once_at) == 2, (
        f"dropped chunk_start {drop_once_at} should have been retried "
        f"exactly once; got {call_log.count(drop_once_at)} occurrences")


# ── (3) ledger spend is upstream of the replay — chunking does NOT
#       multiply it ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chunked_replay_does_not_multiply_ledger_spend(
        monkeypatch, tmp_path):
    """The SP-A ``record_trial_spend`` ledger spend is upstream of the
    final-holdout replay (it lives in ``_run_lab_core`` BEFORE the
    chunked replay block). Even if the chunker invokes ctx_loader 5
    times, the ledger gets exactly ONE row per Lab run — the existing
    SP-A invariant is preserved (spec §2.3, §4.5).

    Mirrors the test_lab_targeting_consistency ledger-spy pattern but
    asserts the spend count is INDEPENDENT of the chunk count.
    """
    import ops.lab.run as lab_run
    from tpcore.backtest.credibility import CredibilityScore
    from tpcore.lab.context import LabContext

    # Reuse the existing ledger-spy doubles.
    from tpcore.tests.test_lab_sp_d_make_or_break import _SharedPool

    rubric = CredibilityScore(
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True, score=80)

    @dataclass
    class _RR:
        credibility_score = 80
        credibility_rubric = rubric
        # Multiple trades so n_trades >= 3 floor passes.
        trade_log: list

    final_holdout_runner_calls: list = []
    walk_forward_ctx_calls: list = []

    # Walk-forward path: ctx_loader + ctx_runner (UNTOUCHED by chunking).
    async def _ctx_loader(*, db_url, start, end, universe):
        walk_forward_ctx_calls.append((start, end))
        return object()

    def _ctx_runner(context, *, overrides):
        return _RR(trade_log=[
            _Trade(entry_date=date(2021, 6, 3) + timedelta(days=i),
                   pnl_pct=0.01)
            for i in range(10)
        ])

    # Final-holdout chunked replay path: runner = run_for_search per chunk.
    async def _runner(*, db_url, start, end, overrides, universe):
        final_holdout_runner_calls.append((start, end))
        # The chunk's [chunk_start, chunk_end] filter keeps trades dated
        # inside the chunk slice (chunk_start = start + 365d warmup).
        return _RR(trade_log=[
            _Trade(entry_date=start + timedelta(days=365 + i + 1),
                   pnl_pct=0.01)
            for i in range(10)
        ])

    monkeypatch.setattr(
        "ops.lab.run._context_runner_for", lambda e: _ctx_runner)
    monkeypatch.setattr(
        "ops.lab.run._context_loader_for", lambda e: _ctx_loader)
    monkeypatch.setattr(
        "ops.lab.run._runner_for", lambda e: _runner)

    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    async def _fw(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fw, raising=True)

    ns = argparse.Namespace(
        engine="reversion", trials=10, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        # A 4.5-year final-holdout span → 5 per-year chunks — the exact
        # 2026-05-21 operator-reproduced shape.
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2026, 5, 15),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=0, output=tmp_path / "x.csv",
        database_url="postgres://fake/db",
        dsr_threshold=0.0, credibility_threshold=0,
        universe_tier_max=None)

    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(ns, candidate="chunktest")

    # Did NOT crash; produced a result.
    assert not isinstance(core, int), (
        f"chunked replay must produce a verdict, not an rc; got "
        f"{core!r}")

    # Final-holdout ran multiple chunk runner calls (the transport
    # invariant) — 5 per-year chunks for a 4.5y span.
    assert len(final_holdout_runner_calls) == 5, (
        f"chunked replay must issue exactly 5 runner calls for a 4.5y "
        f"per-year-chunked span; got {len(final_holdout_runner_calls)} "
        f"— chunking did not engage")

    # Ledger spend rows: EXACTLY ONE per Lab run, regardless of chunk
    # count. This is the SP-A §2.3 invariant the operator's brief calls
    # out as critical.
    ledger_rows = [
        r for r in shared.rows
        if str(r["source"]).startswith("lab_trial_ledger")
    ]
    assert len(ledger_rows) == 1, (
        f"chunked replay must NOT multiply the ledger spend; expected "
        f"1 row, got {len(ledger_rows)}: {ledger_rows!r}")
