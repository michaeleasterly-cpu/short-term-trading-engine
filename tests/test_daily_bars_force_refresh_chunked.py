"""Regression test for the ``_stage_daily_bars`` force_refresh chunking
fix (2026-05-21).

Why this exists: the operator's two 2026-05-21 (08:43 + 09:19 UTC)
force_refresh runs EACH hit the 3600s stage timeout — a single
``handle_daily_bars`` call over the full ~7,000-ticker active universe
provably exceeds the budget. The fix mirrors the PR #222 Lab final-
holdout chunking pattern: split into 500-ticker slices, run
``handle_daily_bars`` per slice, aggregate ``rows_upserted``.

Five behaviours pinned:

A. Chunked path is reached when ``force_refresh=True`` (and only then —
   the non-force-refresh fast-path is byte-unchanged).
B. The full universe is chunked at ``FORCE_REFRESH_CHUNK_SIZE`` and each
   chunk's ``handle_daily_bars`` call carries a non-overlapping slice.
C. ``rows_upserted`` is the SUM across chunks (aggregation correctness).
D. A single chunk failure does NOT abort the run — ``CHUNK_FAILED`` is
   logged, remaining chunks still run, ``chunks_failed`` is surfaced in
   the stage return value, and the producer-self-validation still gets a
   chance to fire on the aggregate.
E. Idempotency seam — the chunker DOES NOT add a ledger entry (this is
   not a Lab probe); it strips ``force_refresh`` from the per-chunk
   sub-config so the underlying handler does not double-branch.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_force_refresh_chunked", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_force_refresh_chunked"] = ops
_spec.loader.exec_module(ops)


# pytest-xdist: pin to single worker (ops-shadow pattern).
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ────────────────────────────────────────────────────────────────────────
# Fakes — no DB, no network. We replace handle_daily_bars with a stub
# and assert on the call shapes.
# ────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Stub connection used by `_resolve_force_refresh_universe`.

    fetch(sql) returns a fixed ~1,234-row ticker list — wider than two
    chunks so we exercise the multi-chunk loop, narrower than the real
    universe so the test is fast.
    """

    def __init__(self, tickers: list[str]) -> None:
        self._tickers = tickers

    async def fetch(self, sql: str, *args) -> list[dict]:
        # Two SQLs touch the conn: the universe SELECT (in the
        # resolver) and the trailing-coverage SELECT (in the stage
        # producer-validation). Discriminate by a substring.
        # The coverage pre-filter query (2026-05-30) now joins
        # ticker_classifications and uses "GROUP BY pd.date"; the
        # universe SELECT does not. Discriminate by joining/grouping.
        if "FROM platform.prices_daily" in sql and "delisted = false" in sql \
                and "GROUP BY" not in sql:
            return [{"ticker": t} for t in self._tickers]
        # trailing coverage query — return target_session present with
        # n equal to the trailing average so the producer-validation
        # does NOT raise (coverage_collapse compares latest_n vs the
        # trailing average × (1 - COVERAGE_COLLAPSE_PCT)).
        return [
            {"date": date(2026, 5, 20), "n": 1234},
            {"date": date(2026, 5, 19), "n": 1234},
            {"date": date(2026, 5, 16), "n": 1234},
            {"date": date(2026, 5, 15), "n": 1234},
            {"date": date(2026, 5, 14), "n": 1234},
        ]

    async def fetchval(self, sql: str, *args) -> int:
        # already_ingested count — below threshold so force_refresh is
        # not short-circuited.
        return 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakePool:
    def __init__(self, tickers: list[str]) -> None:
        self._tickers = tickers

    def acquire(self):
        return _FakeConn(self._tickers)


@pytest.fixture
def small_universe() -> list[str]:
    """1,234 synthetic tickers — fits 3 chunks @ 500 (500 + 500 + 234)."""
    return [f"T{i:04d}" for i in range(1234)]


# ────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────


def test_chunk_size_constant_is_500() -> None:
    """Pin the 500-ticker chunk size — operator-spec value, 14 chunks
    of a ~7,000 universe @ ~4 min/chunk = 56 min, comfortably under
    the 3600s stage timeout."""
    assert ops.FORCE_REFRESH_CHUNK_SIZE == 500


@pytest.mark.asyncio
async def test_resolve_universe_active(small_universe: list[str]) -> None:
    """Behaviour A (seam): `_resolve_force_refresh_universe` reads the
    active universe from prices_daily — same SQL shape as
    `_handle_daily_bars_explicit`'s `active` branch."""
    pool = _FakePool(small_universe)
    out = await ops._resolve_force_refresh_universe(pool, "active")
    assert out == small_universe


@pytest.mark.asyncio
async def test_resolve_universe_explicit_list() -> None:
    """Behaviour A: explicit list passes through (used by per-chunk
    sub-config)."""
    out = await ops._resolve_force_refresh_universe(
        _FakePool([]), ["aapl", "msft", "spy"],
    )
    assert out == ["AAPL", "MSFT", "SPY"]


@pytest.mark.asyncio
async def test_resolve_universe_csv_string() -> None:
    """Behaviour A: CSV string variant (param-channel scalar)."""
    out = await ops._resolve_force_refresh_universe(
        _FakePool([]), "aapl,msft, spy",
    )
    assert out == ["AAPL", "MSFT", "SPY"]


@pytest.mark.asyncio
async def test_resolve_universe_rejects_unsupported() -> None:
    """`all_active` is the discovery-sweep path — not chunked here."""
    with pytest.raises(ValueError, match="unsupported universe"):
        await ops._resolve_force_refresh_universe(
            _FakePool([]), "all_active",
        )


@pytest.mark.asyncio
async def test_chunked_run_partitions_universe_disjointly(
    small_universe: list[str], monkeypatch,
) -> None:
    """Behaviour B: every ticker appears in EXACTLY ONE chunk; chunk
    sizes are FORCE_REFRESH_CHUNK_SIZE except the tail."""
    calls: list[dict] = []

    async def fake_handler(pool, cfg):
        calls.append({"universe": cfg["universe"]})
        return len(cfg["universe"])  # rows_upserted == universe size

    monkeypatch.setattr(
        "tpcore.ingestion.handlers.handle_daily_bars", fake_handler,
    )

    pool = _FakePool(small_universe)
    out = await ops._force_refresh_chunked(
        pool, {"universe": "active", "lookback_days": 7}, date(2026, 5, 20),
    )

    # 1234 → 3 chunks @ 500 (500 + 500 + 234).
    assert out["chunks_total"] == 3
    assert out["chunks_ok"] == 3
    assert out["chunks_failed"] == 0
    assert out["universe_size"] == 1234

    # Disjoint partition.
    seen: set[str] = set()
    for c in calls:
        assert len(c["universe"]) in (500, 234)
        for t in c["universe"]:
            assert t not in seen, f"ticker {t} appears in two chunks"
            seen.add(t)
    assert seen == set(small_universe)


@pytest.mark.asyncio
async def test_chunked_run_aggregates_rows(
    small_universe: list[str], monkeypatch,
) -> None:
    """Behaviour C: aggregate rows_upserted == sum of per-chunk return."""

    async def fake_handler(pool, cfg):
        return len(cfg["universe"])

    monkeypatch.setattr(
        "tpcore.ingestion.handlers.handle_daily_bars", fake_handler,
    )

    pool = _FakePool(small_universe)
    out = await ops._force_refresh_chunked(
        pool, {"universe": "active"}, date(2026, 5, 20),
    )
    assert out["rows_upserted"] == len(small_universe)


@pytest.mark.asyncio
async def test_single_chunk_failure_does_not_abort_run(
    small_universe: list[str], monkeypatch,
) -> None:
    """Behaviour D: a single chunk's exception is caught + logged via
    structlog; the remaining chunks still run; the aggregate carries
    `chunks_failed` counter + detail."""
    call_idx = {"i": 0}

    async def flaky_handler(pool, cfg):
        call_idx["i"] += 1
        if call_idx["i"] == 2:  # fail the second chunk
            raise RuntimeError("simulated chunk failure (rate limit)")
        return len(cfg["universe"])

    monkeypatch.setattr(
        "tpcore.ingestion.handlers.handle_daily_bars", flaky_handler,
    )

    pool = _FakePool(small_universe)
    out = await ops._force_refresh_chunked(
        pool, {"universe": "active"}, date(2026, 5, 20),
    )

    assert out["chunks_total"] == 3
    assert out["chunks_ok"] == 2
    assert out["chunks_failed"] == 1
    assert len(out["chunks_failed_detail"]) == 1
    detail = out["chunks_failed_detail"][0]
    assert detail["chunk_idx"] == 2
    assert "simulated chunk failure" in detail["error"]


@pytest.mark.asyncio
async def test_per_chunk_subconfig_drops_force_refresh(
    small_universe: list[str], monkeypatch,
) -> None:
    """Behaviour E: the per-chunk handle_daily_bars call MUST NOT carry
    `force_refresh` (the chunker has already taken that responsibility)
    and MUST carry the chunk as an explicit list under `universe`."""
    captured: list[dict] = []

    async def capture_handler(pool, cfg):
        captured.append(dict(cfg))
        return 1

    monkeypatch.setattr(
        "tpcore.ingestion.handlers.handle_daily_bars", capture_handler,
    )

    pool = _FakePool(small_universe)
    await ops._force_refresh_chunked(
        pool,
        {
            "universe": "active",
            "force_refresh": True,
            "lookback_days": 14,
            "end_offset_days": 1,
            "feed": "iex",
        },
        date(2026, 5, 20),
    )

    for cfg in captured:
        assert "force_refresh" not in cfg
        assert isinstance(cfg["universe"], list)
        assert cfg["lookback_days"] == 14
        assert cfg["end_offset_days"] == 1
        assert cfg["feed"] == "iex"


@pytest.mark.asyncio
async def test_empty_universe_returns_zero(monkeypatch) -> None:
    """Behaviour B: empty universe → 0 chunks, no handler calls."""
    calls = {"n": 0}

    async def counting_handler(pool, cfg):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(
        "tpcore.ingestion.handlers.handle_daily_bars", counting_handler,
    )

    pool = _FakePool([])
    out = await ops._force_refresh_chunked(
        pool, {"universe": "active"}, date(2026, 5, 20),
    )
    assert out["chunks_total"] == 0
    assert out["rows_upserted"] == 0
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_stage_daily_bars_dispatches_to_chunker_on_force_refresh(
    small_universe: list[str], monkeypatch,
) -> None:
    """Integration: when ``_stage_daily_bars`` sees ``force_refresh=True``
    it routes through ``_force_refresh_chunked`` (witnessed by the chunk
    detail in the stage's return dict)."""

    async def fake_handler(pool, cfg):
        # Smaller per-chunk row count so the aggregate stays well below
        # the producer-validation coverage_collapse threshold against
        # the trailing 7000-row sessions baked into the _FakeConn.
        return len(cfg["universe"])

    monkeypatch.setattr(
        "tpcore.ingestion.handlers.handle_daily_bars", fake_handler,
    )

    # `previous_close` is read via tpcore.calendar — stub to a fixed
    # date so the test is deterministic and doesn't depend on the
    # NYSE calendar.
    fixed_close = datetime(2026, 5, 20, 21, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "tpcore.calendar.previous_close", lambda _: fixed_close,
    )

    pool = _FakePool(small_universe)
    out = await ops._stage_daily_bars(
        pool, {"universe": "active", "force_refresh": True},
    )

    assert out["mode"] == "force_refresh_chunked"
    assert out["chunks_total"] == 3
    assert out["chunks_ok"] == 3
    assert out["chunks_failed"] == 0
    assert out["rows_upserted"] == len(small_universe)
