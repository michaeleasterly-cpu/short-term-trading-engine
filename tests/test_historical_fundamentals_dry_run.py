"""``backfill_one_ticker`` dry_run knob — hermetic tests.

The 2026-06-02 operator run of
``python scripts/ops.py --stage confirmed_data_gap_evidence_populator
--param dry_run=true --param limit=10`` against the live DB exposed
the defect: ``backfill_one_ticker`` called ``cache.backfill`` ->
``cache.upsert_payload`` UNCONDITIONALLY in dry-run mode, bumping
``recorded_at`` on 5 AXIN rows even though the populator's
evidence-write gate worked correctly (zero new evidence rows).

The fix gates the FMP cache upsert on a new ``dry_run`` kwarg.
When ``dry_run=True``: still perform the FMP fetch (via the public
``cache.fetch_payload`` accessor), build the would-write payload,
return the planned row count — but SKIP ``cache.upsert_payload``.
Mirror semantic with PR #448's ``handle_sec_fundamentals_fallback``.

These hermetic tests pin:

  1. ``dry_run=True`` does NOT call ``cache.upsert_payload``
     (the primary fundamentals_quarterly write).
  2. ``dry_run=True`` DOES call ``cache.fetch_payload`` (the
     read-only public adapter accessor).
  3. ``dry_run=True`` returns the would-write row count from the
     payload (latest + history).
  4. ``dry_run=False`` (default) preserves the existing live write
     path bit-identically — calls ``cache.backfill`` (which calls
     ``cache.upsert_payload``).
  5. Source sentinel: ``backfill_one_ticker`` MUST NOT contain an
     unconditional ``cache.backfill`` call inside the dry-run branch
     (mirrors PR #448's AST/source-sentinel precedent).

Hermetic — stdlib + ``unittest.mock`` only. No DB, no network.
"""
from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.data import fundamentals_backfill as fb_mod
from tpcore.data.fundamentals_backfill import backfill_one_ticker

# ── Cache + db_log stubs ──────────────────────────────────────────────


def _make_cache(
    *,
    payload: dict[str, Any] | None = None,
    fetch_payload_should_raise: Exception | None = None,
) -> MagicMock:
    """Stand-in for ``FundamentalsCache`` with the two relevant
    methods (``backfill``, ``fetch_payload``, ``upsert_payload``)
    spied via ``AsyncMock``."""
    cache = MagicMock(name="FundamentalsCache")
    cache.backfill = AsyncMock(return_value=0)
    cache.upsert_payload = AsyncMock(return_value=0)
    if fetch_payload_should_raise is not None:
        cache.fetch_payload = AsyncMock(side_effect=fetch_payload_should_raise)
    else:
        cache.fetch_payload = AsyncMock(return_value=payload or {})
    return cache


def _make_db_log() -> MagicMock:
    db_log = MagicMock(name="DBLogHandler")
    db_log.log = AsyncMock(return_value=None)
    return db_log


# ── Test 1: dry_run=True does NOT call cache.upsert_payload ───────────


async def test_dry_run_true_does_not_call_cache_upsert_payload() -> None:
    """The dry-run code path MUST NOT call ``cache.upsert_payload``.

    Pre-fix: ``cache.backfill`` ran unconditionally, which called
    ``cache._upsert_payload``. The 2026-06-02 AXIN bump on
    ``recorded_at`` was the visible defect.
    """
    cache = _make_cache(payload={"filing_date": date(2024, 3, 31),
                                  "history": []})
    db_log = _make_db_log()

    rows = await backfill_one_ticker(
        cache, db_log, "AAA", dry_run=True,
    )

    assert cache.upsert_payload.await_count == 0, (
        "dry_run=True must NOT call cache.upsert_payload (the primary "
        "fundamentals_quarterly write)"
    )
    # Also: cache.backfill (which delegates to _upsert_payload) must
    # NOT have run either.
    assert cache.backfill.await_count == 0, (
        "dry_run=True must NOT call cache.backfill (the upsert wrapper)"
    )
    # rows is the would-write count from the payload — at least 1
    # because we provided a single-period payload with filing_date.
    assert rows == 1


# ── Test 2: dry_run=True DOES call cache.fetch_payload ────────────────


async def test_dry_run_true_calls_cache_fetch_payload() -> None:
    """The dry-run code path MUST call ``cache.fetch_payload`` so the
    FMP fetch still runs (mirrors PR #448 SEC handler semantics — the
    dry-run preview still hits the source to compute honest planning
    counters)."""
    payload = {
        "filing_date": date(2024, 6, 30),
        "history": [
            {"filing_date": date(2024, 3, 31)},
            {"filing_date": date(2023, 12, 31)},
        ],
    }
    cache = _make_cache(payload=payload)
    db_log = _make_db_log()

    rows = await backfill_one_ticker(
        cache, db_log, "AAA", dry_run=True,
    )

    assert cache.fetch_payload.await_count == 1, (
        "dry_run=True must call cache.fetch_payload exactly once"
    )
    cache.fetch_payload.assert_awaited_with("AAA")
    # latest + 2 history = 3 rows would-write.
    assert rows == 3


# ── Test 3: dry_run=True reports would-write counts ───────────────────


async def test_dry_run_true_reports_would_write_counts() -> None:
    """The returned int in dry-run is the would-write row count
    (mirrors PR #448's ``archive_rows_planned`` precedent)."""
    payload = {
        "filing_date": date(2024, 6, 30),
        "history": [
            {"filing_date": date(2024, 3, 31)},
            {"filing_date": date(2023, 12, 31)},
            {"filing_date": date(2023, 9, 30)},
        ],
    }
    cache = _make_cache(payload=payload)
    db_log = _make_db_log()

    rows = await backfill_one_ticker(
        cache, db_log, "BBB", dry_run=True,
    )

    # 1 latest + 3 history = 4 rows.
    assert rows == 4, f"expected 4 planned rows; got {rows}"


# ── Test 4: dry_run=True payload rows with no filing_date are dropped


async def test_dry_run_true_drops_rows_with_no_filing_date() -> None:
    """The would-write count must mirror the live-path physical-truth
    gate: ``cache._upsert_payload`` skips rows without a ``filing_date``.
    The dry-run counter MUST do the same so the preview matches what
    live would actually write."""
    payload = {
        "filing_date": None,  # dropped
        "history": [
            {"filing_date": date(2024, 3, 31)},
            {"filing_date": None},  # dropped
            {"filing_date": date(2023, 12, 31)},
        ],
    }
    cache = _make_cache(payload=payload)
    db_log = _make_db_log()

    rows = await backfill_one_ticker(
        cache, db_log, "CCC", dry_run=True,
    )

    # 2 history with filing_date; the None entries drop.
    assert rows == 2, f"expected 2 (drop None-filing_date); got {rows}"


# ── Test 5: dry_run=False (default) preserves cache.backfill call ─────


async def test_dry_run_false_preserves_cache_backfill_call() -> None:
    """The live path (``dry_run=False``, the default) MUST be
    bit-identical to pre-fix behavior: it calls ``cache.backfill``,
    which internally calls ``cache._upsert_payload``."""
    cache = _make_cache()
    cache.backfill = AsyncMock(return_value=11)  # canonical live return
    db_log = _make_db_log()

    rows = await backfill_one_ticker(
        cache, db_log, "AAA",  # dry_run defaults to False
    )

    assert cache.backfill.await_count == 1, (
        "dry_run=False must call cache.backfill once (live write path)"
    )
    cache.backfill.assert_awaited_with("AAA", end_date=None)
    # cache.fetch_payload must NOT have been called in the live path.
    assert cache.fetch_payload.await_count == 0
    assert rows == 11


# ── Test 6: dry_run=True propagates DataProviderOutage as RuntimeError


async def test_dry_run_true_real_outage_re_raises() -> None:
    """A real transient outage on the FMP fetch (DataProviderOutage)
    in dry-run still re-raises as ``RuntimeError`` — same semantics
    as the live path — so the populator's outage counter increments
    correctly. The classification (no-data / 402-premium-gated /
    real outage) mirrors the live path verbatim."""
    from tpcore.outage import DataProviderOutage

    cache = _make_cache(
        fetch_payload_should_raise=DataProviderOutage(
            "FMP read timeout"
        ),
    )
    db_log = _make_db_log()

    with pytest.raises(RuntimeError, match="DataProviderOutage"):
        await backfill_one_ticker(
            cache, db_log, "AAA", dry_run=True,
        )

    # Even on outage, the event log MUST still record the attempt so
    # the resume probe sees the work.
    assert db_log.log.await_count == 1


# ── Test 7: dry_run=True classified skip (no data) → no re-raise ──────


async def test_dry_run_true_no_data_outage_does_not_raise() -> None:
    """``DataProviderOutage('no usable fundamentals…')`` is the
    canonical permanently-empty-symbol signal (ETF / SPAC unit /
    non-issuer). The handler classifies it as a skip — no re-raise,
    no outage counter bump — IN BOTH dry-run and live. Pin that the
    dry-run path preserves the classification."""
    from tpcore.outage import DataProviderOutage

    cache = _make_cache(
        fetch_payload_should_raise=DataProviderOutage(
            "FMP returned no usable fundamentals for AAA as_of=None"
        ),
    )
    db_log = _make_db_log()

    # No raise expected — the skip path returns 0.
    rows = await backfill_one_ticker(
        cache, db_log, "AAA", dry_run=True,
    )

    assert rows == 0
    assert db_log.log.await_count == 1


# ── Test 8: source sentinel — no unconditional cache.backfill in body


def test_source_sentinel_dry_run_branch_does_not_call_cache_backfill() -> None:
    """Mirrors PR #448's source-sentinel precedent: pin the structural
    contract of the fixed ``backfill_one_ticker`` so a future edit
    can't silently regress the dry-run-purity invariant.

    Asserted contract:
      * ``backfill_one_ticker`` accepts a ``dry_run: bool = False`` kwarg.
      * The body branches on ``if dry_run:`` BEFORE the
        ``cache.backfill`` call (i.e., the live ``cache.backfill`` call
        sits inside an ``else:`` of the ``if dry_run:`` block).
      * The body contains a ``cache.fetch_payload`` call (the dry-run
        read-only path).

    If the function is refactored, update this sentinel deliberately
    in the same patch — it's the line-of-defense against the 2026-06-02
    silent-write defect recurring.
    """
    sig = inspect.signature(backfill_one_ticker)
    assert "dry_run" in sig.parameters, (
        "backfill_one_ticker must accept a ``dry_run`` kwarg"
    )
    p = sig.parameters["dry_run"]
    assert p.default is False, (
        "dry_run default must be False (live path bit-identical)"
    )

    src = inspect.getsource(backfill_one_ticker)
    assert "if dry_run:" in src, (
        "function must branch on ``if dry_run:`` (the dry-run gate)"
    )
    assert "cache.fetch_payload" in src, (
        "dry-run branch must call cache.fetch_payload (the public, "
        "DB-free FMP accessor)"
    )
    # The live-path ``cache.backfill(...)`` call must sit AFTER an
    # ``else:`` (or otherwise gated). Search for the open-paren form
    # ``cache.backfill(`` so docstring text mentioning
    # ``FundamentalsCache.backfill`` doesn't false-positive.
    dr_idx = src.find("if dry_run:")
    cb_idx = src.find("cache.backfill(")
    else_idx = src.find("else:", dr_idx)
    assert dr_idx != -1 and cb_idx != -1 and else_idx != -1, (
        "expected ``if dry_run: ... else: ... cache.backfill(...)``"
    )
    assert dr_idx < else_idx < cb_idx, (
        "``cache.backfill(...)`` must sit inside the ``else:`` of the "
        "``if dry_run:`` block (so dry-run never calls it). Got "
        f"dr_idx={dr_idx} else_idx={else_idx} cb_idx={cb_idx}"
    )


# ── Test 9: _count_payload_rows pure-function tests ───────────────────


def test_count_payload_rows_empty_payload_is_zero() -> None:
    assert fb_mod._count_payload_rows({}) == 0


def test_count_payload_rows_no_history_with_filing_date_is_one() -> None:
    assert fb_mod._count_payload_rows({
        "filing_date": date(2024, 3, 31),
    }) == 1


def test_count_payload_rows_latest_plus_history_counted() -> None:
    payload = {
        "filing_date": date(2024, 6, 30),
        "history": [
            {"filing_date": date(2024, 3, 31)},
            {"filing_date": date(2023, 12, 31)},
        ],
    }
    assert fb_mod._count_payload_rows(payload) == 3


def test_count_payload_rows_drops_none_filing_date_entries() -> None:
    payload = {
        "filing_date": None,
        "history": [
            {"filing_date": None},
            {"filing_date": date(2024, 3, 31)},
        ],
    }
    assert fb_mod._count_payload_rows(payload) == 1


# ── Test 10: module-level sentinel — file location matches the
# fixed module so a relocation can't slip past the patch.


def test_fundamentals_backfill_module_lives_at_expected_path() -> None:
    """The fix lives in ``tpcore/data/fundamentals_backfill.py``. If a
    later refactor moves it, this sentinel fires so the test names +
    docstring references stay in sync."""
    repo = Path(__file__).resolve().parents[1]
    target = repo / "tpcore" / "data" / "fundamentals_backfill.py"
    assert target.is_file(), (
        f"fundamentals_backfill.py must live at {target}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
